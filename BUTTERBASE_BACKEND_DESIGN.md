# SPHERE on Butterbase — Backend & SDK Design

Status: **Draft / spike** · Date: 2026-06-10 · Branch: `design/butterbase-sphere-backend`
App: Butterbase `app_21ze8d0ep28o` (hosted, `https://api.butterbase.ai`)

> This is a **comparison build**. The production SPHERE backend (FastAPI) is specified in
> [`BACKEND_DESIGN.md`](./BACKEND_DESIGN.md). This document specifies an equivalent backend built on
> Butterbase, so we can measure — head to head — how much of SPHERE a managed AI-native BaaS gives us
> for free, and how much we still hand-build. Read the two docs side by side.

---

## 1. Problem statement

SPHERE is a **metered LLM gateway product**: a user buys credits, receives an API key, and calls AI
models through an SDK; every call is metered against their prepaid balance and rejected with `402`
when empty. We have a working FastAPI implementation of this (centralized upstream key + per-user
`ad_live_`-style keys + Postgres wallet + Stripe). The question this build answers: **can Butterbase
host the same product, and what is the delta in effort, cost, latency, and control?**

The deliverable is not a throwaway — the two **user-facing headline features are the API key and the
`sphere` SDK**, and they must work end to end. The ADRC portal is treated as *one example client* of
that SDK, not as the product.

## 2. Goals and non-goals

### Goals
- Reproduce SPHERE's core loop on Butterbase: **sign up → buy credits → mint `sphere_sk_` key → SDK call → metered → 402 when empty.**
- Ship the **`sphere` SDK** (PyPI) and **`@sphere/sdk`** (npm) as OpenAI-compatible thin clients.
- Keep **Decimal-exact accounting** (integer micro-cents, never float) — same integrity bar as the FastAPI wallet ([`BACKEND_DESIGN.md` §4.4](./BACKEND_DESIGN.md)).
- Produce a **quantified comparison** (§7) against the FastAPI backend.

### Non-goals (v1)
- WorkOS / enterprise SSO — Butterbase auth is email/OAuth only; we accept that substitution for the spike.
- Streaming (SSE) responses — **non-streaming first** (§5 explains why); streaming is a follow-up slice.
- Replacing the production SPHERE backend. This is an evaluation, not a migration.
- Multi-region, custom domains, RAG, realtime, storage — out of scope for the gateway comparison.

## 3. Relevant context and constraints

- **Butterbase bills the key owner, not per-end-user.** The AI gateway (`POST /v1/{app}/chat/completions`)
  meters credits against *our* Butterbase account. There is **no native per-end-user wallet, cap, or
  resold key**. Per-user metering is therefore the custom layer we build — the same layer SPHERE already owns.
- **Personal `bb_sk_` keys are platform-scoped.** We cannot hand them to end users. End users get our
  own `sphere_sk_` keys, validated by our function.
- **Functions run on Deno** (TS/JS), HTTP/cron/webhook triggers, encrypted env vars, public invocation at
  `ANY /v1/{app}/fn/{name}` ([Functions API](https://docs.butterbase.ai/api-reference/functions-api)).
- **Pricing reality:** gateway charges model price **plus Butterbase's ~$0.10/credit markup**; the proxy
  adds **one network hop** vs. SPHERE's direct Anthropic passthrough. Both are measured in §7.
- **Established billing model** (carried over verbatim): real-time prepaid wallet, synchronous balance
  check, atomic deduction, `402` on insufficient balance, `$10` one-per-email trial. See `BACKEND_DESIGN.md` §4.4.

## 4. Proposed design

### 4.1 Component architecture — the two-key model

```
  sphere SDK  (pip install sphere · npm @sphere/sdk)        ← the product
        │  Authorization: Bearer sphere_sk_...               (the user's key)
        ▼
  ┌─────────────────────────────────────────────────────────────┐
  │  SPHERE backend  =  Butterbase app_21ze8d0ep28o              │
  │                                                              │
  │  fn:gateway (http, auth:none)   fn:keys (http, auth:required)│
  │   1 validate sphere_sk_ → user   mint/list/revoke sphere_sk_ │
  │   2 reserve worst-case cost                                  │
  │   3 call Butterbase gateway ───────┐  (owner bb_sk_ env var) │
  │   4 settle actual cost             │                         │
  │   5 402 if insufficient            ▼                         │
  │  tables: wallets · api_keys ·   AI gateway → Claude/GPT/…    │
  │          usage_log · credit_orders                           │
  │  fn:stripe-webhook ← POST /webhooks/stripe/connect           │
  └─────────────────────────────────────────────────────────────┘
        ▲  imports `sphere`, holds a sphere_sk_ key
  ADRC portal  — example client (one of many)
```

Mapping to the FastAPI design: **owner `bb_sk_` = centralized Anthropic key**; **`sphere_sk_` + wallet =
`ad_live_` keys + Postgres wallet**; **Butterbase gateway = Anthropic + provider routing**. SPHERE keeps
ownership of identity-of-the-caller and money; Butterbase owns the upstream model plumbing and hosting.

### 4.2 Data model (Butterbase schema DSL)

Applied via `manage_schema` (dry-run → apply). All money is **integer micro-cents** (`bigint`); RLS scopes
rows to `user_id`. `manage_schema` payload:

```json
{
  "tables": {
    "wallets": {
      "columns": {
        "user_id": { "type": "uuid", "primary": true },
        "balance_microcents": { "type": "bigint", "nullable": false, "default": "0" },
        "updated_at": { "type": "timestamptz", "default": "now()" }
      }
    },
    "api_keys": {
      "columns": {
        "id": { "type": "uuid", "primary": true, "default": "gen_random_uuid()" },
        "user_id": { "type": "uuid", "nullable": false },
        "key_hash": { "type": "text", "nullable": false, "unique": true },
        "key_prefix": { "type": "text", "nullable": false },
        "name": { "type": "text" },
        "scopes": { "type": "text", "default": "'ai:gateway'" },
        "revoked_at": { "type": "timestamptz" },
        "last_used_at": { "type": "timestamptz" },
        "created_at": { "type": "timestamptz", "default": "now()" }
      },
      "indexes": { "idx_api_keys_user": { "columns": ["user_id"] } }
    },
    "usage_log": {
      "columns": {
        "id": { "type": "uuid", "primary": true, "default": "gen_random_uuid()" },
        "api_key_id": { "type": "uuid", "nullable": false, "references": { "table": "api_keys", "column": "id", "onDelete": "CASCADE" } },
        "user_id": { "type": "uuid", "nullable": false },
        "model": { "type": "text", "nullable": false },
        "input_tokens": { "type": "integer", "default": "0" },
        "output_tokens": { "type": "integer", "default": "0" },
        "cost_microcents": { "type": "bigint", "nullable": false },
        "elapsed_ms": { "type": "integer" },
        "created_at": { "type": "timestamptz", "default": "now()" }
      },
      "indexes": { "idx_usage_user_time": { "columns": ["user_id", "created_at"] } }
    },
    "credit_orders": {
      "columns": {
        "id": { "type": "uuid", "primary": true, "default": "gen_random_uuid()" },
        "user_id": { "type": "uuid", "nullable": false },
        "stripe_session_id": { "type": "text", "nullable": false, "unique": true },
        "amount_microcents": { "type": "bigint", "nullable": false },
        "status": { "type": "text", "nullable": false, "default": "'pending'" },
        "created_at": { "type": "timestamptz", "default": "now()" }
      }
    },
    "trial_grants": {
      "columns": {
        "email": { "type": "text", "primary": true },
        "user_id": { "type": "uuid", "nullable": false },
        "granted_at": { "type": "timestamptz", "default": "now()" }
      }
    }
  },
  "name": "sphere_gateway_core"
}
```

`usage_log` deliberately mirrors the production `v1_resume_matching_usage` shape (api_key_id, model,
tokens, cost, elapsed) so partner-billing queries port over. `credit_orders.stripe_session_id UNIQUE`
is the idempotency guard for webhook replays. `trial_grants` (PK = lowercased email) makes the $10
one-per-email trial a database constraint — both grant paths (§8 Q5) `INSERT … ON CONFLICT DO NOTHING`.

RLS: `enable_rls` on all five tables with **no anon/user policies** — the auto-created service bypass
lets functions (which run as `butterbase_service`, see §8 Q1) operate while the public REST data API
sees nothing.

### 4.3 API keys — issuance & validation (headline feature #1)

Function `fn:keys`, HTTP trigger `auth: required` (end-user JWT), so signed-in users manage their own keys:

| Method | `…/fn/keys` | Behaviour |
|---|---|---|
| POST | mint | generate `sphere_sk_` + 32 random bytes; store **SHA-256 hash only** + `key_prefix`; return plaintext **once** |
| GET | list | return `id`, `key_prefix`, `name`, `created_at`, `last_used_at` — never the full key |
| DELETE `?id=` | revoke | set `revoked_at = now()` |

Ergonomics mirror Butterbase's own `bb_sk_` (hash-at-rest, prefix display, show-once). Validation lives in
`fn:gateway` (§4.5): `sha256(presented) → api_keys WHERE key_hash = $1 AND revoked_at IS NULL`.

### 4.4 Wallet: reserve → settle (the integrity core)

Identical algorithm to [`BACKEND_DESIGN.md` §4.4](./BACKEND_DESIGN.md), re-expressed in SQL inside the Deno function:

1. **Reserve (atomic):** before the upstream call, deduct a worst-case estimate so concurrent calls can't
   overspend:
   ```sql
   UPDATE wallets
      SET balance_microcents = balance_microcents - $reserve
    WHERE user_id = $uid AND balance_microcents >= $reserve
   RETURNING balance_microcents;
   ```
   Zero rows → insufficient balance → `402`. `$reserve = ceil(max_tokens × output_price_microcents_per_token) + input_estimate`.
2. **Settle:** after the response, compute `actual = input_tokens·in_price + output_tokens·out_price`
   (read from `response.usage`, priced from `GET /v1/public/models`), then refund the difference:
   ```sql
   UPDATE wallets SET balance_microcents = balance_microcents + ($reserve - $actual), updated_at = now()
    WHERE user_id = $uid;
   ```
3. **Record:** `INSERT INTO usage_log (...)`.

Atomic `UPDATE … WHERE balance >= reserve` is the Butterbase equivalent of the FastAPI `SELECT … FOR UPDATE`
— no row lock needed because the guard is in the `WHERE`. This is the **invariant**: balance never goes negative.

### 4.5 Metered gateway proxy — `fn:gateway` (the heart)

One function, HTTP trigger **`auth: none`** (we authenticate via `sphere_sk_` ourselves), owner key in an
encrypted env var `OWNER_GATEWAY_KEY`. Pseudocode:

```ts
export default async function handler(req) {
  const t0 = Date.now();
  const key = bearer(req);                                  // 401 if missing
  const row = await db.one(`SELECT id,user_id FROM api_keys
                            WHERE key_hash=$1 AND revoked_at IS NULL`, [sha256(key)]); // 401
  const body = await req.json();                            // OpenAI chat-completions shape
  if (body.stream) return json(400, { error: { type: "invalid_request_error", code: "stream_unsupported" } });
  body.max_tokens ??= 1024;                                 // enforced output ceiling = what we reserve (§8 Q3)
  const reserve = worstCase(body.model, body.max_tokens);   // + ceil(utf8_bytes(messages)/3) input estimate
  const ok = await db.one(`UPDATE wallets SET balance_microcents=balance_microcents-$1
                           WHERE user_id=$2 AND balance_microcents>=$1
                           RETURNING balance_microcents`, [reserve, row.user_id]);
  if (!ok) return json(402, { error: { type: "billing_error", code: "insufficient_credits" } });

  const up = await fetch(`${API}/v1/${APP}/chat/completions`, {                 // owner key
    method: "POST", headers: { Authorization: `Bearer ${env.OWNER_GATEWAY_KEY}` }, body: JSON.stringify(body) });
  const out = await up.json();

  const actual = price(body.model, out.usage);              // settle
  await db.none(`UPDATE wallets SET balance_microcents=balance_microcents+$1, updated_at=now()
                 WHERE user_id=$2`, [reserve - actual, row.user_id]);
  await db.none(`INSERT INTO usage_log (...) VALUES (...)`, [...]);
  await db.none(`UPDATE api_keys SET last_used_at=now() WHERE id=$1`, [row.id]);
  return json(up.status, out);                              // pass body through unchanged (OpenAI shape)
}
```

Public URL: `POST https://api.butterbase.ai/v1/app_21ze8d0ep28o/fn/gateway`. This is the SDK's `base_url`.

### 4.6 Credits — money-in (Stripe Connect)

- **Define product** once (developer auth): `POST /v1/{app}/billing/products` — e.g. `"$10 credit pack" → 10_000_000` micro-cents.
- **User buys** (end-user JWT): `POST /v1/{app}/billing/purchase` → returns Stripe Checkout URL.
- **`fn:stripe-webhook`** on `POST /webhooks/stripe/connect`: on `checkout.session.completed`, idempotently
  (insert `credit_orders` with `UNIQUE stripe_session_id`) credit the wallet:
  ```sql
  INSERT INTO credit_orders (...) ON CONFLICT (stripe_session_id) DO NOTHING;   -- replay guard
  UPDATE wallets SET balance_microcents = balance_microcents + $amt WHERE user_id = $uid;
  ```
  The handler first acks retries via `ctx.idempotency.claim(event.id, { scope: "stripe" })`; the UNIQUE
  constraint stays as the last-line guard.
- **Trial:** post-auth hook grants the `$10` one-per-email credit eagerly; `fn:keys` repeats it lazily as
  the backstop (hook is fire-and-forget — §8 Q5). Idempotent via `trial_grants` PK.

### 4.7 The `sphere` SDK (headline feature #2 — the product surface)

Because `fn:gateway` returns OpenAI-shaped bodies, the SDK is a **thin OpenAI-compatible client** with
`base_url` pre-pointed at the gateway function; the user supplies only `sphere_sk_...`.

```python
from sphere import Client
c = Client(api_key="sphere_sk_...")                 # base_url defaults to SPHERE gateway
r = c.chat.completions.create(
        model="anthropic/claude-3.5-sonnet",
        messages=[{"role": "user", "content": "Hi"}])
print(c.balance())                                  # -> {"balance_usd": 9.83}
```
```ts
import { Sphere } from "@sphere/sdk";
const sphere = new Sphere({ apiKey: "sphere_sk_..." });
const r = await sphere.chat.completions.create({ model, messages });
```

Surface (v1): `chat.completions.create` (stream flag deferred), `embeddings.create`, `models.list`
(proxies `/v1/public/models`), `balance()` (small `fn:balance` endpoint). Typed errors:
`InsufficientCredits` (402), `InvalidKey` (401), `ModelNotFound` (404) — mirroring Butterbase's own error
contract. Implementation can subclass/wrap the official OpenAI SDK with a custom `baseURL` to minimize code.

### 4.8 ADRC portal as example client

The ADRC portal is rewired to import the published `sphere` SDK and call it with a `sphere_sk_` key —
exactly as an external customer would. This doubles as (a) the SDK's first integration test and (b) the
comparison harness in §7. It is explicitly **not** privileged: no internal endpoints, no owner key.

## 5. Edge cases and failure handling

| Case | Handling |
|---|---|
| Concurrent calls draining a wallet | Atomic `UPDATE … WHERE balance >= reserve`; loser gets `402`. Invariant: never negative. |
| Upstream gateway error after reserve | Refund the full reserve in a `finally`; do not write `usage_log`. Mirrors FastAPI reclaim-on-failure. |
| Worst-case reserve > balance but actual would fit | Accept the false `402` in v1 (conservative). Mitigated: omitted `max_tokens` defaults to 1024 *injected upstream*, so the reserve is an enforced ceiling, not a guess (§8 Q3). |
| Revoked/expired key | `401 invalid_api_key` from `fn:gateway`. |
| Webhook replay / double-fire | `credit_orders.stripe_session_id UNIQUE` + `ON CONFLICT DO NOTHING`. |
| **Streaming (`stream:true`)** | **Deferred, feasibility confirmed.** `ctx.waitUntil` exists (§8 Q4): pipe SSE through a `TransformStream`, parse the terminal `usage` chunk, settle under `waitUntil`. v1 returns `400 stream_unsupported`; streaming is its own slice. |
| Function cold start / timeout | `timeoutMs` = 60_000; surface upstream `5xx` as retryable `api_error`. |
| Owner key leakage | Stored only as encrypted function env var; never returned; rotate via Butterbase dashboard. |

## 6. Scalability and extensibility notes

- **Per-user metering is portable.** The wallet/keys/usage tables and the reserve→settle loop are
  Butterbase-agnostic; only `fn:gateway`'s upstream call is Butterbase-specific. Swapping the upstream
  (back to direct Anthropic, or another gateway) is a one-function change.
- **Cost overhead is explicit:** one extra hop (SDK → fn → gateway → model) and the platform markup. If
  the comparison shows the markup dominates, the same SPHERE layer can point at Anthropic directly.
- **Extending the SDK** (new models, embeddings, video) is additive — the gateway already exposes them.
- **Multi-tenant later:** RLS + `user_id` scoping means the same backend serves many client apps (ADRC is
  just the first), which is the whole point of the SDK-as-product framing.

## 7. Verification strategy

Per-slice gates (the build is sliced exactly as §4.2–4.7), each verified before the next:

1. **Schema:** dry-run shows 4 creates; apply; `manage_schema get` confirms columns/types/indexes.
2. **Keys:** mint → `GET` shows prefix only → revoke → revoked key rejected by `fn:gateway`.
3. **Wallet invariant:** property test — N concurrent calls against a balance that fits M<N of them yields
   exactly M successes and N−M `402`s; final balance ≥ 0 and equals start − Σ(actual).
4. **Metering accuracy:** a known prompt debits the wallet by the exact `usage`-derived cost; cross-check
   against `GET /v1/{app}/ai/usage` delta (≤ rounding tolerance).
5. **Money-in:** Stripe test-mode checkout → webhook → balance increases **once** under duplicate webhook delivery.
6. **SDK:** from a clean env, `sphere` (Py) and `@sphere/sdk` (TS) each complete a call, debit correctly,
   and raise typed `InsufficientCredits` at zero balance; `models.list` matches `/v1/public/models`.
7. **Parity report (the deliverable):** run an identical script against (a) FastAPI SPHERE and (b) this
   backend; tabulate **LOC we wrote, p50/p95 latency, $ per 1k calls (incl. markup), and DX notes.**

## 8. Decisions & remaining open questions

**Decided**
- SDK package name **`sphere`** / **`@sphere/sdk`**; key prefix **`sphere_sk_`**; ADRC = example client.
- Integer micro-cents; atomic-`WHERE` reserve→settle; non-streaming v1; OpenAI-compatible SDK surface.
- Doc lives at repo root beside `BACKEND_DESIGN.md`; built agentically via Butterbase MCP tools.

**Resolved (2026-06-10, via `butterbase_docs` + `deploy_function` contract + live `/v1/public/models`)**

1. **Function DB binding — RESOLVED.** Functions receive an injected Postgres client as `ctx.db`
   (`ctx.db.query(sql, params)`); no connection string or env var needed. Role is determined by
   invocation: end-user JWT → `butterbase_user` (RLS enforced), platform key / cron → `butterbase_service`
   (RLS bypassed). Critically for `fn:gateway`: **an `auth: none` HTTP function runs `ctx.db` as
   `butterbase_service`** (documented in the function trigger contract), so it can read `api_keys` and
   update `wallets` while RLS (enabled with *no* anon/user policies) keeps those tables invisible to the
   auto-generated REST data API. One caveat to design around: each `ctx.db.query()` is an **independent
   transaction** — there is no multi-statement transaction API for service-role code. Our integrity
   guard is a single-statement atomic `UPDATE … WHERE balance >= reserve`, so this is fine; settle and
   `usage_log` insert are separate transactions by design (a crash between them loses a log row, never
   money).

2. **Owner key in env — RESOLVED.** Encrypted function `envVars` (read via `ctx.env`, *not*
   `Deno.env.get()`) are the documented home for exactly this pattern — the AI docs' "Using AI in
   serverless functions" example stores the owner key as `envVars: { BUTTERBASE_API_KEY: "bb_sk_..." }`.
   There is **no internal shortcut**: the function calls the app-scoped gateway over the public endpoint
   (`${ctx.env.BUTTERBASE_API_URL}/v1/${ctx.env.BUTTERBASE_APP_ID}/chat/completions`) with the owner key
   as bearer — `BUTTERBASE_APP_ID` / `BUTTERBASE_API_URL` are auto-injected by the runtime. Least
   privilege: mint the owner key with `scopes: ["ai:gateway"]` (grants only chat/embeddings/models —
   nothing else) rather than using a full-access `bb_sk_`.
   *Slice-1 smoke test:* confirm the edge forwards a non-Butterbase `Authorization: Bearer sphere_sk_…`
   header untouched to an `auth: none` function (expected — the edge only enforces auth when the trigger
   demands it). Fallback if it doesn't: SDK sends the key as `x-sphere-key` instead; one-line change in
   both SDKs and `fn:gateway`.

3. **`max_tokens` default — RESOLVED.** Two-part rule:
   (a) If the request omits `max_tokens`, `fn:gateway` **injects `max_tokens: 1024` into the upstream
   body** — the reserve is then not an estimate but an enforced ceiling on the output side (the model
   cannot generate past what we reserved). At the worst-case catalog price today
   (`anthropic/claude-opus-4.7-fast`, $144/M output tokens) a 1024-token default reserves ≈ $0.147 —
   negligible false-402 surface against the $10 trial.
   (b) Input side: estimate `ceil(utf8_bytes(messages)/3)` tokens (conservative ~3 bytes/token), priced
   from the live `GET /v1/public/models` catalog (`inputPricePerMTokens` / `outputPricePerMTokens`,
   cached in-function for 5 min). Client-specified large `max_tokens` can still false-402 a low balance
   — accepted v1 behavior per §5; additionally set the app-level `maxTokensPerRequest` AI config to cap
   abuse.

4. **Streaming slice — FEASIBLE, still deferred.** `ctx.waitUntil(promise)` exists in the function
   contract ("keep alive for background work after response"). Streaming design when we take the slice:
   pipe the upstream SSE body through a `TransformStream`, parse the terminal `usage` chunk, and run
   settle + `usage_log` insert under `waitUntil`. Reserve-then-settle already tolerates a killed
   invocation (user keeps the conservative debit; reconcile against `GET /v1/{app}/ai/usage` by cron if
   it ever matters). v1 still returns `400 stream_unsupported`.

5. **Trial grant hook — RESOLVED: hook + lazy fallback.** The post-auth hook
   (`manage_auth_config configure_auth_hook`) fires on every OAuth login / email login / signup with
   `{ user: { id, email }, isNewUser }` and runs as `butterbase_service` — but it is explicitly
   **fire-and-forget** (no delivery guarantee, no retry). So: the hook function grants the trial
   eagerly for instant UX, and `fn:keys` (POST, the first authenticated thing every user must do)
   repeats the grant lazily as the reliability backstop. Both paths are idempotent via a new
   `trial_grants` table (§4.2) with `UNIQUE(email)` + `ON CONFLICT DO NOTHING` — the one-per-email rule
   is the constraint itself, not application logic.

6. **Key prefix — RESOLVED: `sphere_sk_` stands.** Checked conventions in play: AgentDrive production
   uses `ad_live_` (12-char display prefix); Butterbase platform keys use `bb_sk_`; nothing in this repo
   or `BACKEND_DESIGN.md` claims another SPHERE scheme. No clash; `sphere_sk_` is also visually distinct
   from both neighbors, which matters when keys show up in the same logs.

**Implementation notes picked up along the way**
- `fn:stripe-webhook` should use the platform's `ctx.idempotency.claim(event.id, { scope: "stripe" })`
  primitive *in addition to* the `credit_orders.stripe_session_id UNIQUE` constraint — claim() acks
  retries cheaply before any DB work; the constraint remains the last-line guard.
- Function `timeoutMs` default is 30 000 (max 300 000) — set 60 000 explicitly on `fn:gateway` as §5 states.
- RLS setup per table: `enable_rls` only (service bypass policy is auto-created; we add **no** anon/user
  policies — end users touch these tables exclusively through our functions).
