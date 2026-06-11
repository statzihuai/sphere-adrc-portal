// fn:on-auth — post-auth hook (§8 Q5): eager $10 trial grant.
// Configured via manage_auth_config { action: "configure_auth_hook" }.
// The platform fires this after every successful auth event (fire-and-forget)
// with { event, user: { id, email, ... }, isNewUser, provider }.
//
// Idempotent and unconditional: trial_grants PK is the one-per-account guard,
// so login events after signup (and races with fn:gateway's lazy backstop)
// are no-ops. Forged payloads can at most grant a real user the same one-time
// trial they would get anyway via the backstop.

const TRIAL_MICRO = 10_000_000;
const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

const json = (status: number, body: unknown) =>
  new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });

async function handle(req: Request, ctx: any): Promise<Response> {
  if (req.method !== "POST") return json(405, { ok: false });
  let p: any;
  try { p = await req.json(); } catch { return json(400, { ok: false }); }
  const uid = p?.user?.id;
  const email = typeof p?.user?.email === "string" ? p.user.email.toLowerCase() : null;
  if (typeof uid !== "string" || !UUID_RE.test(uid)) return json(400, { ok: false });

  // Claim + credit in ONE statement (one transaction): a crash can never burn
  // the grant without funding the wallet. The additive ON CONFLICT lets the
  // $10 land even when another path created the wallet row first.
  const r = await ctx.db.query(
    `WITH claim AS (
       INSERT INTO trial_grants (user_id, email) VALUES ($1, $2)
       ON CONFLICT (user_id) DO NOTHING RETURNING 1
     )
     INSERT INTO wallets (user_id, balance_microcents)
     SELECT $1, CASE WHEN EXISTS (SELECT 1 FROM claim) THEN $3::bigint ELSE 0 END
     ON CONFLICT (user_id) DO UPDATE
       SET balance_microcents = wallets.balance_microcents + EXCLUDED.balance_microcents,
           updated_at = now()
     RETURNING (SELECT count(*) FROM claim) AS claimed`,
    [uid, email, TRIAL_MICRO],
  );
  return json(200, { ok: true, granted: Number(r.rows[0]?.claimed ?? 0) > 0 });
}

export async function handler(req: Request, ctx: any): Promise<Response> {
  try {
    return await handle(req, ctx);
  } catch (e) {
    console.error("unhandled on-auth error:", e);
    return json(500, { ok: false }); // never leak stack traces
  }
}

export default handler;
