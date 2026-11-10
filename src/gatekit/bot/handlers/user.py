"""Subscriber-facing flow: plans → invoice → payment check → access."""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone

from collections.abc import Awaitable, Callable

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from aiogram.types import User as TgUser
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from gatekit.bot.keyboards import (
    amount_line,
    back_keyboard,
    currency_keyboard,
    invoice_keyboard,
    lang_keyboard,
    main_menu_keyboard,
)
from gatekit.config import Settings
from gatekit.db.models import AuditLog, Invoice, InvoiceStatus, Membership, MembershipState, User
from gatekit.services import invoices as invoice_service
from gatekit.services.access import AccessManager, audit
from gatekit.texts import LOCALES, state_label, t

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


def _plan_label(settings: Settings, plan_code: str, lang: str) -> str:
    plan = settings.plan_by_code(plan_code)
    if plan:
        return plan.label
    if plan_code == "trial":
        return "Тестовый доступ (24ч)" if lang == "ru" else "Free Trial (24h)"
    return plan_code


async def _home_screen(
    session, user: User, settings: Settings
) -> tuple[str, InlineKeyboardMarkup | None]:
    """Text + keyboard of the main menu. Shared by /start and the "back" button."""
    lang = user.lang
    plans = settings.plan_list
    currencies = settings.enabled_currencies
    if not plans or not currencies:
        return t(lang, "no_plans"), None

    membership = await session.get(Membership, user.id)
    markup = main_menu_keyboard(
        plans,
        currencies,
        lang,
        show_trial=not user.trial_used and membership is None,
        support_url=settings.support_url,
        order_url=settings.order_url,
    )
    if membership and membership.state in {MembershipState.ACTIVE, MembershipState.PENDING}:
        text = t(
            lang,
            "start_active",
            plan=_plan_label(settings, membership.plan_code, lang),
            expires=_fmt(membership.expires_at),
            days_left=_days_left(membership.expires_at),
        )
        return text, markup
    return t(lang, "start_greeting"), markup


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
        text, markup = await _home_screen(session, user, settings)
    await message.answer(text, reply_markup=markup)


def _guess_lang(message: Message, settings: Settings) -> str:
    return _guess_lang_code(message.from_user, settings)


def _guess_lang_code(tg_user: TgUser | None, settings: Settings) -> str:
    code = (tg_user.language_code or "").lower() if tg_user else ""
    if code.startswith("ru") or code.startswith("uk") or code.startswith("be"):
        return "ru"
    if code.startswith("en"):
        return "en"
    return settings.default_lang


@router.callback_query(F.data.startswith("menu:"))
async def menu_action(
    callback: CallbackQuery,
    settings: Settings,
    sessionmaker: async_sessionmaker,
    access: AccessManager,
    **_: object,
) -> None:
    """Main-menu buttons: the same flows as the slash commands, one tap away."""
    if callback.data is None or callback.from_user is None or callback.message is None:
        return
    action = callback.data.split(":", 1)[1]
    tg_user = callback.from_user
    reply = callback.message.answer

    if action == "home":
        async with sessionmaker() as session:
            user = await invoice_service.ensure_user(
                session,
                user_id=tg_user.id,
                username=tg_user.username,
                full_name=tg_user.full_name,
                default_lang=_guess_lang_code(tg_user, settings),
            )
            await session.commit()
            text, markup = await _home_screen(session, user, settings)
        try:
            await callback.message.edit_text(text, reply_markup=markup)  # type: ignore[union-attr]
        except TelegramBadRequest:
            await reply(text, reply_markup=markup)
    elif action == "lang":
        await reply("Choose a language / Выберите язык:", reply_markup=lang_keyboard())
    elif action == "trial":
        await _trial_flow(tg_user, reply, settings, sessionmaker, access)
    elif action == "status":
        async with sessionmaker() as session:
            lang = await _lang(session, tg_user.id, settings)
            text = await _status_text(session, tg_user.id, settings, lang)
        await reply(text, reply_markup=back_keyboard(lang, settings.order_url))
    elif action == "access":
        text, lang = await _access_text(tg_user.id, settings, sessionmaker, access)
        await reply(
            text,
            reply_markup=back_keyboard(lang, settings.order_url),
            disable_web_page_preview=True,
        )
    await callback.answer()


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

    if not currencies:
        await callback.answer(t(lang, "no_plans"), show_alert=True)
        return
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
    if callback.data is None or callback.from_user is None:
        return
    invoice_id = int(callback.data.split(":", 1)[1])
    async with sessionmaker() as session:
        lang = await _lang(session, callback.from_user.id, settings)
        invoice = await session.get(Invoice, invoice_id)
        if invoice is not None and invoice.user_id != callback.from_user.id:
            await callback.answer(t(lang, "not_your_invoice"), show_alert=True)
            return
        if invoice and invoice.status == InvoiceStatus.OPEN:
            invoice.status = InvoiceStatus.CANCELLED
            await session.commit()
    try:
        # Drop the "I paid" button so a cancelled invoice cannot be poked again.
        await callback.message.edit_reply_markup(reply_markup=None)  # type: ignore[union-attr]
    except TelegramBadRequest:
        pass
    await callback.message.answer(  # type: ignore[union-attr]
        t(lang, "invoice_cancelled"), reply_markup=back_keyboard(lang)
    )
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
    if invoice.user_id != callback.from_user.id:
        await callback.answer(t(lang, "not_your_invoice"), show_alert=True)
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


@router.message(Command("trial"))
async def cmd_trial(
    message: Message,
    settings: Settings,
    sessionmaker: async_sessionmaker,
    access: AccessManager,
    **_: object,
) -> None:
    """Grant 24 hours of free trial access to the user."""
    if message.from_user is None:
        return
    await _trial_flow(message.from_user, message.answer, settings, sessionmaker, access)


Reply = Callable[..., Awaitable[object]]


async def _trial_flow(
    tg_user: TgUser,
    reply: Reply,
    settings: Settings,
    sessionmaker: async_sessionmaker,
    access: AccessManager,
) -> None:
    async with sessionmaker() as session:
        user = await invoice_service.ensure_user(
            session,
            user_id=tg_user.id,
            username=tg_user.username,
            full_name=tg_user.full_name,
            default_lang=_guess_lang_code(tg_user, settings),
        )
        lang = user.lang

        # 1. Check if trial was already used (flag, audit log, or past trial membership)
        if getattr(user, "trial_used", False):
            await reply(t(lang, "trial_already_used"))
            return

        trial_audit = await session.scalar(
            select(AuditLog).where(
                AuditLog.user_id == user.id,
                AuditLog.action == "trial_granted",
            )
        )
        if trial_audit is not None:
            user.trial_used = True
            await session.commit()
            await reply(t(lang, "trial_already_used"))
            return

        membership = await session.get(Membership, user.id)
        if membership and membership.plan_code == "trial":
            user.trial_used = True
            await session.commit()
            await reply(t(lang, "trial_already_used"))
            return

        # 2. Check if user already has an active or in-grace paid subscription
        now = datetime.now(tz=timezone.utc)
        if (
            membership
            and (
                (
                    membership.state in {MembershipState.ACTIVE, MembershipState.PENDING}
                    and _aware(membership.expires_at) > now
                )
                or membership.state == MembershipState.IN_GRACE
            )
            and membership.plan_code != "trial"
        ):
            await reply(
                t(lang, "trial_already_active", expires=_fmt(membership.expires_at))
            )
            return

        # 3. Grant 24 hours of free trial access
        user.trial_used = True
        duration = timedelta(hours=settings.trial_duration_hours)
        membership, invite = await access.grant(
            session,
            user_id=user.id,
            plan_code="trial",
            plan_days=1,
            actor="trial",
            duration=duration,
        )
        await audit(
            session,
            "trial_granted",
            user_id=user.id,
            actor="trial",
            expires_at=membership.expires_at,
            hours=settings.trial_duration_hours,
        )
        await session.commit()

        invite_section = ""
        if invite:
            invite_section = t(lang, "access_link", invite=invite) + "\n\n"

        await reply(
            t(
                lang,
                "trial_granted",
                expires=_fmt(membership.expires_at),
                invite_section=invite_section,
            ),
            disable_web_page_preview=True,
        )


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
        text = await _status_text(session, message.from_user.id, settings, lang)
    await message.answer(text)


async def _status_text(session, user_id: int, settings: Settings, lang: str) -> str:
    membership = await session.get(Membership, user_id)
    if membership is None:
        return t(lang, "access_none")
    state = (
        membership.state.value
        if hasattr(membership.state, "value")
        else str(membership.state)
    )
    return t(
        lang,
        "status",
        plan=_plan_label(settings, membership.plan_code, lang),
        state=state_label(lang, state),
        expires=_fmt(membership.expires_at),
        days_left=_days_left(membership.expires_at),
        payments=membership.payments_count or 0,
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
    text, _lang_code = await _access_text(message.from_user.id, settings, sessionmaker, access)
    await message.answer(text, disable_web_page_preview=True)


async def _access_text(
    user_id: int,
    settings: Settings,
    sessionmaker: async_sessionmaker,
    access: AccessManager,
) -> tuple[str, str]:
    async with sessionmaker() as session:
        lang = await _lang(session, user_id, settings)
        membership = await session.get(Membership, user_id)
        if membership is None or membership.state not in {
            MembershipState.ACTIVE,
            MembershipState.PENDING,
            MembershipState.IN_GRACE,
        }:
            return t(lang, "access_none"), lang
        try:
            invite = await access.create_invite(user_id)
        except Exception:  # noqa: BLE001
            log.exception("Re-issuing invite failed")
            return t(lang, "access_error"), lang
        membership.invite_link = invite
        await session.commit()
    return t(lang, "access_link", invite=invite), lang


@router.message(Command("lang"))
async def cmd_lang(message: Message, **_: object) -> None:
    await message.answer("Choose a language / Выберите язык:", reply_markup=lang_keyboard())


@router.callback_query(F.data.startswith("lang:"))
async def set_lang(
    callback: CallbackQuery,
    settings: Settings,
    sessionmaker: async_sessionmaker,
    **_: object,
) -> None:
    if callback.data is None or callback.from_user is None:
        return
    lang = callback.data.split(":", 1)[1]
    if lang not in LOCALES:
        await callback.answer()
        return
    async with sessionmaker() as session:
        user = await session.get(User, callback.from_user.id)
        if user:
            user.lang = lang
            await session.commit()
            text, markup = await _home_screen(session, user, settings)
        else:
            text, markup = t(lang, "lang_switched"), None
    await callback.message.answer(t(lang, "lang_switched"))  # type: ignore[union-attr]
    if markup is not None:
        await callback.message.answer(text, reply_markup=markup)  # type: ignore[union-attr]
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
