#!/usr/bin/env bash
# Gate 1 (§7.1): schema present, RLS fail-closed for anonymous REST access.
# No credentials required — every probe here MUST come back empty/denied.
set -euo pipefail

API="${BUTTERBASE_API:-https://api.butterbase.ai}"
APP="${BUTTERBASE_APP:-app_21ze8d0ep28o}"
fail=0

for t in wallets usage_log credit_orders trial_grants api_keys; do
  body=$(curl -sS "$API/v1/$APP/$t")
  if [ "$body" != "[]" ]; then
    echo "FAIL: anon SELECT on $t returned: $body"
    fail=1
  else
    echo "ok: anon SELECT on $t is empty"
  fi
done

code=$(curl -sS -o /dev/null -w "%{http_code}" -X POST "$API/v1/$APP/wallets" \
  -H 'Content-Type: application/json' \
  -d '{"user_id":"00000000-0000-0000-0000-000000000001","balance_microcents":999999999}')
if [ "$code" -ge 400 ]; then
  echo "ok: anon INSERT into wallets rejected ($code)"
else
  echo "FAIL: anon INSERT into wallets returned $code"
  fail=1
fi

exit $fail
