"""Money handling and the unique-amount trick.

Why this file exists
────────────────────
A TRC20 transfer carries **no memo field**. If two subscribers both send 39.00
USDT to the same wallet, the chain cannot tell you which one is which.

Gatekit solves that without custody and without a payment processor: every
invoice gets a *unique amount*. The price 39.00 becomes e.g. 39.004271 — the
last digits are an invoice tag. Variance stays under one cent and there are
9999 slots, which is far more than the number of invoices open at once.

TON *does* have a comment field, so TON invoices are matched by their code and
do not need the amount trick.
"""

from __future__ import annotations

import secrets
from collections.abc import Iterable
from decimal import ROUND_DOWN, Decimal

# Tag lives in the 3rd..6th decimal place: max surcharge 0.009999 of a unit.
TAG_MIN = 1
TAG_MAX = 9999
TAG_SCALE_EXPONENT = -6  # 0.000001


class AmountAllocationError(RuntimeError):
    """Raised when every unique-amount slot for a price is currently taken."""


def quantize(amount: Decimal, decimals: int) -> Decimal:
    """Truncate to the token's precision (never round up — never ask for more)."""
    return amount.quantize(Decimal(1).scaleb(-decimals), rounding=ROUND_DOWN)


def tag_to_offset(tag: int) -> Decimal:
    """Convert an invoice tag into the tiny amount added to the price."""
    if not TAG_MIN <= tag <= TAG_MAX:
        raise ValueError(f"tag must be within [{TAG_MIN}, {TAG_MAX}], got {tag}")
    return Decimal(tag).scaleb(TAG_SCALE_EXPONENT)


def build_amount(base_price: Decimal, tag: int, decimals: int) -> Decimal:
    """Price + tag offset, at the token's precision."""
    return quantize(base_price + tag_to_offset(tag), decimals)


def allocate_unique_amount(
    base_price: Decimal,
    taken_amounts: Iterable[Decimal],
    decimals: int,
    *,
    max_attempts: int = 200,
) -> tuple[Decimal, int]:
    """Pick an amount for a new invoice that no other open invoice is using.

    Returns ``(amount, tag)``. Random rather than sequential so that subscribers
    cannot enumerate how many invoices you issue.
    """
    taken = {quantize(a, decimals) for a in taken_amounts}

    for _ in range(max_attempts):
        tag = secrets.randbelow(TAG_MAX - TAG_MIN + 1) + TAG_MIN
        amount = build_amount(base_price, tag, decimals)
        if amount not in taken:
            return amount, tag

    # Random probing failed (pathological load) — fall back to a linear scan so
    # we only ever error out when the space is genuinely exhausted.
    for tag in range(TAG_MIN, TAG_MAX + 1):
        amount = build_amount(base_price, tag, decimals)
        if amount not in taken:
            return amount, tag

    raise AmountAllocationError(
        f"All {TAG_MAX} unique-amount slots for price {base_price} are in use. "
        "Shorten INVOICE_TTL_MINUTES or split traffic across more price points."
    )


def format_amount(amount: Decimal, decimals: int) -> str:
    """Render an amount exactly as the payer must send it (no trailing zero trimming).

    Trailing zeros matter here: the subscriber copies this string, and an amount
    of 39.004000 must not be shown as 39.004.
    """
    return f"{quantize(amount, decimals):.{decimals}f}"


def from_raw_units(raw: str | int, decimals: int) -> Decimal:
    """Chain APIs return integer strings in the smallest unit."""
    return quantize(Decimal(str(raw)).scaleb(-decimals), decimals)
