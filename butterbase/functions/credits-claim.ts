// fn:credits-claim — wallet crediting for paid checkout orders (§4.6, amended).
// HTTP trigger: auth NONE (service-role ctx.db), caller identity proven by
// forwarding their end-user JWT to the platform.
//
// Why claim-based instead of the §4.6 fn:stripe-webhook: Butterbase consumes
// the Stripe Connect webhooks itself and exposes the result as order status
// (pending -> paid/failed/refunded). We never see the webhook; we *observe*
// platform-verified payment state. Idempotency therefore moves from webhook
// replay to claim replay: credit_orders.stripe_session_id (the platform order
// id) is UNIQUE, so a paid order credits the wallet exactly once, ever.
//
//   GET ?order_id=<uuid>   Authorization: Bearer <end-user JWT>
//     -> { credited: bool, amount_microcents, balance_microcents }
//
// Ownership: orders are fetched WITH THE CALLER'S JWT — the platform scopes
// /billing/orders to the signed-in user, so a caller can only ever claim
// their own orders.

const MICRO_PER_CENT = 10_000; // 1¢ = 10_000 micro-units (1e-6 USD ledger)

const json = (status: number, body: unknown) =>
  new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });
const err = (status: number, type: string, code: string, message = code) =>
  json(status, { error: { type, code, message } });

export async function handler(req: Request, ctx: any): Promise<Response> {
  if (req.method !== "GET") return err(405, "invalid_request_error", "method_not_allowed");
  const auth = req.headers.get("authorization") ?? "";
  if (!auth.startsWith("Bearer ")) return err(401, "authentication_error", "missing_credentials");
  const orderId = new URL(req.url).searchParams.get("order_id");
  if (!orderId) return err(400, "invalid_request_error", "missing_order_id");

  const api = ctx.env.BUTTERBASE_API_URL;
  const app = ctx.env.BUTTERBASE_APP_ID;

  // Resolve the caller from their JWT (platform-verified).
  const meRes = await fetch(`${api}/auth/${app}/me`, { headers: { authorization: auth } });
  if (!meRes.ok) return err(401, "authentication_error", "invalid_token");
  const me = await meRes.json();
  const userId = me.id ?? me.user?.id;
  if (!userId) return err(401, "authentication_error", "invalid_token");

  // Fetch the order with the caller's own JWT — scoped to their account.
  const oRes = await fetch(`${api}/v1/${app}/billing/orders/${orderId}`, { headers: { authorization: auth } });
  if (oRes.status === 404) return err(404, "invalid_request_error", "order_not_found");
  if (!oRes.ok) return err(502, "api_error", "orders_unavailable");
  const order = await oRes.json();
  const o = order.order ?? order;
  if (o.status !== "paid") {
    return json(200, { credited: false, reason: `order_${o.status ?? "unknown"}` });
  }

  // Credit amount: product metadata.credit_microcents wins; else priceCents.
  let amount = Number(o.metadata?.credit_microcents ?? o.product?.metadata?.credit_microcents ?? 0);
  if (!amount) {
    const cents = Number(o.priceCents ?? o.amountCents ?? o.product?.priceCents ?? 0);
    amount = cents * MICRO_PER_CENT;
  }
  if (!Number.isInteger(amount) || amount <= 0) return err(502, "api_error", "order_amount_unresolved");

  // Exactly-once: the UNIQUE order id is the guard; only the inserting request credits.
  const ins = await ctx.db.query(
    `INSERT INTO credit_orders (user_id, stripe_session_id, amount_microcents, status)
     VALUES ($1, $2, $3, 'paid')
     ON CONFLICT (stripe_session_id) DO NOTHING
     RETURNING id`,
    [userId, o.id ?? orderId, amount],
  );
  if (ins.rows.length) {
    await ctx.db.query(
      `INSERT INTO wallets (user_id, balance_microcents) VALUES ($1, $2)
       ON CONFLICT (user_id) DO UPDATE
         SET balance_microcents = wallets.balance_microcents + $2, updated_at = now()`,
      [userId, amount],
    );
  }
  const bal = await ctx.db.query(`SELECT balance_microcents FROM wallets WHERE user_id = $1`, [userId]);
  return json(200, {
    credited: ins.rows.length > 0,
    amount_microcents: amount,
    balance_microcents: Number(bal.rows[0]?.balance_microcents ?? 0),
  });
}

export default handler;
