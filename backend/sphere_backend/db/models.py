"""ORM models for users, billing, and the credit ledger (BACKEND_DESIGN.md §4.2).

The DDL here is exercised against SQLite in tests via ``Base.metadata.create_all``.
Production Postgres DDL is owned by Alembic migrations (added in the integration
env), so backend-specific column types (``BIGSERIAL``/identity, indexes) are
defined there authoritatively; these models stay portable.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base
from .types import USD

_ZERO = Decimal("0")

# BIGINT on Postgres, but INTEGER on SQLite so the PK is a rowid alias and
# autoincrements (SQLite only auto-increments INTEGER PRIMARY KEY).
_BIGINT_PK = BigInteger().with_variant(Integer, "sqlite")


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(_BIGINT_PK, primary_key=True, autoincrement=True)
    workos_user_id: Mapped[str] = mapped_column(String, unique=True, index=True, nullable=False)
    email: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    billing: Mapped["Billing"] = relationship(
        back_populates="user", uselist=False, cascade="all, delete-orphan"
    )


class Billing(Base):
    __tablename__ = "billing"

    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"), primary_key=True)
    stripe_customer_id: Mapped[str | None] = mapped_column(String, unique=True, nullable=True)
    stripe_sub_id: Mapped[str | None] = mapped_column(String, nullable=True)
    sub_status: Mapped[str | None] = mapped_column(String, nullable=True)
    sub_period_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    credit_balance_usd: Mapped[Decimal] = mapped_column(USD, nullable=False, default=_ZERO)
    reserved_usd: Mapped[Decimal] = mapped_column(USD, nullable=False, default=_ZERO)
    trial_used: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    user: Mapped[User] = relationship(back_populates="billing")


class CreditLedger(Base):
    __tablename__ = "credit_ledger"

    id: Mapped[int] = mapped_column(_BIGINT_PK, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"), index=True, nullable=False)
    ts: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    delta_usd: Mapped[Decimal] = mapped_column(USD, nullable=False)
    balance_after: Mapped[Decimal] = mapped_column(USD, nullable=False)
    type: Mapped[str] = mapped_column(String, nullable=False)
    description: Mapped[str | None] = mapped_column(String, nullable=True)
    stripe_pi_id: Mapped[str | None] = mapped_column(String, nullable=True)
    api_usage_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    # Unique → a grant tagged with a given key lands exactly once, even under
    # concurrent first-logins or webhook retries.
    idempotency_key: Mapped[str | None] = mapped_column(String, unique=True, nullable=True)


class ApiUsageLog(Base):
    """One row per AI request — also the reservation record (BACKEND_DESIGN.md §4.5).

    Created ``pending`` with the hold amount when the proxy reserves; transitions
    to ``settled`` (usage finalized + charged) or ``canceled`` (request failed /
    reclaimed). Holding the lifecycle on this row lets the hold on
    ``billing.reserved_usd`` be released exactly once — by settle, cancel, or the
    stale-reservation reclaim sweep — gated on the status under the billing lock.
    """

    __tablename__ = "api_usage_log"

    id: Mapped[int] = mapped_column(_BIGINT_PK, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"), index=True, nullable=False)
    request_id: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    session_id: Mapped[str | None] = mapped_column(String, nullable=True)
    status: Mapped[str] = mapped_column(String, nullable=False, index=True)  # pending|settled|canceled
    model: Mapped[str] = mapped_column(String, nullable=False)
    reserved_usd: Mapped[Decimal] = mapped_column(USD, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )
    finalized_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Token accounting — populated at settle (the four Anthropic fields + derived).
    input_tokens: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    cache_creation_tokens: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    cache_read_tokens: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    output_tokens: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    billed_input_tokens: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    billed_output_tokens: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    user_charge_usd: Mapped[Decimal | None] = mapped_column(USD, nullable=True)
    sphere_cost_usd: Mapped[Decimal | None] = mapped_column(USD, nullable=True)
    margin_usd: Mapped[Decimal | None] = mapped_column(USD, nullable=True)
