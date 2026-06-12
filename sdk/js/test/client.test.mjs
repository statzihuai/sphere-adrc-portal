// Unit tests for @sphere/sdk against a local fake gateway (node:test, zero deps).
import { test, before, after } from "node:test";
import assert from "node:assert/strict";
import http from "node:http";

import Sphere, {
  InsufficientCreditsError,
  InvalidKeyError,
  InvalidRequestError,
  ModelNotFoundError,
} from "../dist/index.js";

const CHAT_OK = {
  id: "chatcmpl-1",
  model: "anthropic/claude-3-haiku",
  choices: [{ index: 0, message: { role: "assistant", content: "pong" }, finish_reason: "stop" }],
  usage: { prompt_tokens: 9, completion_tokens: 1, total_tokens: 10 },
};

let server, baseURL;
const seen = [];

before(async () => {
  server = http.createServer((req, res) => {
    let chunks = [];
    req.on("data", (c) => chunks.push(c));
    req.on("end", () => {
      const body = chunks.length ? JSON.parse(Buffer.concat(chunks)) : null;
      seen.push({ path: req.url, auth: req.headers.authorization, body });
      const send = (status, obj) => {
        res.writeHead(status, { "content-type": "application/json" });
        res.end(JSON.stringify(obj));
      };
      if (req.method === "POST") {
        if (req.headers.authorization !== "Bearer sphere_sk_good")
          return send(401, { error: { type: "authentication_error", code: "invalid_api_key", message: "invalid_api_key" } });
        if (body.model === "nope/none")
          return send(404, { error: { type: "invalid_request_error", code: "model_not_found", message: "unknown model" } });
        if ((body.max_tokens ?? 0) > 10_000)
          return send(402, { error: { type: "billing_error", code: "insufficient_credits", message: "insufficient_credits" } });
        return send(200, CHAT_OK);
      }
      if (req.url.endsWith("/balance")) return send(200, { balance_microcents: 9_830_000, balance_usd: 9.83 });
      if (req.url.endsWith("/v1/public/models"))
        return send(200, { models: [{ id: "anthropic/claude-3-haiku", inputPricePerMTokens: 0.24, outputPricePerMTokens: 1.2 }] });
      return send(404, { error: { code: "not_found" } });
    });
  });
  await new Promise((r) => server.listen(0, "127.0.0.1", r));
  baseURL = `http://127.0.0.1:${server.address().port}/v1/app_test/fn`;
});

after(() => server.close());

const client = () => new Sphere({ apiKey: "sphere_sk_good", baseURL });

test("chat completion happy path + auth header + payload", async () => {
  const r = await client().chat.completions.create({
    model: "anthropic/claude-3-haiku",
    messages: [{ role: "user", content: "ping" }],
    temperature: 0.2,
  });
  assert.equal(r.choices[0].message.content, "pong");
  assert.equal(r.usage.total_tokens, 10);
  const last = seen.at(-1);
  assert.ok(last.path.endsWith("/gateway"));
  assert.equal(last.auth, "Bearer sphere_sk_good");
  assert.equal(last.body.temperature, 0.2);
});

test("402 -> InsufficientCreditsError", async () => {
  await assert.rejects(
    client().chat.completions.create({ model: "m/x", messages: [], max_tokens: 99_999 }),
    (e) => e instanceof InsufficientCreditsError && e.status === 402 && e.code === "insufficient_credits",
  );
});

test("401 -> InvalidKeyError", async () => {
  const bad = new Sphere({ apiKey: "sphere_sk_wrong", baseURL });
  await assert.rejects(
    bad.chat.completions.create({ model: "m/x", messages: [] }),
    (e) => e instanceof InvalidKeyError && e.status === 401,
  );
});

test("404 -> ModelNotFoundError", async () => {
  await assert.rejects(
    client().chat.completions.create({ model: "nope/none", messages: [] }),
    (e) => e instanceof ModelNotFoundError,
  );
});

test("stream rejected client-side before any network call", () => {
  const n = seen.length;
  assert.throws(
    () => client().chat.completions.create({ model: "m/x", messages: [], stream: true }),
    (e) => e instanceof InvalidRequestError && e.code === "stream_unsupported",
  );
  assert.equal(seen.length, n);
});

test("balance()", async () => {
  const b = await client().balance();
  assert.equal(b.balance_usd, 9.83);
  assert.equal(b.balance_microcents, 9_830_000);
});

test("models.list()", async () => {
  const models = await client().models.list();
  assert.equal(models[0].id, "anthropic/claude-3-haiku");
});

test("key format required at construction", () => {
  assert.throws(() => new Sphere({ apiKey: "bb_sk_nope" }), (e) => e instanceof InvalidKeyError);
});
