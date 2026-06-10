// fn:balance — SDK balance() endpoint (§4.7). HTTP trigger: auth NONE.
// Authenticates via sphere_sk_ exactly like fn:gateway; service-role ctx.db.
// (Helpers duplicated from gateway.ts — deployed functions are single files.)

const TRIAL_MICRO = 10_000_000;

const json = (status: number, body: unknown) =>
  new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });
const err = (status: number, type: string, code: string) =>
  json(status, { error: { type, code, message: code } });

async function sha256Hex(s: string): Promise<string> {
  const d = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(s));
  return [...new Uint8Array(d)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

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
  if (req.method !== "GET") return err(405, "invalid_request_error", "method_not_allowed");
  const auth = req.headers.get("authorization") ?? "";
  const presented = auth.startsWith("Bearer ") ? auth.slice(7) : (req.headers.get("x-sphere-key") ?? "");
  if (!presented.startsWith("sphere_sk_")) return err(401, "authentication_error", "missing_credentials");
  const krow = await ctx.db.query(
    `SELECT user_id FROM api_keys WHERE key_hash = $1 AND revoked_at IS NULL`,
    [await sha256Hex(presented)],
  );
  if (!krow.rows.length) return err(401, "authentication_error", "invalid_api_key");
  const userId = krow.rows[0].user_id;

  await ensureWallet(ctx.db, userId);
  const r = await ctx.db.query(`SELECT balance_microcents FROM wallets WHERE user_id = $1`, [userId]);
  const micro = Number(r.rows[0].balance_microcents);
  return json(200, { balance_microcents: micro, balance_usd: micro / 1_000_000 });
}

export default handler;
