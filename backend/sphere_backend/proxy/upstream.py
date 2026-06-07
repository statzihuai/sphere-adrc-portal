"""Real Anthropic upstream — an injectable async byte-streamer.

The endpoint depends on a ``streamer(body) -> AsyncIterator[bytes]`` callable so
it can be swapped for a fake in tests (no Anthropic key/network). The production
builder forwards the request body **verbatim** (preserving ``cache_control`` for
shared prompt caching — the margin engine) with SPHERE's centralized key, and
relays the raw SSE bytes unbuffered. On a non-200 upstream it emits one SSE
``error`` event and stops, so the caller releases the hold (no usage captured).
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator

import httpx

_ANTHROPIC_VERSION = "2023-06-01"


def build_anthropic_streamer(api_key: str, base_url: str):
    """Return an async ``streamer(body)`` that relays Anthropic's SSE bytes."""

    async def streamer(body: dict) -> AsyncIterator[bytes]:
        forward = {**body, "stream": True}  # streaming is non-negotiable
        headers = {
            "x-api-key": api_key,
            "anthropic-version": _ANTHROPIC_VERSION,
            "content-type": "application/json",
        }
        # Long read timeout: a streamed turn can take minutes; no overall cap.
        timeout = httpx.Timeout(connect=10.0, read=None, write=30.0, pool=10.0)
        async with httpx.AsyncClient(timeout=timeout) as client:
            async with client.stream(
                "POST", f"{base_url}/v1/messages", headers=headers, json=forward
            ) as resp:
                if resp.status_code != 200:
                    detail = (await resp.aread()).decode("utf-8", errors="ignore")
                    payload = json.dumps({"type": "error", "status": resp.status_code, "detail": detail[:2000]})
                    yield f"event: error\ndata: {payload}\n\n".encode()
                    return
                async for chunk in resp.aiter_raw():
                    yield chunk

    return streamer
