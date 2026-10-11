"""Owner-facing numbers and CSV export.

Deliberately blunt metrics: a channel owner wants to know how many people are
paying, what came in this month, and who is about to lapse. No vanity charts.
"""

from __future__ import annotations

import asyncio
import csv
import io
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from gatekit.db.models import (
    Invoice,
    InvoiceStatus,
    Membership,
    MembershipState,
    Payment,
    PaymentStatus,
    User,
)


@dataclass(slots=True)
class Stats:
    active: int = 0
    in_grace: int = 0
    pending: int = 0
    revoked: int = 0
    expiring_7d: int = 0
    # Currencies are never mixed: there is no FX feed in Gatekit, so USDT and
    # TON are reported side by side rather than added into a fake total.
    revenue_30d: Decimal = Decimal(0)
    revenue_total: Decimal = Decimal(0)
    revenue_ton_30d: Decimal = Decimal(0)
    revenue_ton_total: Decimal = Decimal(0)
    payments_30d: int = 0
    unmatched_payments: int = 0
    open_invoices: int = 0
    conversion_30d: float = 0.0

    @property
    def paying(self) -> int:
        return self.active + self.in_grace


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


async def collect(session: AsyncSession) -> Stats:
    now = datetime.now(tz=timezone.utc)
    since_30d = now - timedelta(days=30)
    stats = Stats()

    rows = await session.execute(
        select(Membership.state, func.count()).group_by(Membership.state)
    )
    for state, count in rows:
        key = state.value if hasattr(state, "value") else str(state)
        if key == MembershipState.ACTIVE.value:
            stats.active = count
        elif key == MembershipState.IN_GRACE.value:
            stats.in_grace = count
        elif key == MembershipState.PENDING.value:
            stats.pending = count
        elif key == MembershipState.REVOKED.value:
            stats.revoked = count

    stats.expiring_7d = int(
        await session.scalar(
            select(func.count())
            .select_from(Membership)
            .where(
                Membership.state.in_([MembershipState.ACTIVE, MembershipState.PENDING]),
                Membership.expires_at <= now + timedelta(days=7),
                Membership.expires_at > now,
            )
        )
        or 0
    )

    paid_invoices = await session.scalars(
        select(Invoice).where(Invoice.status == InvoiceStatus.PAID)
    )
    for invoice in paid_invoices:
        # Revenue is tracked at the advertised price, not the unique amount:
        # the tag cents are a routing detail, not income.
        amount = invoice.base_amount_dec
        paid_at = _aware(invoice.paid_at)
        recent = bool(paid_at and paid_at >= since_30d)

        if invoice.currency == "TON":
            stats.revenue_ton_total += amount
            if recent:
                stats.revenue_ton_30d += amount
        else:
            stats.revenue_total += amount
            if recent:
                stats.revenue_30d += amount
        if recent:
            stats.payments_30d += 1

    stats.unmatched_payments = int(
        await session.scalar(
            select(func.count())
            .select_from(Payment)
            .where(Payment.status == PaymentStatus.UNMATCHED)
        )
        or 0
    )
    stats.open_invoices = int(
        await session.scalar(
            select(func.count()).select_from(Invoice).where(Invoice.status == InvoiceStatus.OPEN)
        )
        or 0
    )

    created_30d = int(
        await session.scalar(
            select(func.count()).select_from(Invoice).where(Invoice.created_at >= since_30d)
        )
        or 0
    )
    if created_30d:
        stats.conversion_30d = round(100 * stats.payments_30d / created_30d, 1)

    return stats


async def subscriber_rows(session: AsyncSession) -> list[dict[str, str]]:
    stmt = (
        select(Membership, User)
        .join(User, User.id == Membership.user_id)
        .order_by(Membership.expires_at.desc())
    )
    out: list[dict[str, str]] = []
    for membership, user in await session.execute(stmt):
        expires = _aware(membership.expires_at)
        out.append(
            {
                "user_id": str(user.id),
                "username": user.username or "",
                "full_name": user.full_name or "",
                "plan": membership.plan_code,
                "state": membership.state.value
                if hasattr(membership.state, "value")
                else str(membership.state),
                "started_at": (_aware(membership.started_at) or "").__str__(),
                "expires_at": expires.isoformat() if expires else "",
                "days_left": str((expires - datetime.now(tz=timezone.utc)).days)
                if expires
                else "",
                "joined": "yes" if membership.joined_at else "no",
                "payments_count": str(membership.payments_count or 0),
                "total_paid_usdt": membership.total_paid_usdt or "0",
                "last_payment_at": (
                    _aware(membership.last_payment_at).isoformat()
                    if membership.last_payment_at
                    else ""
                ),
            }
        )
    return out


async def export_csv(session: AsyncSession, directory: str = "exports") -> Path:
    """Write a subscriber CSV the owner can open in Excel or import elsewhere."""
    rows = await subscriber_rows(session)
    fieldnames = [
        "user_id",
        "username",
        "full_name",
        "plan",
        "state",
        "started_at",
        "expires_at",
        "days_left",
        "joined",
        "payments_count",
        "total_paid_usdt",
        "last_payment_at",
    ]
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=fieldnames, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(rows)

    out_dir = Path(directory)
    stamp = datetime.now(tz=timezone.utc).strftime("%Y%m%d-%H%M%S")
    path = out_dir / f"subscribers-{stamp}.csv"

    # Disk IO is blocking: push it off the event loop so a big export cannot
    # stall payment polling or delay someone's invite link.
    def _write() -> None:
        out_dir.mkdir(parents=True, exist_ok=True)
        path.write_text(buffer.getvalue(), encoding="utf-8")

    await asyncio.to_thread(_write)
    return path
