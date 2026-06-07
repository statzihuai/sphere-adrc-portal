"""Stripe adapter (BACKEND_DESIGN.md §4.6).

Endpoints depend on the ``StripeClient`` Protocol so the whole billing surface
tests offline with a fake — no Stripe account or network. The real impl wraps
the official SDK (verified surface: ``stripe.Customer.create``,
``stripe.checkout.Session.create``, ``stripe.billing_portal.Session.create``,
``stripe.Webhook.construct_event``). The API key is passed per call rather than
mutating the SDK global.
"""

from __future__ import annotations

from typing import Any, Mapping, Protocol, runtime_checkable

from ..config import Settings


@runtime_checkable
class StripeClient(Protocol):
    def create_customer(self, *, email: str, metadata: dict) -> str: ...
    def create_payment_checkout(
        self, *, customer_id: str, amount_cents: int, success_url: str, cancel_url: str, metadata: dict
    ) -> str: ...
    def create_subscription_checkout(
        self, *, customer_id: str, price_id: str, success_url: str, cancel_url: str
    ) -> str: ...
    def create_portal_session(self, *, customer_id: str, return_url: str) -> str: ...
    def verify_webhook(self, *, payload: bytes, sig_header: str) -> Mapping[str, Any]: ...


class WebhookVerificationError(Exception):
    """Signature check failed → reject the webhook (400)."""


class RealStripeClient:
    """Production ``StripeClient`` backed by the Stripe SDK."""

    def __init__(self, api_key: str, webhook_secret: str, api_version: str = ""):
        self._key = api_key
        self._webhook_secret = webhook_secret
        if api_version:
            # Pin so event shapes are reproducible across Stripe's default-version bumps.
            import stripe

            stripe.api_version = api_version

    def create_customer(self, *, email: str, metadata: dict) -> str:
        import stripe

        customer = stripe.Customer.create(email=email, metadata=metadata, api_key=self._key)
        return customer["id"]

    def create_payment_checkout(
        self, *, customer_id: str, amount_cents: int, success_url: str, cancel_url: str, metadata: dict
    ) -> str:
        import stripe

        session = stripe.checkout.Session.create(
            mode="payment",
            customer=customer_id,
            line_items=[
                {
                    "price_data": {
                        "currency": "usd",
                        "product_data": {"name": "SPHERE AI credits"},
                        "unit_amount": amount_cents,
                    },
                    "quantity": 1,
                }
            ],
            success_url=success_url,
            cancel_url=cancel_url,
            metadata=metadata,
            api_key=self._key,
        )
        return session["url"]

    def create_subscription_checkout(
        self, *, customer_id: str, price_id: str, success_url: str, cancel_url: str
    ) -> str:
        import stripe

        session = stripe.checkout.Session.create(
            mode="subscription",
            customer=customer_id,
            line_items=[{"price": price_id, "quantity": 1}],
            success_url=success_url,
            cancel_url=cancel_url,
            api_key=self._key,
        )
        return session["url"]

    def create_portal_session(self, *, customer_id: str, return_url: str) -> str:
        import stripe

        session = stripe.billing_portal.Session.create(
            customer=customer_id, return_url=return_url, api_key=self._key
        )
        return session["url"]

    def verify_webhook(self, *, payload: bytes, sig_header: str) -> Mapping[str, Any]:
        import stripe

        try:
            return stripe.Webhook.construct_event(payload, sig_header, self._webhook_secret)
        except Exception as exc:  # SignatureVerificationError / ValueError
            raise WebhookVerificationError(str(exc)) from exc


def build_stripe_client(settings: Settings) -> RealStripeClient | None:
    """Build the real client, or ``None`` if Stripe isn't configured."""
    if not settings.stripe_api_key:
        return None
    return RealStripeClient(
        settings.stripe_api_key, settings.stripe_webhook_secret, settings.stripe_api_version
    )
