"""Admin panel, inside the bot. No web dashboard to secure, no extra port open.

Access is restricted to ADMIN_IDS. Every mutating command writes to the audit
log with ``actor="admin:<id>"`` so owner actions and scheduler actions are
always distinguishable after the fact.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, FSInputFile, Message
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from gatekit.bot.keyboards import confirm_kick_keyboard
from gatekit.config import Settings
from gatekit.db.models import AuditLog, Membership, MembershipState, Payment, PaymentStatus, User
from gatekit.services import stats as stats_service
from gatekit.services.access import AccessManager, audit
from gatekit.texts import state_label

log = logging.getLogger(__name__)
router = Router(name="admin")


def _is_admin(message_or_cb, settings: Settings) -> bool:
    user = getattr(message_or_cb, "from_user", None)
    return bool(user and user.id in settings.admin_id_set)


def _safe_mode_label(settings: Settings) -> str:
    return "ON — nobody gets removed" if settings.safe_mode else "OFF — enforcing"


def _fmt(value: datetime | None) -> str:
    if value is None:
        return "—"
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M")


@router.message(Command("admin"))
async def cmd_admin(message: Message, settings: Settings, **_: object) -> None:
    if not _is_admin(message, settings):
        return
    await message.answer(
        "<b>Admin panel</b>\n\n"
        "/stats — key numbers\n"
        "/subs — subscriber list\n"
        "/expiring — who lapses within 7 days\n"
        "/export — subscriber CSV\n"
        "/unmatched — payments with no matching invoice\n"
        "/pending — removals awaiting confirmation\n"
        "/audit &lt;user_id&gt; — last 20 access events for a user\n"
        "/grant &lt;user_id&gt; &lt;plan_code&gt; — give access manually\n"
        "/extend &lt;user_id&gt; &lt;days&gt; — add days\n"
        "/revoke &lt;user_id&gt; — remove now\n"
        "/broadcast &lt;text&gt; — message every paying member\n"
        "/config — what this instance is currently doing"
    )


@router.message(Command("config"))
async def cmd_config(message: Message, settings: Settings, **_: object) -> None:
    if not _is_admin(message, settings):
        return
    plans = ", ".join(f"{p.label}={p.price_usdt.normalize():f}" for p in settings.plan_list)
    await message.answer(
        "<b>Current configuration</b>\n"
        f"Channel: <code>{settings.channel_id}</code>\n"
        f"Plans: {plans}\n"
        f"Rails: {', '.join(settings.enabled_currencies) or 'none'}\n"
        f"TRON network: <b>{settings.tron_network}</b>\n"
        f"Wallet: <code>{settings.tron_wallet or '—'}</code>\n\n"
        f"SAFE_MODE: <b>{_safe_mode_label(settings)}</b>\n"
        f"Grace period: {settings.grace_period_days} day(s)\n"
        f"Payment protection: {settings.payment_protection_hours}h\n"
        f"Manual kick confirmation: {settings.require_manual_kick_confirmation}\n"
        f"Reminders: {settings.reminder_day_list} day(s) before expiry\n\n"
        "<i>These come from .env — edit the file and restart to change them.</i>"
    )


@router.message(Command("stats"))
async def cmd_stats(
    message: Message, settings: Settings, sessionmaker: async_sessionmaker, **_: object
) -> None:
    if not _is_admin(message, settings):
        return
    async with sessionmaker() as session:
        s = await stats_service.collect(session)
    await message.answer(
        "<b>Numbers</b>\n"
        f"Paying now: <b>{s.paying}</b> (active {s.active}, grace {s.in_grace})\n"
        f"Paid but not joined: {s.pending}\n"
        f"Ended: {s.revoked}\n"
        f"Lapsing within 7 days: <b>{s.expiring_7d}</b>\n\n"
        f"Revenue, last 30 days: <b>{s.revenue_30d.normalize():f} USDT</b> "
        f"({s.payments_30d} payment(s))\n"
        f"Revenue, all time: {s.revenue_total.normalize():f} USDT\n"
        + (
            f"TON, last 30 days: {s.revenue_ton_30d.normalize():f} TON "
            f"(all time {s.revenue_ton_total.normalize():f})\n"
            if s.revenue_ton_total
            else ""
        )
        + f"Invoice → payment conversion, 30d: {s.conversion_30d}%\n\n"
        f"Open invoices: {s.open_invoices}\n"
        f"Unmatched payments: <b>{s.unmatched_payments}</b>"
        + ("\n\n⚠️ Unmatched money is waiting — see /unmatched" if s.unmatched_payments else "")
    )


@router.message(Command("subs"))
async def cmd_subs(
    message: Message, settings: Settings, sessionmaker: async_sessionmaker, **_: object
) -> None:
    if not _is_admin(message, settings):
        return
    async with sessionmaker() as session:
        stmt = (
            select(Membership, User)
            .join(User, User.id == Membership.user_id)
            .order_by(Membership.expires_at.desc())
            .limit(30)
        )
        rows = list(await session.execute(stmt))

    if not rows:
        await message.answer("No subscribers yet.")
        return

    lines = ["<b>Subscribers</b> (most recent 30)", ""]
    for membership, user in rows:
        state = (
            membership.state.value
            if hasattr(membership.state, "value")
            else str(membership.state)
        )
        lines.append(
            f"<code>{user.id}</code> {user.display} — {membership.plan_code}, "
            f"{state_label('en', state)}, until {_fmt(membership.expires_at)}"
        )
    await message.answer("\n".join(lines))


@router.message(Command("expiring"))
async def cmd_expiring(
    message: Message, settings: Settings, sessionmaker: async_sessionmaker, **_: object
) -> None:
    if not _is_admin(message, settings):
        return
    from datetime import timedelta

    now = datetime.now(tz=timezone.utc)
    async with sessionmaker() as session:
        stmt = (
            select(Membership, User)
            .join(User, User.id == Membership.user_id)
            .where(
                Membership.state.in_([MembershipState.ACTIVE, MembershipState.PENDING]),
                Membership.expires_at <= now + timedelta(days=7),
            )
            .order_by(Membership.expires_at.asc())
        )
        rows = list(await session.execute(stmt))

    if not rows:
        await message.answer("Nobody lapses in the next 7 days.")
        return
    lines = ["<b>Lapsing within 7 days</b>", ""]
    for membership, user in rows:
        lines.append(
            f"<code>{user.id}</code> {user.display} — until {_fmt(membership.expires_at)}"
        )
    lines.append("")
    lines.append("<i>Reminders go out automatically.</i>")
    await message.answer("\n".join(lines))


@router.message(Command("export"))
async def cmd_export(
    message: Message, settings: Settings, sessionmaker: async_sessionmaker, **_: object
) -> None:
    if not _is_admin(message, settings):
        return
    async with sessionmaker() as session:
        path = await stats_service.export_csv(session)
    await message.answer_document(
        FSInputFile(str(path)), caption=f"Subscriber export — {path.name}"
    )


@router.message(Command("unmatched"))
async def cmd_unmatched(
    message: Message, settings: Settings, sessionmaker: async_sessionmaker, **_: object
) -> None:
    if not _is_admin(message, settings):
        return
    async with sessionmaker() as session:
        rows = list(
            (
                await session.scalars(
                    select(Payment)
                    .where(Payment.status == PaymentStatus.UNMATCHED)
                    .order_by(Payment.chain_ts.desc())
                    .limit(20)
                )
            ).all()
        )
    if not rows:
        await message.answer("No unmatched payments. Every coin that arrived found its invoice.")
        return
    lines = ["<b>Unmatched payments</b>", ""]
    for payment in rows:
        lines.append(
            f"{_fmt(payment.chain_ts)} — <b>{payment.amount} {payment.currency}</b>\n"
            f"from <code>{payment.from_address or '?'}</code>\n"
            f"tx <code>{payment.tx_id}</code>"
        )
        lines.append("")
    lines.append(
        "<i>Usually someone rounded the amount. Find who paid, then use "
        "/grant &lt;user_id&gt; &lt;plan_code&gt;.</i>"
    )
    await message.answer("\n".join(lines))


@router.message(Command("pending"))
async def cmd_pending(
    message: Message, settings: Settings, sessionmaker: async_sessionmaker, **_: object
) -> None:
    if not _is_admin(message, settings):
        return
    async with sessionmaker() as session:
        rows = list(
            (
                await session.scalars(
                    select(Membership).where(Membership.pending_kick.is_(True)).limit(20)
                )
            ).all()
        )
    if not rows:
        await message.answer("Nothing waiting for confirmation.")
        return
    for membership in rows:
        await message.answer(
            f"Removal queued for <code>{membership.user_id}</code>\n"
            f"Expired: {_fmt(membership.expires_at)}\n"
            f"Last payment: {_fmt(membership.last_payment_at)}",
            reply_markup=confirm_kick_keyboard(membership.user_id),
        )


@router.callback_query(F.data.startswith("kick:"))
async def confirm_kick(
    callback: CallbackQuery,
    settings: Settings,
    sessionmaker: async_sessionmaker,
    access: AccessManager,
    **_: object,
) -> None:
    if not _is_admin(callback, settings) or callback.data is None:
        await callback.answer()
        return
    user_id = int(callback.data.split(":", 1)[1])
    async with sessionmaker() as session:
        ok = await access.revoke(
            session,
            user_id=user_id,
            reason="admin confirmed queued removal",
            actor=f"admin:{callback.from_user.id}",
        )
        await session.commit()
    await callback.answer("Removed" if ok else "Failed — see logs", show_alert=not ok)


@router.callback_query(F.data.startswith("keep:"))
async def cancel_kick(
    callback: CallbackQuery,
    settings: Settings,
    sessionmaker: async_sessionmaker,
    **_: object,
) -> None:
    if not _is_admin(callback, settings) or callback.data is None:
        await callback.answer()
        return
    user_id = int(callback.data.split(":", 1)[1])
    async with sessionmaker() as session:
        membership = await session.get(Membership, user_id)
        if membership:
            membership.pending_kick = False
            await audit(
                session,
                "kick_cancelled",
                user_id=user_id,
                actor=f"admin:{callback.from_user.id}",
            )
            await session.commit()
    await callback.answer("Kept")


@router.message(Command("grant"))
async def cmd_grant(
    message: Message,
    settings: Settings,
    sessionmaker: async_sessionmaker,
    access: AccessManager,
    **_: object,
) -> None:
    if not _is_admin(message, settings):
        return
    parts = (message.text or "").split()
    if len(parts) != 3:
        await message.answer("Usage: /grant &lt;user_id&gt; &lt;plan_code&gt;")
        return
    try:
        user_id = int(parts[1])
    except ValueError:
        await message.answer("user_id must be a number.")
        return
    plan = settings.plan_by_code(parts[2])
    if plan is None:
        codes = ", ".join(p.code for p in settings.plan_list)
        await message.answer(f"Unknown plan. Available: {codes}")
        return

    async with sessionmaker() as session:
        await invoice_ensure_user(session, user_id, settings)
        membership, invite = await access.grant(
            session,
            user_id=user_id,
            plan_code=plan.code,
            plan_days=plan.days,
            actor=f"admin:{message.from_user.id}",  # type: ignore[union-attr]
        )
        await session.commit()

    await message.answer(
        f"Granted <b>{plan.label}</b> to <code>{user_id}</code> until "
        f"{_fmt(membership.expires_at)}."
    )
    if invite:
        try:
            await message.bot.send_message(  # type: ignore[union-attr]
                user_id,
                f"Access granted by the owner.\nYour invite link:\n{invite}",
                disable_web_page_preview=True,
            )
        except Exception as exc:  # noqa: BLE001
            await message.answer(f"Could not DM the user: {exc}\nLink: {invite}")


async def invoice_ensure_user(session, user_id: int, settings: Settings) -> None:
    """Create a bare user row for someone who never started the bot."""
    from gatekit.services.invoices import ensure_user

    await ensure_user(
        session,
        user_id=user_id,
        username=None,
        full_name=None,
        default_lang=settings.default_lang,
    )


@router.message(Command("extend"))
async def cmd_extend(
    message: Message,
    settings: Settings,
    sessionmaker: async_sessionmaker,
    access: AccessManager,
    **_: object,
) -> None:
    if not _is_admin(message, settings):
        return
    parts = (message.text or "").split()
    if len(parts) != 3:
        await message.answer("Usage: /extend &lt;user_id&gt; &lt;days&gt;")
        return
    try:
        user_id, days = int(parts[1]), int(parts[2])
    except ValueError:
        await message.answer("Both arguments must be numbers.")
        return

    async with sessionmaker() as session:
        membership = await access.extend(
            session,
            user_id=user_id,
            days=days,
            actor=f"admin:{message.from_user.id}",  # type: ignore[union-attr]
        )
        await session.commit()
    if membership is None:
        await message.answer("No membership for that user.")
        return
    await message.answer(
        f"Extended <code>{user_id}</code> by {days} day(s) → {_fmt(membership.expires_at)}"
    )


@router.message(Command("revoke"))
async def cmd_revoke(
    message: Message,
    settings: Settings,
    sessionmaker: async_sessionmaker,
    access: AccessManager,
    **_: object,
) -> None:
    if not _is_admin(message, settings):
        return
    parts = (message.text or "").split()
    if len(parts) != 2:
        await message.answer("Usage: /revoke &lt;user_id&gt;")
        return
    try:
        user_id = int(parts[1])
    except ValueError:
        await message.answer("user_id must be a number.")
        return

    async with sessionmaker() as session:
        ok = await access.revoke(
            session,
            user_id=user_id,
            reason="manual admin removal",
            actor=f"admin:{message.from_user.id}",  # type: ignore[union-attr]
        )
        await session.commit()
    await message.answer("Removed." if ok else "Failed — check the logs.")


@router.message(Command("audit"))
async def cmd_audit(
    message: Message, settings: Settings, sessionmaker: async_sessionmaker, **_: object
) -> None:
    if not _is_admin(message, settings):
        return
    parts = (message.text or "").split()
    if len(parts) != 2:
        await message.answer("Usage: /audit &lt;user_id&gt;")
        return
    try:
        user_id = int(parts[1])
    except ValueError:
        await message.answer("user_id must be a number.")
        return

    async with sessionmaker() as session:
        rows = list(
            (
                await session.scalars(
                    select(AuditLog)
                    .where(AuditLog.user_id == user_id)
                    .order_by(AuditLog.ts.desc())
                    .limit(20)
                )
            ).all()
        )
    if not rows:
        await message.answer("No audit entries for that user.")
        return
    lines = [f"<b>Access history for {user_id}</b>", ""]
    for row in rows:
        lines.append(f"{_fmt(row.ts)} · <b>{row.action}</b> · {row.actor}")
        if row.details:
            lines.append(f"<code>{row.details[:300]}</code>")
    await message.answer("\n".join(lines))


@router.message(Command("broadcast"))
async def cmd_broadcast(
    message: Message, settings: Settings, sessionmaker: async_sessionmaker, **_: object
) -> None:
    if not _is_admin(message, settings):
        return
    text = (message.text or "").partition(" ")[2].strip()
    if not text:
        await message.answer("Usage: /broadcast &lt;your message&gt;")
        return

    async with sessionmaker() as session:
        user_ids = list(
            (
                await session.scalars(
                    select(Membership.user_id).where(
                        Membership.state.in_(
                            [
                                MembershipState.ACTIVE,
                                MembershipState.PENDING,
                                MembershipState.IN_GRACE,
                            ]
                        )
                    )
                )
            ).all()
        )

    sent = failed = 0
    for user_id in user_ids:
        try:
            await message.bot.send_message(user_id, text)  # type: ignore[union-attr]
            sent += 1
        except Exception:  # noqa: BLE001
            failed += 1
    await message.answer(f"Broadcast done: {sent} delivered, {failed} failed.")
