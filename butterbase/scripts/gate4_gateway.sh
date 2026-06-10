#!/usr/bin/env bash
# Gateway behavior gates: §7.2 (revoked key rejected), §7.4 (metering accuracy),
# plus the §5 negative paths and the §8-Q2 bearer-passthrough smoke test.
# Needs: SPHERE_KEY (live sphere_sk_), optionally SPHERE_REVOKED_KEY.
# Metering assertions only run once OWNER_GATEWAY_KEY is configured upstream
# (detected by a successful happy-path call).
set -euo pipefail

API="${BUTTERBASE_API:-https://api.butterbase.ai}"
APP="${BUTTERBASE_APP:-app_21ze8d0ep28o}"
FN="$API/v1/$APP/fn"
: "${SPHERE_KEY:?set SPHERE_KEY}"

expect() { # expect <desc> <want_code> <got_code>
  if [ "$3" = "$2" ]; then echo "ok: $1 -> $3"; else echo "FAIL: $1 -> $3 (want $2)"; exit 1; fi
}

# bearer passthrough + balance read
bal0=$(curl -sS "$FN/balance" -H "Authorization: Bearer $SPHERE_KEY")
echo "$bal0" | grep -q balance_microcents || { echo "FAIL: balance unreadable: $bal0"; exit 1; }
B0=$(echo "$bal0" | python3 -c 'import json,sys;print(json.load(sys.stdin)["balance_microcents"])')
echo "ok: bearer passthrough; balance_microcents=$B0"

expect "no credentials"  401 "$(curl -sS -o /dev/null -w '%{http_code}' -X POST "$FN/gateway" -d '{}')"
expect "bogus key"       401 "$(curl -sS -o /dev/null -w '%{http_code}' -X POST "$FN/gateway" -H 'Authorization: Bearer sphere_sk_bogus' -d '{}')"
if [ -n "${SPHERE_REVOKED_KEY:-}" ]; then
  expect "revoked key"   401 "$(curl -sS -o /dev/null -w '%{http_code}' -X POST "$FN/gateway" -H "Authorization: Bearer $SPHERE_REVOKED_KEY" -d '{}')"
fi
expect "stream:true"     400 "$(curl -sS -o /dev/null -w '%{http_code}' -X POST "$FN/gateway" -H "Authorization: Bearer $SPHERE_KEY" -H 'Content-Type: application/json' -d '{"model":"anthropic/claude-3-haiku","messages":[{"role":"user","content":"hi"}],"stream":true}')"
expect "unknown model"   404 "$(curl -sS -o /dev/null -w '%{http_code}' -X POST "$FN/gateway" -H "Authorization: Bearer $SPHERE_KEY" -H 'Content-Type: application/json' -d '{"model":"nope/nothing","messages":[{"role":"user","content":"hi"}]}')"
expect "oversized 402"   402 "$(curl -sS -o /dev/null -w '%{http_code}' -X POST "$FN/gateway" -H "Authorization: Bearer $SPHERE_KEY" -H 'Content-Type: application/json' -d '{"model":"anthropic/claude-opus-4.7-fast","messages":[{"role":"user","content":"hi"}],"max_tokens":100000}')"

# happy path + metering accuracy (§7.4) — only meaningful with a real owner key
resp=$(curl -sS -X POST "$FN/gateway" -H "Authorization: Bearer $SPHERE_KEY" -H 'Content-Type: application/json' \
  -d '{"model":"anthropic/claude-3-haiku","messages":[{"role":"user","content":"Reply with exactly: pong"}],"max_tokens":16}')
code=$(echo "$resp" | python3 -c 'import json,sys
d=json.load(sys.stdin)
print("200" if "usage" in d else "err")')
if [ "$code" != 200 ]; then
  echo "skip: metering check (upstream not configured yet — OWNER_GATEWAY_KEY placeholder?)"
  B1=$(curl -sS "$FN/balance" -H "Authorization: Bearer $SPHERE_KEY" | python3 -c 'import json,sys;print(json.load(sys.stdin)["balance_microcents"])')
  [ "$B1" = "$B0" ] && echo "ok: failed upstream call fully refunded (balance unchanged)" || { echo "FAIL: refund invariant broken: $B0 -> $B1"; exit 1; }
  echo "gateway gates PASS (metering pending owner key)"
  exit 0
fi

B1=$(curl -sS "$FN/balance" -H "Authorization: Bearer $SPHERE_KEY" | python3 -c 'import json,sys;print(json.load(sys.stdin)["balance_microcents"])')
echo "$resp" | python3 - "$B0" "$B1" <<'EOF'
import json, sys, math, urllib.request
resp = json.load(sys.stdin); b0, b1 = int(sys.argv[1]), int(sys.argv[2])
u = resp["usage"]
cat = json.load(urllib.request.urlopen("https://api.butterbase.ai/v1/public/models"))
m = next(x for x in cat["models"] if x["id"] == resp["model"])
inM, outM = round(m["inputPricePerMTokens"]*1e6), round(m["outputPricePerMTokens"]*1e6)
cost = math.ceil(u["prompt_tokens"]*inM/1e6) + math.ceil(u["completion_tokens"]*outM/1e6)
debited = b0 - b1
assert debited == cost, f"FAIL: debited {debited} != usage-derived {cost}"
print(f"ok: metering exact — debited {debited} micro-units == usage-derived cost")
EOF
echo "gateway gates PASS"
