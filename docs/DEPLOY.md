# 部署說明（Deployment）

三種路徑，依複雜度排序。第 2 種是建議的正式做法。

---

## 1. 本機／單機（最快）

```bash
git clone <repo> && cd watchpost
./scripts/install.sh --dev          # 建立 venv、安裝相依、產生 .env、跑測試
source .venv/bin/activate
uvicorn app.main:app --reload
```

開啟 <http://localhost:8000>，API 文件在 `/docs`。

排程器預設在 API 程序內執行（`SCHEDULER_ENABLED=true`），單機下不需要額外程序。

---

## 2. Docker Compose（建議的正式部署）

架構：Postgres + API 程序 + **獨立的 worker 程序**。

```bash
cp .env.example .env
# 編輯 .env：SECRET_KEY、PUBLIC_BASE_URL、Stripe 金鑰（見 docs/SETUP.md）
docker compose up -d --build
docker compose logs -f
```

`docker-compose.yml` 已處理關鍵細節：

- API 容器設 `SCHEDULER_ENABLED=false`，worker 容器設 `true`。
  **兩者都開啟會導致每個監控被檢查兩次**。
- Postgres 有 healthcheck，API 與 worker 等待資料庫就緒才啟動。
- 容器以非 root 使用者（uid 10001）執行。

### 加上 HTTPS（Caddy）

`Caddyfile`：

```
watchpost.你的網域 {
    reverse_proxy localhost:8000
}
```

```bash
sudo apt install -y caddy
sudo systemctl reload caddy
```

Caddy 會自動申請並續期 Let's Encrypt 憑證。

> 在反向代理後方，uvicorn 已帶 `--proxy-headers`，因此
> `X-Forwarded-Proto` / `X-Forwarded-For` 會被正確解析。

---

## 3. systemd（不使用 Docker）

`/etc/systemd/system/watchpost-api.service`：

```ini
[Unit]
Description=Watchpost API
After=network.target

[Service]
Type=simple
User=watchpost
WorkingDirectory=/opt/watchpost
EnvironmentFile=/opt/watchpost/.env
Environment=SCHEDULER_ENABLED=false
ExecStart=/opt/watchpost/.venv/bin/uvicorn app.main:app \
          --host 127.0.0.1 --port 8000 --workers 2 --proxy-headers
Restart=always
RestartSec=5
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ReadWritePaths=/opt/watchpost

[Install]
WantedBy=multi-user.target
```

`/etc/systemd/system/watchpost-worker.service`：

```ini
[Unit]
Description=Watchpost check worker
After=network.target

[Service]
Type=simple
User=watchpost
WorkingDirectory=/opt/watchpost
EnvironmentFile=/opt/watchpost/.env
Environment=SCHEDULER_ENABLED=true
ExecStart=/opt/watchpost/.venv/bin/python -m app.worker
Restart=always
RestartSec=5
NoNewPrivileges=true

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now watchpost-api watchpost-worker
sudo systemctl status watchpost-api watchpost-worker
```

---

## 擴容路徑

依序處理，不要提前優化：

| 階段 | 症狀 | 動作 |
|---|---|---|
| 1 | 起步 | 單機、SQLite、排程器在 API 內 |
| 2 | 監控數 > 幾百 | 換 Postgres，worker 拆成獨立程序 |
| 3 | API 延遲上升 | API 加 `--workers`，或多台 API 副本 |
| 4 | 檢查開始延遲 | 提高 `WORKER_CONCURRENCY` 與 `CHECK_BATCH_SIZE`(見下方容量公式) |
| 5 | 單一 worker 不足 | 見下方「多 worker」 |

### 容量公式(故障注入輪新增)

```
持續容量 = CHECK_BATCH_SIZE / SCHEDULER_TICK_SECONDS   (checks/秒)
```

這是**唯一**決定持續吞吐量的因素,與 CPU 無關。它必須大於你最大方案所需:

| 方案 | 滿載所需 | 單機可容納 |
|---|---|---|
| Free(3 監控 / 60 分) | 0.0008 checks/s | 約 24,000 位 |
| Pro(50 監控 / 5 分) | 0.17 checks/s | 約 120 位 |
| Business(300 監控 / 1 分) | 5.0 checks/s | 約 4 位 |

預設值 `200 / 10s = 20 checks/s`,實測突發吞吐量 16.1 checks/s。
**應用程式啟動時會記錄容量,並在低於最大方案所需時發出
`capacity_below_advertised_plan` 警告。**

> 稽核中發現原本的預設值(50/20s = 2.5 checks/s)**低於 Business 方案
> 單一客戶所需的 5 checks/s** —— 賣得出去但交付不了。

### 多 worker

`claim_due_monitors()` 在挑選監控時**立即把 `next_check_at` 往後推並 commit**，
因此兩個 worker 不會領到同一個監控。這在 Postgres 上可安全水平擴充。

> **SQLite 不要跑多個 worker**：寫入鎖會造成競爭與 `database is locked`。

### 已知的擴容限制

**速率限制是每程序記憶體內的**（`app/deps.py` 的 `TokenBucket`）。
跑 N 個 API 副本時，實際允許的速率會變成 N 倍。
到達這個階段時，把 `TokenBucket` 換成 Redis 後端的共享計數器。
在單一 API 程序下（絕大多數情況）行為完全正確。

---

## 資料庫遷移

目前使用 `Base.metadata.create_all()`，在每次啟動時建立缺少的資料表。
這對「只增加資料表」的變更是安全且冪等的。

**當你需要修改既有欄位時**，導入 Alembic：

```bash
pip install alembic
alembic init migrations
# 編輯 migrations/env.py，令 target_metadata = app.db.Base.metadata
alembic revision --autogenerate -m "describe change"
alembic upgrade head
```

在那之前刻意不引入 Alembic，避免在只有一張 schema 版本時增加操作負擔。

---

## 備份

```bash
# Postgres（docker compose）
docker compose exec -T db pg_dump -U watchpost watchpost | gzip > backup-$(date +%F).sql.gz

# SQLite
sqlite3 watchpost.db ".backup 'backup-$(date +%F).db'"
```

建議每日備份並保留 7–30 天。真正不可重建的資料只有 `users`、`api_keys`、
`monitors`；`checks` 與 `change_events` 是歷史紀錄，可接受部分遺失。

---

## 監控自己的服務

| 端點 | 用途 |
|---|---|
| `GET /healthz` | 存活探針（不碰資料庫），供 load balancer 使用 |
| `GET /readyz` | 就緒探針（檢查資料庫、billing、email 設定） |
| `GET /metrics` | 營運計數：監控總數、逾期數、待送出與失敗的通知數 |

需要注意的訊號：

- `monitors_due` 持續上升 → worker 跟不上，提高並行度或加機器
- `deliveries_failed` 上升 → 客戶的 webhook 端點故障
- 日誌中的 `monitor_auto_paused` → 該監控連續失敗已被自動停用

日誌為 JSON（`LOG_JSON=true`），可直接送進任何日誌系統，
每筆都帶 `request_id` 以便串接單一請求的完整軌跡。

---

## 上線前檢查

```
[ ] ENVIRONMENT=production
[ ] SECRET_KEY 已改為隨機值（未改則應用程式會拒絕啟動）
[ ] ALLOW_PRIVATE_NETWORK_TARGETS=false
[ ] RESPECT_ROBOTS_TXT=true
[ ] DATABASE_URL 指向 Postgres
[ ] 只有一個程序設定 SCHEDULER_ENABLED=true
[ ] PUBLIC_BASE_URL 為 https:// 正式網址
[ ] Stripe webhook 已指向 /v1/billing/webhook 並驗證成功
[ ] 備份工作已排程
[ ] /readyz 回傳 200
[ ] ./scripts/smoke_test.sh https://你的網域 通過
```
