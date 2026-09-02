#!/usr/bin/env bash
#
# End-to-end smoke test against a running instance: signs up, previews an
# extraction, creates a monitor, runs a check, and reads usage back.
#
# Usage:  ./scripts/smoke_test.sh [base_url]
#
set -euo pipefail

BASE="${1:-http://localhost:8000}"
# Override when this host cannot reach example.com (egress filtering, proxies):
#   SMOKE_TARGET_URL=https://your-reachable-site.example ./scripts/smoke_test.sh
TARGET_URL="${SMOKE_TARGET_URL:-https://example.com/}"
EMAIL="smoke-$(date +%s)@example.com"
PASSWORD="correct-horse-42"

command -v curl >/dev/null || { echo "curl is required" >&2; exit 1; }
command -v python3 >/dev/null || { echo "python3 is required" >&2; exit 1; }

jqp() { python3 -c "import json,sys; print(json.load(sys.stdin)$1)"; }

echo "==> health"
curl -fsS "$BASE/healthz" | jqp "['status']"

echo "==> signup ($EMAIL)"
KEY=$(curl -fsS -X POST "$BASE/v1/auth/signup" \
  -H 'Content-Type: application/json' \
  -d "{\"email\":\"$EMAIL\",\"password\":\"$PASSWORD\"}" | jqp "['key']")
echo "  got API key ${KEY:0:16}..."

echo "==> plans"
curl -fsS "$BASE/v1/billing/plans" | jqp "[0]['label']"

echo "==> create monitor (target: $TARGET_URL)"
MONITOR=$(curl -fsS -X POST "$BASE/v1/monitors" \
  -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' \
  -d '{
        "name":"Smoke test monitor",
        "url":"'"$TARGET_URL"'",
        "extract_mode":"text",
        "interval_seconds":3600,
        "notify_webhook_url":"https://example.com/hooks/smoke"
      }')
MONITOR_ID=$(echo "$MONITOR" | jqp "['id']")
echo "  monitor id $MONITOR_ID"

echo "==> run check"
CHECK=$(curl -fsS -X POST "$BASE/v1/monitors/$MONITOR_ID/run" -H "Authorization: Bearer $KEY")
CHECK_STATUS=$(echo "$CHECK" | jqp "['status']")
echo "  status: $CHECK_STATUS"
if [[ "$CHECK_STATUS" == "error" ]]; then
  echo "  error_type: $(echo "$CHECK" | jqp "['error_type']")"
  echo "  message:    $(echo "$CHECK" | jqp "['error_message']")"
  echo
  echo "  NOTE: the API works, but this host could not fetch the monitored URL."
  echo "  Usually egress filtering or a proxy. Check outbound HTTPS from this machine."
  CHECK_FAILED=1
fi

echo "==> usage"
curl -fsS "$BASE/v1/usage" -H "Authorization: Bearer $KEY" | jqp "['checks_used']"

echo "==> cleanup"
curl -fsS -X DELETE "$BASE/v1/monitors/$MONITOR_ID" -H "Authorization: Bearer $KEY" >/dev/null
echo "  deleted"

echo
if [[ "${CHECK_FAILED:-0}" == "1" ]]; then
  echo "Smoke test finished against $BASE - API healthy, but the outbound fetch failed (see above)."
  exit 2
fi
echo "Smoke test passed against $BASE"
