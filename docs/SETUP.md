# SETUP — 需要你提供的資訊

專案在**沒有任何第三方帳號**的情況下就能完整執行:註冊、建立監控、抓取、
偵測變更、送出簽章 webhook。以下是只有你能提供的部分。

🔴 收款必需　🟡 選用　🟢 已有安全預設

## 🔴 Stripe

**阻擋**:`/v1/billing/checkout`、`/portal`、`/webhook`(未設定時回 503,其餘功能不受影響,
所有帳號留在 Free)。

1. 註冊 <https://dashboard.stripe.com> 並完成帳戶啟用。
2. **Developers → API keys** → `STRIPE_SECRET_KEY=sk_live_...`、`STRIPE_PUBLISHABLE_KEY=pk_live_...`
3. **Products** → 建立兩個 monthly recurring 價格,複製 Price ID:

   | 產品 | 價格 | 環境變數 |
   |---|---|---|
   | Pro | $19/月 | `STRIPE_PRICE_PRO=price_...` |
   | Business | $79/月 | `STRIPE_PRICE_BUSINESS=price_...` |

   > 必須與 `app/plans.py` 的 `price_usd_month` 一致,否則顯示金額與實收金額不符。

4. **Developers → Webhooks → Add endpoint**
   - URL:`https://你的網域/v1/billing/webhook`
   - 事件:`checkout.session.completed`、`customer.subscription.created`
     / `.updated` / `.deleted`、`invoice.payment_failed`
   - 複製 `STRIPE_WEBHOOK_SECRET=whsec_...`
5. **Settings → Billing → Customer portal** → 啟用(讓客戶自助取消,這是自動化的關鍵)。

本機測試:
```bash
stripe listen --forward-to localhost:8000/v1/billing/webhook
stripe trigger checkout.session.completed
```

## 🔴 網域與 TLS

`PUBLIC_BASE_URL=https://watchpost.你的網域`。Stripe webhook 需要公開 HTTPS;
TLS 由 Caddy 自動處理(見 `DEPLOY.md`)。

## 🔴 伺服器

最低 2 vCPU / 2 GB / 20 GB,約 $6–12/月(Hetzner、DigitalOcean、Vultr)。

## 🟡 SMTP

**只影響 email 通知,webhook 主渠道不受影響。** 未設定時 email delivery 以清楚錯誤失敗,
不會當機。任何供應商皆可(Postmark、SES、Resend、Mailgun),建議設 SPF 與 DKIM。

```
SMTP_HOST=  SMTP_PORT=587  SMTP_USERNAME=  SMTP_PASSWORD=
NOTIFICATION_FROM_EMAIL=alerts@你的網域
```

## 🟢 SECRET_KEY

`scripts/install.sh` 會自動產生;手動則 `openssl rand -hex 32`。
**`ENVIRONMENT=production` 且仍為預設值時應用程式拒絕啟動。**

## 🟢 User-Agent

`USER_AGENT=WatchpostBot/0.1 (+https://你的網域/bot; ...)` ——
指向一個說明「這 bot 是什麼、如何聯絡、如何要求排除」的頁面。
這是站台管理員最常查的資訊,也是降低被封鎖機率最有效的做法。

## 檢查清單

```
[ ] Stripe 帳戶已啟用,4 個金鑰已填入,Customer portal 已啟用
[ ] 網域已指向伺服器,HTTPS 正常,PUBLIC_BASE_URL 為正式網址
[ ] SECRET_KEY 已改為隨機值,ENVIRONMENT=production
[ ] ALLOW_PRIVATE_NETWORK_TARGETS=false(務必確認)
[ ] USER_AGENT 已改為你的聯絡資訊
[ ] curl https://你的網域/readyz 回報 billing: configured
```

## 驗證收款鏈路

```bash
KEY=$(curl -sS -X POST https://你的網域/v1/auth/signup -H 'Content-Type: application/json' \
  -d '{"email":"test@你的網域","password":"correct-horse-42"}' | jq -r .key)
curl -sS -X POST "https://你的網域/v1/billing/checkout?plan=pro" -H "Authorization: Bearer $KEY"
# 用測試卡 4242 4242 4242 4242 完成付款,然後:
curl -sS https://你的網域/v1/billing/subscription -H "Authorization: Bearer $KEY"
```

顯示 `"plan": "pro"` 即代表**端到端收款鏈路已打通**。
