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

export async function handler(req: Request, ctx: any): Promise<Response> {
  if (req.method !== "POST") return json(405, { ok: false });
  let p: any;
  try { p = await req.json(); } catch { return json(400, { ok: false }); }
  const uid = p?.user?.id;
  const email = typeof p?.user?.email === "string" ? p.user.email.toLowerCase() : null;
  if (typeof uid !== "string" || !UUID_RE.test(uid)) return json(400, { ok: false });

  const claimed = await ctx.db.query(
    `INSERT INTO trial_grants (user_id, email) VALUES ($1, $2)
     ON CONFLICT (user_id) DO NOTHING RETURNING user_id`,
    [uid, email],
  );
  if (claimed.rows.length) {
    await ctx.db.query(
      `INSERT INTO wallets (user_id, balance_microcents) VALUES ($1, $2)
       ON CONFLICT (user_id) DO UPDATE
         SET balance_microcents = wallets.balance_microcents + $2, updated_at = now()`,
      [uid, TRIAL_MICRO],
    );
  }
  return json(200, { ok: true, granted: claimed.rows.length > 0 });
}

export default handler;
