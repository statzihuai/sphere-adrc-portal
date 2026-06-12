// fn:gateway — the metered OpenAI-compatible proxy (§4.5). HTTP trigger: auth NONE.
// ctx.db runs as butterbase_service; the tables are RLS-locked to everyone else,
// so this function is the only public path to the wallet. We authenticate callers
// ourselves via sphere_sk_ keys (Authorization: Bearer, x-sphere-key fallback).
//
// Money flow per request (§4.4): reserve worst-case -> upstream -> settle actual.
// Invariant: balance never goes negative — the guard is in the UPDATE's WHERE,
// and every post-reserve path resolves the reserve exactly once.

const MICRO_PER_USD = 1_000_000; // §4.2: integer micro-cents, 1e-6 USD
const TRIAL_MICRO = 10_000_000;  // $10 one-per-account trial (§8 Q5)
const DEFAULT_MAX_TOKENS = 1024; // §8 Q3: injected upstream => reserve is an enforced ceiling

const json = (status: number, body: unknown) =>
  new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });
const err = (status: number, type: string, code: string, message = code) =>
  json(status, { error: { type, code, message } });

async function sha256Hex(s: string): Promise<string> {
  const d = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(s));
  return [...new Uint8Array(d)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

// Price catalog from GET /v1/public/models, integerized to micro-USD per 1M
// tokens so all cost math stays exact. Module-scope cache survives warm starts.
let priceCache: { at: number; map: Map<string, { inM: number; outM: number }> | null } = { at: 0, map: null };
async function prices(apiUrl: string) {
  if (priceCache.map && Date.now() - priceCache.at < 300_000) return priceCache.map;
  const r = await fetch(`${apiUrl}/v1/public/models`);
  if (!r.ok) throw new Error(`catalog ${r.status}`);
  const { models } = await r.json();
  const map = new Map();
  for (const m of models) {
    map.set(m.id, {
      inM: Math.round((m.inputPricePerMTokens ?? 0) * MICRO_PER_USD),
      outM: Math.round((m.outputPricePerMTokens ?? 0) * MICRO_PER_USD),
    });
  }
  priceCache = { at: Date.now(), map };
  return map;
}
const costMicro = (tokens: number, perM: number) => Math.ceil((tokens * perM) / 1_000_000);

// Lazy trial backstop (§8 Q5): the post-auth hook grants eagerly, but it is
// fire-and-forget, so the first gateway call repairs a missed grant. Claim and
// credit happen in ONE statement (one transaction), so a crash can never burn
// the grant without funding the wallet; the additive ON CONFLICT makes the
// claim winner's $10 land even if a concurrent loser created the row first.
async function ensureWallet(db: any, userId: string) {
  const w = await db.query(`SELECT 1 FROM wallets WHERE user_id = $1`, [userId]);
  if (w.rows.length) return;
  await db.query(
    `WITH claim AS (
       INSERT INTO trial_grants (user_id) VALUES ($1)
       ON CONFLICT (user_id) DO NOTHING RETURNING 1
     )
     INSERT INTO wallets (user_id, balance_microcents)
     SELECT $1, CASE WHEN EXISTS (SELECT 1 FROM claim) THEN $2::bigint ELSE 0 END
     ON CONFLICT (user_id) DO UPDATE
       SET balance_microcents = wallets.balance_microcents + EXCLUDED.balance_microcents,
           updated_at = now()`,
    [userId, TRIAL_MICRO],
  );
}

async function handle(req: Request, ctx: any): Promise<Response> {
  const t0 = Date.now();
  if (req.method !== "POST") return err(405, "invalid_request_error", "method_not_allowed");

  const auth = req.headers.get("authorization") ?? "";
  const presented = auth.startsWith("Bearer ") ? auth.slice(7) : (req.headers.get("x-sphere-key") ?? "");
  if (!presented.startsWith("sphere_sk_")) return err(401, "authentication_error", "missing_credentials");
  const krow = await ctx.db.query(
    `SELECT id, user_id FROM api_keys WHERE key_hash = $1 AND revoked_at IS NULL`,
    [await sha256Hex(presented)],
  );
  if (!krow.rows.length) return err(401, "authentication_error", "invalid_api_key");
  const { id: keyId, user_id: userId } = krow.rows[0];

  let body: any;
  try { body = await req.json(); } catch { return err(400, "invalid_request_error", "invalid_request", "body must be JSON"); }
  if (body?.stream) return err(400, "invalid_request_error", "stream_unsupported", "stream:true is not supported yet (§5)");
  if (typeof body?.model !== "string") return err(400, "invalid_request_error", "invalid_request", "model is required");
  if (!Array.isArray(body?.messages)) return err(400, "invalid_request_error", "invalid_request", "messages is required");
  body.max_tokens = Number.isInteger(body.max_tokens) && body.max_tokens > 0 ? body.max_tokens : DEFAULT_MAX_TOKENS;

  let price;
  try { price = (await prices(ctx.env.BUTTERBASE_API_URL)).get(body.model); }
  catch { return err(502, "api_error", "catalog_unavailable"); }
  if (!price) return err(404, "invalid_request_error", "model_not_found", `unknown model: ${body.model}`);

  await ensureWallet(ctx.db, userId);

  // Worst case = conservative input estimate (~3 bytes/token) + full max_tokens out.
  const inEst = Math.ceil(new TextEncoder().encode(JSON.stringify(body.messages)).length / 3);
  const reserve = costMicro(inEst, price.inM) + costMicro(body.max_tokens, price.outM);

  const reserved = await ctx.db.query(
    `UPDATE wallets SET balance_microcents = balance_microcents - $1, updated_at = now()
      WHERE user_id = $2 AND balance_microcents >= $1
      RETURNING balance_microcents`,
    [reserve, userId],
  );
  if (!reserved.rows.length) return err(402, "billing_error", "insufficient_credits");

  // ---- reserve is held: nothing below may throw without resolving it. ----
  // GREATEST floors the wallet at 0 for the rare settle where actual > reserve
  // (input under-estimate); usage_log still records the true cost.
  const refund = (amt: number) =>
    ctx.db.query(
      `UPDATE wallets SET balance_microcents = GREATEST(balance_microcents + $1, 0), updated_at = now()
        WHERE user_id = $2`,
      [amt, userId],
    );

  let up: Response | null = null, out: any = null;
  try {
    up = await fetch(`${ctx.env.BUTTERBASE_API_URL}/v1/${ctx.env.BUTTERBASE_APP_ID}/chat/completions`, {
      method: "POST",
      headers: { "content-type": "application/json", authorization: `Bearer ${ctx.env.OWNER_GATEWAY_KEY}` },
      body: JSON.stringify(body),
    });
    out = await up.json();
  } catch {
    up = null;
  }

  if (!up) {
    await refund(reserve); // upstream unreachable: full reclaim, no usage_log (§5)
    return err(502, "api_error", "upstream_unreachable");
  }
  if (up.status === 401 || up.status === 403) {
    // Owner-key failure is OUR misconfiguration — never surface it as the
    // caller's 401, or SDKs would raise InvalidKeyError at a paying customer.
    await refund(reserve);
    console.error("upstream auth failed — OWNER_GATEWAY_KEY misconfigured or revoked");
    return err(502, "api_error", "upstream_misconfigured", "gateway upstream auth failed (operator issue, not your key)");
  }
  if (up.status === 402) {
    // The PLATFORM account is out of credits (it runs its own worst-case
    // check per request) — the caller's wallet is fine. Surfacing 402 here
    // would make SDKs raise InsufficientCreditsError at a funded customer.
    await refund(reserve);
    console.error("upstream 402 — platform account credits exhausted for this request's worst case");
    return err(502, "api_error", "upstream_credits_exhausted", "gateway provider account low on credits (operator issue, not your wallet)");
  }
  if (!up.ok || !out?.usage) {
    await refund(reserve); // upstream error: full reclaim, pass the error through (§5)
    return json(up.status, out ?? { error: { type: "api_error", code: "upstream_error", message: "upstream_error" } });
  }

  const inTok = out.usage.prompt_tokens ?? 0;
  const outTok = out.usage.completion_tokens ?? 0;
  const actual = costMicro(inTok, price.inM) + costMicro(outTok, price.outM);
  if (actual > reserve) console.warn(`settle exceeded reserve by ${actual - reserve} micro (uid=${userId})`);
  try {
    await refund(reserve - actual); // settle
  } catch (e) {
    try { await refund(reserve - actual); } // one retry; then fail toward over-debit, never overdraft
    catch (e2) { console.error(`settle failed twice — user ${userId} over-debited by ${reserve - actual}:`, e2); }
  }
  try {
    await ctx.db.query(
      `INSERT INTO usage_log (api_key_id, user_id, model, input_tokens, output_tokens, cost_microcents, elapsed_ms)
       VALUES ($1, $2, $3, $4, $5, $6, $7)`,
      [keyId, userId, body.model, inTok, outTok, actual, Date.now() - t0],
    );
    await ctx.db.query(`UPDATE api_keys SET last_used_at = now() WHERE id = $1`, [keyId]);
  } catch (e) {
    console.error("usage_log write failed (money already settled):", e);
  }
  return json(200, out); // pass body through unchanged (OpenAI shape)
}

export async function handler(req: Request, ctx: any): Promise<Response> {
  try {
    return await handle(req, ctx);
  } catch (e) {
    console.error("unhandled gateway error:", e);
    return err(500, "api_error", "internal_error"); // never leak stack traces
  }
}

export default handler;
