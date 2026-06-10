// fn:gateway — the metered OpenAI-compatible proxy (§4.5). HTTP trigger: auth NONE.
// ctx.db runs as butterbase_service; the tables are RLS-locked to everyone else,
// so this function is the only public path to the wallet. We authenticate callers
// ourselves via sphere_sk_ keys (Authorization: Bearer, x-sphere-key fallback).
//
// Money flow per request (§4.4): reserve worst-case -> upstream -> settle actual.
// Invariant: balance never goes negative — the guard is in the UPDATE's WHERE.

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
// fire-and-forget, so the first gateway call repairs a missed grant. Both
// paths race-safe via ON CONFLICT; trial_grants PK = one trial per account.
async function ensureWallet(db: any, userId: string) {
  const w = await db.query(`SELECT 1 FROM wallets WHERE user_id = $1`, [userId]);
  if (w.rows.length) return;
  const claimed = await db.query(
    `INSERT INTO trial_grants (user_id) VALUES ($1) ON CONFLICT (user_id) DO NOTHING RETURNING user_id`,
    [userId],
  );
  await db.query(
    `INSERT INTO wallets (user_id, balance_microcents) VALUES ($1, $2) ON CONFLICT (user_id) DO NOTHING`,
    [userId, claimed.rows.length ? TRIAL_MICRO : 0],
  );
}

export async function handler(req: Request, ctx: any): Promise<Response> {
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

  const refund = (amt: number) =>
    ctx.db.query(
      `UPDATE wallets SET balance_microcents = balance_microcents + $1, updated_at = now() WHERE user_id = $2`,
      [amt, userId],
    );

  let up: Response, out: any;
  try {
    up = await fetch(`${ctx.env.BUTTERBASE_API_URL}/v1/${ctx.env.BUTTERBASE_APP_ID}/chat/completions`, {
      method: "POST",
      headers: { "content-type": "application/json", authorization: `Bearer ${ctx.env.OWNER_GATEWAY_KEY}` },
      body: JSON.stringify(body),
    });
    out = await up.json();
  } catch {
    await refund(reserve); // upstream unreachable: full reclaim, no usage_log (§5)
    return err(502, "api_error", "upstream_unreachable");
  }
  if (!up.ok || !out?.usage) {
    await refund(reserve); // upstream error: full reclaim, pass the error through (§5)
    return json(up.status, out ?? { error: { type: "api_error", code: "upstream_error", message: "upstream_error" } });
  }

  const inTok = out.usage.prompt_tokens ?? 0;
  const outTok = out.usage.completion_tokens ?? 0;
  const actual = costMicro(inTok, price.inM) + costMicro(outTok, price.outM);
  await refund(reserve - actual); // settle (can only be negative if upstream ignored max_tokens)
  await ctx.db.query(
    `INSERT INTO usage_log (api_key_id, user_id, model, input_tokens, output_tokens, cost_microcents, elapsed_ms)
     VALUES ($1, $2, $3, $4, $5, $6, $7)`,
    [keyId, userId, body.model, inTok, outTok, actual, Date.now() - t0],
  );
  await ctx.db.query(`UPDATE api_keys SET last_used_at = now() WHERE id = $1`, [keyId]);
  return json(200, out); // pass body through unchanged (OpenAI shape)
}

export default handler;
