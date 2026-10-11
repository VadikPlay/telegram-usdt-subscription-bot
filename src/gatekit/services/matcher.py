"""The payment matching engine — the most dangerous code in the project.

A mistake here either gives away free access or, worse, takes money without
granting access. So this module is deliberately:

* **pure** — no database, no network, no clock. Everything comes in as
  arguments, which is why it can be exhaustively unit-tested;
* **exact** — amounts are compared with Decimal equality, never floats, never
  "close enough";
* **single-use** — an invoice consumed inside a batch cannot be matched twice,
  and a tx_id already recorded can never be credited again;
* **deterministic** — given the same inputs it always produces the same output,
  including which invoice wins when two could match.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal

from gatekit.chains.base import ChainTransfer


@dataclass(frozen=True, slots=True)
class OpenInvoice:
    """Minimal view of an open invoice, decoupled from the ORM."""

    id: int
    code: str
    user_id: int
    currency: str
    amount: Decimal
    created_at: datetime
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class Match:
    transfer: ChainTransfer
    invoice: OpenInvoice
    reason: str  # "amount" | "comment" | "manual_tx"


@dataclass(slots=True)
class MatchResult:
    matches: list[Match] = field(default_factory=list)
    unmatched: list[ChainTransfer] = field(default_factory=list)
    duplicates: list[ChainTransfer] = field(default_factory=list)
    ambiguous: list[ChainTransfer] = field(default_factory=list)

    @property
    def matched_invoice_ids(self) -> set[int]:
        return {m.invoice.id for m in self.matches}


@dataclass(frozen=True, slots=True)
class MatchPolicy:
    """Time tolerances. Kept explicit so tests can pin them."""

    clock_skew: timedelta = timedelta(seconds=120)
    late_tolerance: timedelta = timedelta(minutes=180)

    def window_contains(self, invoice: OpenInvoice, ts: datetime) -> bool:
        start = invoice.created_at - self.clock_skew
        end = invoice.expires_at + self.late_tolerance
        return start <= ts <= end


def _normalise_comment(comment: str | None) -> str:
    return (comment or "").strip().upper()


def match_transfers(
    transfers: Sequence[ChainTransfer],
    invoices: Sequence[OpenInvoice],
    *,
    known_tx_ids: Iterable[str] = (),
    wallets: dict[str, str] | None = None,
    policy: MatchPolicy | None = None,
) -> MatchResult:
    """Pair incoming transfers with the invoices that expected them.

    Matching strategy, in order of strength:
      1. **comment** — TON only: the payer echoed the invoice code. Unambiguous.
      2. **amount** — exact equality on the invoice's unique amount, inside the
         invoice's time window.

    Anything left over is reported rather than guessed at: ``unmatched`` money
    is surfaced to the admin, ``ambiguous`` transfers are never auto-credited.
    """
    policy = policy or MatchPolicy()
    seen = {tx.lower() for tx in known_tx_ids}
    result = MatchResult()

    # Oldest first: if someone opened two invoices, the earlier one is settled
    # first, which is what a human would expect.
    by_code: dict[str, OpenInvoice] = {inv.code.strip().upper(): inv for inv in invoices}
    remaining = sorted(invoices, key=lambda i: (i.created_at, i.id))
    consumed: set[int] = set()

    for transfer in sorted(transfers, key=lambda t: (t.ts, t.tx_id)):
        if not transfer.tx_id:
            result.unmatched.append(transfer)
            continue
        if transfer.tx_id.lower() in seen:
            result.duplicates.append(transfer)
            continue
        # Money must have landed in the wallet we actually advertised.
        if wallets is not None:
            expected_wallet = wallets.get(transfer.currency)
            if expected_wallet and transfer.to_address != expected_wallet:
                result.unmatched.append(transfer)
                continue

        # 1. Comment match (TON).
        code = _normalise_comment(transfer.comment)
        if code and code in by_code:
            invoice = by_code[code]
            if (
                invoice.id not in consumed
                and invoice.currency == transfer.currency
                and transfer.amount >= invoice.amount
            ):
                consumed.add(invoice.id)
                seen.add(transfer.tx_id.lower())
                result.matches.append(Match(transfer, invoice, "comment"))
                continue

        # 2. Exact unique-amount match inside the window.
        candidates = [
            inv
            for inv in remaining
            if inv.id not in consumed
            and inv.currency == transfer.currency
            and inv.amount == transfer.amount
            and policy.window_contains(inv, transfer.ts)
        ]
        if not candidates:
            result.unmatched.append(transfer)
            continue
        if len(candidates) > 1:
            # Two open invoices with the same exact amount should be impossible
            # (allocate_unique_amount prevents it). If it ever happens, refuse to
            # guess: a human decides, and the transfer is reported.
            result.ambiguous.append(transfer)
            continue

        invoice = candidates[0]
        consumed.add(invoice.id)
        seen.add(transfer.tx_id.lower())
        result.matches.append(Match(transfer, invoice, "amount"))

    return result


def match_single_transfer(
    transfer: ChainTransfer,
    invoices: Sequence[OpenInvoice],
    *,
    known_tx_ids: Iterable[str] = (),
    wallets: dict[str, str] | None = None,
    policy: MatchPolicy | None = None,
) -> Match | None:
    """Convenience wrapper for the "I paid, here is my tx hash" flow."""
    result = match_transfers(
        [transfer], invoices, known_tx_ids=known_tx_ids, wallets=wallets, policy=policy
    )
    return result.matches[0] if result.matches else None
