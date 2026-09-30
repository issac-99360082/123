# 部署

## 1. 本機

```bash
./scripts/install.sh --dev      # venv、相依、.env(含產生的 SECRET_KEY)、schema、測試
uvicorn app.main:app --reload
```

排程器預設在 API 程序內執行,單機不需額外程序。

## 2. Docker Compose(建議)

Postgres + API + **獨立 worker**:

```bash
cp .env.example .env    # 編輯 SECRET_KEY、PUBLIC_BASE_URL、Stripe(見 SETUP.md)
docker compose up -d --build
```

已處理的關鍵細節:API 容器 `SCHEDULER_ENABLED=false`、worker 容器 `true`
(**兩者都開會導致每個監控被檢查兩次**);Postgres healthcheck;容器以 uid 10001 執行。

HTTPS 用 Caddy(自動申請並續期憑證):

```
watchpost.你的網域 { reverse_proxy localhost:8000 }
```

uvicorn 已帶 `--proxy-headers`,`X-Forwarded-*` 會被正確解析。

> 若設 `TRUST_PROXY_HEADERS=true`,**代理必須覆寫 `X-Forwarded-For`**。
> Caddy 與 nginx 的預設是**附加**而非覆寫;設錯會讓所有 per-IP 防護失效
> (稽核實測:標頭被信任時,單一攻擊者在限制為 5 的情況下建立 20/20 個帳號)。

## 3. systemd

兩個 unit,差別只在 `Environment=SCHEDULER_ENABLED`(API `false`,worker `true`)
與 `ExecStart`:

```ini
[Service]
Type=simple
User=watchpost
WorkingDirectory=/opt/watchpost
EnvironmentFile=/opt/watchpost/.env
Environment=SCHEDULER_ENABLED=false
ExecStart=/opt/watchpost/.venv/bin/uvicorn app.main:app \
          --host 127.0.0.1 --port 8000 --workers 2 --proxy-headers
# worker: Environment=SCHEDULER_ENABLED=true
# worker: ExecStart=/opt/watchpost/.venv/bin/python -m app.worker
Restart=always
RestartSec=5
NoNewPrivileges=true
PrivateTmp=true
```

## 容量公式

```
持續容量 = CHECK_BATCH_SIZE / SCHEDULER_TICK_SECONDS   (checks/秒)
```

這是**唯一**決定持續吞吐量的因素,與 CPU 無關,且必須大於最大方案所需:

| 方案 | 滿載所需 | 單機可容納 |
|---|---|---|
| Free(3 監控/60 分) | 0.0008 checks/s | 約 24,000 位 |
| Pro(50 監控/5 分) | 0.17 checks/s | 約 120 位 |
| Business(300 監控/1 分) | 5.0 checks/s | 約 4 位 |

預設 `200 / 10s = 20 checks/s`,實測突發 16.1 checks/s。
**啟動時會記錄容量,低於最大方案所需時發出 `capacity_below_advertised_plan` 警告。**

> 稽核發現原預設(50/20s = 2.5 checks/s)**低於 Business 單一客戶所需的 5** —— 賣得出去但交付不了。

## 擴容

| 階段 | 症狀 | 動作 |
|---|---|---|
| 1 | 起步 | 單機、SQLite、排程器在 API 內 |
| 2 | 監控數 > 幾百 | 換 Postgres,worker 拆成獨立程序 |
| 3 | API 延遲上升 | 加 `--workers` 或多台 API 副本 |
| 4 | 檢查延遲 | 提高 `CHECK_BATCH_SIZE` / 降低 `SCHEDULER_TICK_SECONDS` |
| 5 | 單 worker 不足 | 多 worker + Postgres |

`claim_due_monitors()` 以**條件式原子更新**認領,只有回報「剛好更新 1 列」才領走,
因此多 worker 同時執行不會重複認領(實測:修正前 4 worker 各領走全部 40 個 = 160 次;
修正後 40 次、零重複)。**SQLite 仍不建議多 worker**(寫入鎖競爭)。

**已知限制**:速率限制存於單一程序記憶體,N 個 API 副本時實際限制變成 N 倍;
到那個階段須換 Redis 後端。

## 資料庫遷移

目前用 `Base.metadata.create_all()`,對「只增加資料表」安全且冪等。
**需要修改既有欄位時**才導入 Alembic(`alembic init` → `target_metadata = app.db.Base.metadata`)。
在那之前刻意不引入,避免只有一版 schema 時增加操作負擔。

## 備份

```bash
docker compose exec -T db pg_dump -U watchpost watchpost | gzip > backup-$(date +%F).sql.gz
sqlite3 watchpost.db ".backup 'backup-$(date +%F).db'"
```

建議每日、保留 7–30 天。真正不可重建的只有 `users`、`api_keys`、`monitors`;
`checks` 與 `change_events` 是歷史,可接受部分遺失。

## 監控自己的服務

| 端點 | 用途 |
|---|---|
| `GET /healthz` | 存活探針(不碰 DB),供 load balancer |
| `GET /readyz` | 就緒探針(DB、billing、email 設定) |
| `GET /metrics` | 營運計數 + **佇列年齡**(需認證) |

需要注意的訊號:

- **`oldest_due_monitor_age_seconds` 持續上升 → 最重要的一條。**
  worker 停止工作或被慢速目標拖住時,`/healthz` 仍會回 200,只有這個數字會說實話
- `oldest_pending_delivery_age_seconds` 上升 → 送信佇列卡住(深度看不出,年齡可以)
- `deliveries_failed` 上升 → 客戶的 webhook 端點故障
- 日誌 `monitor_auto_paused` / `capacity_below_advertised_plan`

> 故障注入稽核的結論:**最危險的三種失效情境全部是靜默的** ——
> 服務看起來健康,檢查卻已停止。上面前兩條是唯一能揭露它們的指標。

## 上線前檢查

```
[ ] ENVIRONMENT=production,SECRET_KEY 已改(未改則拒絕啟動)
[ ] ALLOW_PRIVATE_NETWORK_TARGETS=false,RESPECT_ROBOTS_TXT=true
[ ] DATABASE_URL 指向 Postgres
[ ] 只有一個程序 SCHEDULER_ENABLED=true
[ ] TRUST_PROXY_HEADERS 與代理設定一致(見上方警告)
[ ] Stripe webhook 已指向 /v1/billing/webhook 並驗證成功
[ ] 防火牆封鎖 worker 對 RFC1918 與 169.254.0.0/16 的出站(見 SECURITY.md R1)
[ ] 備份已排程;/readyz 回 200;./scripts/smoke_test.sh 通過
[ ] 告警已接上 oldest_due_monitor_age_seconds
```
