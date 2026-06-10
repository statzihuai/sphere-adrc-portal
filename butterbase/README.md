# SPHERE on Butterbase — deployment artifacts

Implements [`../BUTTERBASE_BACKEND_DESIGN.md`](../BUTTERBASE_BACKEND_DESIGN.md) against the hosted
Butterbase app `app_21ze8d0ep28o` (`https://api.butterbase.ai`). There is no server to run locally —
this directory holds the sources of truth for what is deployed.

## Layout

- `schema/sphere_gateway_core.json` — the §4.2 data model, in `manage_schema` DSL. Applied as
  migration 1 (dry-run first, then apply).
- `functions/` — Deno function sources deployed via `deploy_function`. The deployed code must always
  match these files.
- `scripts/` — the §7 verification gates, one script per gate. Each is a repeatable regression check.

## RLS configuration (applied 2026-06-10)

| Table | Setup | Effect |
|---|---|---|
| `wallets`, `usage_log`, `credit_orders`, `trial_grants` | `enable_rls` only | service-only: invisible to anon and end-user REST; functions (service role) have full access |
| `api_keys` | `create_user_isolation` on `user_id` | signed-in users manage their own key rows (used by `fn:keys`, which runs as `butterbase_user`); `user_id` auto-populated on insert |

Note: the `api_keys` isolation policy means an authenticated end user could also touch their own key
rows via the raw REST API. That is capability-equivalent to `fn:keys` (they can only affect their own
account) and is accepted; the plaintext key never exists server-side, only `key_hash`.

## Verification gates

| Script | Gate | Needs |
|---|---|---|
| `gate1_schema.sh` | §7.1 schema + RLS fail-closed | nothing (anon probes) |
| `gate2_keys.sh` | §7.2 key lifecycle | `SPHERE_TEST_EMAIL`, `SPHERE_TEST_PASSWORD` |
| `gate4_gateway.sh` | §7.2 revoked-key, §7.4 metering, §5 negative paths, §8-Q2 bearer smoke | `SPHERE_KEY` (+ optional `SPHERE_REVOKED_KEY`) |

## Owner key (fn:gateway upstream)

`fn:gateway` calls the app-scoped AI gateway with `OWNER_GATEWAY_KEY` from its encrypted env.
Deployed with a placeholder — set the real key (mint with `scopes: ["ai:gateway"]` via
`POST /api-keys` with a dashboard JWT, per least-privilege §8 Q2) without redeploying:

    manage_function { action: "update_env", function_name: "gateway",
                      env: { OWNER_GATEWAY_KEY: "bb_sk_..." } }

Until then the happy path returns the upstream 401 passthrough and fully refunds the reserve
(verified); all other gateway behavior is live.
