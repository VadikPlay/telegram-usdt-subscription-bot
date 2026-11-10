from datetime import timedelta
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from gatekit.bot.handlers.user import cancel_invoice
from gatekit.bot.keyboards import main_menu_keyboard
from gatekit.bot.middlewares import ThrottlingMiddleware
from gatekit.config import Plan, contact_url
from gatekit.db.base import Base
from gatekit.db.models import Invoice, InvoiceStatus, User, utcnow

PLANS = [Plan(code="month", days=30, price_usdt=Decimal("39"), label="1 month")]


def _buttons(markup):
    return [b for row in markup.inline_keyboard for b in row]


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("@orbitdev", "https://t.me/orbitdev"),
        ("orbit_dev", "https://t.me/orbit_dev"),
        ("t.me/orbitdev", "https://t.me/orbitdev"),
        ("https://example.com/order", "https://example.com/order"),
        ("", None),
        ("   ", None),
        ("not a contact!", None),
    ],
)
def test_contact_url(raw, expected):
    assert contact_url(raw) == expected


def test_main_menu_has_order_button_when_configured():
    markup = main_menu_keyboard(
        PLANS, ["USDT"], "ru", show_trial=True, order_url="https://t.me/orbitdev"
    )
    buttons = _buttons(markup)
    order = [b for b in buttons if b.url == "https://t.me/orbitdev"]
    assert len(order) == 1
    assert order[0].text == "🚀 Заказать такого бота"
    # Order button is the last row, plans stay on top.
    assert markup.inline_keyboard[-1] == order
    assert buttons[0].callback_data == "plan:month"
    assert any(b.callback_data == "menu:trial" for b in buttons)


def test_main_menu_hides_optional_buttons():
    buttons = _buttons(main_menu_keyboard(PLANS, ["USDT"], "en"))
    assert all(b.url is None for b in buttons)
    assert not any(b.callback_data == "menu:trial" for b in buttons)
    assert {"menu:status", "menu:access", "menu:lang"} <= {b.callback_data for b in buttons}


def test_throttle_blocks_burst_and_recovers():
    now = [100.0]
    throttle = ThrottlingMiddleware(1.0, exempt={42}, clock=lambda: now[0])
    assert throttle.allow(1)
    assert not throttle.allow(1)
    now[0] += 1.1
    assert throttle.allow(1)
    # Admins are never throttled.
    assert throttle.allow(42) and throttle.allow(42)


def test_throttle_evicts_idle_users():
    now = [0.0]
    throttle = ThrottlingMiddleware(1.0, clock=lambda: now[0])
    throttle.allow(1)
    now[0] += 10_000
    throttle.allow(2)
    assert set(throttle._last_seen) == {2}


@pytest.mark.asyncio
async def test_cannot_cancel_someone_elses_invoice():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)

    async with sessionmaker() as session:
        session.add_all([User(id=1, lang="en"), User(id=2, lang="en")])
        await session.flush()
        invoice = Invoice(
            code="GK-TEST1",
            user_id=1,
            plan_code="month",
            plan_days=30,
            currency="USDT",
            base_amount="39.00",
            amount="39.004271",
            tag=4271,
            status=InvoiceStatus.OPEN,
            expires_at=utcnow() + timedelta(hours=1),
        )
        session.add(invoice)
        await session.commit()
        invoice_id = invoice.id

    callback = AsyncMock()
    callback.data = f"cxl:{invoice_id}"
    callback.from_user = MagicMock(id=2)
    settings = MagicMock(default_lang="en")

    await cancel_invoice(callback, settings, sessionmaker)

    callback.answer.assert_awaited_once()
    assert callback.answer.call_args.kwargs.get("show_alert") is True
    async with sessionmaker() as session:
        assert (await session.get(Invoice, invoice_id)).status == InvoiceStatus.OPEN
    await engine.dispose()
