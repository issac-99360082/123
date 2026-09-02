# SETUP — 需要你提供的資訊

專案在**沒有任何第三方帳號**的情況下就能完整執行：可以註冊、建立監控、
抓取頁面、偵測變更、送出簽章 webhook。以下清單是**只有你能提供**的部分，
按「阻擋什麼」排序。

圖例：🔴 收款必需　🟡 選用增強　🟢 已有安全預設

---

## 🔴 1. Stripe（沒有它就無法收款）

**阻擋**：`/v1/billing/checkout`、`/v1/billing/portal`、`/v1/billing/webhook`
（未設定時回傳 `503 service_unavailable`，其餘功能不受影響，所有帳號留在 Free 方案）

### 步驟

1. 註冊 <https://dashboard.stripe.com> 並完成帳戶啟用（需要銀行帳戶與身分驗證）。
2. **Developers → API keys** → 複製 Secret key。
   ```
   STRIPE_SECRET_KEY=sk_live_...        # 測試時用 sk_test_...
   STRIPE_PUBLISHABLE_KEY=pk_live_...
   ```
3. **Products** → 建立兩個「Recurring / monthly」價格，複製其 Price ID：

   | 產品 | 價格 | 環境變數 |
   |---|---|---|
   | Watchpost Pro | $19 / 月 | `STRIPE_PRICE_PRO=price_...` |
   | Watchpost Business | $79 / 月 | `STRIPE_PRICE_BUSINESS=price_...` |

   > 價格必須與 `app/plans.py` 中的 `price_usd_month` 一致，否則網站顯示的
   > 金額會與實際收取的金額不符。

4. **Developers → Webhooks → Add endpoint**
   - URL：`https://你的網域/v1/billing/webhook`
   - 事件：
     - `checkout.session.completed`
     - `customer.subscription.created`
     - `customer.subscription.updated`
     - `customer.subscription.deleted`
     - `invoice.payment_failed`
   - 複製 Signing secret：
     ```
     STRIPE_WEBHOOK_SECRET=whsec_...
     ```

5. **Customer portal**：Settings → Billing → Customer portal → 啟用，
   讓客戶自助取消與更換方案（這是「高度自動化」的關鍵，避免人工客服）。

### 本機測試 webhook

```bash
stripe login
stripe listen --forward-to localhost:8000/v1/billing/webhook
# 把印出的 whsec_... 填進 .env，然後：
stripe trigger checkout.session.completed
```

---

## 🔴 2. 網域與 TLS

**阻擋**：Stripe webhook 需要公開 HTTPS URL；瀏覽器與客戶也需要可信網址。

```
PUBLIC_BASE_URL=https://watchpost.你的網域
```

TLS 由 Caddy 自動處理（見 `docs/DEPLOY.md`），無需手動申請憑證。

---

## 🔴 3. 伺服器

最低規格：**2 vCPU / 2 GB RAM / 20 GB 磁碟**，約 $6–$12/月
（Hetzner、DigitalOcean、Vultr 皆可）。

---

## 🟡 4. SMTP（只影響 email 通知）

**阻擋**：僅 email 通知渠道。**webhook 通知不受影響**，這是主要渠道。

未設定時，email 類型的 delivery 會以清楚的錯誤訊息失敗
（`SMTP is not configured`），不會導致當機。

```
SMTP_HOST=smtp.postmarkapp.com
SMTP_PORT=587
SMTP_USERNAME=...
SMTP_PASSWORD=...
NOTIFICATION_FROM_EMAIL=alerts@你的網域
```

任何 SMTP 供應商皆可（Postmark、SES、Resend、Mailgun）。
建議設定 SPF 與 DKIM，否則通知信會進垃圾郵件匣。

---

## 🟢 5. SECRET_KEY（已自動處理）

`scripts/install.sh` 會自動產生。若手動部署：

```bash
openssl rand -hex 32
```

> 應用程式在 `ENVIRONMENT=production` 且 `SECRET_KEY` 仍為預設值時**拒絕啟動**。
> 這是刻意的：預設值外洩等同於沒有金鑰。

---

## 🟢 6. User-Agent 聯絡資訊（建議修改）

```
USER_AGENT=WatchpostBot/0.1 (+https://你的網域/bot; monitoring on behalf of subscribers)
```

指向一個說明「這個 bot 是什麼、如何聯絡、如何要求排除」的頁面。
這是被監控站台管理員最常查詢的資訊，也是降低被封鎖機率最有效的做法。

---

## 檢查清單

```
[ ] Stripe 帳戶已啟用（可收款狀態）
[ ] STRIPE_SECRET_KEY 已填入
[ ] STRIPE_WEBHOOK_SECRET 已填入
[ ] STRIPE_PRICE_PRO / STRIPE_PRICE_BUSINESS 已填入
[ ] Stripe Customer portal 已啟用
[ ] 網域已指向伺服器，HTTPS 正常
[ ] PUBLIC_BASE_URL 已設為正式網址
[ ] SECRET_KEY 已改為隨機值
[ ] ENVIRONMENT=production
[ ] ALLOW_PRIVATE_NETWORK_TARGETS=false（務必確認）
[ ] USER_AGENT 已改為你的聯絡資訊
[ ] （選用）SMTP 已設定
[ ] curl https://你的網域/readyz 回報 billing: configured
```

## 驗證收款流程是否真的可用

```bash
# 1. 註冊
KEY=$(curl -sS -X POST https://你的網域/v1/auth/signup \
  -H 'Content-Type: application/json' \
  -d '{"email":"test@你的網域","password":"correct-horse-42"}' \
  | python3 -c "import json,sys;print(json.load(sys.stdin)['key'])")

# 2. 取得 checkout 連結
curl -sS -X POST "https://你的網域/v1/billing/checkout?plan=pro" \
  -H "Authorization: Bearer $KEY"

# 3. 用 Stripe 測試卡 4242 4242 4242 4242 完成付款

# 4. 確認方案已升級（應顯示 "plan": "pro"）
curl -sS https://你的網域/v1/billing/subscription -H "Authorization: Bearer $KEY"
```

第 4 步顯示 `"plan": "pro"` 即代表**端到端收款鏈路已打通**。
