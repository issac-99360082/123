# Terms of Use

_Last updated: 2026-09-02_

> **This is a plain-language starting template, not legal advice.**
> Have a lawyer review it before you take real payments.
> Placeholders in `[brackets]` must be filled in.

## 1. The service

Watchpost ("the Service") fetches URLs you nominate on a schedule, extracts the
portion of the response you configure, and notifies you when that content
changes. The Service is operated by [YOUR LEGAL ENTITY], [JURISDICTION].

## 2. Your account

You are responsible for keeping your API keys secret and for all activity under
your account. Notify us at [SECURITY CONTACT EMAIL] if you believe a key has
been exposed. You may revoke and reissue keys at any time.

You must be legally capable of entering into this agreement, and you may not use
the Service if you are barred from doing so under applicable law.

## 3. Acceptable use

You may only monitor URLs that you are permitted to access. Specifically, you
must not use the Service to:

- access content behind a paywall, login, or other access control you are not
  authorised to bypass;
- monitor a site in violation of that site's terms of service;
- collect personal data unlawfully, or monitor content in order to harass,
  stalk, or profile an individual;
- place unreasonable load on a third party's infrastructure;
- attempt to reach private, internal, or non-public network addresses.

We enforce some of these technically — robots.txt is respected by default,
per-host request rates are throttled, an identifiable User-Agent is sent, and
private network addresses are refused — but **you remain responsible for the
legality of what you choose to monitor.**

We may suspend or terminate an account that breaches this section, without
refund where the breach is serious or repeated.

## 4. Plans, billing and refunds

- Paid plans are billed monthly in advance through Stripe. We do not receive or
  store your card details.
- Plan limits (monitor count, minimum check interval, monthly check volume) are
  published on the pricing page and enforced by the Service.
- You may cancel at any time through the customer portal. Cancellation takes
  effect at the end of the current billing period; the plan remains active until
  then.
- We do not provide pro-rata refunds for partial months except where required by
  law, or at our discretion where the Service has materially failed.
- We may change prices with at least 30 days' notice to the email on your
  account. Continued use after that notice period constitutes acceptance.

## 5. Service levels

The Service is provided on an "as is" and "as available" basis. We do not
guarantee that:

- every change will be detected, or detected within a specific time;
- every notification will be delivered;
- monitored sites will remain reachable or keep the structure your extraction
  rule depends on.

Checks run on a best-effort schedule. Failed webhook deliveries are retried on a
published backoff schedule and then abandoned.

**Do not rely on the Service as the sole control for anything safety-critical,
financially critical, or legally required.**

## 6. Limitation of liability

To the maximum extent permitted by law, our total aggregate liability arising
out of or relating to the Service is limited to the fees you paid us in the
three months preceding the event giving rise to the claim.

We are not liable for indirect, incidental, special, consequential, or punitive
damages, or for lost profits, revenue, data, or business opportunity, even if
advised of the possibility.

Nothing here excludes liability that cannot lawfully be excluded.

## 7. Your content

You retain all rights to the URLs, extraction rules, and endpoints you
configure. We store the extracted content and its history solely to operate the
Service for you — principally, to compare the current observation against the
previous one and to show you your own history.

## 8. Termination

You may delete your account at any time. We may terminate or suspend access for
breach of these terms, for non-payment, or if required by law. On termination we
delete your monitors and their history within [30] days.

## 9. Changes

We may update these terms. Material changes will be announced by email to the
address on your account at least [30] days before taking effect.

## 10. Contact

[YOUR CONTACT EMAIL]
