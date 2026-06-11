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

// Claim + credit in ONE statement (one transaction) — see gateway.ts ensureWallet.
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

export async function handler(req: Request, ctx: any): Promise<Response> {
  try {
    return await handle(req, ctx);
  } catch (e) {
    console.error("unhandled balance error:", e);
    return err(500, "api_error", "internal_error"); // never leak stack traces
  }
}

export default handler;
