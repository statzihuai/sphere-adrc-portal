#!/usr/bin/env bash
# Gate 2 (§7.2): mint → list shows prefix only → revoke → double-revoke 404.
# Needs a test end-user: SPHERE_TEST_EMAIL / SPHERE_TEST_PASSWORD env vars.
# (Revoked-key-rejected-by-gateway is asserted in gate4_gateway.sh.)
set -euo pipefail

API="${BUTTERBASE_API:-https://api.butterbase.ai}"
APP="${BUTTERBASE_APP:-app_21ze8d0ep28o}"
: "${SPHERE_TEST_EMAIL:?set SPHERE_TEST_EMAIL}"
: "${SPHERE_TEST_PASSWORD:?set SPHERE_TEST_PASSWORD}"
KEYS="$API/v1/$APP/fn/keys"

TOK=$(curl -sS -X POST "$API/auth/$APP/login" -H 'Content-Type: application/json' \
  -d "{\"email\":\"$SPHERE_TEST_EMAIL\",\"password\":\"$SPHERE_TEST_PASSWORD\"}" | python3 -c 'import json,sys;print(json.load(sys.stdin)["access_token"])')

# unauthenticated must be rejected at the edge
code=$(curl -sS -o /dev/null -w "%{http_code}" "$KEYS")
[ "$code" = 401 ] && echo "ok: unauthenticated GET -> 401" || { echo "FAIL: unauthenticated GET -> $code"; exit 1; }

mint=$(curl -sS -X POST "$KEYS" -H "Authorization: Bearer $TOK" -H 'Content-Type: application/json' -d '{"name":"gate2"}')
KEY=$(echo "$mint" | python3 -c 'import json,sys;print(json.load(sys.stdin)["key"])')
KID=$(echo "$mint" | python3 -c 'import json,sys;print(json.load(sys.stdin)["id"])')
case "$KEY" in sphere_sk_*) echo "ok: minted $KEY" | sed 's/sk_\(.....\).*/sk_\1.../' ;; *) echo "FAIL: bad key format"; exit 1 ;; esac

list=$(curl -sS "$KEYS" -H "Authorization: Bearer $TOK")
echo "$list" | grep -q "$KEY" && { echo "FAIL: plaintext key in list"; exit 1; } || echo "ok: list never contains plaintext"
echo "$list" | grep -q key_hash && { echo "FAIL: key_hash in list"; exit 1; } || echo "ok: list never contains key_hash"
echo "$list" | grep -q "${KEY:0:15}" && echo "ok: list shows prefix" || { echo "FAIL: prefix missing from list"; exit 1; }

code=$(curl -sS -o /dev/null -w "%{http_code}" -X DELETE "$KEYS?id=$KID" -H "Authorization: Bearer $TOK")
[ "$code" = 200 ] && echo "ok: revoke -> 200" || { echo "FAIL: revoke -> $code"; exit 1; }
code=$(curl -sS -o /dev/null -w "%{http_code}" -X DELETE "$KEYS?id=$KID" -H "Authorization: Bearer $TOK")
[ "$code" = 404 ] && echo "ok: double-revoke -> 404" || { echo "FAIL: double-revoke -> $code"; exit 1; }

# malformed id must be a clean 400 with no leaked internals (review F3 regression)
resp=$(curl -sS -w "\n%{http_code}" -X DELETE "$KEYS?id=not-a-uuid" -H "Authorization: Bearer $TOK")
code=$(echo "$resp" | tail -1); body=$(echo "$resp" | head -1)
[ "$code" = 400 ] && echo "ok: non-uuid id -> 400" || { echo "FAIL: non-uuid id -> $code"; exit 1; }
echo "$body" | grep -qi 'stack\|PostgresError' && { echo "FAIL: internals leaked: $body"; exit 1; } || echo "ok: no stack trace leaked"

echo "gate 2 PASS"
