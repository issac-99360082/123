# API 參考

互動式文件:`/docs`　OpenAPI:`/openapi.json`

## 認證

除 `/healthz`、`/readyz`、`/v1/billing/plans` 與 webhook 外皆需 API key:
`Authorization: Bearer wp_live_xxx`(或 `X-API-Key`)。
key 僅在建立時回傳一次,資料庫只存 SHA-256 雜湊。

## 錯誤格式

```json
{ "error": { "type": "quota_exceeded", "message": "...", "detail": {} } }
```

| `type` | HTTP | 意義 |
|---|---|---|
| `bad_request` | 400 | 請求本身不合理 |
| `unauthorized` / `forbidden` / `not_found` | 401/403/404 | — |
| `quota_exceeded` | 402 | 超出方案限制 → 升級 |
| `validation_failed` | 422 | 欄位驗證失敗,`detail` 標出欄位 |
| `rate_limited` | 429 | `detail.retry_after_seconds` |
| `service_unavailable` | 503 | 相依服務未設定或不可用(含 DB 中斷,帶 `Retry-After`) |
| `internal_error` | 500 | 未預期錯誤,`message` 含追查用 reference |

## 端點

| 方法 | 路徑 | 說明 |
|---|---|---|
| GET | `/healthz` `/readyz` | 存活 / 就緒探針 |
| GET | `/metrics` | 營運計數 + 佇列年齡(**需認證**) |
| POST | `/v1/auth/signup` `/login` | 建立帳號 / 換一把新 key |
| GET | `/v1/auth/me` | 帳號資訊 |
| GET POST | `/v1/auth/keys` | 列出 / 建立 API key |
| DELETE | `/v1/auth/keys/{id}` | 撤銷 |
| GET | `/v1/auth/me/export` | 匯出本帳號所有資料 |
| DELETE | `/v1/auth/me?confirm=<email>` | **永久刪除**帳號與所有資料 |
| POST GET | `/v1/monitors` | 建立(回傳一次性 `webhook_secret`)/ 列出 |
| GET PATCH DELETE | `/v1/monitors/{id}` | 取得 / 更新 / 刪除 |
| POST | `/v1/monitors/{id}/run` | 立即執行一次檢查 |
| GET | `/v1/monitors/{id}/checks` `/changes` | 檢查歷史 / 變更事件 |
| POST | `/v1/preview` | 試跑擷取,不建立監控 |
| GET | `/v1/usage` | 本月用量與方案限制 |
| GET | `/v1/billing/plans` | 公開價目 |
| POST | `/v1/billing/checkout?plan=pro` `/portal` | Stripe 連結 |
| GET | `/v1/billing/subscription` | 訂閱狀態 |
| POST | `/v1/billing/webhook` | Stripe 接收端(簽章驗證) |

## 擷取模式

| `extract_mode` | `extract_value` | 說明 |
|---|---|---|
| `text` | — | 整頁 HTML 去標籤後的可見文字 |
| `raw` | — | 未處理的回應內容 |
| `css` | CSS 選擇器 | 只取符合節點的文字,如 `.price` |
| `regex` | 正規表達式 | 取所有符合項(有 capture group 則取該組) |
| `json` | 點號路徑 | 如 `data.items[0].status` |

擷取後一律正規化(Unicode NFC、統一換行、壓縮空白、去空行),
因此**純排版差異不會觸發通知**。`ignore_patterns` 可再清掉每次都變的內容:

```json
{ "ignore_patterns": ["\\d{4}-\\d{2}-\\d{2} \\d{2}:\\d{2}:\\d{2}", "csrf_token=\\w+"] }
```

無效或過慢的 pattern 會被略過而非讓整個檢查失敗(有 2 秒硬上限,防 ReDoS)。

## Webhook payload

```http
POST /your-endpoint
X-Watchpost-Event: monitor.changed
X-Watchpost-Delivery: evt_142
X-Watchpost-Timestamp: 1767225600
X-Watchpost-Signature: t=1767225600,v1=5f3a...
```

```json
{
  "event": "monitor.changed",
  "event_id": "evt_142",
  "created_at": "2026-09-02T11:04:33+00:00",
  "monitor": { "id": 7, "name": "Competitor pricing", "url": "https://example.com/pricing",
               "extract_mode": "css", "extract_value": ".price", "interval_seconds": 3600 },
  "change": {
    "previous_hash": "9f2c...", "current_hash": "41ab...", "similarity": 0.94,
    "lines_added": 0, "lines_removed": 0, "lines_modified": 1,
    "added": [], "removed": [], "modified": [{ "before": "$19.00", "after": "$17.50" }],
    "truncated": false, "is_first_observation": false,
    "unified_diff": "--- previous\n+++ current\n@@ -1 +1 @@\n-$19.00\n+$17.50"
  }
}
```

### 驗證簽章(務必做,否則任何人都能偽造變更事件)

```python
import hashlib, hmac, time

def verify(secret: str, signature_header: str, raw_body: bytes, tolerance: int = 300) -> bool:
    parts = dict(p.split("=", 1) for p in signature_header.split(","))
    timestamp, received = parts["t"], parts["v1"]
    if abs(time.time() - int(timestamp)) > tolerance:      # 拒絕重放
        return False
    expected = hmac.new(secret.encode(), timestamp.encode() + b"." + raw_body,
                        hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, received)
```

```javascript
const crypto = require("crypto");
function verify(secret, signatureHeader, rawBody, tolerance = 300) {
  const parts = Object.fromEntries(signatureHeader.split(",").map((p) => p.split("=", 2)));
  if (Math.abs(Date.now() / 1000 - Number(parts.t)) > tolerance) return false;
  const expected = crypto.createHmac("sha256", secret)
    .update(parts.t + "." + rawBody).digest("hex");
  if (!parts.v1 || parts.v1.length !== expected.length) return false;   // timingSafeEqual 會拋出
  return crypto.timingSafeEqual(Buffer.from(expected), Buffer.from(parts.v1));
}
```

> 必須用**原始 request body 位元組**計算。先 parse 再重新序列化會改變位元組,簽章就不符。

### 送達與重試

2xx 視為成功。失敗重試間隔 1 分 → 5 分 → 30 分 → 2 小時 → 6 小時(共 5 次)後放棄。
逾時 10 秒,請先回 200 再做耗時處理。

**語意是 at-least-once**:程序在送出後、commit 前被硬性 kill 時會重送。
**接收端必須以 `event_id` 去重**(同一事件的重試 id 相同)。

## 速率限制

預設 120 req/min、突發 40,以 API key 計(未帶 key 則以來源位址計)。
超過回 429 並附 `detail.retry_after_seconds`。

## 方案限制

| | Free | Pro | Business |
|---|---|---|---|
| 價格 | $0 | $19/月 | $79/月 |
| 監控數 | 3 | 50 | 300 |
| 最短間隔 | 60 分 | 5 分 | 1 分 |
| 每月檢查數 | 2,200 | 450,000 | 5,000,000 |
| 歷史保留 | 7 天 | 90 天 | 365 天 |

超過時回 `402 quota_exceeded`,`detail` 指出是哪一項。
