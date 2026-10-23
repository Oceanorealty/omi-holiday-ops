"""Optional GST breakdown for the Finance page — off unless the business is
GST-registered. Every price field in this app (bookings, statements) is
GST-inclusive, matching how Australian consumer pricing works, so the GST
component of an amount is amount / (1 + rate) * rate, not amount * rate."""

import os
from decimal import Decimal, ROUND_HALF_UP


def gst_registered() -> bool:
    return os.environ.get("GST_REGISTERED", "false").strip().lower() in ("1", "true", "yes")


def gst_rate() -> Decimal:
    return Decimal(os.environ.get("GST_RATE", "0.10"))


def gst_component(amount: Decimal) -> Decimal:
    """The GST included within a GST-inclusive amount."""
    rate = gst_rate()
    return (amount * rate / (1 + rate)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
