"""Custom SQLAlchemy column types."""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy import Numeric, String, types

from ..billing.usage import quantize_usd


class USD(types.TypeDecorator):
    """Exact 6-dp USD money column.

    ``NUMERIC(12,6)`` on Postgres (real numeric for SQL-side wallet arithmetic),
    ``TEXT`` on SQLite (avoids REAL float drift in tests). Always reads back a
    quantized ``Decimal`` so application code never sees a float.
    """

    impl = Numeric(12, 6, asdecimal=True)
    cache_ok = True

    def load_dialect_impl(self, dialect):
        if dialect.name == "sqlite":
            return dialect.type_descriptor(String(32))
        return dialect.type_descriptor(Numeric(12, 6, asdecimal=True))

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        quantized = quantize_usd(Decimal(value))
        return str(quantized) if dialect.name == "sqlite" else quantized

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        return quantize_usd(Decimal(str(value)))
