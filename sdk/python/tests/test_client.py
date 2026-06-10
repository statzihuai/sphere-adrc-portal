"""Unit tests for the sphere client against a local fake gateway (stdlib only)."""

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from sphere import (
    Client,
    InsufficientCreditsError,
    InvalidKeyError,
    InvalidRequestError,
    ModelNotFoundError,
)

CHAT_OK = {
    "id": "chatcmpl-1",
    "model": "anthropic/claude-3-haiku",
    "choices": [{"index": 0, "message": {"role": "assistant", "content": "pong"}, "finish_reason": "stop"}],
    "usage": {"prompt_tokens": 9, "completion_tokens": 1, "total_tokens": 10},
}
MODELS = {"models": [{"id": "anthropic/claude-3-haiku", "inputPricePerMTokens": 0.24, "outputPricePerMTokens": 1.2}]}


class FakeGateway(BaseHTTPRequestHandler):
    seen = []

    def _send(self, status, body):
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        FakeGateway.seen.append((self.path, dict(self.headers), body))
        auth = self.headers.get("Authorization", "")
        if auth != "Bearer sphere_sk_good":
            return self._send(401, {"error": {"type": "authentication_error", "code": "invalid_api_key", "message": "invalid_api_key"}})
        if body["model"] == "nope/none":
            return self._send(404, {"error": {"type": "invalid_request_error", "code": "model_not_found", "message": "unknown model"}})
        if body.get("max_tokens", 0) > 10_000:
            return self._send(402, {"error": {"type": "billing_error", "code": "insufficient_credits", "message": "insufficient_credits"}})
        return self._send(200, CHAT_OK)

    def do_GET(self):
        if self.path.endswith("/balance"):
            return self._send(200, {"balance_microcents": 9_830_000, "balance_usd": 9.83})
        if self.path.endswith("/v1/public/models"):
            return self._send(200, MODELS)
        return self._send(404, {"error": {"code": "not_found"}})

    def log_message(self, *a):  # keep test output quiet
        pass


@pytest.fixture(scope="module")
def base_url():
    srv = HTTPServer(("127.0.0.1", 0), FakeGateway)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{srv.server_port}/v1/app_test/fn"
    srv.shutdown()


@pytest.fixture()
def client(base_url):
    return Client(api_key="sphere_sk_good", base_url=base_url)


def test_chat_completion_happy_path(client):
    r = client.chat.completions.create(
        model="anthropic/claude-3-haiku",
        messages=[{"role": "user", "content": "ping"}],
    )
    assert r.choices[0].message.content == "pong"
    assert r["usage"]["total_tokens"] == 10  # dict access works too
    path, headers, body = FakeGateway.seen[-1]
    assert path.endswith("/gateway")
    assert headers["Authorization"] == "Bearer sphere_sk_good"
    assert body["messages"][0]["content"] == "ping"


def test_kwargs_passed_through(client):
    client.chat.completions.create(model="m/x", messages=[], temperature=0.2, max_tokens=64)
    assert FakeGateway.seen[-1][2]["temperature"] == 0.2
    assert FakeGateway.seen[-1][2]["max_tokens"] == 64


def test_insufficient_credits_typed_error(client):
    with pytest.raises(InsufficientCreditsError) as e:
        client.chat.completions.create(model="m/x", messages=[], max_tokens=99_999)
    assert e.value.status == 402
    assert e.value.code == "insufficient_credits"


def test_invalid_key_typed_error(base_url):
    bad = Client(api_key="sphere_sk_wrong", base_url=base_url)
    with pytest.raises(InvalidKeyError) as e:
        bad.chat.completions.create(model="m/x", messages=[])
    assert e.value.status == 401


def test_model_not_found_typed_error(client):
    with pytest.raises(ModelNotFoundError):
        client.chat.completions.create(model="nope/none", messages=[])


def test_stream_rejected_client_side(client):
    with pytest.raises(InvalidRequestError):
        client.chat.completions.create(model="m/x", messages=[], stream=True)
    assert not FakeGateway.seen or not FakeGateway.seen[-1][2].get("stream")  # never sent


def test_balance(client):
    b = client.balance()
    assert b.balance_usd == 9.83
    assert b.balance_microcents == 9_830_000


def test_models_list(client):
    models = client.models.list()
    assert models[0]["id"] == "anthropic/claude-3-haiku"


def test_key_required():
    with pytest.raises(InvalidKeyError):
        Client(api_key="not_a_sphere_key")


def test_embeddings_deferred(client):
    with pytest.raises(NotImplementedError):
        client.embeddings
