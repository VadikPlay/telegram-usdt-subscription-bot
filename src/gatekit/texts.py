"""User-facing strings, English and Russian.

Two languages because the two markets that actually pay for this are English
crypto communities and Russian-speaking ones. Adding a third is a matter of
copying one dict.
"""

from __future__ import annotations

from typing import Any

EN: dict[str, str] = {
    "start_greeting": (
        "<b>Welcome.</b>\n\n"
        "This bot sells access to a private channel and pays you directly — "
        "payments go straight to the owner's wallet, no middleman holds them.\n\n"
        "Pick a plan to get started."
    ),
    "start_active": (
        "<b>Your subscription is active.</b>\n"
        "Plan: <b>{plan}</b>\n"
        "Valid until: <b>{expires}</b> ({days_left} day(s) left)\n\n"
        "You can renew early — extra days stack on top of what you have."
    ),
    "choose_plan": "Choose a plan:",
    "choose_currency": "How would you like to pay?",
    "no_plans": "No plans are configured yet. Please contact the owner.",
    "invoice": (
        "<b>Invoice {code}</b>\n\n"
        "Send <b>exactly</b> this amount:\n"
        "<code>{amount}</code> {currency}\n\n"
        "To this address:\n<code>{address}</code>\n\n"
        "{network_note}"
        "⏳ Valid for {ttl} minutes.\n\n"
        "⚠️ <b>The amount must match to the last digit.</b> Those final decimals "
        "are how the bot recognises your payment — a rounded amount will not be "
        "credited automatically.\n\n"
        "Access is granted automatically, usually within a minute of the "
        "transfer confirming."
    ),
    "invoice_network_trc20": "Network: <b>TRON (TRC20)</b>. Do not send from another network.\n\n",
    "invoice_network_ton": "Network: <b>TON</b>. Put <code>{code}</code> in the comment field.\n\n",
    "invoice_check": "I have paid — check now",
    "invoice_cancel": "Cancel",
    "invoice_cancelled": "Invoice cancelled. Send /start when you are ready.",
    "checking": "Checking the chain…",
    "not_found_yet": (
        "No matching payment yet.\n\n"
        "If you just sent it, give the network a minute and press the button again. "
        "If you sent a different amount, use /help — an admin can sort it out."
    ),
    "send_tx_hash": (
        "If you have the transaction hash, send it to me now and I will look it up directly."
    ),
    "tx_hash_not_found": (
        "I could not find that transaction going to our wallet. Double-check the hash, "
        "or contact {support}."
    ),
    "tx_hash_wrong_amount": (
        "I found that transaction, but the amount does not match any open invoice. "
        "Contact {support} and quote the hash — this needs a human."
    ),
    "payment_confirmed": (
        "✅ <b>Payment confirmed. You're in.</b>\n\n"
        "Your personal invite link (single use, expires soon):\n{invite}\n\n"
        "Access valid until <b>{expires}</b>."
    ),
    "payment_confirmed_no_link": (
        "✅ <b>Payment confirmed.</b> Access is valid until <b>{expires}</b>.\n\n"
        "I could not generate your invite link just now — send /access in a minute "
        "and I will issue it."
    ),
    "access_link": "Your invite link (single use):\n{invite}",
    "access_none": "You do not have an active subscription. Send /start to pick a plan.",
    "access_error": "Could not create an invite link right now. Try again in a minute.",
    "status": (
        "<b>Subscription status</b>\n"
        "Plan: <b>{plan}</b>\n"
        "State: <b>{state}</b>\n"
        "Valid until: <b>{expires}</b> ({days_left} day(s) left)\n"
        "Payments made: {payments}"
    ),
    "reminder": (
        "⏰ Your access expires in <b>{days} day(s)</b> (on {expires}).\n\n"
        "Renew now and the new period stacks on top of the days you have left."
    ),
    "grace_warning": (
        "⚠️ Your subscription expired on {expires}.\n\n"
        "You still have access for a short grace period. Renew to keep it."
    ),
    "revoked_notice": (
        "Your access has ended because the subscription expired.\n\n"
        "You are welcome back any time — send /start to resubscribe."
    ),
    "help": (
        "<b>Commands</b>\n"
        "/start — plans and subscribing\n"
        "/status — your subscription\n"
        "/access — request your invite link again\n"
        "/lang — switch language\n"
        "/help — this message\n\n"
        "Problem with a payment? Contact {support}."
    ),
    "help_no_support": (
        "<b>Commands</b>\n"
        "/start — plans and subscribing\n"
        "/status — your subscription\n"
        "/access — request your invite link again\n"
        "/lang — switch language\n"
        "/help — this message"
    ),
    "lang_switched": "Language set to English.",
    "not_admin": "This command is for admins only.",
    "plan_button": "{label} — {price} {currency}",
    "days_left_none": "0",
}

RU: dict[str, str] = {
    "start_greeting": (
        "<b>Добро пожаловать.</b>\n\n"
        "Этот бот продаёт доступ в закрытый канал. Оплата уходит напрямую на кошелёк "
        "владельца — посредник деньги не держит.\n\n"
        "Выберите тариф, чтобы начать."
    ),
    "start_active": (
        "<b>Подписка активна.</b>\n"
        "Тариф: <b>{plan}</b>\n"
        "Действует до: <b>{expires}</b> (осталось дней: {days_left})\n\n"
        "Можно продлить заранее — новые дни добавятся к оставшимся."
    ),
    "choose_plan": "Выберите тариф:",
    "choose_currency": "Чем будете платить?",
    "no_plans": "Тарифы пока не настроены. Напишите владельцу канала.",
    "invoice": (
        "<b>Счёт {code}</b>\n\n"
        "Отправьте <b>точно</b> эту сумму:\n"
        "<code>{amount}</code> {currency}\n\n"
        "На адрес:\n<code>{address}</code>\n\n"
        "{network_note}"
        "⏳ Счёт действует {ttl} минут.\n\n"
        "⚠️ <b>Сумма должна совпасть до последней цифры.</b> Последние знаки после "
        "запятой — это то, по чему бот узнаёт ваш платёж. Округлённая сумма "
        "автоматически не зачтётся.\n\n"
        "Доступ выдаётся автоматически, обычно в течение минуты после подтверждения "
        "перевода."
    ),
    "invoice_network_trc20": (
        "Сеть: <b>TRON (TRC20)</b>. Не отправляйте из другой сети.\n\n"
    ),
    "invoice_network_ton": (
        "Сеть: <b>TON</b>. Укажите <code>{code}</code> в поле комментария.\n\n"
    ),
    "invoice_check": "Я оплатил — проверить",
    "invoice_cancel": "Отменить",
    "invoice_cancelled": "Счёт отменён. Напишите /start, когда будете готовы.",
    "checking": "Проверяю блокчейн…",
    "not_found_yet": (
        "Платёж пока не найден.\n\n"
        "Если вы только что отправили — дайте сети минуту и нажмите кнопку снова. "
        "Если отправили другую сумму, напишите /help, администратор разберётся."
    ),
    "send_tx_hash": (
        "Если у вас есть хеш транзакции — пришлите его, и я проверю напрямую."
    ),
    "tx_hash_not_found": (
        "Не нашёл такую транзакцию на наш кошелёк. Проверьте хеш или напишите {support}."
    ),
    "tx_hash_wrong_amount": (
        "Транзакцию нашёл, но сумма не совпадает ни с одним открытым счётом. "
        "Напишите {support} и приложите хеш — здесь нужен человек."
    ),
    "payment_confirmed": (
        "✅ <b>Платёж подтверждён. Вы в канале.</b>\n\n"
        "Ваша персональная ссылка (одноразовая, скоро истечёт):\n{invite}\n\n"
        "Доступ действует до <b>{expires}</b>."
    ),
    "payment_confirmed_no_link": (
        "✅ <b>Платёж подтверждён.</b> Доступ действует до <b>{expires}</b>.\n\n"
        "Ссылку сейчас создать не удалось — напишите /access через минуту, и я её выдам."
    ),
    "access_link": "Ваша ссылка (одноразовая):\n{invite}",
    "access_none": "Активной подписки нет. Напишите /start, чтобы выбрать тариф.",
    "access_error": "Не удалось создать ссылку прямо сейчас. Попробуйте через минуту.",
    "status": (
        "<b>Статус подписки</b>\n"
        "Тариф: <b>{plan}</b>\n"
        "Состояние: <b>{state}</b>\n"
        "Действует до: <b>{expires}</b> (осталось дней: {days_left})\n"
        "Платежей сделано: {payments}"
    ),
    "reminder": (
        "⏰ Доступ заканчивается через <b>{days} дн.</b> ({expires}).\n\n"
        "Продлите сейчас — новый период добавится к оставшимся дням."
    ),
    "grace_warning": (
        "⚠️ Подписка закончилась {expires}.\n\n"
        "Доступ пока сохраняется — это короткий льготный период. Продлите, чтобы остаться."
    ),
    "revoked_notice": (
        "Доступ закрыт: подписка закончилась.\n\n"
        "Возвращайтесь в любой момент — напишите /start, чтобы подписаться снова."
    ),
    "help": (
        "<b>Команды</b>\n"
        "/start — тарифы и подписка\n"
        "/status — ваша подписка\n"
        "/access — запросить ссылку заново\n"
        "/lang — сменить язык\n"
        "/help — это сообщение\n\n"
        "Проблема с оплатой? Напишите {support}."
    ),
    "help_no_support": (
        "<b>Команды</b>\n"
        "/start — тарифы и подписка\n"
        "/status — ваша подписка\n"
        "/access — запросить ссылку заново\n"
        "/lang — сменить язык\n"
        "/help — это сообщение"
    ),
    "lang_switched": "Язык переключён на русский.",
    "not_admin": "Эта команда только для администраторов.",
    "plan_button": "{label} — {price} {currency}",
    "days_left_none": "0",
}

LOCALES: dict[str, dict[str, str]] = {"en": EN, "ru": RU}

STATE_LABELS: dict[str, dict[str, str]] = {
    "en": {
        "pending": "paid, not joined yet",
        "active": "active",
        "in_grace": "expired (grace period)",
        "revoked": "ended",
    },
    "ru": {
        "pending": "оплачено, ещё не вошли",
        "active": "активна",
        "in_grace": "истекла (льготный период)",
        "revoked": "закрыта",
    },
}


def t(lang: str, key: str, **kwargs: Any) -> str:
    """Translate ``key``, falling back to English and then to the key itself."""
    table = LOCALES.get(lang) or EN
    template = table.get(key) or EN.get(key) or key
    if not kwargs:
        return template
    try:
        return template.format(**kwargs)
    except KeyError:
        # A missing placeholder must never crash a payment flow.
        return template


def state_label(lang: str, state: str) -> str:
    return STATE_LABELS.get(lang, STATE_LABELS["en"]).get(state, state)
