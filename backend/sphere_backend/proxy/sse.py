"""SSE helpers for the proxy: token capture, reserve estimation, balance event.

The proxy streams Anthropic's SSE bytes to the browser **unbuffered** while
tee-ing a copy here to extract the four token fields (BACKEND_DESIGN.md §4.5):

  - ``message_start.message.usage`` → input_tokens, cache_creation_input_tokens,
    cache_read_input_tokens  (the input/cache counts)
  - ``message_delta.usage.output_tokens`` → the final, cumulative output count

Capturing only ``message_delta`` would miss the cache fields and undercharge.
"""

from __future__ import annotations

import json
import math

from ..billing.usage import Usage


def estimate_input_tokens(body: dict) -> int:
    """Conservative input-token estimate for the pre-flight reserve.

    We can't know the exact count before forwarding, and the hold must dominate
    the settled charge, so over-estimate: ~chars/3 across system + tools +
    messages (every input token is billed at full rate, cache status aside).
    Settle uses the *actual* counts Anthropic returns, so this only affects the
    size of the hold, never the final charge.
    """
    chars = 0
    for key in ("system", "tools", "messages"):
        value = body.get(key)
        if value is not None:
            chars += len(json.dumps(value, ensure_ascii=False, default=str))
    return max(256, math.ceil(chars / 3))


def balance_event(balance_usd) -> bytes:
    """A trailing ``sphere_balance`` SSE event so the client can update its display."""
    payload = json.dumps({"balance_usd": str(balance_usd)})
    return f"event: sphere_balance\ndata: {payload}\n\n".encode()


class UsageCapture:
    """Accumulates streamed SSE bytes and extracts the final usage.

    Feed raw chunks; call ``usage()`` after the stream ends. Returns ``None`` if
    no ``message_start`` was seen (e.g. an upstream error before any tokens).
    """

    def __init__(self) -> None:
        self._buf = ""
        self._input = 0
        self._cache_creation = 0
        self._cache_read = 0
        self._output: int | None = None
        self._saw_start = False

    def feed(self, chunk: bytes) -> None:
        self._buf += chunk.decode("utf-8", errors="ignore")
        # Process complete lines; keep the trailing partial in the buffer.
        lines = self._buf.split("\n")
        self._buf = lines.pop()
        for line in lines:
            if not line.startswith("data:"):
                continue
            payload = line[len("data:") :].strip()
            if not payload or payload == "[DONE]":
                continue
            try:
                event = json.loads(payload)
            except json.JSONDecodeError:
                continue
            self._absorb(event)

    def _absorb(self, event: dict) -> None:
        etype = event.get("type")
        if etype == "message_start":
            usage = (event.get("message") or {}).get("usage") or {}
            self._saw_start = True
            self._input = int(usage.get("input_tokens", 0) or 0)
            self._cache_creation = int(usage.get("cache_creation_input_tokens", 0) or 0)
            self._cache_read = int(usage.get("cache_read_input_tokens", 0) or 0)
            # output here is the initial count; the authoritative final is in message_delta
            if usage.get("output_tokens") is not None:
                self._output = int(usage["output_tokens"])
        elif etype == "message_delta":
            usage = event.get("usage") or {}
            if usage.get("output_tokens") is not None:
                self._output = int(usage["output_tokens"])

    def usage(self) -> Usage | None:
        if not self._saw_start:
            return None
        return Usage(
            input_tokens=self._input,
            cache_creation_tokens=self._cache_creation,
            cache_read_tokens=self._cache_read,
            output_tokens=self._output or 0,
        )
