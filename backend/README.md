# SPHERE Backend

FastAPI backend for the SPHERE AI Agent: WorkOS auth, a prepaid USD wallet,
an Anthropic streaming proxy with exact token accounting, and Stripe billing.
See [`../BACKEND_DESIGN.md`](../BACKEND_DESIGN.md) for the full design.

Status: scaffold + billing/wallet core. Auth, proxy, and Stripe land in later
slices.

## Layout

```
sphere_backend/
  app.py            create_app() factory + `app` for uvicorn; mounts routers, CORS
  config.py         env-driven Settings (get_settings)
  api/health.py     GET /health (liveness)
  billing/          model rates + token charge/cost/margin + reserve estimate
  wallet/           prepaid wallet: reserve → settle, 402, ledger entries
tests/              pytest suite (pure-logic tests + /health)
```

## Run it locally

Requires Python 3.10+.

```bash
cd backend
python3 -m venv .venv
source .venv/bin/activate            # Windows: .venv\Scripts\activate
pip install -e ".[server,test]"      # FastAPI + uvicorn + httpx + pytest

# run the API
uvicorn sphere_backend.app:app --reload
# → http://127.0.0.1:8000/health  and interactive docs at /docs
```

In another shell:

```bash
curl -s localhost:8000/health
# {"status":"ok","service":"sphere-backend","version":"0.0.1"}
```

## Test

```bash
cd backend
pip install -e ".[server,test]"
pytest                 # runs billing, wallet, AND the /health tests
```

The `/health` tests `importorskip` FastAPI, so `pip install -e ".[test]"` alone
(no server extra) still runs the pure billing/wallet suite and skips the HTTP
tests — handy in minimal CI.

## Auth (WorkOS AuthKit)

The auth endpoints — `GET /auth/login`, `GET /auth/callback`, `POST /auth/refresh`,
`GET /auth/me` — use WorkOS. **Tests need no WorkOS account** (the provider is
mocked and JWTs are verified with a locally-generated keypair). To exercise the
real browser sign-up flow locally:

1. Create a WorkOS account, enable **AuthKit**, and stay in **test mode**.
2. In the WorkOS dashboard, add redirect URI `http://localhost:8000/auth/callback`.
3. `cp .env.example .env` and fill in `WORKOS_API_KEY`, `WORKOS_CLIENT_ID`.
4. Run with the env file:
   ```bash
   uvicorn sphere_backend.app:app --reload --env-file .env
   ```
5. Visit `http://localhost:8000/auth/login` → WorkOS hosted page → on return,
   `/auth/callback` provisions your account (+ $10 trial) and returns tokens.
   Call `GET /auth/me` with `Authorization: Bearer <access_token>` to see the
   balance.

Without WorkOS env vars, `/auth/*` return **503** and the rest of the app runs
normally.

## Configuration

| Env var               | Default                              | Purpose                          |
|-----------------------|--------------------------------------|----------------------------------|
| `SPHERE_APP_ENV`      | `development`                        | `development`/`staging`/`production` |
| `SPHERE_CORS_ORIGINS` | Stanford AFS + localhost (see config) | comma-separated allowed origins  |
| `SPHERE_DATABASE_URL`  | `sqlite+aiosqlite:///./sphere.db`    | async SQLAlchemy URL (Postgres: `postgresql+asyncpg://…`) |
| `WORKOS_API_KEY`       | — (auth disabled)                    | WorkOS secret key (test or live) |
| `WORKOS_CLIENT_ID`     | — (auth disabled)                    | WorkOS client id |
| `WORKOS_REDIRECT_URI`  | `http://localhost:8000/auth/callback` | must match the WorkOS dashboard |
