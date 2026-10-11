"""Subscriber-facing flow: plans → invoice → payment check → access."""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone

from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import async_sessionmaker

from gatekit.bot.keyboards import (
    amount_line,
    currency_keyboard,
    invoice_keyboard,
    lang_keyboard,
    plans_keyboard,
)
from gatekit.config import Settings
from gatekit.db.models import Invoice, InvoiceStatus, Membership, MembershipState
from gatekit.services import invoices as invoice_service
from gatekit.services.access import AccessManager
from gatekit.texts import state_label, t

log = logging.getLogger(__name__)
router = Router(name="user")

# TRON tx ids are 64 hex chars; TON hashes are base64-ish. Accept both shapes.
TX_HASH_RE = re.compile(r"^(?:0x)?[0-9a-fA-F]{64}$|^[A-Za-z0-9+/=_-]{44,64}$")


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _fmt(value: datetime) -> str:
    return _aware(value).strftime("%Y-%m-%d %H:%M UTC")


def _days_left(expires_at: datetime) -> int:
    delta = _aware(expires_at) - datetime.now(tz=timezone.utc)
    return max(0, delta.days)


async def _lang(session, user_id: int, settings: Settings) -> str:
    from gatekit.db.models import User

    user = await session.get(User, user_id)
    return user.lang if user else settings.default_lang


@router.message(CommandStart())
async def cmd_start(
    message: Message,
    settings: Settings,
    sessionmaker: async_sessionmaker,
    **_: object,
) -> None:
    if message.from_user is None:
        return
    async with sessionmaker() as session:
        user = await invoice_service.ensure_user(
            session,
            user_id=message.from_user.id,
            username=message.from_user.username,
            full_name=message.from_user.full_name,
            default_lang=_guess_lang(message, settings),
        )
        await session.commit()
        lang = user.lang

        membership = await session.get(Membership, user.id)
        plans = settings.plan_list
        currencies = settings.enabled_currencies

        if not plans or not currencies:
            await message.answer(t(lang, "no_plans"))
            return

        if membership and membership.state in {MembershipState.ACTIVE, MembershipState.PENDING}:
            plan = settings.plan_by_code(membership.plan_code)
            await message.answer(
                t(
                    lang,
                    "start_active",
                    plan=plan.label if plan else membership.plan_code,
                    expires=_fmt(membership.expires_at),
                    days_left=_days_left(membership.expires_at),
                ),
                reply_markup=plans_keyboard(plans, currencies, lang),
            )
            return

        await message.answer(
            t(lang, "start_greeting"),
            reply_markup=plans_keyboard(plans, currencies, lang),
        )


def _guess_lang(message: Message, settings: Settings) -> str:
    code = (message.from_user.language_code or "").lower() if message.from_user else ""
    if code.startswith("ru") or code.startswith("uk") or code.startswith("be"):
        return "ru"
    if code.startswith("en"):
        return "en"
    return settings.default_lang


@router.callback_query(F.data.startswith("plan:"))
async def pick_plan(
    callback: CallbackQuery,
    settings: Settings,
    sessionmaker: async_sessionmaker,
    **_: object,
) -> None:
    if callback.data is None or callback.from_user is None:
        return
    code = callback.data.split(":", 1)[1]
    plan = settings.plan_by_code(code)
    if plan is None:
        await callback.answer("Unknown plan", show_alert=True)
        return

    currencies = [c for c in settings.enabled_currencies if plan.price_for(c) is not None]
    async with sessionmaker() as session:
        lang = await _lang(session, callback.from_user.id, settings)

    if len(currencies) > 1:
        await callback.message.answer(  # type: ignore[union-attr]
            t(lang, "choose_currency"), reply_markup=currency_keyboard(plan, currencies)
        )
        await callback.answer()
        return

    await _issue_invoice(callback, settings, sessionmaker, code, currencies[0])


@router.callback_query(F.data.startswith("cur:"))
async def pick_currency(
    callback: CallbackQuery,
    settings: Settings,
    sessionmaker: async_sessionmaker,
    **_: object,
) -> None:
    if callback.data is None:
        return
    _, plan_code, currency = callback.data.split(":", 2)
    await _issue_invoice(callback, settings, sessionmaker, plan_code, currency)


async def _issue_invoice(
    callback: CallbackQuery,
    settings: Settings,
    sessionmaker: async_sessionmaker,
    plan_code: str,
    currency: str,
) -> None:
    plan = settings.plan_by_code(plan_code)
    if plan is None or callback.from_user is None:
        await callback.answer("Unknown plan", show_alert=True)
        return

    async with sessionmaker() as session:
        lang = await _lang(session, callback.from_user.id, settings)
        try:
            invoice = await invoice_service.create_invoice(
                session,
                settings,
                user_id=callback.from_user.id,
                plan=plan,
                currency=currency,
            )
        except ValueError as exc:
            log.warning("Invoice creation refused: %s", exc)
            await callback.answer("This plan is not payable in that currency.", show_alert=True)
            return
        await session.commit()

        decimals = settings.decimals_for(currency)
        if currency == "TON":
            network_note = t(lang, "invoice_network_ton", code=invoice.code)
        else:
            network_note = t(lang, "invoice_network_trc20")

        text = t(
            lang,
            "invoice",
            code=invoice.code,
            amount=amount_line(invoice.amount_dec, decimals),
            currency=currency,
            address=settings.wallet_for(currency),
            network_note=network_note,
            ttl=settings.invoice_ttl_minutes,
        )
        await callback.message.answer(  # type: ignore[union-attr]
            text,
            reply_markup=invoice_keyboard(invoice.id, lang),
            disable_web_page_preview=True,
        )
    await callback.answer()


@router.callback_query(F.data.startswith("cxl:"))
async def cancel_invoice(
    callback: CallbackQuery,
    settings: Settings,
    sessionmaker: async_sessionmaker,
    **_: object,
) -> None:
    if callback.data is None:
        return
    invoice_id = int(callback.data.split(":", 1)[1])
    async with sessionmaker() as session:
        lang = await _lang(session, callback.from_user.id, settings)
        invoice = await session.get(Invoice, invoice_id)
        if invoice and invoice.status == InvoiceStatus.OPEN:
            invoice.status = InvoiceStatus.CANCELLED
            await session.commit()
    await callback.message.answer(t(lang, "invoice_cancelled"))  # type: ignore[union-attr]
    await callback.answer()


@router.callback_query(F.data.startswith("chk:"))
async def check_payment(
    callback: CallbackQuery,
    settings: Settings,
    sessionmaker: async_sessionmaker,
    watcher,
    **_: object,
) -> None:
    """Manual "I paid" button: poll the chain right now instead of waiting."""
    if callback.data is None or callback.from_user is None:
        return
    invoice_id = int(callback.data.split(":", 1)[1])

    async with sessionmaker() as session:
        lang = await _lang(session, callback.from_user.id, settings)
        invoice = await session.get(Invoice, invoice_id)

    if invoice is None:
        await callback.answer("Invoice not found", show_alert=True)
        return
    if invoice.status == InvoiceStatus.PAID:
        await callback.answer("Already confirmed ✅", show_alert=True)
        return

    await callback.answer(t(lang, "checking"))
    try:
        await watcher.tick()
    except Exception:  # noqa: BLE001 - a failed manual check must not break the chat
        log.exception("Manual payment check failed")

    async with sessionmaker() as session:
        refreshed = await session.get(Invoice, invoice_id)
        if refreshed and refreshed.status == InvoiceStatus.PAID:
            return  # the watcher already sent the confirmation + invite
        await callback.message.answer(  # type: ignore[union-attr]
            t(lang, "not_found_yet") + "\n\n" + t(lang, "send_tx_hash")
        )


@router.message(F.text.regexp(TX_HASH_RE))
async def check_by_hash(
    message: Message,
    settings: Settings,
    sessionmaker: async_sessionmaker,
    watcher,
    **_: object,
) -> None:
    """The user pasted a transaction hash — look it up directly."""
    if message.from_user is None or message.text is None:
        return
    tx_id = message.text.strip()

    async with sessionmaker() as session:
        lang = await _lang(session, message.from_user.id, settings)
        open_invoice = await invoice_service.user_open_invoice(session, message.from_user.id)

    if open_invoice is None:
        await message.answer(t(lang, "access_none"))
        return

    support = settings.support_contact or "the channel owner"
    client = next(
        (c for c in watcher.clients if c.currency == open_invoice.currency), None
    )
    if client is None:
        await message.answer(t(lang, "tx_hash_not_found", support=support))
        return

    await message.answer(t(lang, "checking"))
    try:
        transfer = await client.fetch_by_tx_id(tx_id)
    except Exception:  # noqa: BLE001
        log.exception("Lookup by hash failed")
        transfer = None

    if transfer is None:
        await message.answer(t(lang, "tx_hash_not_found", support=support))
        return

    granted = await watcher.process_transfers([transfer], chain_name="manual")
    if not granted:
        # The money exists but does not line up with an open invoice. Never
        # auto-grant on a guess — a human decides.
        await message.answer(t(lang, "tx_hash_wrong_amount", support=support))


@router.message(Command("status"))
async def cmd_status(
    message: Message,
    settings: Settings,
    sessionmaker: async_sessionmaker,
    **_: object,
) -> None:
    if message.from_user is None:
        return
    async with sessionmaker() as session:
        lang = await _lang(session, message.from_user.id, settings)
        membership = await session.get(Membership, message.from_user.id)
        if membership is None:
            await message.answer(t(lang, "access_none"))
            return
        plan = settings.plan_by_code(membership.plan_code)
        state = (
            membership.state.value
            if hasattr(membership.state, "value")
            else str(membership.state)
        )
        await message.answer(
            t(
                lang,
                "status",
                plan=plan.label if plan else membership.plan_code,
                state=state_label(lang, state),
                expires=_fmt(membership.expires_at),
                days_left=_days_left(membership.expires_at),
                payments=membership.payments_count or 0,
            )
        )


@router.message(Command("access"))
async def cmd_access(
    message: Message,
    settings: Settings,
    sessionmaker: async_sessionmaker,
    access: AccessManager,
    **_: object,
) -> None:
    """Re-issue an invite link for an already-paid subscriber."""
    if message.from_user is None:
        return
    async with sessionmaker() as session:
        lang = await _lang(session, message.from_user.id, settings)
        membership = await session.get(Membership, message.from_user.id)
        if membership is None or membership.state not in {
            MembershipState.ACTIVE,
            MembershipState.PENDING,
            MembershipState.IN_GRACE,
        }:
            await message.answer(t(lang, "access_none"))
            return
        try:
            invite = await access.create_invite(message.from_user.id)
        except Exception:  # noqa: BLE001
            log.exception("Re-issuing invite failed")
            await message.answer(t(lang, "access_error"))
            return
        membership.invite_link = invite
        await session.commit()
    await message.answer(t(lang, "access_link", invite=invite), disable_web_page_preview=True)


@router.message(Command("lang"))
async def cmd_lang(message: Message, **_: object) -> None:
    await message.answer("Choose a language / Выберите язык:", reply_markup=lang_keyboard())


@router.callback_query(F.data.startswith("lang:"))
async def set_lang(
    callback: CallbackQuery,
    sessionmaker: async_sessionmaker,
    **_: object,
) -> None:
    from gatekit.db.models import User

    if callback.data is None or callback.from_user is None:
        return
    lang = callback.data.split(":", 1)[1]
    async with sessionmaker() as session:
        user = await session.get(User, callback.from_user.id)
        if user:
            user.lang = lang
            await session.commit()
    await callback.message.answer(t(lang, "lang_switched"))  # type: ignore[union-attr]
    await callback.answer()


@router.message(Command("help"))
async def cmd_help(
    message: Message,
    settings: Settings,
    sessionmaker: async_sessionmaker,
    **_: object,
) -> None:
    if message.from_user is None:
        return
    async with sessionmaker() as session:
        lang = await _lang(session, message.from_user.id, settings)
    if settings.support_contact:
        await message.answer(t(lang, "help", support=settings.support_contact))
    else:
        await message.answer(t(lang, "help_no_support"))
