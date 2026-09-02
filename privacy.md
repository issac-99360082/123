# Privacy Policy

_Last updated: 2026-09-02_

> **This is a plain-language starting template, not legal advice.**
> If you have users in the EU/UK (GDPR), California (CCPA/CPRA), or similar
> jurisdictions, have a lawyer review it. Placeholders in `[brackets]` must be
> filled in.

Watchpost is operated by [YOUR LEGAL ENTITY], [JURISDICTION].
Data protection contact: [PRIVACY CONTACT EMAIL].

## What we collect

**Account data**
- Email address — to identify your account and send service notices.
- Password — stored only as a bcrypt hash. We never store or see the plaintext.
- API keys — stored only as a SHA-256 hash. The plaintext is shown once, at
  creation, and cannot be retrieved afterwards.

**Configuration you provide**
- The URLs you monitor, your extraction rules, and your notification
  destinations (webhook URL, email address).

**Operational data**
- The extracted content of each monitored URL and its change history. This is
  the core function of the Service: we must retain the previous observation to
  detect the next change.
- Check results: timestamps, HTTP status codes, durations, and error reasons.
- Delivery records: what we sent, where, when, and whether it succeeded.
- Usage counters (checks and changes per month) for quota enforcement and
  billing.

**Logs**
- Request logs containing timestamp, path, HTTP status, duration, a request id,
  and — where authenticated — the account id. Retained for [30] days.

**Payment data**
- Handled entirely by Stripe. **We never receive or store card numbers.** We
  store only your Stripe customer and subscription identifiers, your plan, and
  your subscription status.

## What we do not do

- We do not sell your data.
- We do not share it with advertisers.
- We do not use your monitored content to train machine learning models.
- We do not read your monitored content except as needed to operate the Service
  or to investigate a fault you report.

## Legal basis (GDPR)

- **Performance of a contract** — operating the monitors you configure, billing.
- **Legitimate interests** — security, abuse prevention, service reliability.
- **Legal obligation** — retaining financial records where required.

## Sub-processors

| Provider | Purpose | Data |
|---|---|---|
| Stripe | Payments and subscriptions | Email, customer id, billing data |
| [YOUR HOSTING PROVIDER] | Hosting and database | All service data |
| [YOUR SMTP PROVIDER] | Email notifications | Recipient address, message content |

## Retention

- Account data: for the life of the account.
- Monitor content and change history: for the retention window of your plan
  (7 / 90 / 365 days), then deleted.
- Logs: [30] days.
- After account deletion: monitors and history removed within [30] days, except
  where financial records must be retained by law.

## Your rights

Depending on where you live, you may have the right to access, correct, export,
or delete your personal data, to object to or restrict processing, and to lodge a
complaint with a supervisory authority.

To exercise any of these, contact [PRIVACY CONTACT EMAIL]. We respond within
[30] days.

## A note on third-party content

The Service fetches pages that you nominate. Those pages may contain personal
data belonging to other people. **You are the controller of that data; we are
the processor acting on your instructions.** You are responsible for ensuring
you have a lawful basis for monitoring what you choose to monitor. See the
acceptable-use section of the Terms of Use.

## Security

- Passwords are bcrypt-hashed; API keys are stored only as SHA-256 hashes.
- All traffic is served over HTTPS in production.
- Outbound webhooks are signed with HMAC-SHA256 so recipients can verify them.
- The service refuses to fetch private, loopback, and link-local addresses.
- Access to production systems is limited to [WHO HAS ACCESS].

Report a security issue to [SECURITY CONTACT EMAIL].

## Changes

We will announce material changes by email to the address on your account at
least [30] days before they take effect.
