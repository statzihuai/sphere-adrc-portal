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

## Configuration

| Env var               | Default                              | Purpose                          |
|-----------------------|--------------------------------------|----------------------------------|
| `SPHERE_APP_ENV`      | `development`                        | `development`/`staging`/`production` |
| `SPHERE_CORS_ORIGINS` | Stanford AFS + localhost (see config) | comma-separated allowed origins  |
