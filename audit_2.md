# audit_2 — Watchpost production-readiness audit(第 2 次)

**日期**:2026-09-02
**基準 commit**:`8c99900`(audit_1 修正後的狀態)
**立場**:同 audit_1 —— 試圖證明這個專案不該上線。
**本輪重點**:
1. **audit_1 的修正是否引入了新的問題**(這是最容易被忽略的部分)
2. audit_1 沒有涵蓋的面向:資料生命週期、法律承諾與實作的落差、時間型資源耗盡

---

## 摘要

**結論:又找到 5 個 High。其中 3 個是 audit_1 的修正自己造成的。**

這是本輪最重要的發現:**修 bug 會產生 bug**。
audit_1 為了擋 SSRF 而逐跳驗證,順帶讓惡意重導向鏈可以放大 5 倍佔用時間;
為了擋濫用註冊而引入 IP 節流,卻把分桶的鑰匙交給了攻擊者。

| 等級 | 數量 | 已修 | 殘餘 |
|---|---|---|---|
| Critical | 0 | — | 0 |
| High | 5 | 5 | 0 |
| Medium | 3 | 2 | 1 |
| Low | 1 | 0 | 1(已記錄) |

測試數:118 → **126**。audit_1 的所有修正經重新攻擊確認**仍然有效**(見文末回歸驗證)。

---

## High

### H1 — audit_1 的註冊節流可被一個標頭完全繞過(**由 audit_1 引入**)

audit_1 新增 `client_ip()` 來取得來源位址,並**無條件信任 `X-Forwarded-For`**。
但這個標頭是**由客戶端提供的**。攻擊者只要每次請求換一個值,就等於自己挑選 rate-limit 分桶。

**實測(修正前)**:
```
A. 固定來源位址          -> accounts created:  5/20   (節流生效)
B. 每次請求偽造 X-Forwarded-For -> accounts created: 20/20   ← 節流完全失效
```

audit_1 宣稱「已修正 H3 免費方案濫用」。**那個修正在真實部署下是無效的。**

**修正**:新增 `TRUST_PROXY_HEADERS`(預設 `false`)。
只有在確定前方有會**覆寫**該標頭的反向代理時才啟用。

**重新測試**:`B. -> accounts created: 0/20` ✅

**額外提醒**(寫入 `docs/SECURITY.md` R1b):
Caddy 與 nginx 的預設行為是**附加**而非覆寫 `X-Forwarded-For`。
把這個開關打開之前,必須先確認代理設定。

---

### H2 — audit_1 的登入鎖定變成阻斷合法使用者的工具(**由 audit_1 引入**)

audit_1 的鎖定完全以**帳號**為鍵:8 次失敗即鎖定 15 分鐘。
因此**任何知道某人 email 的人,都能隨時讓那個人登不進去**。
這把「防暴力破解」變成了「阻斷服務」。

**實測(修正前)**:
```
12 次失敗來自 12 個攻擊者位址
擁有者以「正確密碼」登入 -> 429
!! anyone who knows an email can lock that account out at will
```

**修正**:改為兩層 ——
- 主要以 `(帳號, 來源位址)` 為鍵,8 次失敗鎖定(真實猜測攻擊的形狀)
- 帳號整體另設較寬鬆的上限 50 次(分散式攻擊仍會被擋)

**重新測試**:
```
12 次失敗來自 12 個攻擊者位址
擁有者以正確密碼登入 -> 200      ✅ 不受影響
同一來源連續 12 次猜測 -> {401: 8, 429: 4}   ✅ 暴力破解仍被擋
50 次分散式失敗 -> 觸發帳號整體上限        ✅
```

---

### H3 — audit_1 的逐跳驗證讓惡意重導向鏈放大佔用時間 5 倍(**由 audit_1 引入**)

audit_1 為了修 SSRF,把重導向改成逐跳處理 —— 但**每一跳都會套用每網域節流等待**。
`fetch_timeout` 是**單次請求**的上限,整個檢查沒有總時間上限。

**實測(修正前,以正式設定 `MIN_SECONDS_BETWEEN_HITS_PER_HOST=20`)**:
```
6 跳的重導向鏈 -> 單一檢查佔用約 100 秒
```
`WORKER_CONCURRENCY=8`,只要 8 個這樣的 URL 就能讓檢查系統近乎停擺。
攻擊者只需要一台自己的伺服器。

**修正**:新增 `FETCH_TOTAL_DEADLINE_SECONDS=45`,涵蓋所有重導向跳轉與節流等待;
每一跳的 timeout 也會被剩餘預算夾住。

**重新測試**:`blocked: deadline_exceeded` ✅

---

### H4 — 一個慢速 webhook 接收端會拖慢所有客戶的通知

`process_pending_deliveries()` 是**序列**執行的。
一批 50 筆送信、接收端各花到 10 秒 timeout,整個 tick 會被佔用 **500 秒**。

**影響**:A 客戶的伺服器壞掉 → B、C、D 客戶的變更通知延遲數分鐘。
這是典型的 head-of-line blocking,而通知延遲**直接就是這個產品的價值本身**。

**修正**:送信改為以執行緒池併發,每筆送信各自使用獨立 session;
批次在釋放 session 前先「認領」(把 `next_attempt_at` 推後),避免第二個 worker 重送。

---

### H5 — 保留期限從未被執行,儲存無上限成長

方案表宣傳 7 / 90 / 365 天的歷史保留,`privacy.md` 也承諾「逾期資料會被刪除」。

**實測**:全專案搜尋 `delete|prune|retention|purge`,
只找到 6 個結果,全部是 API key 撤銷、監控刪除與 rate-limiter 內部清理。
**沒有任何程式碼會刪除過期的 check 或 change_event。**
`history_days` 只被用來**過濾查詢**。

**量化影響**:
```
Business 方案上限 = 12,960,000 次檢查/月
× 約 150 bytes/列 ≈ 1.9 GB/月 —— 來自「一個」客戶,且永不刪除
```
`UNIT_ECONOMICS.md` 的 $12/月成本模型直接忽略了這一項。

**修正**:新增 `app/core/retention.py`,依每個監控擁有者的方案刪除逾期資料,
由排程器每小時執行一次(`RETENTION_ENABLED`、`RETENTION_SWEEP_MINUTES`)。
已送達/已失敗的 delivery 保留 30 天後刪除。

**重新測試**:回歸測試建立 5 筆 40 天前的舊資料與 3 筆新資料,
sweep 後舊的全被刪除、新的保留、監控本身不受影響 ✅

**同時修正 `UNIT_ECONOMICS.md`**,加入穩定狀態儲存量與其成本。

---

## Medium

### M1 — 法律頁面承諾了 API 做不到的事(**合規風險**)

`terms.md` 寫著「You may delete your account at any time」,
`privacy.md` 寫著使用者有 access / export / delete 的權利。

**實測**:列出所有 auth 端點 ——
```
['/v1/auth/signup', '/v1/auth/login', '/v1/auth/me', '/v1/auth/keys', '/v1/auth/keys/{id}']
endpoint to delete your own account exists: False
```

**這不只是缺功能,是公開文件中的不實陳述。** 在 GDPR 管轄範圍內,
這是可申訴的事項;而且它會在**第一個要求刪除帳號的使用者**出現時立刻爆發。

**修正**:
- `DELETE /v1/auth/me?confirm=<email>` —— 需帶與帳號相同的 email 作為確認,
  避免外洩的 API key 一個裸 DELETE 就毀掉帳號。級聯刪除 monitors、checks、
  change_events、deliveries、api_keys。
- `GET /v1/auth/me/export` —— 匯出該帳號的所有資料。

**線上實測**:
```
export  -> account: del@corp.example | monitors: 0
delete (錯誤 confirm) -> Confirmation does not match.
delete (正確 confirm) -> Account and all associated ... deleted.
刪除後以原 key 存取 -> 401     ✅
```

### M2 — 反向代理後方所有匿名請求共用一個 rate-limit 分桶

`rate_limit()` 對未認證的請求以 `request.client.host` 為鍵。
在反向代理後方,那是**代理的位址** —— 所有匿名流量共用一個 120/min 的分桶。
一個濫用者就能讓**全站無法註冊或登入**。

已認證的請求以 API key 為鍵,不受影響(實測確認:客戶 1 用光額度後,客戶 2 仍為 `{200: 10}`)。

**修正**:改用 `client_ip()`,與其他 per-IP 控制一致地尊重 `TRUST_PROXY_HEADERS`。

### M3 — 檢查與送信共用同一個 tick 與執行緒池(**未修正**)

送信已改為併發,但仍與檢查在同一個 tick 內序列執行。極端負載下大量送信會延後下一輪檢查。
已記錄於 `docs/SECURITY.md` R8,並給出以 `monitors_due` + `deliveries_pending` 同時上升作為告警訊號的做法。

## Low

### L1 — `last_content` 每個監控最多保存 200 KB(**未修正**)

`Monitor.last_content` 是比對基準,必須保留;但 300 個監控 × 200 KB ≈ 60 MB/客戶。
不影響正確性,已納入儲存成本估算。

---

## audit_1 修正的回歸驗證

本輪重新執行 audit_1 的每一個攻擊腳本,確認修正未被本輪變更破壞:

| audit_1 findings | 重新攻擊結果 |
|---|---|
| C1 SSRF 重導向 | `status: 400 / blocked_target` ✅ 仍被擋 |
| C2 付費客戶拿不到方案 | 首次 503 且不記錄 → 重送後 `plan: pro` ✅ |
| H2 畸形 metadata | `HTTP 200`,無 500、無無限重送 ✅ |
| H3 註冊濫用 | 見本輪 H1(**原修正無效,已重修**) |
| H4 密碼暴力破解 | 見本輪 H2(**原修正過度,已重修**) |
| H5 併發重複認領 | `total claims: 40 unique: 40 DUPLICATES: 0` ✅ |

**測試結果:126 passed**(audit_1 後為 118)。
從全新環境部署與 smoke test 仍然通過;排程器確認同時載入 `tick` 與 `run_retention` 兩個工作。

---

## 明確回答四個問題(第 2 輪,取代 audit_1 的答案)

### Q1. 現在是否適合連上真實 Stripe?

**測試模式:可以,而且應該現在就做。**
**正式模式:再等一件事 —— 一次真實的 test-mode 端到端付款。**

理由:C2(付費卻拿不到方案)已修正並驗證,但**驗證用的是假的 Stripe 事件**。
Stripe 真實環境有一些模擬不了的行為:事件順序可能顛倒
(`customer.subscription.updated` 先於 `checkout.session.completed` 抵達)、
簽章時鐘偏移、重送間隔。這些只有真的接上去才知道。

**建議順序**:
1. 接 test mode,用 4242 卡跑完一次:付款 → 升級 → 取消 → 降級
2. 確認 webhook 失敗時你會**收到告警**(目前失敗只寫 log,沒人會看到 —— 這仍是缺口)
3. 再切正式模式

### Q2. 現在是否適合公開給陌生使用者?

**仍然不行,而且阻擋條件比 audit_1 時更明確了。**

三個硬性前置條件:

1. **網路層封鎖 worker 對 RFC1918 與 `169.254.0.0/16` 的出站連線。**
   DNS rebinding 無法在應用層完全消除(`docs/SECURITY.md` R1)。
2. **確認 `TRUST_PROXY_HEADERS` 與你的代理設定一致。**
   本輪 H1 證明:設錯的話,所有 per-IP 防護等於不存在。
   而且 Caddy/nginx 的**預設是附加而非覆寫**。
3. **接上 webhook 失敗與 `monitors_due` 的告警。**
   目前 worker 停止工作時,`/healthz` 仍然回 200,沒有任何人會知道。

做完這三件事,可以**邀請制或限量**公開。
完全開放註冊之前,建議先解決 R3(免費方案仍無 email 驗證)。

**兩輪 audit 後我對程式碼的信心明顯提高了,但對「部署設定」的信心沒有。**
剩下的風險幾乎都在設定與營運,不在程式碼。

### Q3. 如果只有新台幣 500 元測試預算,最先應驗證哪個商業假設?

**答案與 audit_1 相同,而且第二輪讓我更確定:驗證「有人願意付錢」。**

補充一個第二輪才看清楚的理由:**這兩輪 audit 修掉了 12 個問題,
沒有任何一個會影響「有沒有人想要這個產品」。** 我花了兩輪把工程風險壓下來,
而商業風險一格都沒有動。這正好說明了資源該往哪裡放。

500 元的具體用法(不變):
1. **不要花在伺服器上**,用免費層。
2. 花在**取得 20–30 次真實對話**,找出「現在正在手動盯著某一頁」的人。
3. 判準不是註冊數,而是**有多少人能具體說出他要盯哪一頁**。

**最省的做法仍然是:先賣再做。**
產品已經能跑,直接問 5 個人「一個月 $19,我幫你盯這頁,要嗎?」
**1 個當場願意付的人,比 100 個免費註冊更有資訊量。**

### Q4. 如果這個產品最終失敗,最可能死在哪裡?

**排序不變:死於需求不足,不是死於技術。**

第二輪要補充的是 —— **我原本低估了「營運」這個死因,現在把它往上調。**

修正後的排序:

**1. 需求強度不足(最可能,不變)**
「知道頁面變了」對多數人是 nice to have。會痛到每月付錢的人很少。

**2. 免費方案就夠用**
3 個監控 × 每小時一次對多數個人使用者已經足夠。

**3. 通知品質決定續訂,而通知品質很難(不變)**
誤報三次就會被取消訂閱。CSS 選擇器會因對方改版失效。

**4. 營運疏忽 —— 本輪調升**
兩輪 audit 顯示這個系統有大量**靜默失敗模式**:
worker 停了但 `/healthz` 仍是 200;webhook 送不出去只寫 log;
保留期限沒跑則儲存無聲成長。**單一開發者的 SaaS 不是死於當機,是死於沒發現壞掉。**
在客戶告訴你「我上週沒收到通知」之前,你不會知道。
這一項在 audit_1 我列在最後,現在我認為它比「被大廠做掉」更可能先發生。

**5. 被大廠順手做掉**

**6. 目標站台封鎖與法律灰帶**

**最不可能的死因**:安全事故。
兩輪 audit 修掉了 2 個 Critical 與 9 個 High,剩下的殘餘風險都已寫進
`docs/SECURITY.md` 並附上緩解方式。**前提是部署時真的照做。**

> 兩輪下來最誠實的一句話:
> **工程風險已經從「會出事」降到「可管理」,商業風險一動也沒動。**
> 如果這個產品失敗,回頭看會發現,問題從來不在這 126 個測試裡。

---

## 本輪變更檔案

```
app/config.py           TRUST_PROXY_HEADERS、兩層鎖定、fetch deadline、retention 設定
app/deps.py             client_ip 尊重代理設定、兩層登入鎖定、匿名分桶改用 client_ip
app/api/auth.py         登入鎖定帶來源、DELETE /me、GET /me/export
app/core/fetcher.py     全域 deadline(H3)
app/core/notifier.py    送信併發化與批次認領(H4)
app/core/retention.py   新增:依方案保留期限刪除歷史(H5)
app/core/scheduler.py   註冊 run_retention 排程工作
app/schemas.py          AccountExportOut
tests/test_security_regressions.py   +8 個回歸測試(共 23)
docs/SECURITY.md        新增 R1b、R8,更新已驗證防護表
docs/API.md             新增 export / delete 端點,標註 /metrics 需認證
docs/UNIT_ECONOMICS.md  加入儲存成本與穩定狀態估算
.env.example            新增 8 個設定項與說明
```

**測試結果:126 passed**(audit_1 後 118,原始 103)
