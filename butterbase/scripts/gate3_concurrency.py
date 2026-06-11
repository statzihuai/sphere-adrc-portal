#!/usr/bin/env python3
"""Gate 3 (§7.3): wallet invariant under concurrency.

Phase A (always): a single call whose worst-case reserve exceeds the balance
  must 402 — the WHERE-guard is the only thing between us and overdraft.
Phase B (always): N simultaneous calls sized so ~1 reserve fits. Asserts the
  integrity invariants that hold regardless of refund timing:
    - final balance ≥ 0                                   [never negative]
    - final == start − Σ(usage-derived cost of settled calls)  [conservation]
  In refund mode (OWNER_GATEWAY_KEY unset → every upstream call fails) the sum
  is zero, so final must be bit-identical to start.
Phase C (settled mode only, auto-detected by a 200 with usage): asserts the
  402 guard actually fired under contention — real model latency (~1s) holds
  reserves open, so N=16 calls against ~1 affordable reserve must contend.

Note on §7.3's "exactly M successes": strict only while reserves are held;
refunds let late arrivals legitimately succeed. Phase C asserts the guard
fires; conservation (B) is the exact accounting property in both modes.

Env: SPHERE_TEST_EMAIL2 / SPHERE_TEST_PASSWORD (user is created on first run).
"""
import asyncio, json, math, os, urllib.request

API = os.environ.get("BUTTERBASE_API", "https://api.butterbase.ai")
APP = os.environ.get("BUTTERBASE_APP", "app_21ze8d0ep28o")
EMAIL = os.environ["SPHERE_TEST_EMAIL2"]
PASSWORD = os.environ["SPHERE_TEST_PASSWORD"]
# max_tokens must clear THREE ceilings or upstream rejects and every call lands
# in the refund path (no settles): the app's maxTokensPerRequest cap, the
# model's own output limit, and — discovered live — the PLATFORM's worst-case
# credit check against the owner account (OWNER_AFFORDABLE_USD, default $0.90).
MODEL = "anthropic/claude-fable-5"
OUT_MICRO_PER_TOK = 48
OWNER_AFFORDABLE_USD = float(os.environ.get("OWNER_AFFORDABLE_USD", "0.9"))
MAX_TOKENS_CEILING = int(OWNER_AFFORDABLE_USD * 1_000_000) // OUT_MICRO_PER_TOK

def http(path, body=None, headers=None, method=None):
    req = urllib.request.Request(
        API + path,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json", **(headers or {})},
        method=method,
    )
    def body_of(raw):
        try:
            b = json.loads(raw or b"{}")
        except ValueError:
            b = {}
        return b if isinstance(b, dict) else {}  # edge can return bare JSON strings
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, body_of(r.read())
    except urllib.error.HTTPError as e:
        return e.code, body_of(e.read())

def setup_key():
    code, login = http(f"/auth/{APP}/login", {"email": EMAIL, "password": PASSWORD})
    if code != 200:
        code, _ = http(f"/auth/{APP}/signup", {"email": EMAIL, "password": PASSWORD})
        assert code == 201, f"signup failed: {code}"
        code, login = http(f"/auth/{APP}/login", {"email": EMAIL, "password": PASSWORD})
        assert code == 200, f"login failed: {code}"
    tok = login["access_token"]
    code, mint = http(f"/v1/{APP}/fn/keys", {"name": "gate3"}, {"Authorization": f"Bearer {tok}"})
    assert code == 201, f"mint failed: {code} {mint}"
    return mint["key"]

def balance(key):
    code, b = http(f"/v1/{APP}/fn/balance", headers={"Authorization": f"Bearer {key}"}, method="GET")
    assert code == 200, f"balance failed: {code} {b}"
    return b["balance_microcents"]

def catalog_prices(model):
    cat = json.load(urllib.request.urlopen(f"{API}/v1/public/models"))
    m = next(x for x in cat["models"] if x["id"] == model)
    return round(m["inputPricePerMTokens"] * 1e6), round(m["outputPricePerMTokens"] * 1e6)

def cost(usage, in_m, out_m):
    return math.ceil(usage["prompt_tokens"] * in_m / 1e6) + math.ceil(usage["completion_tokens"] * out_m / 1e6)

async def main():
    key = setup_key()
    start = balance(key)
    print(f"start balance: {start} micro-units")

    # Phase A — reserve > balance must 402.
    over = start // OUT_MICRO_PER_TOK + 1000
    code, body = http(f"/v1/{APP}/fn/gateway",
                      {"model": MODEL, "max_tokens": over, "messages": [{"role": "user", "content": "hi"}]},
                      {"Authorization": f"Bearer {key}"})
    assert code == 402 and body["error"]["code"] == "insufficient_credits", f"A FAIL: {code} {body}"
    assert balance(key) == start, "A FAIL: 402 path must not touch the balance"
    print("ok: phase A — oversized reserve -> 402, balance untouched")

    # Phase B — N concurrent, sized so contention is guaranteed: more in-flight
    # calls than reserves that fit, with N scaled to the fit count.
    max_tokens = min(MAX_TOKENS_CEILING, max(1, int(start * 0.45) // OUT_MICRO_PER_TOK - 200))
    req_body = {"model": MODEL, "max_tokens": max_tokens, "messages": [{"role": "user", "content": "hi"}]}
    fits = start // (max_tokens * OUT_MICRO_PER_TOK)
    N = min(32, max(16, int(fits) * 2 + 4))
    print(f"phase B: N={N} concurrent, max_tokens={max_tokens} (~{fits} reserves fit)")

    def call():
        return http(f"/v1/{APP}/fn/gateway", req_body, {"Authorization": f"Bearer {key}"})

    results = await asyncio.gather(*[asyncio.to_thread(call) for _ in range(N)])

    def err_code(b):
        e = b.get("error")
        return e.get("code") if isinstance(e, dict) else None  # `error` can be a bare string

    n402 = sum(1 for c, b in results if c == 402 and err_code(b) == "insufficient_credits")
    settled = [(c, b) for c, b in results if c == 200 and "usage" in b]
    for c, b in results:
        assert c in (200, 401, 402, 502), f"B FAIL: unexpected status {c}: {b}"
    print(f"  settled: {len(settled)}, reserve-then-refunded: {N - n402 - len(settled)}, 402: {n402}")

    final = balance(key)
    assert final >= 0, f"B FAIL: balance negative: {final}"
    in_m, out_m = catalog_prices(MODEL)
    spent = sum(cost(b["usage"], in_m, out_m) for _, b in settled)
    assert final == start - spent, f"B FAIL: conservation broken: {start} - {spent} != {final}"
    mode = "settled" if settled else "refund"
    print(f"ok: phase B — conservation exact in {mode} mode ({start} - {spent} == {final}), never negative")

    # Phase C — only meaningful when reserves are held by real model latency.
    if settled:
        assert n402 >= 1, "C FAIL: settled mode but guard never fired under 16-way contention"
        print(f"ok: phase C — guard fired {n402}x under contention")
    else:
        print("skip: phase C — no calls settled (owner key unset, or model/params rejected upstream)")
    print("gate 3 PASS")

asyncio.run(main())
