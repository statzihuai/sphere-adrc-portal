# SPHERE Backend — Design

**Status:** Draft for implementation · **Date:** 2026-06-06
**Scope:** Phase 1–2 of `ENGINEER_BRIEF.md` (auth, wallet, Anthropic proxy, Stripe, full-data serving)
**Stack:** FastAPI · PostgreSQL · Redis · WorkOS AuthKit · Stripe · CDN

---

## 1. Problem statement

The portal's SPHERE AI Agent currently calls Anthropic **directly from the browser** using a user-pasted `sk-ant-` key ([`index.html:_agentStreamCall` ~L4125](portal/index.html#L4125)). This:

- exposes no business model — every user pays Anthropic directly, SPHERE earns nothing;
- forfeits **shared prompt caching** (each BYOK key has its own cache namespace);
- has no identity, no metering, no payments.

We need a backend that authenticates users, holds **one** centralized Anthropic key, proxies the streaming API, meters usage against a **prepaid USD wallet**, and bills via Stripe — without changing the client-side Pyodide analysis model (data never leaves the browser).

---

## 2. Goals and non-goals

### Goals
- WorkOS-backed sign-in (individual researchers now; enterprise SSO later) replacing the API-key box.
- Centralized Anthropic **streaming proxy** with exact 4-field token accounting and preserved `cache_control`.
- Real-time **prepaid wallet**: synchronous balance check → `402` if short → atomic deduction after usage.
- Stripe money-in: $10 trial, credit packs, optional $29/mo subscription, via Checkout + Customer Portal.
- Multi-cost-center metering on one wallet: **AI tokens**, **generation**, **certification**, **data egress**.
- Transparent platform-fee pricing (cache spread = upside, not the foundation).
- Authenticated full-data serving (Phase 2) with `scrna` as parquet + egress metering.
- Minimal, surgical changes to the single-file `index.html`.

### Non-goals (v1)
- Enterprise SAML/SCIM, DPA workflows, PO invoicing (architecture must not preclude; not built now).
- Moving Pyodide/Python server-side. **Analysis stays in the browser, permanently.**
- Rewriting `index.html` into a framework/components.
- Generation/certification *compute* (runs client-side; backend only meters cells).
- Multi-currency, tax/VAT handling beyond what Stripe Checkout provides by default.

---

## 3. Relevant context and constraints

| Constraint | Source | Implication |
|---|---|---|
| Pyodide stays client-side | brief §"Key constraints" | Backend sees chat messages only, never data/results |
| SSE streaming non-negotiable | brief §"Key constraints" | Proxy must pass chunks through unbuffered |
| 4 token fields must be logged | brief §Phase 4 | Capture `message_start` **and** `message_delta` usage |
| `cache_control` already set on system + last tool + 2nd-last user msg | [`index.html:4090-4142`](portal/index.html#L4090) | Proxy must forward these unmodified |
| `index.html` is one 4,884-line file | brief §"Key constraints" | Touch only `agentConnect`, `_agentStreamCall`, report call, `_agentLoadDatasets`, status bar |
| AFS hosts static only | brief §Deployment | Backend on Railway/Render/AWS; CORS to the AFS origin |
| `scrna.csv` = 1.1 GB | `.gitignore`, LFS | Real egress cost; serve parquet + CDN + meter |
| Model id `claude-opus-4-5` is stale | [`index.html:1531`](portal/index.html#L1531), L3394 | Decouple billing from model via `model_rates` table; serve a current model |
| Two direct Anthropic calls exist | [L4125](portal/index.html#L4125) (agent) + [L4634](portal/index.html#L4634) (report) | **Both** must route through the proxy |
| WorkOS access tokens ~5 min TTL | WorkOS AuthKit | One agent turn spans many requests/minutes → browser must refresh |

**Reused patterns:** the existing tool-loop ([`agentLoop` L4247](portal/index.html#L4247)) and SSE parser stay as-is; only the fetch target/headers change. The brief's SQL schemas are adopted with additions below.

---

## 4. Proposed design

### 4.1 Component architecture

```
Browser (index.html, Pyodide — UNCHANGED analysis path)
  │  Sign in → WorkOS AuthKit (hosted redirect)
  │  Bearer <workos access token> on every backend call
  ▼
SPHERE Backend (FastAPI)
  ├─ auth/         WorkOS code exchange, JWKS verify, JIT provision + trial grant
  ├─ wallet/       balance read, reserve/settle, atomic deduct, ledger writes
  ├─ proxy/        /v1/agent — SSE passthrough + usage capture + settle
  ├─ billing/      Stripe Checkout (packs + sub), Customer Portal, webhooks
  ├─ data/         authed file URLs (CDN-signed), egress metering
  └─ ops/          cache-warm heartbeat, DUA intake, admin usage views
  ▼
Postgres (source of truth: users, wallet, ledger, usage)
Redis (JWKS cache, idempotency keys, rate limits, heartbeat lock)
Stripe · WorkOS · CDN(+object store for data files)
```

### 4.2 Data model (Alembic migrations)

```sql
CREATE TABLE users (
  id              BIGSERIAL PRIMARY KEY,
  workos_user_id  TEXT UNIQUE NOT NULL,
  email           TEXT NOT NULL,
  created_at      TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE billing (
  user_id            BIGINT PRIMARY KEY REFERENCES users(id),
  stripe_customer_id TEXT UNIQUE,
  stripe_sub_id      TEXT,
  sub_status         TEXT,                       -- active|past_due|canceled|null
  sub_period_end     TIMESTAMPTZ,
  credit_balance_usd NUMERIC(12,6) NOT NULL DEFAULT 0,   -- can go slightly negative on settle
  reserved_usd       NUMERIC(12,6) NOT NULL DEFAULT 0,   -- in-flight holds
  trial_used         BOOLEAN NOT NULL DEFAULT FALSE
);

CREATE TABLE credit_ledger (
  id            BIGSERIAL PRIMARY KEY,
  user_id       BIGINT REFERENCES users(id),
  ts            TIMESTAMPTZ DEFAULT now(),
  delta_usd     NUMERIC(12,6) NOT NULL,          -- + credit, - debit
  balance_after NUMERIC(12,6) NOT NULL,
  type          TEXT NOT NULL,                    -- trial_grant|credit_pack|subscription_grant
                                                  -- |ai_usage|generation|certify|data_egress|refund
  description   TEXT,
  stripe_pi_id  TEXT,
  api_usage_id  BIGINT REFERENCES api_usage_log(id),
  idempotency_key TEXT UNIQUE                     -- dedupe webhook/grant double-apply
);

CREATE TABLE api_usage_log (
  id                    BIGSERIAL PRIMARY KEY,
  user_id               BIGINT REFERENCES users(id),
  session_id            TEXT,
  request_id            TEXT UNIQUE,             -- our id; ties reserve→settle
  ts                    TIMESTAMPTZ DEFAULT now(),
  model                 TEXT,
  input_tokens          INTEGER,
  cache_creation_tokens INTEGER,
  cache_read_tokens     INTEGER,
  output_tokens         INTEGER,
  billed_input_tokens   INTEGER,                 -- input+cache_creation+cache_read
  billed_output_tokens  INTEGER,
  user_charge_usd       NUMERIC(12,6),
  sphere_cost_usd       NUMERIC(12,6),
  margin_usd            NUMERIC(12,6)
);

CREATE TABLE model_rates (                        -- NEVER hardcode rates
  model              TEXT NOT NULL,
  input_rate         NUMERIC,                     -- user-facing $/token
  output_rate        NUMERIC,
  platform_mult      NUMERIC NOT NULL DEFAULT 1.3,-- transparent fee (locked: 1.3×)
  sphere_input_rate  NUMERIC,                     -- our negotiated $/token
  sphere_output_rate NUMERIC,
  cache_write_mult   NUMERIC DEFAULT 1.25,        -- 5-min TTL (2.0 for 1-hr TTL)
  cache_read_mult    NUMERIC DEFAULT 0.10,
  effective_from     TIMESTAMPTZ DEFAULT now(),
  PRIMARY KEY (model, effective_from)
);

-- Seed (current authoritative Anthropic pricing, 2026-06):
--   sonnet 4.6 (default served): input $3/1M, output $15/1M   → claude-sonnet-4-6
--   opus 4.8   (premium tier):   input $5/1M, output $25/1M   → claude-opus-4-8
--   haiku 4.5  (cheap lookups):  input $1/1M, output $5/1M    → claude-haiku-4-5
-- platform_mult 1.3 on all; cache read 0.10×, cache write 1.25× (5-min TTL).
-- Provider: Anthropic (decided — stack is Anthropic-shaped; Gemini deferrable
-- as a future cheap tier via this same table, not v1).

CREATE INDEX ON credit_ledger (user_id, ts DESC);
CREATE INDEX ON api_usage_log (user_id, ts DESC);
```

### 4.3 Auth flow (WorkOS)

1. Frontend "Sign in" → `GET /auth/login` → 302 to AuthKit hosted UI.
2. AuthKit → `GET /auth/callback?code=...` → backend exchanges code for `{access_token, refresh_token, user}`.
3. **JIT provisioning** (idempotent on `workos_user_id`, single transaction):
   - upsert `users`; if newly created → create Stripe Customer, insert `billing`, and if `NOT trial_used` insert `credit_ledger(+10, 'trial_grant', idempotency_key='trial:<user_id>')` + set `trial_used=true, credit_balance_usd=10`.
4. Backend returns tokens to the browser (stored where `agent_key` lived in `sessionStorage`).
5. **Per-request auth dependency:** verify access JWT against WorkOS JWKS (keys cached in Redis, refreshed on `kid` miss); extract `sub`→`workos_user_id`→`user_id`. Reject `401` on failure.
6. **Refresh:** `POST /auth/refresh` (refresh_token → new access_token). Browser refreshes proactively at ~4 min and reactively on `401` mid-loop, then retries the failed request once.

### 4.4 Wallet: reserve → settle (the integrity core)

Because cost is unknown until a turn completes and **an SSE stream can't be un-sent**, use **reserve-then-settle** rather than naive post-deduction:

- **Pre-flight (before forwarding):** compute the turn ceiling `RESERVE = (input_tokens·input_rate + max_output_tokens·output_rate)·platform_mult`, where `input_tokens` is the **counted size of the outbound request** (system + tools + messages) — not a fixed allowance. Since output is hard-capped at `max_output_tokens` and every input token is billed at full rate (cache status only affects SPHERE's cost), this hold is provably ≥ the settled charge, including large-cached-prefix agent turns. (`reserve_estimate(..., input_tokens=...)`; a fixed fallback is used only if the count is unavailable.) In one `FOR UPDATE` txn: if `credit_balance_usd - reserved_usd < RESERVE` → **402**; else `reserved_usd += RESERVE`, write a pending `api_usage_log(request_id)`.
- **Settle (after usage known):** in one `FOR UPDATE` txn: compute actual `user_charge`; `reserved_usd -= RESERVE`; `credit_balance_usd -= user_charge`; finalize `api_usage_log`; insert `credit_ledger(-user_charge, 'ai_usage', api_usage_id, idempotency_key=request_id)`.
- **Floor:** a small `MIN_BALANCE` (e.g. $0) — balance may settle marginally negative on one unusually large turn (acceptable; next request blocks at pre-flight). This is the deliberate trade vs. aborting a paid Anthropic call mid-stream.

All other cost centers (generation/certify/egress) use the **same `FOR UPDATE` deduct helper**, just without the reserve (cost is known up front).

### 4.5 Anthropic streaming proxy — `POST /v1/agent`

Request body: `{ model, system, tools, messages, max_tokens, stream:true }` (identical to today's payload).

```
1. auth dependency → user_id
2. resolve rate row from model_rates (reject 400 if model not priced)
3. wallet.reserve(user_id, RESERVE)  → 402 if insufficient
4. open httpx stream POST → https://api.anthropic.com/v1/messages
     headers: x-api-key=SPHERE_KEY, anthropic-version, anthropic-beta: prompt-caching-*
     body: forwarded VERBATIM (cache_control preserved)
5. async-generator passthrough: yield each SSE line to the client unbuffered,
   while tee-ing two events:
     - message_start.message.usage → input_tokens, cache_creation, cache_read
     - message_delta.usage         → output_tokens (final, cumulative)
6. on stream end (or client disconnect): wallet.settle(request_id, actual usage)
7. append one trailing SSE event `event: sphere_balance\ndata: {"balance_usd":N}`
   before [DONE] so the client can update the status bar.
```

**Critical usage-capture detail:** in Anthropic streaming, **input/cache token counts arrive in `message_start`**, and **`output_tokens` in the final `message_delta`** — capture both; relying on `message_delta` alone loses the cache fields and undercharges.

Billing math (from `model_rates`):
```
billed_input  = input + cache_creation + cache_read
user_charge   = (billed_input·input_rate + output·output_rate) · platform_mult
sphere_cost   = input·s_input + cache_creation·s_input·1.25
              + cache_read·s_input·0.10 + output·s_output
margin        = user_charge - sphere_cost
```

The **report-generation call** ([L4634](portal/index.html#L4634)) routes through the same proxy (non-stream variant `/v1/agent` with `stream:false`, or reuse with a flag).

### 4.6 Stripe (money-in)

- **Customer**: created during JIT provisioning.
- **Credit packs**: `POST /billing/checkout/pack {amount}` → Stripe **Checkout `payment` mode** session → redirect. Credits granted on `checkout.session.completed` webhook (not client redirect).
- **Subscription**: `POST /billing/checkout/subscribe` → Checkout `subscription` mode. (Ship behind a flag; PAYG-first.)
- **Manage**: `POST /billing/portal` → Stripe **Customer Portal** session (card/sub/invoices — zero custom UI).
- **Webhooks** `POST /billing/webhook` (verify via `stripe.Webhook.construct_event`; every grant carries `idempotency_key = event.id`):
  | Event | Action |
  |---|---|
  | `checkout.session.completed` (mode=payment) | grant pack credits |
  | `invoice.payment_succeeded` | grant +$20 AI + allowances, set `sub_status=active`, `sub_period_end` |
  | `invoice.payment_failed` | `sub_status=past_due`, email, restrict after grace |
  | `customer.subscription.deleted` | `sub_status=canceled`, drop allowances |

### 4.7 Data serving + egress (Phase 2)

- Files in **Cloudflare R2** (zero egress fees; Cloudflare already connected) behind signed URLs; `GET /data/{modality}` → short-lived **signed R2 URL** (auth required).
- Convert `scrna.csv` → `scrna.parquet` (~50 MB, 20× smaller; build step alongside `inject_data.py`); support `?genes=` column filtering.
- **Egress is effectively free** on R2 + parquet (raw CSV on AWS ≈ $0.10/download; parquet on R2 ≈ $0). **Decision: do NOT meter `data_egress` in v1** — just **gate full-data download behind auth + any payment** (`trial_used` or a positive ledger entry) to stop free-tier abuse. `data_egress` stays in the `ledger.type` enum for the future but is unused now.
- Frontend: [`_agentLoadDatasets` L3824](portal/index.html#L3824) swaps `window.ADRC_DATA[key]` for `fetch(signedUrl)` → Pyodide FS. Nothing else in the analysis path changes.

### 4.8 Frontend changes (surgical)

| Location | Change |
|---|---|
| [`agentConnect` L3626](portal/index.html#L3626), key input L1505 | Replace with "Sign in" → `/auth/login`; store access token in `sessionStorage` (reuse `_agentKey`) |
| [`_agentStreamCall` L4125](portal/index.html#L4125) | URL → `${API}/v1/agent`; header `x-api-key`→`Authorization: Bearer`; drop `anthropic-dangerous-direct-browser-access` |
| Report call [L4634](portal/index.html#L4634) | Same repoint |
| Status bar | Render balance from `sphere_balance` SSE event; `<$2` warning; `402`→"Add credits"→Checkout |
| Token refresh | Proactive ~4 min + reactive on 401 |
| Model select L1531, `_agentModel` L3394, `n=682` L3502 | Fix stale model id + n-count while here |

---

## 5. Edge cases and failure handling

| Case | Handling |
|---|---|
| Access token expires mid agent-loop | 401 → silent refresh → retry once; only surface error if refresh fails |
| Insufficient credits at pre-flight | `402` before any Anthropic call; frontend opens Checkout. **Fail closed.** |
| Anthropic call fails after reserve | release reserve, no charge, no ledger debit; bubble error SSE |
| Client disconnects mid-stream | server still drains usage from upstream and settles actual cost (user consumed tokens) |
| Usage missing from stream (parse miss) | settle with conservative estimate = RESERVE; flag row for reconciliation; alert |
| Concurrent requests / multi-tab | per-user `FOR UPDATE` serializes reserve/settle; reserved_usd prevents double-spend |
| Webhook replay / double-delivery | `idempotency_key` UNIQUE on ledger → second insert no-ops |
| Stripe webhook before client redirect (or vice-versa) | webhook is sole source of truth for grants; redirect only navigates UI |
| JIT race (two first-requests) | upsert + `idempotency_key='trial:<user_id>'` → trial granted once |
| Model not in `model_rates` | `400` at proxy entry — never serve an unpriced model |
| `cache_control` accidentally stripped | contract test asserts forwarded body is byte-identical in cached fields |
| Negative balance after big turn | allowed once; next pre-flight blocks; never silently keep serving |
| Large data pull abuse on free tier | egress gated behind payment/trial; signed URLs short-lived & single-use |

**Fail-closed defaults:** unpriced model, failed auth, failed reserve, missing trial idempotency → deny. Settlement always errs toward charging (RESERVE) on ambiguity, reconciled later.

---

## 6. Scalability and extensibility notes

- **Grows with traffic:** the proxy (I/O-bound — use async `httpx` streaming, no buffering) and `api_usage_log` (partition by month later; index on `(user_id, ts)`).
- **Wallet hotspot:** per-user row lock is fine to thousands of users; if a single account ever fans out concurrently, shard reserves or move to an append-only ledger with computed balance.
- **`model_rates` table** makes Anthropic price changes / model swaps config-only — no redeploy. Set it to whatever current model you serve (Opus 4.8 / Sonnet 4.6), not the stale `4-5` id.
- **One wallet, many cost centers:** `credit_ledger.type` already spans AI/generation/certify/egress/refund — new SPHERE products (new modalities, new datasets) just add a `type`, no schema change.
- **Enterprise path:** WorkOS connections + a `org_id` on `users`/`billing` later; the wallet primitive serves enterprise (PO top-up) unchanged.
- **Cache spread as upside:** `platform_mult` decouples headline margin from cache warmth, so low-traffic cold-cache periods don't make you unprofitable; margin tracking (`sphere_cost`/`margin`) still logged per request for when traffic warms the cache.
- **Heartbeat** (4-min cache-warm ping, Redis lock so only one instance runs it; stop after 30-min idle) is independent and removable.

---

## 7. Verification strategy

**Automated**
- *Unit:* billing math vs. the brief's worked examples (warm ≈40% margin; cold ≈−8%); reserve/settle arithmetic; rate resolution.
- *Concurrency:* N parallel `/v1/agent` for one user with low balance → exactly one succeeds past pre-flight, no double-spend, `reserved_usd` returns to 0.
- *Idempotency:* replay each Stripe webhook ×3 → single grant; replay trial JIT → single grant.
- *Contract:* forwarded Anthropic body preserves `cache_control` on system + last tool + 2nd-last user message (byte-diff test).
- *Usage capture:* feed a recorded SSE fixture with `message_start` + `message_delta` → all 4 fields parsed; a fixture missing usage → reconciliation flag set.

**Manual / focused**
- **SSE passthrough**: real query in the portal shows the live typing effect (no buffering regression) — the highest-risk regression.
- Token expiry mid-analysis: force a 5-min+ multi-tool run → no visible 401.
- 402 → Checkout → balance updates → query succeeds, end to end (Stripe test mode).
- Stripe **test→live** cutover with separate webhook endpoints.
- Reconcile a day of `api_usage_log.sphere_cost` against the Anthropic console.

**Most likely regressions:** (1) buffering the SSE stream and killing the typing effect; (2) losing cache token fields by reading only `message_delta`; (3) stripping `cache_control` in the forward; (4) double-granting on webhook replay. Each has a dedicated test above.

---

## 8. Decisions & remaining open questions

**Resolved (2026-06-06):**
1. **Platform fee** — `platform_mult = 1.3×` (transparent, applied in `model_rates`).
2. **Provider/model** — **Anthropic**. Serve **Sonnet 4.6** (`claude-sonnet-4-6`, $3/$15) as default; **Opus 4.8** (`claude-opus-4-8`, $5/$25) as premium tier; **Haiku 4.5** for cheap lookups. Fix stale `claude-opus-4-5` in the frontend. Gemini deferred to a possible future cheap tier via `model_rates`.
3. **Egress** — **not metered in v1**. Parquet + Cloudflare R2 ⇒ ~$0 egress; gate full-data download behind auth + any payment.
4. **Subscription** — **ship both at launch**: $29/mo subscription *and* PAYG credit packs.
5. **Reserve ceiling** — derived from the **counted request input** + `max_output_tokens` (not a fixed allowance), guaranteeing the hold covers the settled charge for large-context turns. Fixed fallback only when the count is unavailable.
6. **Generation/certify metering** — **out of scope.** This portal does AI Q&A over pre-generated data only; it never generates, so there are no cells to meter. (Revisit if SPHERE's generation product later shares this wallet.)

**Still open:**
7. **DUA intake** — fold into this backend (DB + PI email) now, or leave the Google Apps Script `DUA_ENDPOINT` until Phase 2?
```
