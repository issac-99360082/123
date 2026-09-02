# audit_1 — Watchpost production-readiness audit(第 1 次)

**日期**:2026-09-02
**基準 commit**:`bcf602a`
**立場**:資深軟體工程師 / 安全工程師 / SaaS 投資人,任務是**證明這個專案不該上線**。
**方法**:實際執行攻擊與測試,不只閱讀程式碼。所有結論附可重現的實測輸出。

---

## 摘要

**結論:基準版本不適合上線。** 發現 2 個 Critical、4 個 High。
其中一個是可被任意註冊使用者利用的 SSRF 資料外洩,另一個會讓**付了錢的客戶拿不到方案**。

全部 Critical 與 High 已修正並重新測試。測試數:103 → **118**(新增 15 個回歸測試)。

| 等級 | 數量 | 已修 | 殘餘 |
|---|---|---|---|
| Critical | 2 | 2 | 0 |
| High | 4 | 4 | 0 |
| Medium | 6 | 3 | 3(見 `docs/SECURITY.md`) |
| Low | 2 | 1 | 1 |

---

## Critical

### C1 — 透過重導向繞過 SSRF 防護,任意使用者可讀取內網內容

**攻擊**:`validate_target_url()` 只驗證使用者提交的**第一個** URL,
但 `httpx.Client(follow_redirects=True)` 會自動跟隨重導向,**後續每一跳都未經檢查**。
攻擊者只要用一台合法的公開主機回應 `302 Location: http://169.254.169.254/`,
就能讓我們的伺服器去抓內網。

`/v1/preview` 會把抓到的內容**原樣回傳**,因此這是完整的讀取型 SSRF。

**實測(修正前)**:
```
=== 透過公開端點 /v1/preview 外洩內網內容 ===
status: 200
!! CRITICAL: internal content returned to an unprivileged API user
  extracted: AWS-METADATA-TOKEN-xyz789
```

**影響**:雲端 metadata 憑證外洩 → IAM 角色接管。這是 SaaS 最典型的致命漏洞。

**修正**:`app/core/fetcher.py` 的 `fetch()` 改為**手動逐跳跟隨重導向**,
每一跳都重新跑完整驗證(scheme、DNS 解析、公開位址判定、robots、節流)。
POST 的 body 不會被重放到不同來源。

**重新測試**:
```
--- B) fetch a URL that REDIRECTS to the internal address ---
  blocked: blocked_target Target 127.0.0.1 is not a public address.
--- 透過 /v1/preview ---
  status: 400  {'type': 'bad_request', 'detail': {'kind': 'blocked_target'}}
```

**回歸測試**:`test_redirect_to_private_address_is_blocked`、
`test_redirect_chain_is_bounded`

**殘餘**:DNS rebinding 仍未完全消除(見 `docs/SECURITY.md` R1)。
需要在網路層封鎖 worker 對私有網段的出站連線。

---

### C2 — 付費客戶可能永遠拿不到方案(收入直接損失)

**攻擊**:`/v1/billing/webhook` 先把事件寫進 `stripe_events`(標記為已處理),
**然後**才呼叫 Stripe API 同步訂閱。若該呼叫失敗(Stripe 短暫故障、網路問題),
處理中止,但事件已被記為已處理 → **Stripe 的重送被當成重複事件丟棄** → 客戶付了錢,方案永遠是 free。

**實測(修正前)**:
```
first delivery -> HTTP 500
user plan after failure: free
event recorded as processed: True
Stripe retry     -> HTTP 200 {'message': 'duplicate event ignored'}
user plan after retry: free
!! CUSTOMER PAID BUT NEVER GOT THEIR PLAN
```

**影響**:靜默的收入損失 + 客訴。**不會有任何告警**,因為第二次是 HTTP 200。

**修正**:把事件記錄移到**處理成功之後**。
Stripe API 錯誤 → 回 503 且**不**寫入事件,讓 Stripe 正常重送。

**重新測試**:
```
first delivery -> HTTP 503
event recorded as processed: False
Stripe retry     -> HTTP 200 {'message': 'ok'}
user plan after retry: pro      ✅
```

---

## High

### H1 — 使用者提供的正規表達式造成 CPU 阻斷服務(ReDoS)

**攻擊**:`extract_mode="regex"` 讓使用者把任意 pattern 交給我們的伺服器執行。
stdlib `re` 對災難性回溯毫無防護。

**實測(修正前)**:pattern `(a+)+$`、輸入僅 **66 bytes** →
```
!! ReDoS CONFIRMED: still running after 10s
   one request pins a worker thread indefinitely
```
`WORKER_CONCURRENCY=8`,8 個這種監控即可讓整個檢查系統停擺。

**修正**(三層,`app/core/extractor.py`):
1. 改用 `regex` 套件取代 `re`(對經典病態模式有內建保護)
2. 每次呼叫帶 `timeout=2.0` 硬上限
3. pattern 長度上限 1,000 字元、regex 輸入上限 100,000 字元

**重新測試**:
```
  '(a+)+$'       -> 0.001s
  '(x+x+)+y'     -> 0.080s
  '(a|a)*$'      -> 2.001s   <- timeout 確實觸發
  '(.*a){25}'    -> 0.001s
  '(a?){1,50}b'  -> 0.001s
  '([a-z]+)+#'   -> 0.000s
worst case: 2.001s (修正前:無上限)
```
`(a|a)*$` 剛好落在 2.001s,**證明 `timeout=` 在此環境確實被強制執行**,
而不只是依賴 `regex` 的最佳化。

---

### H2 — Stripe webhook 對畸形資料回 500,導致無限重送

**實測(修正前)**:`metadata.watchpost_user_id = "not-an-integer"` →
`int()` 拋出 ValueError → HTTP 500。Stripe 會持續重送數天。

**修正**:新增 `_as_user_id()` 安全轉型;整個處理流程包在例外處理中,
永久性資料錯誤 → 記錄後回 200(重送無意義),暫時性錯誤 → 503(讓 Stripe 重送)。

**重新測試**:`-> HTTP 200 {'message': 'ok'}`

---

### H3 — 免費方案濫用:可無限量開帳號

**實測(修正前)**:單一來源一次連續建立 **30/30** 個帳號成功,
無 email 驗證、無 CAPTCHA、無任何身分證明 → 90 個免費監控額度,成本由我們承擔。

**修正**:新增每 IP 每小時 5 個帳號的滑動視窗限制(`SIGNUP_LIMIT_PER_HOUR_PER_IP`)。

**重新測試**:`accounts created in one burst: 5/30`

**殘餘**:擁有大量 IP 者仍可繞過。真正的解法是 email 驗證或 CAPTCHA。
成本上限已量化於 `docs/SECURITY.md` R3。

---

### H4 — 密碼暴力破解:無單一帳號鎖定

**實測(修正前)**:對同一帳號連續 200 次錯誤密碼 → **56 次得到 401**,
只受共用的每 IP 速率限制約束。分散式攻擊者輪換 IP 即可無限嘗試。

**修正**:新增**以帳號為鍵**的失敗計數與鎖定
(`LOGIN_MAX_FAILURES=8`,`LOGIN_LOCKOUT_MINUTES=15`),成功登入即清零。

**重新測試**:
```
15 次猜測來自 15 個不同 IP -> {401: 8, 429: 7}
-> 鎖定以帳號為鍵,輪換 IP 無效
3 次失敗後正確密碼 -> 200,計數已清零(正常使用者不受影響)
```

---

### H5 — 多 worker 重複認領同一監控(成本 ×N、通知 ×N)

**攻擊面**:非惡意,但直接影響成本與正確性。
`claim_due_monitors()` 是「先讀取、再寫入」,兩者之間沒有任何鎖。

**實測(修正前)**:4 個 worker、40 個到期監控 →
```
total claims: 160  unique: 40  DUPLICATES: 120
!! 同一監控被多個 worker 認領 -> 重複抓取、重複 webhook、4 倍成本
```

**同時發現**:`docs/DEPLOY.md` 原本明確寫著「兩個 worker 不會領到同一個監控…
可安全水平擴充」。**這份文件是錯的**,會直接誤導營運者。

**修正**:改為條件式原子更新,只有回報「剛好更新 1 列」才認領:
```sql
UPDATE monitors SET next_check_at = :new WHERE id = :id AND next_check_at <= :now
```
並修正 `docs/DEPLOY.md`。

**重新測試**:`total claims: 40  unique: 40  DUPLICATES: 0`

---

## Medium

| # | 問題 | 處置 |
|---|---|---|
| M1 | `/metrics` 無需認證即可讀取(客戶數、監控數、送信失敗數等商業情報) | **已修**:改為需要 API key。`/healthz`、`/readyz` 維持公開供 LB 使用 |
| M2 | 帳號列舉:註冊回應揭露信箱是否已註冊 | **部分修正**:訊息不再揭露,但狀態碼仍不同(400 vs 201)。完全關閉需 email 驗證流程 → `docs/SECURITY.md` R2 |
| M3 | 速率限制存於單一程序記憶體,多副本時失效、重啟即清空 | **未修**,已記錄 → R4。單一 API 程序下正確 |
| M4 | 使用者的 `request_headers`(常含其正式環境 API token)以明文存放於資料庫 | **未修**,已記錄 → R5。API 回應不會回傳,但備份會含有 |
| M5 | DNS rebinding | **部分緩解**(逐跳驗證),真正的解法在網路層 → R1 |
| M6 | 單一 worker 故障時檢查靜默停止,`/healthz` 仍回 200 | **未修**,已記錄 → R7,並給出以 `monitors_due` 告警的做法 |

## Low

| # | 問題 | 處置 |
|---|---|---|
| L1 | 錯誤訊息回報內部位址判定結果 | 已記錄 → R6。判定純依 IP 區段,不代表主機存在 |
| L2 | 無備份自動化腳本(僅文件說明指令) | 已記錄於 `docs/DEPLOY.md`,建議上線前設定排程 |

---

## 測試過但**未發現**問題的項目

誠實記錄:以下是我認為會有問題、實際攻擊後發現防護有效的部分。

| 項目 | 實測結果 |
|---|---|
| gzip 解壓縮炸彈 | 305 KB 壓縮 → 314 MB 展開。串流上限在 2,000,000 bytes 精準截斷,`truncated=True`,記憶體有界 ✅ |
| 跨帳號資料隔離 | 他人讀/改/刪監控一律 404,列表為空 ✅ |
| Stripe 簽章偽造 | 拒絕,且**不留下事件紀錄**(不會毒化後續正常事件)✅ |
| Stripe 事件重放 | 相同 event id 回 `duplicate event ignored` ✅ |
| 直接指定內網位址 | `127.0.0.1`、`169.254.169.254`、`10.x`、`192.168.x`、`[::1]`、`file://` 全部擋下 ✅ |
| robots.txt | 對真實站台生效(PyPI `/simple/` 被拒)✅ |
| 從全新環境依 README 部署 | `install.sh --dev` 全程成功:venv → 相依 → 產生 `.env` 與 `SECRET_KEY` → 建 schema → 測試通過 ✅ |

---

## 明確回答四個問題

### Q1. 現在是否適合連上真實 Stripe?

**修正前:絕對不行。** C2 會讓付費客戶靜默拿不到方案。

**修正後:可以連測試模式(test mode),正式模式再等一輪。**

連正式 Stripe 前還缺:
1. 一次真實的 test-mode 端到端付款(用 4242 卡),確認 webhook 真的送達你的網域
2. Stripe webhook 失敗告警(目前失敗只寫 log,沒有人會看到)
3. 至少一次真實的訂閱取消流程驗證

C2 的修正是用假的 Stripe 事件驗證的,**尚未對真實 Stripe 端點驗證過**。

### Q2. 現在是否適合公開給陌生使用者?

**不行 —— 但差的不是程式碼,是網路層設定。**

阻擋條件只有一個是硬性的:**必須在防火牆封鎖 worker 對 RFC1918 與
`169.254.0.0/16` 的出站連線**(`docs/SECURITY.md` R1)。
應用層的 SSRF 防護已擋下所有實測攻擊,但 DNS rebinding 無法在應用層完全消除。
沒有這道網路層防線就公開,等於把內網暴露給任何註冊者。

做完這件事之後,可以**小規模、可撤回地**公開(例如邀請制或限量註冊),
但同時要接受 R3(免費濫用)還沒有真正的解法。

### Q3. 如果只有新台幣 500 元測試預算,最先應驗證哪個商業假設?

**驗證「有人願意付錢」,不要驗證「產品能不能用」。**

整份 `UNIT_ECONOMICS.md` 建立在一個未經檢驗的數字上:**3% 免費轉付費率**。
它是唯一能讓 12 個月 MRR 在 $310 與 $1,900 之間擺盪的變數。技術風險已經很低了
(產品能跑、成本每月 $12),**商業風險才是全部的風險**。

500 元(約 $15)最有效的花法:

1. **不要花在伺服器上。** 用免費層或現有機器就好。
2. 花在**取得 20–30 次真實對話**:在 2–3 個開發者/電商/法遵社群發文,
   描述「監控頁面變更並推 webhook」,附一個報名連結。
3. 觀察的指標不是註冊數,是**有多少人願意描述自己「現在正在手動盯著哪一頁」**。
   能具體說出頁面的人,才是有痛點的人。

**更省的做法:先賣再做。** 產品已經能跑了,直接找 5 個人問
「一個月 $19,我幫你盯這頁,要嗎?」——**有 1 個人當場願意付,
比 100 個免費註冊更有資訊量。**

如果 30 次對話裡沒有任何人能具體說出他要盯哪一頁,那 3% 的假設是錯的,
而且是**在你花任何錢做行銷之前**就知道了。

### Q4. 如果這個產品最終失敗,最可能死在哪裡?

**不會死於技術。會死於「這個痛不夠痛」。**

按可能性排序:

**1. 需求強度不足(最可能)**
「知道頁面變了」對多數人是 *nice to have*,不是 *must have*。
會痛到願意每月付錢的人很少,而且他們往往已經有內部腳本了。
**這是最可能的死因,而且和程式碼品質完全無關。**

**2. 免費方案就夠用**
3 個監控 × 每小時一次,對絕大多數個人使用者已經足夠。
升級的誘因(更多監控、更短間隔)只對**重度商業使用者**成立,
而那群人數量遠小於總註冊數。付費轉換可能卡在 1% 以下。

**3. 通知品質決定續訂,而通知品質很難**
真正的競爭壁壘不是抓取,是**不要吵**。一個每週誤報三次的監控會被關掉並取消訂閱。
CSS 選擇器會因為對方改版而失效,`ignore_patterns` 需要使用者自己調。
**流失率會直接由誤報率決定**,而這需要長期打磨,不是 MVP 能解決的。

**4. 被大廠順手做掉**
這功能對 Zapier、n8n、Make 這類平台是一個 node 的工作量。
差異化(結構化 diff、webhook-first)守不了太久。

**5. 目標站台的封鎖與法律灰帶**
我們遵守 robots.txt、節流、標明身分——但**被監控的網站不一定歡迎**。
大規模監控某個電商的價格頁,對方可能封 IP,甚至主張違反使用條款。
這是這個商業模式結構性的、無法用工程完全解決的風險。

**最不可能的死因**:伺服器撐不住、成本失控、被駭。
單機容量估算是保守的 500 萬次檢查/月,成本每月 $12,
安全問題在這輪已經修掉最嚴重的部分。

> 一句話:**這個專案的風險幾乎全部在商業面,而不在工程面。**
> 這既是好消息(工程可控),也是壞消息(工程做得再好也救不了沒有需求)。

---

## 本輪變更檔案

```
app/core/fetcher.py     逐跳重導向驗證(C1)
app/core/extractor.py   ReDoS 三層防護,改用 regex 套件(H1)
app/core/engine.py      條件式原子認領(H5)
app/api/billing.py      事件記錄移到處理成功之後、安全轉型(C2, H2)
app/api/auth.py         註冊節流、帳號鎖定、訊息去識別(H3, H4, M2)
app/api/health.py       /metrics 改為需認證(M1)
app/config.py           新增 3 個 auth 節流設定
app/deps.py             SlidingWindow 限制器、client_ip、登入鎖定
tests/conftest.py       測試間重置節流狀態
tests/test_security_regressions.py   新增 15 個回歸測試
docs/SECURITY.md        新增:威脅模型與 7 項殘餘風險
docs/DEPLOY.md          修正錯誤的多 worker 說明
requirements.txt        新增 regex
```

**測試結果:118 passed**(修正前 103)
