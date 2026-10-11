from decimal import Decimal

import pytest

from gatekit.money import (
    AmountAllocationError,
    allocate_unique_amount,
    build_amount,
    format_amount,
    from_raw_units,
    quantize,
    tag_to_offset,
)

USDT = 6


def test_quantize_truncates_never_rounds_up():
    # Never ask a subscriber for more than the price.
    assert quantize(Decimal("39.0000009"), USDT) == Decimal("39.000000")
    assert quantize(Decimal("39.1234569"), USDT) == Decimal("39.123456")


def test_tag_offset_is_sub_cent():
    assert tag_to_offset(1) == Decimal("0.000001")
    assert tag_to_offset(9999) == Decimal("0.009999")
    # The whole point: the surcharge is invisible next to the price.
    assert tag_to_offset(9999) < Decimal("0.01")


def test_tag_out_of_range_is_rejected():
    with pytest.raises(ValueError):
        tag_to_offset(0)
    with pytest.raises(ValueError):
        tag_to_offset(10_000)


def test_build_amount_is_exact():
    assert build_amount(Decimal("39.00"), 4271, USDT) == Decimal("39.004271")


def test_allocate_avoids_taken_amounts():
    base = Decimal("39.00")
    taken: list[Decimal] = []
    for _ in range(50):
        amount, tag = allocate_unique_amount(base, taken, USDT)
        assert amount not in taken
        assert 1 <= tag <= 9999
        taken.append(amount)
    assert len(set(taken)) == 50


def test_allocate_raises_only_when_space_is_exhausted():
    base = Decimal("10.00")
    taken = [build_amount(base, tag, USDT) for tag in range(1, 10_000)]
    with pytest.raises(AmountAllocationError):
        allocate_unique_amount(base, taken, USDT)


def test_allocate_still_works_when_almost_full():
    """Random probing fails here; the linear fallback must find the last slot."""
    base = Decimal("10.00")
    taken = [build_amount(base, tag, USDT) for tag in range(1, 9_999)]
    amount, tag = allocate_unique_amount(base, taken, USDT)
    assert tag == 9999
    assert amount == Decimal("10.009999")


def test_format_keeps_trailing_zeros():
    # The payer copies this string; 39.004000 must not become 39.004.
    assert format_amount(Decimal("39.004"), USDT) == "39.004000"
    assert format_amount(Decimal("39"), USDT) == "39.000000"


def test_from_raw_units_handles_chain_integers():
    assert from_raw_units("39004271", USDT) == Decimal("39.004271")
    assert from_raw_units(1_000_000, USDT) == Decimal("1.000000")
    # TON uses 9 decimals.
    assert from_raw_units("9500000000", 9) == Decimal("9.500000000")


def test_no_float_contamination():
    """A float sneaking in is the classic way money code goes wrong."""
    amount = build_amount(Decimal("0.1"), 1, USDT) + build_amount(Decimal("0.2"), 1, USDT)
    assert amount == Decimal("0.300002")
