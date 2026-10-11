"""Inline keyboards. Callback payloads are short and versioned by prefix."""

from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from gatekit.config import Plan
from gatekit.money import format_amount
from gatekit.texts import t


def plans_keyboard(
    plans: list[Plan], currencies: list[str], lang: str, decimals: int = 6
) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    primary = currencies[0] if currencies else "USDT"
    for plan in plans:
        price = plan.price_for(primary)
        if price is None:
            continue
        label = t(
            lang,
            "plan_button",
            label=plan.label,
            price=f"{price.normalize():f}",
            currency=primary,
        )
        builder.button(text=label, callback_data=f"plan:{plan.code}")
    builder.adjust(1)
    return builder.as_markup()


def currency_keyboard(plan: Plan, currencies: list[str]) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for currency in currencies:
        price = plan.price_for(currency)
        if price is None:
            continue
        builder.button(
            text=f"{currency} — {price.normalize():f}",
            callback_data=f"cur:{plan.code}:{currency}",
        )
    builder.adjust(1)
    return builder.as_markup()


def invoice_keyboard(invoice_id: int, lang: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=t(lang, "invoice_check"), callback_data=f"chk:{invoice_id}"
                )
            ],
            [
                InlineKeyboardButton(
                    text=t(lang, "invoice_cancel"), callback_data=f"cxl:{invoice_id}"
                )
            ],
        ]
    )


def lang_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="English", callback_data="lang:en"),
                InlineKeyboardButton(text="Русский", callback_data="lang:ru"),
            ]
        ]
    )


def confirm_kick_keyboard(user_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Remove", callback_data=f"kick:{user_id}"),
                InlineKeyboardButton(text="🚫 Keep", callback_data=f"keep:{user_id}"),
            ]
        ]
    )


def amount_line(amount, decimals: int) -> str:
    """The exact string a payer must send — trailing zeros preserved."""
    return format_amount(amount, decimals)
