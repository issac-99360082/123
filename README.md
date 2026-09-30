# Watchpost

> **⚠️ 商業驗收結論:Pivot —— 在驗證需求之前請勿再開發功能。**
> 本專案**沒有不可替代優勢**,面對免費開源且功能更完整的
> [changedetection.io](https://github.com/dgtlmoon/changedetection.io)(33.5k stars,託管 $8.99/月)。
> 保守漏斗:每 9,895 名訪客才 1 位付費客戶。下方的定價**尚未經過任何市場驗證**。
> 完整歷程與 Pivot 方向見 [`HISTORY.md`](HISTORY.md)。

**網頁／API 變更監控。** 監看任何公開 URL 或 JSON API,只擷取你在意的片段,
真正變化時把**帶 HMAC 簽章的結構化 JSON diff** POST 到你的 webhook。

```
排程 ─▶ fetch ─▶ extract ─▶ diff ─▶ deliver ─▶ 你的 webhook
        │         │          │        └ HMAC 簽章 + 指數退避重試
        │         │          └ SHA-256 比對,只有真正變化才往下走
        │         └ CSS / regex / JSON path + 正規化 + 忽略規則
        └ robots.txt + SSRF 防護 + 每網域節流 + 總時限
```

## 啟動

```bash
./scripts/install.sh --dev      # venv + 相依 + .env + DB + 測試
uvicorn app.main:app --reload   # http://localhost:8000,文件在 /docs
./scripts/smoke_test.sh         # 端到端煙霧測試
```

Docker:`cp .env.example .env && docker compose up -d --build`(Postgres + API + 獨立 worker)

**不需要任何第三方帳號即可完整運行**;Stripe 只在要收款時才需要(見 [`docs/SETUP.md`](docs/SETUP.md))。

## 使用

```bash
KEY=$(curl -sS -X POST localhost:8000/v1/auth/signup -H 'Content-Type: application/json' \
  -d '{"email":"you@example.com","password":"correct-horse-42"}' | jq -r .key)

# 先預覽會比對到什麼,再建立監控
curl -sS -X POST localhost:8000/v1/preview -H "Authorization: Bearer $KEY" \
  -H 'Content-Type: application/json' \
  -d '{"url":"https://pypi.org/project/fastapi/","extract_mode":"css","extract_value":"h1"}'

curl -sS -X POST localhost:8000/v1/monitors -H "Authorization: Bearer $KEY" \
  -H 'Content-Type: application/json' \
  -d '{"name":"fastapi release","url":"https://pypi.org/project/fastapi/",
       "extract_mode":"css","extract_value":"h1","interval_seconds":3600,
       "notify_webhook_url":"https://your-app.example/hooks/watchpost"}'
```

## 結構

```
app/
  main.py  config.py  db.py  models.py  schemas.py      應用、設定、ORM、API 契約
  security.py  errors.py  deps.py  plans.py             雜湊/簽章、錯誤格式、認證與配額
  worker.py                                             獨立 worker 進入點
  core/  fetcher extractor differ engine scheduler       抓取→擷取→比對→排程
         notifier retention                              送達、保留期限
  api/   auth monitors billing health                    20 個端點
templates/  tests/(142)  scripts/  docs/
```

## 方案

| | Free | Pro | Business |
|---|---|---|---|
| 價格 | $0 | $19/月 | $79/月 |
| 監控數 | 3 | 50 | 300 |
| 最短間隔 | 60 分 | 5 分 | 1 分 |
| 歷史保留 | 7 天 | 90 天 | 365 天 |

自助訂閱,Stripe Checkout 收款、Customer Portal 取消,全程無人工介入。
**Stripe 是唯一真實來源**:只有通過簽章驗證的 webhook 才會升級方案。

## 安全

SSRF 防護(逐跳重導向驗證 + DNS 後檢查)、API key 僅存 SHA-256 雜湊、
bcrypt 密碼(先 SHA-256 避免 72 bytes 截斷)、webhook HMAC 簽章、
Stripe 簽章驗證 + 事件去重與時序防護、兩層登入鎖定、速率限制、
ReDoS 防護、回應大小與總時限上限、安全標頭、容器非 root、
production 未換 `SECRET_KEY` 拒絕啟動。殘餘風險見 [`docs/SECURITY.md`](docs/SECURITY.md)。

**對目標站台**:預設遵守 `robots.txt`(對真實站台驗證過)、明確 User-Agent、
每網域節流、連續失敗自動暫停、只抓公開內容、通知只送使用者自己設定的目的地。

## 測試

```bash
python -m pytest -q     # 142 passed
```

`test_security_regressions.py` 與 `test_fault_injection.py` 釘住每一個**實際被利用成功**
的漏洞;`test_end_to_end.py` 不使用 mock,啟動真實 HTTP 伺服器、真實排程、
真實 webhook 接收並驗證簽章。

## 文件

| | |
|---|---|
| [`HISTORY.md`](HISTORY.md) | 六輪選題、稽核、商業驗收與 Pivot 決策 |
| [`docs/SETUP.md`](docs/SETUP.md) | **需要你提供的資訊**(Stripe、網域、SMTP) |
| [`docs/DEPLOY.md`](docs/DEPLOY.md) | 部署、容量公式、擴容、備份、上線檢查 |
| [`docs/API.md`](docs/API.md) | API 參考與 webhook 簽章驗證範例 |
| [`docs/SECURITY.md`](docs/SECURITY.md) | 威脅模型與殘餘風險(**上線前必讀**) |

## 已知限制

無 JS 渲染(需無頭瀏覽器)、無代理池、無網頁儀表板(API-first)、
無 Alembic(僅一版 schema)、速率限制存於單一程序記憶體(多副本需 Redis)、
單一 worker 為單點(認領邏輯已支援多 worker,需 Postgres)。

授權:MIT
