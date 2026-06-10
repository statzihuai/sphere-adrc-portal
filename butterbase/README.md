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
