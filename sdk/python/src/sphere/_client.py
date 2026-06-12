"""Thin OpenAI-compatible client over the SPHERE gateway. Stdlib only.

The gateway returns OpenAI-shaped bodies unchanged, so this client is mostly
transport + typed errors. Responses are attribute-accessible dict views
(``r.choices[0].message.content`` and ``r["choices"]`` both work).
"""

from __future__ import annotations

import json as _json
import os
import urllib.error
import urllib.request

DEFAULT_BASE_URL = "https://api.butterbase.ai/v1/app_21ze8d0ep28o/fn"


class SphereError(Exception):
    """Base error. Carries the HTTP status and the server's error code."""

    def __init__(self, message: str, *, status: int = 0, code: str = "", type: str = ""):
        super().__init__(message)
        self.status = status
        self.code = code
        self.type = type


class InvalidKeyError(SphereError):
    """401 — missing, malformed, revoked, or unknown sphere_sk_ key."""


class InsufficientCreditsError(SphereError):
    """402 — wallet cannot cover the worst-case reserve for this call."""


class ModelNotFoundError(SphereError):
    """404 — model id is not in the gateway catalog."""


class InvalidRequestError(SphereError):
    """400 — malformed request (including stream=True, unsupported in v1)."""


class APIError(SphereError):
    """5xx / unexpected — upstream or gateway failure; retry with backoff."""


_ERROR_BY_STATUS = {
    400: InvalidRequestError,
    401: InvalidKeyError,
    402: InsufficientCreditsError,
    404: ModelNotFoundError,
}


class SphereObject(dict):
    """Dict with recursive attribute access, mirroring OpenAI SDK ergonomics."""

    def __getattr__(self, name):
        try:
            return _wrap(self[name])
        except KeyError:
            raise AttributeError(name) from None


def _wrap(v):
    if isinstance(v, dict):
        return SphereObject(v)
    if isinstance(v, list):
        return [_wrap(x) for x in v]
    return v


def _raise_for(status: int, body: dict):
    err = body.get("error") or {}
    cls = _ERROR_BY_STATUS.get(status, APIError)
    raise cls(
        err.get("message") or err.get("code") or f"HTTP {status}",
        status=status,
        code=err.get("code", ""),
        type=err.get("type", ""),
    )


class Client:
    def __init__(self, api_key: str | None = None, base_url: str | None = None, timeout: float = 120.0):
        self.api_key = api_key or os.environ.get("SPHERE_API_KEY") or ""
        if not self.api_key.startswith("sphere_sk_"):
            raise InvalidKeyError("api_key must be a sphere_sk_... key (or set SPHERE_API_KEY)", status=0)
        self.base_url = (base_url or os.environ.get("SPHERE_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")
        self.timeout = timeout
        self.chat = _Chat(self)
        self.models = _Models(self)

    # -- transport -----------------------------------------------------------
    def _request(self, method: str, url: str, payload: dict | None = None) -> SphereObject:
        req = urllib.request.Request(
            url,
            data=_json.dumps(payload).encode() if payload is not None else None,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
                "User-Agent": "sphere-python/0.1.0",
            },
            method=method,
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return _wrap(_json.load(r))
        except urllib.error.HTTPError as e:
            try:
                body = _json.loads(e.read() or b"{}")
            except ValueError:
                body = {}
            _raise_for(e.code, body)
        except urllib.error.URLError as e:
            raise APIError(f"connection failed: {e.reason}", status=0) from None

    # -- surface -------------------------------------------------------------
    def balance(self) -> SphereObject:
        return self._request("GET", f"{self.base_url}/balance")

    @property
    def embeddings(self):
        raise NotImplementedError("embeddings are deferred in SDK v1 (gateway proxies chat completions only)")


class _Completions:
    def __init__(self, client: Client):
        self._client = client

    def create(self, *, model: str, messages: list, stream: bool = False, **kwargs) -> SphereObject:
        if stream:
            raise InvalidRequestError("streaming is not supported in v1", status=0, code="stream_unsupported")
        payload = {"model": model, "messages": messages, **kwargs}
        return self._client._request("POST", f"{self._client.base_url}/gateway", payload)


class _Chat:
    def __init__(self, client: Client):
        self.completions = _Completions(client)


class _Models:
    def __init__(self, client: Client):
        self._client = client

    def list(self) -> list:
        # Public catalog lives at the platform root, not under /fn.
        root = self._client.base_url.split("/v1/")[0]
        return self._client._request("GET", f"{root}/v1/public/models")["models"]
