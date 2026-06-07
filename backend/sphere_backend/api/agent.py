"""The Anthropic streaming proxy — `POST /v1/agent` (BACKEND_DESIGN.md §4.5).

Replaces the browser's direct, BYOK Anthropic call. Flow:

  auth → resolve+price model → pre-flight reserve (402 if short) + pending
  usage row → stream Anthropic's SSE bytes straight to the browser (unbuffered)
  while tee-ing the four token fields → finalize (settle + charge) on success,
  cancel (release hold) on any failure/disconnect → trailing balance event.

The reservation hold is released exactly once: ``finalize`` on the happy path,
``cancel`` in the generator's ``finally`` otherwise (and ``reclaim_stale`` mops
up a hard process crash). Short-lived DB sessions are used for reserve and
settle so no connection is pinned for the stream's duration.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from fastapi.responses import StreamingResponse

from ..auth.dependencies import current_user, get_anthropic_streamer, get_sessionmaker
from ..billing import get_rate, reserve_estimate
from ..billing.rates import UnknownModelError
from ..config import get_settings
from ..db.models import User
from ..proxy.sse import UsageCapture, balance_event, estimate_input_tokens
from ..usage import cancel, finalize, open_reservation
from ..wallet import InsufficientCreditsError, repository

router = APIRouter(tags=["agent"])

_DEFAULT_MAX_TOKENS = 8192


@router.post("/v1/agent")
async def agent(
    request: Request,
    user: User = Depends(current_user),
    streamer=Depends(get_anthropic_streamer),
    sessionmaker=Depends(get_sessionmaker),
    x_sphere_session: str | None = Header(default=None),
) -> StreamingResponse:
    body = await request.json()
    if not isinstance(body, dict) or "messages" not in body:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "missing messages")

    model = body.get("model") or get_settings().default_model
    body["model"] = model
    try:
        rate = get_rate(model)
    except UnknownModelError:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"model not priced: {model}")

    max_output = int(body.get("max_tokens") or _DEFAULT_MAX_TOKENS)
    reserve_amount = reserve_estimate(
        rate, max_output_tokens=max_output, input_tokens=estimate_input_tokens(body)
    )

    request_id = "req_" + uuid.uuid4().hex

    # ── pre-flight: reserve + pending usage row (short-lived session) ──────────
    async with sessionmaker() as session:
        try:
            await open_reservation(
                session,
                user_id=user.id,
                request_id=request_id,
                model=model,
                reserve_amount=reserve_amount,
                session_id=x_sphere_session,
            )
        except InsufficientCreditsError:
            raise HTTPException(status.HTTP_402_PAYMENT_REQUIRED, "insufficient credits")

    # ── stream upstream, tee usage, settle on completion ──────────────────────
    async def body_stream():
        captured = UsageCapture()
        settled = False
        try:
            async for chunk in streamer(body):
                captured.feed(chunk)
                yield chunk
            usage = captured.usage()
            if usage is not None:
                async with sessionmaker() as session:
                    await finalize(session, request_id=request_id, usage=usage, rate=rate)
                    balance = (await repository.get_state(session, user.id)).balance_usd
                settled = True
                yield balance_event(balance)
        finally:
            if not settled:
                # no usage (upstream error), exception, or client disconnect →
                # release the hold so it isn't orphaned.
                async with sessionmaker() as session:
                    await cancel(session, request_id=request_id)

    return StreamingResponse(body_stream(), media_type="text/event-stream")
