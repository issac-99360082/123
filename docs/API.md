# API 參考

互動式文件（可直接送出請求）：`https://你的網域/docs`
OpenAPI schema：`https://你的網域/openapi.json`

## 認證

除了 `/healthz`、`/readyz`、`/metrics`、`/v1/billing/plans` 與 webhook 之外，
所有端點都需要 API key：

```
Authorization: Bearer wp_live_xxxxx
```

也支援 `X-API-Key: wp_live_xxxxx`。

API key 僅在建立時回傳一次；資料庫只保存 SHA-256 雜湊，遺失只能重新建立。

## 錯誤格式

所有錯誤共用同一個外層結構：

```json
{ "error": { "type": "quota_exceeded", "message": "...", "detail": { } } }
```

| `type` | HTTP | 意義 |
|---|---|---|
| `bad_request` | 400 | 請求本身不合理 |
| `unauthorized` | 401 | API key 缺少、無效或已撤銷 |
| `forbidden` | 403 | 已認證但無權限 |
| `not_found` | 404 | 資源不存在，或不屬於你 |
| `quota_exceeded` | 402 | 超出方案限制 → 升級 |
| `validation_failed` | 422 | 欄位驗證失敗，`detail` 標出欄位 |
| `rate_limited` | 429 | 超過速率限制，`detail.retry_after_seconds` |
| `service_unavailable` | 503 | 相依服務未設定或不可用 |
| `internal_error` | 500 | 未預期錯誤，`message` 含追查用的 reference |

---

## 端點

### 系統

| 方法 | 路徑 | 說明 |
|---|---|---|
| GET | `/healthz` | 存活檢查，不碰資料庫 |
| GET | `/readyz` | 就緒檢查（資料庫、billing、email） |
| GET | `/metrics` | 營運計數 |

### 認證

| 方法 | 路徑 | 說明 |
|---|---|---|
| POST | `/v1/auth/signup` | 建立帳號，回傳第一把 API key |
| POST | `/v1/auth/login` | 以帳密換一把新的 API key |
| GET | `/v1/auth/me` | 目前帳號資訊 |
| GET | `/v1/auth/keys` | 列出 API keys（不含明文） |
| POST | `/v1/auth/keys` | 建立新的 API key |
| DELETE | `/v1/auth/keys/{id}` | 撤銷 API key |

### 監控

| 方法 | 路徑 | 說明 |
|---|---|---|
| POST | `/v1/monitors` | 建立監控（回傳一次性的 `webhook_secret`） |
| GET | `/v1/monitors` | 列出監控（`?active=`、`?limit=`、`?offset=`） |
| GET | `/v1/monitors/{id}` | 取得單一監控 |
| PATCH | `/v1/monitors/{id}` | 更新監控 |
| DELETE | `/v1/monitors/{id}` | 刪除監控 |
| POST | `/v1/monitors/{id}/run` | 立即執行一次檢查 |
| GET | `/v1/monitors/{id}/checks` | 檢查歷史 |
| GET | `/v1/monitors/{id}/changes` | 變更事件（含 diff） |
| POST | `/v1/preview` | 試跑擷取，不建立監控 |
| GET | `/v1/usage` | 本月用量與方案限制 |

### 帳務

| 方法 | 路徑 | 說明 |
|---|---|---|
| GET | `/v1/billing/plans` | 公開價目（無需認證） |
| POST | `/v1/billing/checkout?plan=pro` | 取得 Stripe Checkout 連結 |
| POST | `/v1/billing/portal` | 取得 Stripe 客戶自助入口連結 |
| GET | `/v1/billing/subscription` | 目前訂閱狀態與限制 |
| POST | `/v1/billing/webhook` | Stripe webhook 接收端（簽章驗證） |

---

## 擷取模式（`extract_mode`）

決定「拿頁面的哪一部分來比對」。選對模式是避免雜訊通知的關鍵。

| 模式 | `extract_value` | 說明 |
|---|---|---|
| `text` | 不需要 | 整頁 HTML 去掉標籤後的可見文字 |
| `raw` | 不需要 | 未經處理的回應內容（適合純文字／JSON API） |
| `css` | CSS 選擇器 | 只取符合節點的文字，例如 `.price` |
| `regex` | 正規表達式 | 取所有符合項（有 capture group 則取該組） |
| `json` | 點號路徑 | JSON 回應的路徑，例如 `data.items[0].status` |

擷取之後一律經過正規化：Unicode NFC、統一換行、壓縮空白、去除空行。
因此**純排版差異不會觸發通知**。

### `ignore_patterns`

擷取後再套用的正規表達式清單，用來清掉每次都會變的內容：

```json
{ "ignore_patterns": ["\\d{4}-\\d{2}-\\d{2} \\d{2}:\\d{2}:\\d{2}", "csrf_token=\\w+"] }
```

無效的 pattern 會被略過而不是讓整個檢查失敗。

---

## Webhook payload

```http
POST /your-endpoint HTTP/1.1
Content-Type: application/json
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
  "monitor": {
    "id": 7,
    "name": "Competitor pricing",
    "url": "https://example.com/pricing",
    "extract_mode": "css",
    "extract_value": ".price",
    "interval_seconds": 3600
  },
  "change": {
    "previous_hash": "9f2c...",
    "current_hash": "41ab...",
    "similarity": 0.94,
    "lines_added": 0,
    "lines_removed": 0,
    "lines_modified": 1,
    "added": [],
    "removed": [],
    "modified": [{ "before": "$19.00", "after": "$17.50" }],
    "truncated": false,
    "unified_diff": "--- previous\n+++ current\n@@ -1 +1 @@\n-$19.00\n+$17.50",
    "is_first_observation": false
  }
}
```

### 驗證簽章

**務必驗證**，否則任何人都能偽造變更事件打進你的系統。

```python
import hashlib, hmac, time

def verify(secret: str, signature_header: str, raw_body: bytes, tolerance: int = 300) -> bool:
    parts = dict(p.split("=", 1) for p in signature_header.split(","))
    timestamp, received = parts["t"], parts["v1"]

    # 拒絕過舊的請求，避免重放攻擊
    if abs(time.time() - int(timestamp)) > tolerance:
        return False

    expected = hmac.new(
        secret.encode(), timestamp.encode() + b"." + raw_body, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, received)
```

```javascript
const crypto = require("crypto");

function verify(secret, signatureHeader, rawBody, tolerance = 300) {
  const parts = Object.fromEntries(
    signatureHeader.split(",").map((p) => p.split("=", 2))
  );
  if (Math.abs(Date.now() / 1000 - Number(parts.t)) > tolerance) return false;

  const expected = crypto
    .createHmac("sha256", secret)
    .update(parts.t + "." + rawBody)
    .digest("hex");
  // timingSafeEqual throws on length mismatch, so check length first.
  if (!parts.v1 || parts.v1.length !== expected.length) return false;
  return crypto.timingSafeEqual(Buffer.from(expected), Buffer.from(parts.v1));
}
```

> 必須使用**原始 request body 位元組**計算，不能先 parse 再重新序列化 —
> 重新序列化會改變位元組內容，導致簽章不符。

### 送達與重試

- 接收端回傳 2xx 視為成功。
- 失敗會重試，間隔為 1 分 → 5 分 → 30 分 → 2 小時 → 6 小時（共 5 次）。
- 端點應**冪等**：以 `event_id` 去重，重試可能造成重複送達。
- 逾時為 10 秒；請先回 200 再做耗時處理。

---

## 速率限制

預設每分鐘 120 次請求，突發上限 40，以 API key 計算（未帶 key 則以 IP 計算）。
超過時回傳 429，`detail.retry_after_seconds` 指出建議的等待秒數。

## 方案限制

| | Free | Pro | Business |
|---|---|---|---|
| 價格 | $0 | $19/月 | $79/月 |
| 監控數 | 3 | 50 | 300 |
| 最短間隔 | 60 分 | 5 分 | 1 分 |
| 每月檢查數 | 2,200 | 450,000 | 5,000,000 |
| 歷史保留 | 7 天 | 90 天 | 365 天 |

超過限制時回傳 `402 quota_exceeded`，`detail` 會指出是哪一項限制。
