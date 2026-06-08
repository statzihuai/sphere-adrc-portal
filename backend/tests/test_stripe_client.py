"""RealStripeClient.verify_webhook — runs the actual Stripe SDK offline.

`construct_event` does local HMAC verification (no network), so we can mint a
valid signature and exercise the real verifier. Regression guard for the bug the
live smoke test caught: the real verifier must return a plain ``dict`` (Stripe's
Event object isn't dict-backed — ``.get`` breaks `handle_event`).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time

import pytest

from sphere_backend.billing.stripe_client import RealStripeClient, WebhookVerificationError

SECRET = "whsec_test"


def _signed(payload: bytes, secret: str = SECRET) -> str:
    ts = int(time.time())
    sig = hmac.new(secret.encode(), f"{ts}.".encode() + payload, hashlib.sha256).hexdigest()
    return f"t={ts},v1={sig}"


def test_verify_webhook_returns_plain_dict():
    client = RealStripeClient("sk_test_x", SECRET)
    payload = json.dumps(
        {
            "id": "evt_1",
            "object": "event",
            "type": "checkout.session.completed",
            "data": {"object": {"mode": "payment", "customer": "cus_1"}},
        }
    ).encode()

    event = client.verify_webhook(payload=payload, sig_header=_signed(payload))

    assert isinstance(event, dict)
    # `.get` must work end-to-end (the StripeObject regression)
    assert event.get("type") == "checkout.session.completed"
    assert event["data"]["object"].get("mode") == "payment"


def test_verify_webhook_bad_signature_raises():
    client = RealStripeClient("sk_test_x", SECRET)
    payload = b'{"id":"evt_1","object":"event","type":"x","data":{"object":{}}}'
    with pytest.raises(WebhookVerificationError):
        client.verify_webhook(payload=payload, sig_header="t=1,v1=deadbeef")
