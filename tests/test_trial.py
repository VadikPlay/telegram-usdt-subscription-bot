from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from gatekit.bot.handlers.user import cmd_trial
from gatekit.config import Settings
from gatekit.db.base import Base
from gatekit.db.models import AuditLog, Membership, MembershipState, User, utcnow
from gatekit.services.access import AccessManager
from gatekit.services.access_rules import AccessAction, AccessPolicy, MembershipView, decide


@pytest.fixture
async def test_db():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    yield sessionmaker
    await engine.dispose()


@pytest.fixture
def mock_access():
    access = MagicMock(spec=AccessManager)
    access.settings = MagicMock(spec=Settings)
    access.settings.channel_id = -100123456789
    access.create_invite = AsyncMock(return_value="https://t.me/+test_invite")

    # Real implementation of grant for testing
    async def _grant(
        session,
        *,
        user_id,
        plan_code,
        plan_days=1,
        actor="system",
        duration=None,
        **kwargs,
    ):
        now = utcnow()
        target_duration = duration if duration is not None else timedelta(days=plan_days)
        membership = Membership(
            user_id=user_id,
            plan_code=plan_code,
            state=MembershipState.PENDING,
            started_at=now,
            expires_at=now + target_duration,
        )
        session.add(membership)
        await session.flush()
        return membership, "https://t.me/+test_invite"

    access.grant = AsyncMock(side_effect=_grant)
    return access


@pytest.fixture
def test_settings():
    settings = MagicMock(spec=Settings)
    settings.default_lang = "ru"
    settings.trial_duration_hours = 24
    settings.plan_list = []
    settings.enabled_currencies = ["USDT"]
    return settings


@pytest.mark.asyncio
async def test_trial_grants_24_hours_and_invite(test_db, mock_access, test_settings):
    user_id = 999001
    message = AsyncMock()
    message.from_user.id = user_id
    message.from_user.username = "trial_user"
    message.from_user.full_name = "Trial User"
    message.from_user.language_code = "ru"
    message.answer = AsyncMock()

    await cmd_trial(
        message=message,
        settings=test_settings,
        sessionmaker=test_db,
        access=mock_access,
    )

    # 1. Answer was sent confirming trial
    assert message.answer.call_count == 1
    answer_text = message.answer.call_args.args[0]
    assert "24 часа" in answer_text or "24 hours" in answer_text
    assert "https://t.me/+test_invite" in answer_text

    # 2. Database records verify trial
    async with test_db() as session:
        user = await session.get(User, user_id)
        assert user is not None
        assert user.trial_used is True

        membership = await session.get(Membership, user_id)
        assert membership is not None
        assert membership.plan_code == "trial"
        # Check expiry is roughly 24 hours from started_at
        delta = membership.expires_at - membership.started_at
        assert delta.total_seconds() == pytest.approx(86400, abs=5)

        # Audit log has trial_granted
        audit = await session.scalar(
            select(AuditLog).where(AuditLog.user_id == user_id, AuditLog.action == "trial_granted")
        )
        assert audit is not None


@pytest.mark.asyncio
async def test_trial_cannot_be_reused(test_db, mock_access, test_settings):
    user_id = 999002
    message = AsyncMock()
    message.from_user.id = user_id
    message.from_user.username = "repeat_user"
    message.from_user.full_name = "Repeat User"
    message.from_user.language_code = "ru"
    message.answer = AsyncMock()

    # First attempt: success
    await cmd_trial(message=message, settings=test_settings, sessionmaker=test_db, access=mock_access)
    assert message.answer.call_count == 1

    # Second attempt: rejected
    message.answer.reset_mock()
    await cmd_trial(message=message, settings=test_settings, sessionmaker=test_db, access=mock_access)
    assert message.answer.call_count == 1
    rejected_text = message.answer.call_args.args[0]
    assert "уже использовали" in rejected_text or "already used" in rejected_text


@pytest.mark.asyncio
async def test_trial_skipped_if_active_paid_membership(test_db, mock_access, test_settings):
    user_id = 999003
    now = utcnow()
    async with test_db() as session:
        user = User(id=user_id, username="paying_customer", lang="ru", trial_used=False)
        membership = Membership(
            user_id=user_id,
            plan_code="month",
            state=MembershipState.ACTIVE,
            started_at=now,
            expires_at=now + timedelta(days=20),
        )
        session.add_all([user, membership])
        await session.commit()

    message = AsyncMock()
    message.from_user.id = user_id
    message.from_user.username = "paying_customer"
    message.from_user.full_name = "Paying Customer"
    message.from_user.language_code = "ru"
    message.answer = AsyncMock()

    await cmd_trial(message=message, settings=test_settings, sessionmaker=test_db, access=mock_access)
    assert message.answer.call_count == 1
    answer_text = message.answer.call_args.args[0]
    assert "уже есть активная подписка" in answer_text or "already have an active subscription" in answer_text


def test_trial_lifecycle_and_expiry_rules():
    """Verify trial memberships are subject to standard subscription rules."""
    now = datetime(2026, 10, 14, 12, 0, tzinfo=timezone.utc)
    policy = AccessPolicy(
        safe_mode=False,
        grace_period=timedelta(days=2),
        payment_protection=timedelta(hours=48),
    )

    # 1. 10 hours into trial:
    # A) Reminder not sent yet -> triggers REMIND
    active_trial_new = MembershipView(
        user_id=1,
        state="active",
        expires_at=now + timedelta(hours=14),
    )
    assert decide(active_trial_new, now, policy).action is AccessAction.REMIND

    # B) Reminders already sent -> stays active (NONE)
    active_trial_reminded = MembershipView(
        user_id=1,
        state="active",
        expires_at=now + timedelta(hours=14),
        reminders_sent=frozenset({3, 1}),
    )
    assert decide(active_trial_reminded, now, policy).action is AccessAction.NONE

    # 2. 2 hours after trial expired (inside 2-day grace period)
    lapsed_trial = MembershipView(
        user_id=1,
        state="active",
        expires_at=now - timedelta(hours=2),
    )
    assert decide(lapsed_trial, now, policy).action is AccessAction.WARN_GRACE

    # 3. 3 days after trial expired (past 2-day grace period) -> Revoke!
    dead_trial = MembershipView(
        user_id=1,
        state="in_grace",
        expires_at=now - timedelta(days=3),
    )
    decision = decide(dead_trial, now, policy)
    assert decision.action is AccessAction.REVOKE


@pytest.mark.asyncio
async def test_real_access_manager_grant_trial_leaves_payments_zero_and_no_last_payment(test_db):
    """Ensure real AccessManager.grant does not count trial as a payment or trigger payment protection."""
    mock_bot = AsyncMock()
    mock_invite = MagicMock()
    mock_invite.invite_link = "https://t.me/+real_test_invite"
    mock_bot.create_chat_invite_link = AsyncMock(return_value=mock_invite)

    settings = MagicMock(spec=Settings)
    settings.channel_id = -1009999999999
    settings.invite_link_ttl_minutes = 30
    settings.default_lang = "ru"

    real_access = AccessManager(mock_bot, settings)

    user_id = 999888
    async with test_db() as session:
        user = User(id=user_id, username="clean_trial_user", lang="ru")
        session.add(user)
        await session.commit()

        membership, invite = await real_access.grant(
            session,
            user_id=user_id,
            plan_code="trial",
            plan_days=1,
            actor="trial",
            duration=timedelta(hours=24),
        )
        await session.commit()

        assert membership.plan_code == "trial"
        # Crucial: Free trial must NOT count as a payment or record a payment timestamp
        assert membership.payments_count == 0
        assert membership.last_payment_at is None
        assert invite == "https://t.me/+real_test_invite"
        delta = membership.expires_at - membership.started_at
        assert delta.total_seconds() == pytest.approx(86400, abs=5)

        # After trial expires + grace period ends, verify decide() revokes access
        now = membership.expires_at + timedelta(days=3)
        view = MembershipView(
            user_id=membership.user_id,
            state="in_grace",
            expires_at=membership.expires_at,
            last_payment_at=membership.last_payment_at,
        )
        policy = AccessPolicy(safe_mode=False, grace_period=timedelta(days=2))
        decision = decide(view, now, policy)
        # Because last_payment_at is None, Guard 3 does not block revocation!
        assert decision.action is AccessAction.REVOKE


@pytest.mark.asyncio
async def test_trial_rejected_if_membership_in_grace(test_db, mock_access, test_settings):
    user_id = 999004
    now = utcnow()
    async with test_db() as session:
        user = User(id=user_id, username="grace_customer", lang="ru", trial_used=False)
        membership = Membership(
            user_id=user_id,
            plan_code="month",
            state=MembershipState.IN_GRACE,
            started_at=now - timedelta(days=31),
            expires_at=now - timedelta(hours=1),
        )
        session.add_all([user, membership])
        await session.commit()

    message = AsyncMock()
    message.from_user.id = user_id
    message.from_user.username = "grace_customer"
    message.from_user.full_name = "Grace Customer"
    message.from_user.language_code = "ru"
    message.answer = AsyncMock()

    await cmd_trial(message=message, settings=test_settings, sessionmaker=test_db, access=mock_access)
    assert message.answer.call_count == 1
    answer_text = message.answer.call_args.args[0]
    assert "уже есть активная подписка" in answer_text or "already have an active subscription" in answer_text

