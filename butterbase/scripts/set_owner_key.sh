#!/usr/bin/env bash
# Operator-run: install OWNER_GATEWAY_KEY into fn:gateway (redeploys with same
# code + real env). Run this yourself — it handles a live credential.
#
#   ./butterbase/scripts/set_owner_key.sh                  # use the MCP key from ~/.claude.json
#   BB_OWNER_KEY=bb_sk_... ./butterbase/scripts/set_owner_key.sh   # or an explicit (ideally ai:gateway-scoped) key
#
# The key is read into the request body and never printed.
set -euo pipefail
cd "$(dirname "$0")/../.."

API="${BUTTERBASE_API:-https://api.butterbase.ai}"
APP="${BUTTERBASE_APP:-app_21ze8d0ep28o}"

KEY="${BB_OWNER_KEY:-$(python3 -c "import json;print(json.load(open('$HOME/.claude.json'))['mcpServers']['butterbase']['headers']['Authorization'].split()[-1])")}"
case "$KEY" in bb_sk_*) ;; *) echo "no bb_sk_ key found"; exit 1;; esac

python3 - "$KEY" <<'EOF'
import json, pathlib, sys
key = sys.argv[1]
payload = {
    "name": "gateway",
    "code": pathlib.Path("butterbase/functions/gateway.ts").read_text(),
    "description": "SPHERE metered OpenAI-compatible proxy: sphere_sk_ auth, reserve->settle wallet accounting, usage_log. Source: butterbase/functions/gateway.ts",
    "timeoutMs": 60000,
    "envVars": {"OWNER_GATEWAY_KEY": key},
    "triggers": [{"type": "http", "config": {"auth": "none"}}],
}
pathlib.Path("/tmp/gw_deploy.json").write_text(json.dumps(payload))
EOF

curl -sS -X POST "$API/v1/$APP/functions" \
  -H "Authorization: Bearer $KEY" \
  -H 'Content-Type: application/json' \
  --data @/tmp/gw_deploy.json \
  | python3 -c "import json,sys;d=json.load(sys.stdin);print('deployed:',d.get('name'),d.get('deployedAt') or d.get('error') or d.get('message'))"
rm -f /tmp/gw_deploy.json
echo "done — tell Claude to rerun gates 3 and 4 (metering + settled-mode assertions activate)."
