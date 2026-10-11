"""Shared shape for every payment rail.

A chain client has exactly one job: return incoming transfers to our wallet,
newest cursor included. It never signs, never sends, never holds keys — read
access only. That property is what lets a customer point Gatekit at their own
wallet without trusting the operator.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Protocol, runtime_checkable


@dataclass(frozen=True, slots=True)
class ChainTransfer:
    """One incoming transfer, normalised across chains."""

    tx_id: str
    currency: str
    amount: Decimal
    to_address: str
    from_address: str | None
    ts: datetime
    comment: str | None = None  # TON memo; always None on TRC20

    def __str__(self) -> str:  # pragma: no cover - logging sugar
        return f"{self.amount} {self.currency} tx={self.tx_id[:12]}… at {self.ts.isoformat()}"


@runtime_checkable
class ChainClient(Protocol):
    """Read-only view of a chain."""

    currency: str

    async def fetch_incoming(
        self, since_ts_ms: int, limit: int = 200
    ) -> tuple[list[ChainTransfer], int]:
        """Return (transfers, new_cursor_ms) for transfers at/after ``since_ts_ms``."""
        ...

    async def fetch_by_tx_id(self, tx_id: str) -> ChainTransfer | None:
        """Look up a single transaction, for the "I paid, here is my hash" path."""
        ...

    async def close(self) -> None: ...
