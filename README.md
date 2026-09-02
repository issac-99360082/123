# Watchpost

> ## ⚠️ 商業驗收結論:**Pivot** —— 在驗證需求之前,請勿再開發任何功能
>
> 第五輪商業驗收([`AUDIT_03_BUSINESS_VALIDATION.md`](AUDIT_03_BUSINESS_VALIDATION.md))
> 的結論是:本專案目前**沒有不可替代的優勢**,而且面對一個免費、開源、
> 功能更完整的領導者([changedetection.io](https://github.com/dgtlmoon/changedetection.io),
> 33.5k stars,託管版 $8.99/月)。
>
> 保守漏斗模型顯示:**每 9,895 名訪客才會產生 1 位付費客戶**,
> $500 MRR 需要約 260,000 名訪客 —— 對沒有受眾的獨立開發者不可行。
>
> **下一步不是寫程式,是用一週、NT$0 做人工需求驗證**(見該文件第 10 節)。
>
> 第六輪已選定 Pivot 方向:[`AUDIT_04_PIVOT_SELECTION.md`](AUDIT_04_PIVOT_SELECTION.md)
> —— **進出口貿易法規/關稅變更監控,賣給報關行與貿易顧問**。
> 既有程式碼中 **53% 的核心引擎保留、45% 的自助 SaaS 外殼凍結**。
> 下方的定價與方案設計**尚未經過任何市場驗證**,請視為假設而非事實。

**開發者導向的網頁／API 變更監控服務。**

監看任何公開 URL 或 JSON API，只擷取你在意的那一小塊內容，
在它真正改變時，把**帶 HMAC 簽章的結構化 JSON diff** POST 到你的 webhook。

不是截圖、不是紅框，是程式可以直接消費的事件。

```
                  ┌──────────┐   robots.txt / SSRF guard / per-host throttle
     排程 ────────▶│  fetch   │────────────────────────────────▶ 目標站台
                  └────┬─────┘
                       ▼
                  ┌──────────┐   CSS / regex / JSON path + 正規化 + 忽略規則
                  │ extract  │
                  └────┬─────┘
                       ▼
                  ┌──────────┐   SHA-256 比對 → 只有真正變化才往下走
                  │  diff    │
                  └────┬─────┘
                       ▼
                  ┌──────────┐   HMAC 簽章 + 指數退避重試
                  │ deliver  │────────────────────────────────▶ 你的 webhook
                  └──────────┘
```

---

## 這個專案是什麼

這是一份完整、可部署、具備收費機制的 MVP，不是 prototype。
選題經過明確的評分比較（見 [`docs/OPPORTUNITY_ANALYSIS.md`](docs/OPPORTUNITY_ANALYSIS.md)），
在「實際可產生收入、高度自動化、低啟動成本、低維護、單人可管理、不違法不擾民」
六項限制下，於 7 個候選方向中以加權分數 **7.85 / 10** 勝出。

**下檔風險是每月 $12 的伺服器費用；第一位付費客戶即可損益兩平。**
（推導見 [`docs/UNIT_ECONOMICS.md`](docs/UNIT_ECONOMICS.md)）

---

## 60 秒啟動

```bash
./scripts/install.sh --dev          # venv + 相依套件 + .env + 資料庫 + 測試
source .venv/bin/activate
uvicorn app.main:app --reload
```

- 首頁：<http://localhost:8000>
- 互動式 API 文件：<http://localhost:8000/docs>
- 端到端煙霧測試：`./scripts/smoke_test.sh`

Docker：

```bash
cp .env.example .env
docker compose up -d --build        # Postgres + API + 獨立 worker
```

**不需要任何第三方帳號即可完整運行**——註冊、建立監控、抓取、偵測變更、
送出簽章 webhook 全部可用。Stripe 只在你要開始收款時才需要
（見 [`docs/SETUP.md`](docs/SETUP.md)）。

---

## 使用流程

```bash
# 1. 註冊，取得 API key（完全自助，無需人工）
KEY=$(curl -sS -X POST http://localhost:8000/v1/auth/signup \
  -H 'Content-Type: application/json' \
  -d '{"email":"you@example.com","password":"correct-horse-42"}' \
  | python3 -c "import json,sys;print(json.load(sys.stdin)['key'])")

# 2. 先預覽會比對到什麼，再決定要不要建立監控
curl -sS -X POST http://localhost:8000/v1/preview \
  -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' \
  -d '{"url":"https://pypi.org/project/fastapi/","extract_mode":"css","extract_value":"h1"}'
# → {"extracted": "fastapi 0.141.1", "content_hash": "5206d3...", ...}

# 3. 建立監控（webhook_secret 只回傳這一次）
curl -sS -X POST http://localhost:8000/v1/monitors \
  -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' \
  -d '{
        "name":"fastapi release",
        "url":"https://pypi.org/project/fastapi/",
        "extract_mode":"css",
        "extract_value":"h1",
        "interval_seconds":3600,
        "notify_webhook_url":"https://your-app.example/hooks/watchpost"
      }'
```

之後就不需要再做任何事：排程器會自動抓取，只有在 `h1` 的內容真的改變時
才會送出通知。

---

## 專案結構

```
app/
  main.py            FastAPI 應用、中介層、安全標頭
  config.py          環境變數設定（pydantic-settings）
  db.py              引擎、session、UTC-aware 時間欄位型別
  models.py          ORM：users / api_keys / monitors / checks
                     / change_events / deliveries / monthly_usage / stripe_events
  schemas.py         Pydantic 請求與回應模型（即 API 契約）
  security.py        密碼雜湊、API key、HMAC webhook 簽章
  errors.py          統一的 JSON 錯誤格式與例外處理
  deps.py            認證、配額檢查、速率限制
  plans.py           方案定義與限制
  worker.py          獨立 worker 進入點
  core/
    fetcher.py       HTTP 抓取 + SSRF 防護 + robots.txt + 每網域節流
    extractor.py     CSS / regex / JSON path 擷取 + 正規化
    differ.py        結構化 diff（webhook payload 的核心產物）
    notifier.py      webhook / email 送達、簽章、指數退避重試
    engine.py        檢查主迴圈：fetch → extract → compare → notify
    retention.py     依方案保留期限刪除過期歷史(成本控制)
    scheduler.py     APScheduler 排程與執行緒池
  api/
    auth.py          註冊、登入、API key 管理
    monitors.py      監控 CRUD、手動執行、歷史、預覽
    billing.py       Stripe checkout / portal / webhook
    health.py        healthz / readyz / metrics
templates/           首頁與付款結果頁
tests/               142 個測試(含 39 個 audit 與故障注入回歸測試)
scripts/             install.sh、smoke_test.sh
docs/                選型分析、單位經濟、SETUP、DEPLOY、API
```

---

## 商業模式

自助訂閱制。使用者自行註冊、自行刷卡、自行取消，**全程無人工介入**。

| | Free | Pro | Business |
|---|---|---|---|
| 價格 | $0 | **$19/月** | **$79/月** |
| 監控數 | 3 | 50 | 300 |
| 最短檢查間隔 | 60 分 | 5 分 | 1 分 |
| 每月檢查數 | 2,200 | 450,000 | 5,000,000 |
| 歷史保留 | 7 天 | 90 天 | 365 天 |

- 收費由 Stripe Checkout 處理，取消與換方案由 Stripe Customer Portal 處理。
- **Stripe 是唯一的真實來源**：只有通過簽章驗證的 webhook 才會升級方案，
  單純造訪 success URL 不會給予任何權限。
- 配額在 API 層（建立監控時）與排程層（執行檢查時）雙重強制。

毛利率約 89–92%，推導見 [`docs/UNIT_ECONOMICS.md`](docs/UNIT_ECONOMICS.md)。

---

## 安全措施

| 措施 | 實作位置 |
|---|---|
| SSRF 防護：拒絕私有／loopback／link-local／metadata 位址，含 DNS 解析後檢查 | `core/fetcher.py` |
| API key 僅存 SHA-256 雜湊，明文只顯示一次 | `security.py` |
| 密碼以 bcrypt 雜湊，並先經 SHA-256 避免 72 bytes 截斷 | `security.py` |
| Webhook HMAC-SHA256 簽章，含時間戳以防重放 | `security.py` |
| Stripe webhook 簽章驗證 + 事件去重（冪等） | `api/billing.py` |
| 速率限制（token bucket，以 key 或 IP 計） | `deps.py` |
| 資料隔離：所有查詢以 `user_id` 過濾 | `api/monitors.py` |
| 危險請求標頭過濾、標頭注入防護 | `core/fetcher.py` |
| 回應大小上限、逾時、redirect 上限 | `core/fetcher.py` |
| 安全標頭（CSP、nosniff、DENY、HSTS） | `main.py` |
| 錯誤不外洩內部細節，僅回傳追查 reference | `errors.py` |
| 容器以非 root 使用者執行 | `Dockerfile` |
| production 若未更換 `SECRET_KEY` 則拒絕啟動 | `main.py` |

## 合規與對目標站台的行為

第 6 項限制（不違法、不欺騙、不擾民、不違反第三方條款）在程式碼中強制執行：

- **預設遵守 `robots.txt`**（已對真實站台驗證：PyPI 的 `/simple/` 被正確拒絕）
- **明確的 User-Agent**，含服務識別與聯絡網址，不偽裝成瀏覽器使用者
- **每網域最小抓取間隔**，避免對目標站台造成壓力
- **連續失敗自動暫停**，不會對已失效的 URL 無限期重試
- **只抓公開內容**，不提供繞過付費牆或登入牆的功能
- **通知只送到使用者自己設定的目的地**，不做任何未經同意的推播

---

## 測試

```bash
python -m pytest -q          # 142 passed
```

| 檔案 | 涵蓋範圍 |
|---|---|
| `test_security.py` | 密碼、API key、HMAC 簽章、SSRF 防護（18） |
| `test_extraction.py` | 五種擷取模式、正規化、忽略規則、diff（18） |
| `test_engine.py` | 變更偵測、錯誤處理、退避、自動暫停、配額（14） |
| `test_api.py` | 認證、CRUD、跨帳號隔離、配額、錯誤格式（28） |
| `test_notifications.py` | payload、簽章、重試、失敗上限（10） |
| `test_billing.py` | 無 Stripe 的降級、webhook 同步、冪等、簽章（12） |
| `test_end_to_end.py` | 真實 HTTP 伺服器 + 真實排程 + 真實 webhook 接收（3） |

`test_end_to_end.py` 不使用任何 mock：它啟動真的 HTTP 伺服器、跑真的排程 tick、
接收真的 webhook 並驗證簽章。

---

## 文件

| 文件 | 內容 |
|---|---|
| [`docs/OPPORTUNITY_ANALYSIS.md`](docs/OPPORTUNITY_ANALYSIS.md) | 7 個方向的評分模型與選型理由 |
| [`docs/UNIT_ECONOMICS.md`](docs/UNIT_ECONOMICS.md) | 成本、毛利、損益兩平、成長情境、風險 |
| [`docs/SETUP.md`](docs/SETUP.md) | **需要你提供的資訊**（Stripe、網域、SMTP） |
| [`docs/DEPLOY.md`](docs/DEPLOY.md) | 三種部署路徑、擴容、備份、上線檢查 |
| [`docs/API.md`](docs/API.md) | 完整 API 參考與 webhook 簽章驗證範例 |
| [`docs/SECURITY.md`](docs/SECURITY.md) | 威脅模型與**殘餘風險**(上線前必讀) |
| [`audit_1.md`](audit_1.md) / [`audit_2.md`](audit_2.md) | 兩輪敵意 production-readiness audit 與修正紀錄 |
| [`AUDIT_02_FAULT_INJECTION.md`](AUDIT_02_FAULT_INJECTION.md) | 故障注入、壓力與恢復能力稽核;**含實測負載範圍** |
| [`AUDIT_03_BUSINESS_VALIDATION.md`](AUDIT_03_BUSINESS_VALIDATION.md) | **商業可行性敵對驗收**:競品、定價、漏斗、pre-mortem、verdict |
| [`AUDIT_04_PIVOT_SELECTION.md`](AUDIT_04_PIVOT_SELECTION.md) | **垂直市場選擇**:14 個方向評分、敏感度分析、pre-mortem、最終推薦與 MVP 範圍 |

---

## 目前的狀態與限制

**已驗證可運作**：完整的檢查迴圈（對真實外部網站實測）、robots.txt 強制、
SSRF 防護、簽章 webhook 送達、方案配額強制、Stripe webhook 驅動的方案同步。

**尚未完成，且原因明確**：

| 項目 | 原因 |
|---|---|
| 真實刷卡驗證 | 需要你的 Stripe 帳戶。程式路徑已用假的 Stripe 事件完整測過 |
| Email 通知實測 | 需要 SMTP 帳號。未設定時以清楚錯誤失敗，不會當機 |
| 網頁版儀表板 | MVP 刻意 API-first。首頁 + `/docs` 已足以完成從註冊到建立監控的流程 |
| JavaScript 渲染頁面 | 目前只抓 HTML 原始碼。需要無頭瀏覽器，成本與維護負擔顯著提高 |
| Alembic 遷移 | 目前只有一版 schema，`create_all()` 已足夠。需要改欄位時再導入 |
| 分散式速率限制 | 記憶體內實作，單一 API 程序下正確。多副本時需換 Redis |

授權：MIT
