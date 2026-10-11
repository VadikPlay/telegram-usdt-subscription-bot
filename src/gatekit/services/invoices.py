"""Invoice lifecycle: create, list open, expire, settle."""

from __future__ import annotations

import logging
import secrets
import string
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from gatekit.config import Plan, Settings
from gatekit.db.models import Invoice, InvoiceStatus, User, utcnow
from gatekit.money import allocate_unique_amount, quantize
from gatekit.services.matcher import OpenInvoice

log = logging.getLogger(__name__)

CODE_ALPHABET = string.ascii_uppercase + string.digits
CODE_AMBIGUOUS = {"O", "0", "I", "1", "L"}
CODE_SAFE = "".join(c for c in CODE_ALPHABET if c not in CODE_AMBIGUOUS)


def generate_code() -> str:
    """Short human-typable invoice code, e.g. GK-7F3A9 (used as the TON memo)."""
    body = "".join(secrets.choice(CODE_SAFE) for _ in range(5))
    return f"GK-{body}"


async def ensure_user(
    session: AsyncSession,
    *,
    user_id: int,
    username: str | None,
    full_name: str | None,
    default_lang: str,
) -> User:
    user = await session.get(User, user_id)
    if user is None:
        user = User(
            id=user_id,
            username=username,
            full_name=full_name,
            lang=default_lang,
        )
        session.add(user)
        await session.flush()
    else:
        # Keep the display data fresh — admins search by @username.
        if username != user.username or full_name != user.full_name:
            user.username = username
            user.full_name = full_name
    return user


async def open_invoices(session: AsyncSession, currency: str | None = None) -> list[Invoice]:
    stmt = select(Invoice).where(Invoice.status == InvoiceStatus.OPEN)
    if currency:
        stmt = stmt.where(Invoice.currency == currency)
    return list((await session.scalars(stmt)).all())


async def open_invoice_views(
    session: AsyncSession, currency: str | None = None
) -> list[OpenInvoice]:
    """Open invoices in the pure-logic shape the matcher expects."""
    return [to_view(inv) for inv in await open_invoices(session, currency)]


def to_view(invoice: Invoice) -> OpenInvoice:
    return OpenInvoice(
        id=invoice.id,
        code=invoice.code,
        user_id=invoice.user_id,
        currency=invoice.currency,
        amount=invoice.amount_dec,
        created_at=_aware(invoice.created_at),
        expires_at=_aware(invoice.expires_at),
    )


def _aware(value: datetime) -> datetime:
    """SQLite can hand back naive datetimes; normalise to UTC."""
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


async def create_invoice(
    session: AsyncSession,
    settings: Settings,
    *,
    user_id: int,
    plan: Plan,
    currency: str,
) -> Invoice:
    """Issue an invoice with an amount no other open invoice is using."""
    price = plan.price_for(currency)
    if price is None:
        raise ValueError(f"Plan {plan.code} has no price configured for {currency}")

    decimals = settings.decimals_for(currency)

    # Cancel this user's other open invoices for the same currency: two live
    # invoices for one person is how support tickets are born.
    await session.execute(
        update(Invoice)
        .where(
            Invoice.user_id == user_id,
            Invoice.currency == currency,
            Invoice.status == InvoiceStatus.OPEN,
        )
        .values(status=InvoiceStatus.CANCELLED)
    )

    taken = [inv.amount_dec for inv in await open_invoices(session, currency)]
    amount, tag = allocate_unique_amount(price, taken, decimals)

    now = utcnow()
    invoice = Invoice(
        code=generate_code(),
        user_id=user_id,
        plan_code=plan.code,
        plan_days=plan.days,
        currency=currency,
        base_amount=str(quantize(price, decimals)),
        amount=str(amount),
        tag=tag,
        status=InvoiceStatus.OPEN,
        created_at=now,
        expires_at=now + timedelta(minutes=settings.invoice_ttl_minutes),
    )
    session.add(invoice)
    await session.flush()
    log.info(
        "Invoice %s created: user=%s plan=%s %s %s",
        invoice.code,
        user_id,
        plan.code,
        amount,
        currency,
    )
    return invoice


async def settle_invoice(
    session: AsyncSession, invoice_id: int, *, tx_id: str, paid_at: datetime
) -> Invoice | None:
    """Mark an invoice paid. Returns None if it was already settled."""
    invoice = await session.get(Invoice, invoice_id)
    if invoice is None or invoice.status != InvoiceStatus.OPEN:
        return None
    invoice.status = InvoiceStatus.PAID
    invoice.paid_at = paid_at
    invoice.paid_tx_id = tx_id
    await session.flush()
    return invoice


async def expire_stale_invoices(session: AsyncSession, settings: Settings) -> int:
    """Close invoices nobody paid, once even the late-tolerance has passed."""
    cutoff = datetime.now(tz=timezone.utc) - timedelta(
        minutes=settings.payment_late_tolerance_minutes
    )
    result = await session.execute(
        update(Invoice)
        .where(Invoice.status == InvoiceStatus.OPEN, Invoice.expires_at < cutoff)
        .values(status=InvoiceStatus.EXPIRED)
    )
    count = int(result.rowcount or 0)
    if count:
        log.info("Expired %s stale invoice(s)", count)
    return count


async def user_open_invoice(
    session: AsyncSession, user_id: int
) -> Invoice | None:
    stmt = (
        select(Invoice)
        .where(Invoice.user_id == user_id, Invoice.status == InvoiceStatus.OPEN)
        .order_by(Invoice.created_at.desc())
        .limit(1)
    )
    return await session.scalar(stmt)


def total_usdt(previous: str | Decimal, add: Decimal) -> str:
    return str(quantize(Decimal(str(previous)) + add, 6))
