from datetime import datetime, timedelta, timezone

from gatekit.services.access_rules import (
    AccessAction,
    AccessPolicy,
    MembershipView,
    decide,
    next_expiry,
)

NOW = datetime(2026, 9, 16, 12, 0, tzinfo=timezone.utc)
ENFORCING = AccessPolicy(safe_mode=False)


def member(
    expires_at: datetime,
    state: str = "active",
    last_payment_at: datetime | None = None,
    reminders: frozenset[int] = frozenset(),
    pending_kick: bool = False,
) -> MembershipView:
    return MembershipView(
        user_id=1,
        state=state,
        expires_at=expires_at,
        last_payment_at=last_payment_at,
        reminders_sent=reminders,
        pending_kick=pending_kick,
    )


# ── the guards that protect paying members ──────────────────────────────────
def test_healthy_member_is_left_alone():
    d = decide(member(NOW + timedelta(days=20)), NOW, ENFORCING)
    assert d.action is AccessAction.NONE


def test_safe_mode_never_removes_anyone():
    long_expired = member(NOW - timedelta(days=30))
    d = decide(long_expired, NOW, AccessPolicy(safe_mode=True))
    assert d.action is AccessAction.WARN_ONLY


def test_recent_payment_beats_stale_expiry_dates():
    """The most important guard: money arrived, so nobody gets removed."""
    view = member(
        NOW - timedelta(days=30),  # dates say long gone
        last_payment_at=NOW - timedelta(hours=2),  # but they just paid
    )
    d = decide(view, NOW, ENFORCING)
    assert d.action is AccessAction.NONE
    assert "payment-protection" in d.reason


def test_payment_protection_expires_too():
    view = member(NOW - timedelta(days=30), last_payment_at=NOW - timedelta(days=10))
    assert decide(view, NOW, ENFORCING).action is AccessAction.REVOKE


def test_grace_period_warns_instead_of_removing():
    d = decide(member(NOW - timedelta(hours=6)), NOW, ENFORCING)
    assert d.action is AccessAction.WARN_GRACE


def test_removal_only_after_grace_ends():
    policy = AccessPolicy(safe_mode=False, grace_period=timedelta(days=2))
    assert decide(member(NOW - timedelta(days=1)), NOW, policy).action is AccessAction.WARN_GRACE
    assert decide(member(NOW - timedelta(days=3)), NOW, policy).action is AccessAction.REVOKE


def test_already_revoked_is_never_touched_again():
    view = member(NOW - timedelta(days=99), state="revoked")
    assert decide(view, NOW, ENFORCING).action is AccessAction.NONE


def test_queued_kick_is_not_requeued():
    view = member(NOW - timedelta(days=10), pending_kick=True)
    assert decide(view, NOW, ENFORCING).action is AccessAction.NONE


def test_manual_confirmation_mode_queues_instead_of_removing():
    policy = AccessPolicy(safe_mode=False, require_manual_kick=True)
    d = decide(member(NOW - timedelta(days=10)), NOW, policy)
    assert d.action is AccessAction.QUEUE_KICK


# ── reminders ───────────────────────────────────────────────────────────────
def test_three_day_reminder_fires_once():
    view = member(NOW + timedelta(days=2, hours=12))
    d = decide(view, NOW, ENFORCING)
    assert d.action is AccessAction.REMIND
    assert d.reminder_day == 3

    already = member(NOW + timedelta(days=2, hours=12), reminders=frozenset({3}))
    assert decide(already, NOW, ENFORCING).action is AccessAction.NONE


def test_one_day_reminder_takes_over_closer_in():
    view = member(NOW + timedelta(hours=12), reminders=frozenset({3}))
    d = decide(view, NOW, ENFORCING)
    assert d.action is AccessAction.REMIND
    assert d.reminder_day == 1


def test_no_reminder_far_from_expiry():
    assert decide(member(NOW + timedelta(days=25)), NOW, ENFORCING).action is AccessAction.NONE


# ── renewal arithmetic ──────────────────────────────────────────────────────
def test_early_renewal_stacks_remaining_days():
    current = NOW + timedelta(days=10)
    assert next_expiry(current, NOW, 30) == current + timedelta(days=30)


def test_renewal_after_a_lapse_starts_from_now():
    lapsed = NOW - timedelta(days=5)
    assert next_expiry(lapsed, NOW, 30) == NOW + timedelta(days=30)


def test_first_purchase_starts_from_now():
    assert next_expiry(None, NOW, 30) == NOW + timedelta(days=30)
