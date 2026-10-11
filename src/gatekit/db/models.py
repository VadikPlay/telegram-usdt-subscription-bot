"""Database schema.

Money is stored as a string and converted through Decimal at the edges. Floats
never touch an amount anywhere in Gatekit — 0.1 + 0.2 problems are not
acceptable when the number decides whether someone keeps channel access.
"""

from __future__ import annotations

import enum
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import BigInteger, Boolean, DateTime, Enum, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from gatekit.db.base import Base


def utcnow() -> datetime:
    return datetime.now(tz=timezone.utc)


class TZDateTime(DateTime):
    """DateTime that always round-trips as timezone-aware UTC."""

    def __init__(self) -> None:
        super().__init__(timezone=True)


class InvoiceStatus(str, enum.Enum):
    OPEN = "open"
    PAID = "paid"
    EXPIRED = "expired"
    CANCELLED = "cancelled"


class MembershipState(str, enum.Enum):
    PENDING = "pending"  # paid, invite issued, not joined yet
    ACTIVE = "active"
    IN_GRACE = "in_grace"
    REVOKED = "revoked"


class PaymentStatus(str, enum.Enum):
    MATCHED = "matched"
    UNMATCHED = "unmatched"  # money arrived that no open invoice expected
    DUPLICATE = "duplicate"


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)  # Telegram user id
    username: Mapped[str | None] = mapped_column(String(64))
    full_name: Mapped[str | None] = mapped_column(String(256))
    lang: Mapped[str] = mapped_column(String(8), default="en")
    created_at: Mapped[datetime] = mapped_column(TZDateTime, default=utcnow)
    is_blocked: Mapped[bool] = mapped_column(Boolean, default=False)

    invoices: Mapped[list[Invoice]] = relationship(back_populates="user")
    membership: Mapped[Membership | None] = relationship(back_populates="user", uselist=False)

    @property
    def display(self) -> str:
        if self.username:
            return f"@{self.username}"
        return self.full_name or str(self.id)


class Invoice(Base):
    __tablename__ = "invoices"
    __table_args__ = (
        Index("ix_invoices_open_lookup", "status", "currency", "amount"),
        Index("ix_invoices_user", "user_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(24), unique=True)  # shown to the payer, TON memo
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"))
    plan_code: Mapped[str] = mapped_column(String(32))
    plan_days: Mapped[int] = mapped_column(Integer)
    currency: Mapped[str] = mapped_column(String(8))
    base_amount: Mapped[str] = mapped_column(String(40))  # the advertised price
    amount: Mapped[str] = mapped_column(String(40))  # what the payer must send
    tag: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[InvoiceStatus] = mapped_column(
        Enum(InvoiceStatus, native_enum=False), default=InvoiceStatus.OPEN
    )
    created_at: Mapped[datetime] = mapped_column(TZDateTime, default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(TZDateTime)
    paid_at: Mapped[datetime | None] = mapped_column(TZDateTime)
    paid_tx_id: Mapped[str | None] = mapped_column(String(128))

    user: Mapped[User] = relationship(back_populates="invoices")

    @property
    def amount_dec(self) -> Decimal:
        return Decimal(self.amount)

    @property
    def base_amount_dec(self) -> Decimal:
        return Decimal(self.base_amount)


class Payment(Base):
    __tablename__ = "payments"
    __table_args__ = (Index("ix_payments_seen", "seen_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # Chain transaction id. Unique, so a replayed chain page can never double-credit.
    tx_id: Mapped[str] = mapped_column(String(128), unique=True)
    currency: Mapped[str] = mapped_column(String(8))
    amount: Mapped[str] = mapped_column(String(40))
    from_address: Mapped[str | None] = mapped_column(String(128))
    to_address: Mapped[str | None] = mapped_column(String(128))
    comment: Mapped[str | None] = mapped_column(String(256))
    chain_ts: Mapped[datetime] = mapped_column(TZDateTime)
    seen_at: Mapped[datetime] = mapped_column(TZDateTime, default=utcnow)
    status: Mapped[PaymentStatus] = mapped_column(
        Enum(PaymentStatus, native_enum=False), default=PaymentStatus.UNMATCHED
    )
    invoice_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("invoices.id"))

    @property
    def amount_dec(self) -> Decimal:
        return Decimal(self.amount)


class Membership(Base):
    __tablename__ = "memberships"
    __table_args__ = (Index("ix_memberships_expiry", "state", "expires_at"),)

    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"), primary_key=True)
    plan_code: Mapped[str] = mapped_column(String(32))
    state: Mapped[MembershipState] = mapped_column(
        Enum(MembershipState, native_enum=False), default=MembershipState.PENDING
    )
    started_at: Mapped[datetime] = mapped_column(TZDateTime, default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(TZDateTime)
    joined_at: Mapped[datetime | None] = mapped_column(TZDateTime)
    last_payment_at: Mapped[datetime | None] = mapped_column(TZDateTime)
    total_paid_usdt: Mapped[str] = mapped_column(String(40), default="0")
    payments_count: Mapped[int] = mapped_column(Integer, default=0)
    invite_link: Mapped[str | None] = mapped_column(Text)
    # Which reminders have already gone out for the CURRENT period, as "3,1".
    reminders_sent: Mapped[str] = mapped_column(String(64), default="")
    revoked_at: Mapped[datetime | None] = mapped_column(TZDateTime)
    pending_kick: Mapped[bool] = mapped_column(Boolean, default=False)
    notes: Mapped[str | None] = mapped_column(Text)

    user: Mapped[User] = relationship(back_populates="membership")

    @property
    def reminder_set(self) -> set[int]:
        return {int(x) for x in self.reminders_sent.split(",") if x.strip()}

    def mark_reminder(self, day: int) -> None:
        current = self.reminder_set
        current.add(day)
        self.reminders_sent = ",".join(str(d) for d in sorted(current, reverse=True))


class AuditLog(Base):
    """Every access decision, forever.

    This table is the answer to "your bot kicked a paying member": you can show
    exactly what happened, when, why, and whether a human or the scheduler did it.
    """

    __tablename__ = "audit_log"
    __table_args__ = (Index("ix_audit_user_ts", "user_id", "ts"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(TZDateTime, default=utcnow)
    actor: Mapped[str] = mapped_column(String(32), default="system")  # "system" | "admin:<id>"
    action: Mapped[str] = mapped_column(String(48))
    user_id: Mapped[int | None] = mapped_column(BigInteger)
    details: Mapped[str | None] = mapped_column(Text)


class ChainCursor(Base):
    """Where the watcher stopped reading each chain, so restarts do not rescan."""

    __tablename__ = "chain_cursors"

    chain: Mapped[str] = mapped_column(String(16), primary_key=True)
    last_ts_ms: Mapped[int] = mapped_column(BigInteger, default=0)
    updated_at: Mapped[datetime] = mapped_column(TZDateTime, default=utcnow)
