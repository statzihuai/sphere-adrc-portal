// fn:keys — sphere_sk_ key management (§4.3). HTTP trigger: auth REQUIRED.
// Runs as butterbase_user: RLS (api_keys_user_isolation) scopes every query
// to ctx.user — no manual user_id filtering needed, the DB enforces it.
//
//   POST   {name?}   mint a key; plaintext returned ONCE
//   GET              list own keys (prefix only, never hash or plaintext)
//   DELETE ?id=      revoke (sets revoked_at; fn:gateway rejects from then on)

const json = (status: number, body: unknown) =>
  new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });

const b64url = (bytes: Uint8Array) =>
  btoa(String.fromCharCode(...bytes))
    .replaceAll("+", "-").replaceAll("/", "_").replaceAll("=", "");

async function sha256Hex(s: string): Promise<string> {
  const d = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(s));
  return [...new Uint8Array(d)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

export async function handler(req: Request, ctx: any): Promise<Response> {
  if (!ctx.user) return json(401, { error: { type: "authentication_error", code: "missing_credentials" } });

  if (req.method === "POST") {
    let name: string | null = null;
    try {
      const body = await req.json();
      if (typeof body?.name === "string") name = body.name.slice(0, 100);
    } catch { /* empty body is fine */ }

    const plaintext = "sphere_sk_" + b64url(crypto.getRandomValues(new Uint8Array(32)));
    const keyPrefix = plaintext.slice(0, 15); // "sphere_sk_" + 5 chars, for UI display
    const keyHash = await sha256Hex(plaintext);

    // user_id is auto-populated by the RLS isolation trigger.
    const r = await ctx.db.query(
      `INSERT INTO api_keys (key_hash, key_prefix, name)
       VALUES ($1, $2, $3)
       RETURNING id, key_prefix, name, scopes, created_at`,
      [keyHash, keyPrefix, name],
    );
    return json(201, { ...r.rows[0], key: plaintext });
  }

  if (req.method === "GET") {
    const r = await ctx.db.query(
      `SELECT id, key_prefix, name, scopes, created_at, last_used_at, revoked_at
         FROM api_keys ORDER BY created_at DESC`,
    );
    return json(200, { keys: r.rows });
  }

  if (req.method === "DELETE") {
    const id = new URL(req.url).searchParams.get("id");
    if (!id) return json(400, { error: { type: "invalid_request_error", code: "missing_id" } });
    const r = await ctx.db.query(
      `UPDATE api_keys SET revoked_at = now()
        WHERE id = $1 AND revoked_at IS NULL
       RETURNING id, revoked_at`,
      [id],
    );
    if (r.rows.length === 0) return json(404, { error: { type: "invalid_request_error", code: "key_not_found" } });
    return json(200, r.rows[0]);
  }

  return json(405, { error: { type: "invalid_request_error", code: "method_not_allowed" } });
}

export default handler;
