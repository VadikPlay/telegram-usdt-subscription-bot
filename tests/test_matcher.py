from datetime import datetime, timedelta, timezone
from decimal import Decimal

from gatekit.chains.base import ChainTransfer
from gatekit.services.matcher import MatchPolicy, OpenInvoice, match_transfers

WALLET = "TWalletOwnerAddress0000000000000000"
WALLETS = {"USDT": WALLET, "TON": "EQTonWallet"}
NOW = datetime(2026, 9, 16, 12, 0, tzinfo=timezone.utc)


def invoice(
    _id: int = 1,
    amount: str = "39.004271",
    currency: str = "USDT",
    created: datetime = NOW,
    ttl_minutes: int = 60,
    code: str = "GK-7F3A9",
    user_id: int = 555,
) -> OpenInvoice:
    return OpenInvoice(
        id=_id,
        code=code,
        user_id=user_id,
        currency=currency,
        amount=Decimal(amount),
        created_at=created,
        expires_at=created + timedelta(minutes=ttl_minutes),
    )


def transfer(
    tx: str = "tx1",
    amount: str = "39.004271",
    currency: str = "USDT",
    ts: datetime | None = None,
    to: str = WALLET,
    comment: str | None = None,
) -> ChainTransfer:
    return ChainTransfer(
        tx_id=tx,
        currency=currency,
        amount=Decimal(amount),
        to_address=to,
        from_address="TSomePayer",
        ts=ts or (NOW + timedelta(minutes=5)),
        comment=comment,
    )


def test_exact_amount_matches():
    result = match_transfers([transfer()], [invoice()], wallets=WALLETS)
    assert len(result.matches) == 1
    assert result.matches[0].reason == "amount"
    assert not result.unmatched


def test_rounded_amount_does_not_match():
    """The whole scheme depends on this: 39.00 must NOT satisfy 39.004271."""
    result = match_transfers([transfer(amount="39.00")], [invoice()], wallets=WALLETS)
    assert not result.matches
    assert len(result.unmatched) == 1


def test_one_cent_off_does_not_match():
    result = match_transfers([transfer(amount="39.004272")], [invoice()], wallets=WALLETS)
    assert not result.matches
    assert len(result.unmatched) == 1


def test_already_seen_tx_is_a_duplicate_not_a_second_credit():
    result = match_transfers(
        [transfer(tx="TX-ABC")], [invoice()], known_tx_ids=["tx-abc"], wallets=WALLETS
    )
    assert not result.matches
    assert len(result.duplicates) == 1


def test_same_tx_twice_in_one_batch_credits_once():
    t = transfer(tx="TX-SAME")
    result = match_transfers([t, t], [invoice()], wallets=WALLETS)
    assert len(result.matches) == 1
    assert len(result.duplicates) == 1


def test_payment_to_another_wallet_is_ignored():
    result = match_transfers(
        [transfer(to="TSomeoneElsesWallet")], [invoice()], wallets=WALLETS
    )
    assert not result.matches
    assert len(result.unmatched) == 1


def test_payment_before_invoice_is_outside_the_window():
    early = transfer(ts=NOW - timedelta(hours=3))
    result = match_transfers([early], [invoice()], wallets=WALLETS)
    assert not result.matches


def test_small_clock_skew_is_tolerated():
    slightly_early = transfer(ts=NOW - timedelta(seconds=60))
    result = match_transfers([slightly_early], [invoice()], wallets=WALLETS)
    assert len(result.matches) == 1


def test_late_payment_inside_tolerance_is_honoured():
    late = transfer(ts=NOW + timedelta(minutes=60 + 90))
    result = match_transfers([late], [invoice()], wallets=WALLETS)
    assert len(result.matches) == 1


def test_very_late_payment_is_not_credited():
    too_late = transfer(ts=NOW + timedelta(hours=10))
    result = match_transfers([too_late], [invoice()], wallets=WALLETS)
    assert not result.matches
    assert len(result.unmatched) == 1


def test_two_invoices_same_amount_is_reported_not_guessed():
    """Should be impossible, but if it happens a human decides, not the bot."""
    a = invoice(_id=1, user_id=111)
    b = invoice(_id=2, user_id=222, code="GK-OTHER")
    result = match_transfers([transfer()], [a, b], wallets=WALLETS)
    assert not result.matches
    assert len(result.ambiguous) == 1


def test_two_payments_two_invoices_are_paired_oldest_first():
    a = invoice(_id=1, amount="39.000001", user_id=111, created=NOW - timedelta(minutes=10))
    b = invoice(_id=2, amount="39.000002", user_id=222, code="GK-B")
    result = match_transfers(
        [transfer(tx="t1", amount="39.000002"), transfer(tx="t2", amount="39.000001")],
        [a, b],
        wallets=WALLETS,
    )
    assert len(result.matches) == 2
    paired = {m.transfer.tx_id: m.invoice.user_id for m in result.matches}
    assert paired == {"t1": 222, "t2": 111}


def test_invoice_cannot_be_paid_twice_in_one_batch():
    result = match_transfers(
        [transfer(tx="t1"), transfer(tx="t2")], [invoice()], wallets=WALLETS
    )
    assert len(result.matches) == 1
    assert len(result.unmatched) == 1


def test_ton_matches_on_comment_even_with_overpay():
    inv = invoice(currency="TON", amount="9.500000000", code="GK-TON01")
    t = transfer(
        currency="TON", amount="10.000000000", to="EQTonWallet", comment="gk-ton01"
    )
    result = match_transfers([t], [inv], wallets=WALLETS)
    assert len(result.matches) == 1
    assert result.matches[0].reason == "comment"


def test_ton_comment_with_underpay_is_not_credited():
    inv = invoice(currency="TON", amount="9.500000000", code="GK-TON01")
    t = transfer(currency="TON", amount="1.000000000", to="EQTonWallet", comment="GK-TON01")
    result = match_transfers([t], [inv], wallets=WALLETS)
    assert not result.matches


def test_currency_mismatch_never_matches():
    inv = invoice(currency="TON", amount="39.004271")
    result = match_transfers([transfer(currency="USDT")], [inv], wallets=WALLETS)
    assert not result.matches


def test_empty_tx_id_is_never_credited():
    result = match_transfers([transfer(tx="")], [invoice()], wallets=WALLETS)
    assert not result.matches
    assert len(result.unmatched) == 1


def test_policy_tolerances_are_configurable():
    strict = MatchPolicy(clock_skew=timedelta(0), late_tolerance=timedelta(0))
    late = transfer(ts=NOW + timedelta(minutes=61))
    assert not match_transfers([late], [invoice()], wallets=WALLETS, policy=strict).matches


def test_matching_is_deterministic():
    invs = [
        invoice(_id=i, amount=f"39.00000{i}", user_id=100 + i, code=f"GK-{i}")
        for i in (1, 2, 3)
    ]
    transfers = [transfer(tx=f"t{i}", amount=f"39.00000{i}") for i in (3, 1, 2)]
    first = match_transfers(transfers, invs, wallets=WALLETS)
    second = match_transfers(transfers, invs, wallets=WALLETS)
    assert [(m.transfer.tx_id, m.invoice.id) for m in first.matches] == [
        (m.transfer.tx_id, m.invoice.id) for m in second.matches
    ]
