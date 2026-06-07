"""UsageCapture unit tests — robust SSE parsing across arbitrary chunk splits.

The four token fields must be captured correctly regardless of how the byte
stream is chunked, including a multi-byte char split mid-character in a text
delta and a final event not terminated by a newline.
"""

from __future__ import annotations

from sphere_backend.billing.usage import Usage
from sphere_backend.proxy.sse import UsageCapture, estimate_input_tokens

# Includes a multi-byte char (é, emoji) in a text delta to stress the decoder.
SSE = (
    b'event: message_start\n'
    b'data: {"type":"message_start","message":{"usage":{"input_tokens":500,'
    b'"cache_creation_input_tokens":10,"cache_read_input_tokens":4500,"output_tokens":1}}}\n\n'
    b'event: content_block_delta\n'
    b'data: {"type":"content_block_delta","delta":{"type":"text_delta","text":"caf\xc3\xa9 \xf0\x9f\x93\x8a"}}\n\n'
    b'event: message_delta\n'
    b'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"},"usage":{"output_tokens":1000}}\n\n'
    b'event: message_stop\ndata: {"type":"message_stop"}\n\n'
)
EXPECTED = Usage(input_tokens=500, cache_creation_tokens=10, cache_read_tokens=4500, output_tokens=1000)


def _capture_in_chunks(data: bytes, size: int) -> Usage | None:
    cap = UsageCapture()
    for i in range(0, len(data), size):
        cap.feed(data[i : i + size])
    return cap.usage()


def test_usage_captured_for_any_chunk_size():
    # tiny chunk sizes guarantee splits land mid-event and mid-multibyte-char
    for size in (1, 2, 3, 5, 7, 13, 64, len(SSE)):
        assert _capture_in_chunks(SSE, size) == EXPECTED, f"failed at chunk size {size}"


def test_usage_none_without_message_start():
    cap = UsageCapture()
    cap.feed(b'event: error\ndata: {"type":"error"}\n\n')
    assert cap.usage() is None


def test_trailing_event_without_newline_is_processed():
    cap = UsageCapture()
    # message_delta as the very last bytes, no terminating newline
    cap.feed(b'data: {"type":"message_start","message":{"usage":{"input_tokens":5}}}\n')
    cap.feed(b'data: {"type":"message_delta","usage":{"output_tokens":42}}')
    u = cap.usage()
    assert u is not None and u.output_tokens == 42 and u.input_tokens == 5


def test_estimate_input_tokens_floor_and_scaling():
    assert estimate_input_tokens({}) == 256  # floor
    big = {"messages": [{"role": "user", "content": "x" * 3000}]}
    assert estimate_input_tokens(big) >= 1000
