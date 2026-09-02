# AUDIT_02_FAULT_INJECTION — 故障注入與韌性稽核

**日期**:2026-09-02
**基準 commit**:`8e527e6`(audit_1 + audit_2 修正後)
**方法**:故障注入。建立可程式化的故障伺服器與測試環境,**實際觸發**每一種異常,
再觀察系統行為。所有結論都附實測輸出;沒有任何一項只靠閱讀程式碼推論。
**立場**:主動證偽系統穩定性,不為了讓結果好看而降低標準。

---

## 0. 先驗證前兩輪的修正是否真的存在且有效

規格明確要求「不要預設上一輪修正是正確的」。因此在注入任何新故障之前,
先獨立重跑前兩輪的攻擊腳本:

| 前輪 finding | 重新驗證結果 |
|---|---|
| audit_1 C1 SSRF 重導向 | `status: 400 / blocked_target` ✅ 仍被擋 |
| audit_1 C2 付費卻拿不到方案 | 首次 503 且不記錄 → 重送後 `plan: pro` ✅ |
| audit_1 H2 畸形 metadata | `HTTP 200`,無 500 ✅ |
| audit_1 H5 併發重複認領 | `40 claims / 40 unique / 0 duplicates` ✅ |
| audit_2 H1 X-Forwarded-For 偽造 | `0/20` 建立成功 ✅ |
| audit_2 H2 鎖定阻斷合法使用者 | 12 個攻擊來源後擁有者仍可登入 ✅ |
| audit_2 H3 重導向鏈時間放大 | `deadline_exceeded` ✅ |

程式碼層級也逐一確認關鍵符號存在(`deadline_exceeded`、`safe_findall`、
`_process_event`、`trust_proxy_headers`、`login_max_failures_global`、
`retention`、`_attempt_in_own_session`、`result.rowcount == 1`)。

**結論:前兩輪的修正確實存在且仍然有效。** 本輪的所有新發現都是獨立的問題。

---

## 1. 測試方法與環境

建立了一台可程式化的故障伺服器(`faultserver.py`),單一程序即可產生
timeout、trickle、redirect loop、gzip bomb、malformed HTML、非 UTF-8、
空白頁、JS-only 頁、非 HTML 內容等所有需要的回應形態,
再以 10 支注入腳本驅動真實的 `fetch()`、`run_check()`、`tick()` 與 HTTP API。

| 腳本 | 覆蓋情境 |
|---|---|
| `f1_network.py` | 1–11 網路與 HTTP 層故障 |
| `f2_content.py` | 12–17、50 內容與邊界輸入 |
| `f2b_blank.py` | 暫時性空白頁的誤報行為 |
| `f3_crash.py` | 18–22、38、41–43 crash / restart / 積壓 |
| `f4_db.py` | 23–24 資料庫不可用與連線池 |
| `f5_notify.py` | 25–29、39、40 通知失敗、重複、retry storm |
| `f6_stripe.py` | 29–33 Stripe replay、亂序、併發 |
| `f7_starve.py` / `f7b_fair.py` | 34–36、45–46 資源獨佔與公平性 |
| `f9_capacity.py` | 吞吐量實測 |
| `f10_degrade.py` | 第三方失效時的降級行為、log 量 |

---

## 2. 執行過的故障情境與結果

### 2.1 網路與 HTTP 層(情境 1–11)

修正前後對照(`FETCH_TIMEOUT=5s`、`DEADLINE=12s`):

| # | 情境 | 修正前 | 修正後 |
|---|---|---|---|
| 1 | target timeout | 5.09s `timeout` ✅ | 不變 |
| 2 | DNS lookup 失敗 | 0.06s `network_error` ⚠️ | `dns_error` ✅ |
| 3 | connection refused | 0.06s `network_error` ⚠️ | `connection_refused` ✅ |
| 4 | TLS / certificate error | 0.06s `network_error` ⚠️ | `tls_error` ✅ |
| 5 | HTTP 404 | 記錄為 http_error ✅ | 不變 |
| 6 | HTTP 500 / 503 | 記錄為 http_error ✅ | 不變 |
| 7 | redirect loop | `too_many_redirects` ✅ | 不變 |
| 8 | 過多 redirects | `too_many_redirects` ✅ | 不變 |
| 9 | **非常慢的網站(逐位元組)** | **60.08s(超出 deadline 5 倍)** ❌ | **12.07s `deadline_exceeded`** ✅ |
| 10 | 非常大的 HTML | 在 2,000,000 字元處截斷 ✅ | 不變 |
| 11 | gzip 炸彈(305 KB → 314 MB) | 在 2,000,000 字元處截斷,記憶體有界 ✅ | 不變 |
| — | 連線中途 reset | `network_error` ✅ | `connection_reset` ✅ |

修正後 13 個情境**零未捕捉例外、零 deadline 超時**,最壞 12.07s。

### 2.2 內容與編碼(情境 12–17、50)

| # | 情境 | 結果 |
|---|---|---|
| 12 | malformed HTML | 正確解析出 `unclosed tags price: $19.00`,不當機 ✅ |
| 13a | 非 UTF-8(Big5) | 正確解碼為 `價格 $19.00 繁體中文` ✅ |
| 13b | 無效位元組序列 | `errors="replace"` 安全降級 ✅ |
| 14 | 空白頁 / 只有空白字元 | 兩者正規化為 `''`(見下方 F2,這是問題來源) |
| 15 | JavaScript 渲染頁 | 擷取為 `''` —— **靜默失敗**,見 M4 |
| 16a | 非 HTML(PDF 位元組) | 當作文字處理,不當機 ✅ |
| 16b | 非 HTML(JSON)+ json 模式 | 正確取出 `$19.00` ✅ |
| 16c | 對 HTML 使用 json 模式 | `not_json` 型別化錯誤 ✅ |
| 17 | **網站改版導致 selector 失效** | `selector_no_match`,**基準值保留**,`change events fired: 0` ✅ |
| 50 | 2 MB 單行內容 | 截斷至 200,000 字元上限 ✅ |
| 50 | 50,000 行全變更 diff | 0.09s,`truncated=True`,unified_diff 1,239 字元 ✅ |

情境 17 特別值得記錄:網站改版 → 復原的整個過程**沒有產生任何假變更通知**,
基準值全程保留。這是前幾輪就設計對的地方。

### 2.3 排程、crash 與恢復(情境 18–22、38、41–43)

| # | 情境 | 修正前 | 修正後 |
|---|---|---|---|
| 18/38 | 同一任務重複執行 | 原子認領,第二次取得空集合 ✅ | 不變 |
| 19/39 | 大量 job 同時到期 | 批次上限防止 thundering herd ✅ | 不變 |
| 20/41 | **worker 認領後 crash** | **檢查靜默遺失,24 小時後才重試** ❌ | **3 分鐘 lease 到期即重試** ✅ |
| 22/42/43 | scheduler / container restart | 無狀態,重啟即繼續 ✅ | 不變 |

### 2.4 資料庫(情境 23–24)

| 端點 | DB 中斷時的行為(修正前) | 修正後 |
|---|---|---|
| `/healthz` | 200 ✅(liveness 不碰 DB) | 不變 |
| `/readyz` | 503 + `database: error` ✅ | 不變 |
| `/v1/monitors` | **500 `internal_error`** ⚠️ | **503 `service_unavailable` + `Retry-After: 15`** ✅ |
| 內部細節外洩 | 否 ✅ | 不變 |

連線池:`pool_timeout` 行為正確。但發現連線池**未依 worker 並行度設定**(見 M5)。

### 2.5 通知(情境 25–29、39、40)

| # | 情境 | 結果 |
|---|---|---|
| 25 | 接收端 500 | 記錄錯誤並排程重試 ✅ |
| 39 | **retry storm** | backoff `[60, 300, 1800, 7200]s`,5 次後 `failed`,**共送出 5 次** ✅ 有界且遞增 |
| 26/28 | 4 個 worker 同時排空佇列 | **收到 1 次 POST**,`event_ids={'evt_2': 1}` ✅ exactly-once |
| 27 | 通知延遲 | 由 backoff 決定,可觀測性見 M2 |
| 33 | **送出成功但本地 transaction 失敗** | 收到 1 次 POST,不重複 ✅(硬 kill 仍可能重送,見 L2) |
| 40 | queue backlog | `deliveries_pending` 可見,但**沒有年齡指標**(見 M2) |

### 2.6 Stripe(情境 29–33)

| # | 情境 | 修正前 | 修正後 |
|---|---|---|---|
| 30 | webhook replay | `duplicate event ignored` ✅ | 不變 |
| 29/31 | **亂序送達** | **已取消的客戶被還原成 pro** ❌ | **維持 free** ✅ |
| 31b | 6 個併發方案變更 | **3 個回傳 500** ❌ | **6 個全部 200**,最終方案由最新事件決定 ✅ |
| 32 | 付款成功但 webhook 延遲 | 顯示 free,無「處理中」狀態 ⚠️ | 未修正,見 M6 |

### 2.7 資源與濫用(情境 34–36、44–49)

**惡意使用者獨佔 worker(情境 35)** —— 攻擊者 45 個指向無回應端點的監控,
受害者 5 個正常監控:

| | 修正前 | 修正後 |
|---|---|---|
| 單一 tick 耗時 | 19.7s(正式設定推算 **125s**) | **12.8s** |
| 受害者完成的檢查 | 5/5(但延遲) | 5/5 ✅ |
| 攻擊者佔用的批次 | 45/45(整批) | 26/45(受公平上限節流) |

**公平性驗證(`f7b_fair.py`)** —— hog 有 200 個到期監控,alice/bob/carol 各 3 個:

| | 修正前 | 修正後 |
|---|---|---|
| 批次分配 | `{hog: 20}` —— 其餘三人**完全被排擠** ❌ | `{hog: 10, alice: 3, bob: 3, carol: 3}` ✅ |
| 單一使用者情境的吞吐量 | 50/tick | **50/tick(未下降)** ✅ |

**第三方失效時的降級(`f10_degrade.py`)** —— Stripe 完全不可用時:

| 功能 | 狀態 |
|---|---|
| 註冊 / 建立監控 / 列表 / 執行檢查 / 用量 / 預覽 / 方案表 / 訂閱查詢 / healthz / readyz | **全部正常** ✅ |
| `/v1/billing/checkout` | 503(唯一失效的功能) |

**只有付費升級路徑失效,監控產品本身端到端持續運作。** 這是正確的優雅降級。

---

## 3. 發現的問題、嚴重程度與修正

### Critical

#### C1 — 逐位元組回應可無限佔用 worker 執行緒(slow-loris)

**測試方法**:故障伺服器以 chunked 編碼每 1 秒送出 15 bytes,持續 60 秒。
每個 chunk 都遠在 socket timeout(5s)之內。

**結果**:單一檢查佔用 **60.08 秒**,而總 deadline 設定為 12 秒。

**根因**:`FETCH_TOTAL_DEADLINE_SECONDS` 只在**重導向跳轉之間**檢查,
body 串流讀取迴圈內完全沒有檢查。httpx 的 timeout 是「每次 socket 操作」的,
因此「持續送、但送得極慢」的伺服器永遠不會觸發它。

**影響**:`WORKER_CONCURRENCY` 個這種 URL 即可讓**整個檢查系統停止運作**。
任何使用者都能觸發,甚至可能無意間觸發(監控一個很慢的網站)。
這是本輪唯一評為 Critical 的問題,因為它讓**所有使用者**的核心功能失效。

**修正**:在 `iter_bytes()` 迴圈內每個 chunk 檢查一次 deadline
(`app/core/fetcher.py`)。

**重新測試**:60.08s → **12.07s**,錯誤型別 `deadline_exceeded`。
13 個網路情境全部零 deadline 超時。

**regression test**:`test_trickling_site_cannot_hold_a_worker_past_the_deadline`

---

### High

#### H1 — worker crash 造成檢查靜默遺失(無 lease / heartbeat)

**測試方法**:讓 worker 認領一個間隔 24 小時的監控,然後在寫入任何 Check 之前
中止流程,再以重啟後的 worker 視角查詢資料庫。

**結果**:
```
claimed: [1]
next_check_at moved: 2026-09-02T13:48 -> 2026-09-03T13:48
Check rows written: 0
due monitors found after restart: []
this monitor will not be retried for another 24.0 hours
```

**根因**:認領時直接把 `next_check_at` 推進**一整個間隔**。
重啟後的 worker 無法區分「已完成」與「認領後死亡」。

**影響**:每次 worker crash / OOM / 部署重啟,**當下所有進行中的檢查全部靜默遺失**,
沒有 Check 紀錄、沒有錯誤、沒有重試。對長間隔監控等於漏掉一整個週期。
這正是「單人 SaaS 死於沒發現壞掉」的具體形態。

**修正**:認領改為**短期 lease**(`CLAIM_LEASE_SECONDS=180`),
真正的 `next_check_at` 由 `run_check()` 完成時寫入;worker 死亡則 lease 到期後重試。

**重新測試**:重試等待從 **24.0 小時 → 0.0 小時**(3 分鐘)。

**regression test**:`test_claim_is_a_short_lease_not_a_full_reschedule`、
`test_completed_check_reschedules_at_the_full_interval`(確保 lease 沒有變成正常節奏)

---

#### H2 — 暫時性空白頁產生假變更通知

**測試方法**:同一個價格頁,序列為 正常 → 空白 → 正常 → 空白 → 正常。
內容從頭到尾**沒有真正改變**。

**結果(修正前)**:
```
change events fired: 4
webhook deliveries queued: 4
   evt 1: removed=['$19.00']
   evt 2: added=['$19.00']
   evt 3: removed=['$19.00']
   evt 4: added=['$19.00']
```
兩次短暫空白 → **4 次假通知**。

**根因**:HTTP 200 + 空 body 被當成合法內容。實務上這幾乎都是網站部署中、
中介頁或軟錯誤頁。

**影響**:這是**流失率的直接來源**。前兩輪我把「通知品質決定續訂」列為主要死因,
這就是它最常見的觸發形態。

**修正**:空內容取代非空內容時,必須連續觀察到
`EMPTY_CONTENT_CONFIRMATIONS`(預設 2)次才採信;
未達確認次數則記為 `empty_content` 錯誤並**保留基準值**。
確認次數由 Check 歷史推導,不需要 schema migration(專案尚無 migration 工具)。

**重新測試**:
```
change events fired: 0      (4 → 0)
webhook deliveries queued: 0
基準值全程保留為 '$19.00'
```

**對照測試(確認沒有把功能改壞)**:真正變空的頁面 ——
```
blank observation 1: status=error error=empty_content
blank observation 2: status=changed          <- 仍然回報
change events fired: 1
```
真實的清空**仍會通知**,只是延後一個間隔。

**regression test**:`test_transient_blank_page_produces_no_false_notification`、
`test_a_genuinely_emptied_page_is_still_reported`

---

#### H3 — Stripe 亂序送達使已取消的客戶被還原為付費方案

**測試方法**:先送出 `subscription.deleted`(created=2000),
再送出較早的 `subscription.updated / active`(created=1000),模擬 Stripe 延遲送達。

**結果(修正前)**:
```
plan after cancellation: free
stale active(created=1000) delivered LATE -> 200
plan after the stale event: pro
!! A CANCELLED CUSTOMER WAS SILENTLY RE-UPGRADED
```

**根因**:完全忽略事件時序。**Stripe 明確不保證 webhook 順序。**

**影響**:已取消訂閱的帳號無限期保有付費功能,且沒有任何告警。
這是收入完整性問題,而且是靜默的。

**修正**:在 `users` 新增 `last_stripe_event_at`,
比事件 `created` 舊的事件一律忽略並記錄 `stripe_event_stale_ignored`。

**重新測試**:`plan after the stale event: free` ✅
並驗證較新的事件仍然正常套用(`test_a_newer_event_still_applies`)。

**regression test**:`test_a_stale_event_cannot_resurrect_a_cancelled_subscription`

---

#### H4 — Stripe 事件記錄的 commit 未受保護,併發時回傳 500

**測試方法**:8 個併發 webhook 送往同一帳號。

**結果(修正前)**:`6 of 8 concurrent webhooks -> 500`。

**根因**:`db.add(StripeEvent(...)); db.commit()` 位於 try/except **之外**,
是整條路徑上唯一未受保護的寫入。這是 **audit_2 自己的修正留下的破口**。

**影響**:Stripe 對 500 會重送 → 併發變成重送風暴。

**修正**:該 commit 移入受保護區塊;失敗時 rollback 並記錄警告
(所有處理分支本身是冪等的,重送安全)。

**重新測試**:`6 concurrent plan-change webhooks -> 全部 200`。

**regression test**:`test_concurrent_webhook_redelivery_never_returns_500`

---

#### H5 — 缺乏使用者間公平性,單一帳號可獨佔整個批次

**測試方法**:hog 帳號 200 個到期監控,alice/bob/carol 各 3 個,批次上限 20。

**結果(修正前)**:`{hog: 20}` —— 另外三個使用者**一個都沒被服務**。

**根因**:認領查詢是單一的 `ORDER BY next_check_at LIMIT n`。
候選集合本身就被 hog 填滿,**任何下游的公平性邏輯都救不回來**
(我第一次嘗試用「取樣加寬 + 交錯」修正,實測仍然是 `{hog: 20}` —— 失敗,
因為 hog 的監控數超過取樣窗)。

**修正**:公平性移到**查詢層**。先取出有待辦工作的帳號清單,
再**逐帳號**分別查詢其最舊的待辦監控,交錯後套用每帳號上限(批次的 25%),
最後以第二輪填滿剩餘名額。

**重新測試**:
```
{hog: 10, alice: 3, bob: 3, carol: 3}   小型使用者全部被服務 ✅
單一使用者情境:50/tick(吞吐量未因公平性下降)✅
情境 35 的 tick 時間:19.7s → 12.8s
```

**regression test**:`test_one_user_cannot_monopolise_the_batch`、
`test_a_single_user_still_gets_the_whole_batch`

---

#### H6 — 出貨設定的容量低於販售方案所承諾的量

**測試方法**:實測吞吐量,並與 `app/plans.py` 的方案上限對照。

**結果(修正前)**:
```
系統持續容量 = CHECK_BATCH_SIZE / SCHEDULER_TICK_SECONDS = 50/20s = 2.50 checks/s
Business 方案單一客戶所需 = 300 monitors / 60s     = 5.00 checks/s
-> 一個 Business 客戶需要「整個系統預設容量的 2 倍」
```

**根因**:持續容量完全由 batch/tick 決定,與 CPU 無關;
而這兩個預設值從未與定價頁對照過。`UNIT_ECONOMICS.md` 的容量估算是以
CPU 推導的,**估錯了瓶頸所在**。

**影響**:賣得出去、但交付不了。漏掉的檢查對客戶而言就是產品故障。

**修正**:
- 預設值調整為 `CHECK_BATCH_SIZE=200`、`SCHEDULER_TICK_SECONDS=10`、
  `WORKER_CONCURRENCY=16` → 持續容量 20 checks/s
- 新增 `Settings.capacity_report()`,**啟動時記錄容量**,
  且當容量低於最大方案所需時發出 `capacity_below_advertised_plan` 警告
- `.env.example` 寫明容量公式

**重新測試**:
```
sustained_checks_per_second: 20.0
can_serve_one_largest_plan_customer: true
實測吞吐量:16.1 checks/second(967/min)
```

**regression test**:`test_configured_capacity_can_serve_the_largest_plan`、
`test_capacity_report_detects_an_under_provisioned_configuration`

---

### Medium

| # | 問題 | 處置 |
|---|---|---|
| M1 | DB 中斷時 API 回傳泛用 500,LB 與客戶端無法區分「暫時性」 | **已修**:`OperationalError` / `DBAPIError` → 503 `service_unavailable` + `Retry-After: 15` |
| M2 | 佇列只有深度沒有年齡,無法區分「健康的尖峰」與「卡住一小時」 | **已修**:`/metrics` 新增 `oldest_pending_delivery_age_seconds` 與 `oldest_due_monitor_age_seconds`(實測 42 分鐘的積壓正確回報 2520) |
| M3 | DNS / TLS / refused / reset 全部塌縮成 `network_error` | **已修**:新增 `dns_error`、`tls_error`、`connection_refused`、`connection_reset`、`connection_failed` |
| M4 | JS 渲染頁面靜默擷取為空 —— 使用者以為在監控,其實什麼都沒監控 | **未修**,已記錄。緩解:`/v1/preview` 會顯示擷取結果為空,建立監控前可看見。真正的解法需要無頭瀏覽器 |
| M5 | 連線池未依 worker 並行度設定;`WORKER_CONCURRENCY` 提高到 16 後已超過 SQLAlchemy 預設池(5+10) | **已修**:Postgres 引擎的 `pool_size` / `max_overflow` 由 `worker_concurrency` 推導(16 + 16 = 32) |
| M6 | 付款成功但 webhook 延遲時,客戶看到 `plan: free` 且無「處理中」提示 | **未修**,已記錄。需要在 checkout 後寫入 pending 狀態,牽涉額外狀態機 |

### Low

| # | 問題 | 處置 |
|---|---|---|
| L1 | POST 實際成功但處理程序在 commit 前崩潰時,delivery 被記為 `failed`(稽核紀錄失真,不影響客戶) | 記錄 |
| L2 | 硬性 kill 後 pending 的 delivery 會重送 —— at-least-once 語意 | 記錄。已由**穩定的 `event_id`** 緩解,且 `docs/API.md` 明確要求接收端以 `event_id` 去重 |
| L3 | 應用程式本身不做 log rotation,依賴容器 runtime / systemd | 記錄於下方「尚未解決」 |

---

## 4. 系統性問題檢核表

| 檢查項 | 結論 |
|---|---|
| job 是否具備 idempotency | ✅ 原子認領 + delivery 批次認領 + Stripe 事件去重 |
| retry 是否有上限與 exponential backoff | ✅ 實測 `[60, 300, 1800, 7200]s`,5 次後放棄 |
| 是否可能產生 retry storm | ✅ 已排除(H4 修正前的 500 是唯一來源) |
| 是否可能重複收費 | ✅ 不會。本系統從不主動扣款,金流全由 Stripe Checkout 執行 |
| 是否可能重複通知 | ⚠️ at-least-once:硬 kill 後可能重送,由穩定 `event_id` 緩解(L2) |
| 是否可能漏掉通知 | ✅ **本輪修正**(H1 lease)。修正前 crash 會靜默漏掉整個週期 |
| crash 後是否能安全恢復 | ✅ 無狀態 worker + lease 到期重試 |
| scheduler 是否可能同一任務跑兩次 | ✅ 條件式原子更新,實測 4 worker 零重複 |
| 是否有 dead job / zombie job | ✅ **本輪修正**。lease 之前,被認領後死亡的 job 就是 zombie |
| queue backlog 是否可觀察 | ✅ **本輪補齊**:深度 + 年齡 |
| 是否有 timeout 上限 | ✅ 單次 timeout + 總 deadline,**含 body 串流**(本輪修正) |
| response size 是否有限制 | ✅ 2 MB 串流截斷,對 314 MB 解壓縮炸彈有效 |
| concurrency 是否有限制 | ✅ `WORKER_CONCURRENCY` + 批次上限 + 連線池(本輪對齊) |
| 每個使用者是否有資源配額 | ✅ 監控數、最短間隔、每月檢查數 |
| 單一惡意使用者是否可拖垮整個服務 | ⚠️ **修正前:可以**(C1 + H5)。修正後受 deadline 與公平上限限制 |
| memory / CPU / disk / DB exhaustion | ✅ 記憶體實測 500 及 1000 個監控 RSS 成長 0.0 MB;儲存由 audit_2 的保留期限控制 |
| circuit breaker / graceful degradation | ⚠️ **無 circuit breaker**(見「尚未解決」);但降級行為正確 |
| 部分第三方失效時能否繼續服務 | ✅ Stripe 全掛時,除 checkout 外**全部功能正常** |

---

## 5. 當前系統能承受的大致負載範圍

以本輪實測為準(單一容器、SQLite、對本機目標;數字為**本機環境實測**,
真實網際網路目標的延遲會更高):

| 指標 | 實測值 |
|---|---|
| 突發吞吐量 | **16.1 checks/s**(967/min),1000 個監控 5 個 tick 完成 |
| 持續容量上限(batch/tick) | **20 checks/s** = 51.8M checks/月 |
| 記憶體 | 500 → 1000 個監控,RSS 成長 **0.0 MB**(無洩漏跡象) |
| 單一 tick 最壞情況 | 全批次逾時 URL,受 `FETCH_TOTAL_DEADLINE_SECONDS=45` 約束 |

換算成可支撐的監控數:

| 檢查間隔 | 可支撐監控數 |
|---|---|
| 1 分鐘 | 約 1,200 |
| 5 分鐘 | 約 6,000 |
| 60 分鐘 | 約 72,000 |

換算成客戶數(以方案上限滿載計,實務上遠低於此):

| 方案 | 滿載所需 | 單機可容納 |
|---|---|---|
| Free(3 監控 / 60 分) | 0.0008 checks/s | 數萬 |
| Pro(50 監控 / 5 分) | 0.17 checks/s | **約 120 位** |
| Business(300 監控 / 1 分) | 5.0 checks/s | **約 4 位** |

> **注意**:tick 實際耗時 10.9–18.0s,已超過 10s 的 tick 間隔。
> APScheduler 的 `max_instances=1` + `coalesce=True` 會讓它優雅地降級為
> 「盡快連續執行」而不是堆積,但這代表**worker pool 已是新的瓶頸**。
> 超過上述負載時,下一步是提高 `WORKER_CONCURRENCY` 並改用 Postgres 拆分多 worker。

---

## 6. 最容易造成整體服務失效的三個情境

依「可能性 × 影響 × 偵測難度」排序:

### 第一名 —— worker 執行緒被慢速目標耗盡(本輪 C1)

修正前,**任何**使用者監控一個逐位元組回應的網站,就能無限佔用一個 worker 執行緒;
`WORKER_CONCURRENCY` 個就足以讓**所有客戶的檢查全部停止**。
最危險的地方在於:`/healthz` 仍然回 200,API 仍然正常回應,
**外部看起來一切健康**,只有檢查悄悄停了。

已由 body 串流 deadline 修正,但這仍是排名第一,因為
「執行緒池耗盡 + 健康檢查說沒事」是這個架構最容易重演的失效形態。
**監控方式**:`/metrics` 的 `oldest_due_monitor_age_seconds` 持續上升。

### 第二名 —— 單一使用者的監控佔滿批次(本輪 H5)

不需要惡意,只要一個 Business 客戶把 300 個監控設成 1 分鐘,
在原本的 `ORDER BY next_check_at` 之下就會持續填滿每一個批次,
其他所有客戶的檢查無限延後。實測 `{hog: 20}`,其餘三名使用者**完全沒被服務**。

已由查詢層公平性修正。之所以排第二,是因為它**完全不需要攻擊者**,
一個正常的大客戶就會觸發。

### 第三名 —— worker crash 造成大量檢查靜默遺失(本輪 H1)

修正前,每次部署、OOM 或 crash,當下所有被認領的檢查全部消失,
沒有紀錄、沒有錯誤、沒有重試。對日檢查的監控等於漏掉一整天。
使用者只會在「我上週沒收到通知」時才發現,而那時已經無從追查。

已由 lease 機制修正。

> 三者的共同點:**失效時系統看起來是健康的。** 這正是本輪把
> `oldest_due_monitor_age_seconds` 與 `oldest_pending_delivery_age_seconds`
> 加進 `/metrics` 的原因 —— 沒有這兩個數字,上述三種情境都是靜默的。

---

## 7. 尚未解決的問題

| 問題 | 為什麼沒修 | 緩解 |
|---|---|---|
| **無 circuit breaker** | 對持續失敗的目標站台,目前只有指數退避與連續失敗自動暫停,沒有真正的斷路器。加入完整斷路器會顯著增加狀態複雜度,而現有機制已涵蓋主要情境 | `CONSECUTIVE_FAILURES_BEFORE_PAUSE=20` 後自動停用;webhook 5 次後放棄 |
| **JS 渲染頁面靜默擷取為空**(M4) | 需要無頭瀏覽器,成本與維護負擔量級不同 | `/v1/preview` 會顯示空結果,建立前可見 |
| **付款處理中無狀態顯示**(M6) | 需要額外的 pending 狀態機 | 客戶重新整理即可,Stripe webhook 通常數秒內送達 |
| **at-least-once 通知**(L2) | 分散式系統中 exactly-once 需要接收端配合 | 穩定 `event_id` + `docs/API.md` 明確要求去重 |
| **應用層不做 log rotation**(L3) | 標準做法是交給容器 runtime / systemd | Docker 預設 json-file driver 需設定 `max-size`;已列入部署檢查 |
| **單一 worker 仍是單點** | 多 worker 需要 Postgres;認領邏輯已支援 | `oldest_due_monitor_age_seconds` 告警 |
| **DNS rebinding**(前輪殘餘) | 應用層無法完全消除 | 網路層 egress 封鎖,見 `docs/SECURITY.md` R1 |

---

## 8. 明確回答五個問題

### Q1. 如果今天有 100 個陌生使用者同時開始使用,系統最可能先在哪裡壞掉?

**不會壞在容量,會壞在「有人監控了一個慢網站」。**

100 個使用者若都是 Free 方案(3 監控 / 每小時),總負載是
`100 × 3 / 3600 = 0.083 checks/s` —— 僅為系統容量(20 checks/s)的 **0.4%**。
容量完全不是問題。

實際最先出問題的順序:

1. **一兩個使用者監控了回應很慢或很大的網站**,吃掉不成比例的 worker 時間。
   修正前這會無限累積;修正後受 45 秒 deadline 約束,但仍會拉高整體延遲。
2. **註冊節流誤傷**:`SIGNUP_LIMIT_PER_HOUR_PER_IP=5`。
   若這 100 人來自同一個公司網路或同一場活動的 NAT,**第 6 個人開始就註冊失敗**。
   這是最可能的第一通客訴,而且它是我們自己的防護造成的。
3. **webhook 接收端品質參差**:陌生使用者的 endpoint 有相當比例會是壞的,
   `deliveries_failed` 會快速上升 —— 這是正常現象,但目前**沒有任何告警**。

**不會壞的地方**:CPU、記憶體(1000 個監控 RSS 成長 0.0 MB)、資料庫容量。

### Q2. 如果某個使用者惡意建立大量監控任務,目前防護是否足夠?

**現在夠了,但這正是本輪修掉的東西 —— 修正前完全不夠。**

現有四層防護,全部經過實測:

| 層 | 機制 | 實測 |
|---|---|---|
| 數量 | 方案監控數上限(3 / 50 / 300) | 前輪驗證 |
| 頻率 | 方案最短間隔 + 每網域節流 | 前輪驗證 |
| 時間 | 單次檢查 45 秒總 deadline(**本輪**) | 60s → 12s |
| 公平 | 每帳號批次上限 25%(**本輪**) | `{hog: 20}` → `{hog: 10, alice: 3, bob: 3, carol: 3}` |
| 總量 | 每月檢查數配額 | 前輪驗證 |

**仍然不足的地方**:免費方案沒有 email 驗證(`docs/SECURITY.md` R3)。
攻擊者仍可用多個 IP 開多個帳號,每個帳號各自都在配額內 ——
**每個帳號都守規矩,總和卻不受控**。這是目前最大的濫用缺口,
公平性機制只保證單一帳號不獨佔,不保證同一個人不開 50 個帳號。

### Q3. 如果 worker 或 server 在任務執行途中 crash,是否會造成資料錯亂、重複通知或重複收費?

分三部分回答,因為三者的答案不同:

**資料錯亂:不會。**
每個檢查各自使用獨立 session,寫入是單一交易。crash 只會留下「沒寫入」,
不會留下寫到一半的狀態。實測 crash 後資料庫狀態一致。

**重複收費:不會。**
本系統**從不主動扣款**。所有金流由 Stripe Checkout 執行,
我們只是被動同步訂閱狀態。crash 最壞的結果是方案同步延遲,
Stripe 重送後即恢復(audit_1 C2 已修正,本輪重新驗證通過)。

**重複通知:有可能,這是刻意的設計取捨。**
若程序在 webhook POST 送出之後、資料庫 commit 之前被硬性 kill,
該 delivery 會維持 pending 並在 lease 到期後重送 —— 客戶會收到兩次相同事件。
這是 at-least-once 語意:在分散式系統中,「絕不重複」與「絕不遺漏」只能選一個,
**而對監控服務來說,漏掉通知遠比重複通知嚴重**。
緩解方式是 payload 帶有穩定的 `event_id`,`docs/API.md` 明確要求接收端據此去重。

**本輪修正的是相反方向的問題**:crash 曾經會造成**漏掉**通知(H1),
因為被認領的檢查會靜默消失。那個問題比重複嚴重得多,現已修正。

### Q4. 目前是否已經具備 production 所需的基本 fault tolerance?

**具備「基本」,不具備「成熟」。**

已經到位(全部經實測驗證):

- 所有外部故障都有型別化的錯誤處理,零未捕捉例外
- 每個檢查有硬性時間上限,涵蓋重導向與 body 串流
- 回應大小上限對解壓縮炸彈有效
- 重試有上限且指數退避,無 retry storm
- crash 後可安全恢復,不遺失工作
- 併發下無重複認領、無重複送信
- 第三方(Stripe)全掛時核心產品仍完整運作
- 資料庫中斷時降級為 503 而非 500

仍然缺少的「成熟」部分:

- **沒有 circuit breaker**:對持續失敗的目標只有退避,沒有真正的斷路
- **沒有告警**:所有指標都存在,但沒有任何東西會主動通知你。
  這是最大的缺口 —— **指標沒人看,等於沒有指標**
- **單一 worker 仍是單點**:認領邏輯已支援多 worker,但需要 Postgres
- **沒有 migration 工具**:本輪為此刻意避開 schema 變更(H2 的確認次數改用查詢歷史推導)

> 一句話:**系統現在會「正確地失敗」,但還不會「告訴你它失敗了」。**

### Q5. 哪些問題仍然會阻止公開上線?

按阻擋強度排序。**前三項是硬性阻擋,沒做完不應該公開。**

**1.(硬性)網路層 egress 封鎖 —— 來自 audit_1/2 的殘餘風險**
必須在防火牆封鎖 worker 對 RFC1918 與 `169.254.0.0/16` 的出站連線。
DNS rebinding 在應用層無法完全消除。這是唯一一個「不做就是把內網暴露給任何註冊者」的項目。

**2.(硬性)基本告警**
至少三條:`oldest_due_monitor_age_seconds` 持續上升、
`deliveries_failed` 異常增加、Stripe webhook 失敗。
本輪三個最危險的失效情境**全部是靜默的** —— 沒有告警,
你只會從客戶的抱怨中得知,而那時已無從追查。

**3.(硬性)一次真實的 Stripe test-mode 端到端付款**
本輪的 Stripe 修正(H3 亂序、H4 併發)全部是用**模擬事件**驗證的。
真實 Stripe 的事件順序、時鐘偏移與重送間隔無法完全模擬。

**4.(強烈建議)免費方案的 email 驗證或 CAPTCHA**
公平性機制保證單一帳號不獨佔,但不阻止一個人開 50 個帳號。
公開註冊前應至少加上其一。

**5.(建議)反向代理設定與 `TRUST_PROXY_HEADERS` 的一致性複查**
audit_2 已證明設錯會讓所有 per-IP 防護失效,而 Caddy/nginx 的預設是附加而非覆寫。

**不再阻擋上線的項目**(本輪已修正):worker 執行緒耗盡、
單一使用者獨佔批次、crash 漏掉檢查、假變更通知、Stripe 亂序、容量低於販售方案。

---

## 9. 本輪變更檔案

```
app/core/fetcher.py     body 串流 deadline(C1)、傳輸錯誤分類(M3)
app/core/engine.py      lease 認領(H1)、空白內容確認(H2)、查詢層公平性(H5)
app/api/billing.py      事件時序防護(H3)、事件記錄 commit 保護(H4)
app/models.py           User.last_stripe_event_at
app/config.py           容量預設值調整與 capacity_report()(H6)、lease / 公平 / 空白確認設定
app/main.py             啟動時容量檢查與警告(H6)
app/errors.py           資料庫故障降級為 503 + Retry-After(M1)
app/api/health.py       佇列與檢查的「年齡」指標(M2)
app/db.py               連線池依 worker 並行度設定(M5)
.env.example            容量公式說明與新設定項
tests/test_fault_injection.py   新增 16 個故障回歸測試
```

**測試結果:142 passed**(本輪前 126,audit_1 前 103)

實測環境:單一容器、Python 3.11、SQLite、本機故障伺服器。
所有數字均為本輪實際執行所得,未經修飾。
