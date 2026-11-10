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


def main_menu_keyboard(
    plans: list[Plan],
    currencies: list[str],
    lang: str,
    *,
    show_trial: bool = False,
    support_url: str | None = None,
    order_url: str | None = None,
) -> InlineKeyboardMarkup:
    """Plans on top, then the self-service rows, then the optional order button."""
    builder = InlineKeyboardBuilder()
    builder.attach(InlineKeyboardBuilder.from_markup(plans_keyboard(plans, currencies, lang)))

    if show_trial:
        builder.row(
            InlineKeyboardButton(text=t(lang, "menu_trial"), callback_data="menu:trial")
        )
    builder.row(
        InlineKeyboardButton(text=t(lang, "menu_status"), callback_data="menu:status"),
        InlineKeyboardButton(text=t(lang, "menu_access"), callback_data="menu:access"),
    )
    bottom = [InlineKeyboardButton(text=t(lang, "menu_lang"), callback_data="menu:lang")]
    if support_url:
        bottom.append(InlineKeyboardButton(text=t(lang, "menu_support"), url=support_url))
    builder.row(*bottom)
    if order_url:
        builder.row(InlineKeyboardButton(text=t(lang, "menu_order"), url=order_url))
    return builder.as_markup()


def back_keyboard(lang: str, order_url: str | None = None) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text=t(lang, "menu_back"), callback_data="menu:home")]]
    if order_url:
        rows.append([InlineKeyboardButton(text=t(lang, "menu_order"), url=order_url)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


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


def ad_inline_keyboard(url: str, text: str = "🔗 Открыть объявление") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text=text, url=url),
            ]
        ]
    )


def amount_line(amount, decimals: int) -> str:
    """The exact string a payer must send — trailing zeros preserved."""
    return format_amount(amount, decimals)
