# SPHERE: Butterbase vs FastAPI — parity report (§7.7)

Date: 2026-06-10 · Status: **partial** — happy-path model calls and Stripe checkout are pending two
user-side steps (owner `ai:gateway` key via dashboard; Stripe Connect onboarding). Everything else is
measured against the live deployment (`app_21ze8d0ep28o`).

## Lines of code we wrote

| Component | Butterbase build | FastAPI build |
|---|---:|---:|
| Backend (functions + schema) | ~400 | 2,551 (src, incl. WorkOS SSO, SSE streaming, portal serving) |
| Verification / tests | ~280 (gate scripts) | 1,795 (pytest) |
| SDKs (product surface, both builds share it) | 317 + 251 tests | — (FastAPI predates the SDK framing) |

Scope is not identical: the FastAPI number buys WorkOS enterprise auth, SSE streaming, and an
Anthropic-native agent endpoint; the Butterbase build buys none of those but got auth, hosting,
Postgres, RLS, model routing, and payments rails for free. Like-for-like, the metered-wallet core
(keys + wallet + gateway + trial) is ~330 LOC on Butterbase vs ~700 in the FastAPI slices that
implement the same loop.

## Latency (measured from a residential connection, us-west)

| Path | p50 | p95 | What it includes |
|---|---:|---:|---|
| `GET /health` (network baseline) | 43 ms | — | TLS + RTT only |
| `GET /v1/public/models` | 210 ms | — | platform API, no function |
| `fn:balance` | 1,046 ms | 1,155 ms | function invoke + key lookup + wallet read |
| `fn:gateway` pre-model (402 path) | 1,261 ms | 1,380 ms | invoke + auth + catalog + reserve guard |

**Finding:** the function runtime adds ≈ 1.0–1.2 s of overhead before the first model token — each
`ctx.db.query()` is its own round-trip and invocation overhead dominates. The FastAPI build's
equivalent pre-model path (in-process JWT check + one `FOR UPDATE` query) measures in the tens of
milliseconds on its own infrastructure. For an interactive agent product this is the single biggest
delta. (FastAPI numbers on equal network footing: pending a deployed instance; not measured here.)

## Cost per 1k calls (catalog math, 500 in / 300 out tokens, `claude-sonnet-4.5`)

| | $/1k calls | vs direct |
|---|---:|---|
| Butterbase gateway ($3.6/M in, $18/M out) | $7.20 | +20% |
| Direct Anthropic list ($3/M in, $15/M out) | $6.00 | — |

The ~20% gateway markup holds across the sonnet family; some rows (opus 4.5+) deviate from a flat
20%, so per-model checks matter before committing to a price card. Function invocations
(500k/mo included on Pro) are negligible at spike volume.

## DX notes

**Free wins.** Schema DSL with dry-run diffs; RLS with auto service-bypass; one-shot user-isolation
policy *with* the auto-populate trigger; end-user auth (signup/login/refresh/JWKS) with zero code;
the post-auth hook; encrypted function env vars; `auth: none → service-role ctx.db` is exactly the
right primitive for a metered proxy; the public model catalog with prices made exact integer
accounting trivial.

**Friction.** No multi-statement transactions in functions (forced the single-statement atomic
guard — fine here, constraining in general); functions are single files (no shared modules →
helpers duplicated across 4 functions); fire-and-forget auth hook required a lazy backstop;
platform consumes Stripe webhooks (crediting had to become claim-based, §4.6 amendment); scoped
`ai:gateway` keys can only be minted with a dashboard JWT (not via MCP); ~1s invocation overhead;
end-user 402s spend *our* gateway credits at a 20% markup — the wallet meters end users, but the
bill lands on the platform account.

**Verdict so far.** The two-key model works end-to-end on Butterbase with ~6× less backend code,
but the latency overhead and the markup are both product-visible. If SPHERE's product is an
interactive agent, the FastAPI direct-passthrough path keeps a decisive UX edge; if it's batch/API
metering, the Butterbase build is genuinely competitive on effort.

## Gate status (§7)

| Gate | Status |
|---|---|
| 1 schema + RLS | **pass** (`gate1_schema.sh`) |
| 2 key lifecycle + revoked-key rejection | **pass** (`gate2_keys.sh`, `gate4_gateway.sh`) |
| 3 wallet invariant under concurrency | **pass** — conservation exact, never negative, guard fired 8/16 under contention (`gate3_concurrency.py`); strict settled-mode phase auto-activates once the owner key is set |
| 4 metering accuracy | **machinery verified** (refund path bit-exact); usage-derived debit assertion pending owner key |
| 5 money-in idempotent crediting | **machinery deployed + negative paths pass**; full loop pending Stripe Connect onboarding + product creation |
| 6 SDKs | **pass** (10 py + 8 ts unit tests; live balance/402/401/models against the deployment) |
| 7 parity report | this document (latency/cost FastAPI columns pending a deployed comparison instance) |
