"""Access decisions — pure rules, no side effects.

This is the module that answers the single biggest objection a channel owner
has to handing access control to someone else's bot:

    "One bug and it kicks a paying subscriber. The chat sees it, people panic,
     and the reputational damage costs more than a year of saved fees."

So removal is not a simple "expired? kick" branch. Five independent guards have
to agree before anyone loses access, and every decision is explainable.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from datetime import datetime, timedelta


class AccessAction(str, enum.Enum):
    NONE = "none"
    REMIND = "remind"  # renewal reminder, N days before expiry
    WARN_GRACE = "warn_grace"  # expired but inside the grace period
    WARN_ONLY = "warn_only"  # would be removed, but SAFE_MODE is on
    QUEUE_KICK = "queue_kick"  # removal needs an admin's click
    REVOKE = "revoke"  # remove from the channel now


@dataclass(frozen=True, slots=True)
class MembershipView:
    """Everything the rules are allowed to know about a member."""

    user_id: int
    state: str  # "pending" | "active" | "in_grace" | "revoked"
    expires_at: datetime
    last_payment_at: datetime | None = None
    reminders_sent: frozenset[int] = frozenset()
    pending_kick: bool = False


@dataclass(frozen=True, slots=True)
class AccessPolicy:
    grace_period: timedelta = timedelta(days=2)
    payment_protection: timedelta = timedelta(hours=48)
    reminder_days: tuple[int, ...] = (3, 1)
    safe_mode: bool = True
    require_manual_kick: bool = False


@dataclass(frozen=True, slots=True)
class Decision:
    action: AccessAction
    reason: str
    reminder_day: int | None = None

    def __str__(self) -> str:  # pragma: no cover - logging sugar
        return f"{self.action.value} ({self.reason})"


def decide(view: MembershipView, now: datetime, policy: AccessPolicy) -> Decision:
    """Decide what, if anything, should happen to this member right now."""
    # Guard 1: already revoked — never act twice. Makes the sweep idempotent.
    if view.state == "revoked":
        return Decision(AccessAction.NONE, "already revoked")

    # Guard 2: a removal is already awaiting an admin's click.
    if view.pending_kick:
        return Decision(AccessAction.NONE, "kick already queued for admin confirmation")

    hard_deadline = view.expires_at + policy.grace_period

    if now > hard_deadline:
        # Guard 3: recent payment beats every expiry calculation. If money
        # arrived and some other bug left the dates stale, the member stays.
        if (
            view.last_payment_at is not None
            and now - view.last_payment_at <= policy.payment_protection
        ):
            return Decision(
                AccessAction.NONE,
                "paid within the payment-protection window — refusing to remove",
            )
        # Guard 4: safe mode never removes anyone, it only reports.
        if policy.safe_mode:
            return Decision(AccessAction.WARN_ONLY, "expired past grace, SAFE_MODE is on")
        # Guard 5: optional human in the loop.
        if policy.require_manual_kick:
            return Decision(AccessAction.QUEUE_KICK, "expired past grace, awaiting admin")
        return Decision(AccessAction.REVOKE, "expired past grace period")

    if now > view.expires_at:
        return Decision(AccessAction.WARN_GRACE, "expired, inside grace period")

    # Still active: should a renewal reminder go out?
    for day in sorted(policy.reminder_days, reverse=True):
        threshold = view.expires_at - timedelta(days=day)
        if now >= threshold and day not in view.reminders_sent:
            return Decision(AccessAction.REMIND, f"{day} day(s) before expiry", reminder_day=day)

    return Decision(AccessAction.NONE, "active, nothing due")


def next_expiry(
    current_expires_at: datetime | None, now: datetime, plan_days: int
) -> datetime:
    """Extend a subscription without ever stealing paid-for days.

    Renewing early stacks on top of the remaining time; renewing after a lapse
    starts from now.
    """
    base = current_expires_at if current_expires_at and current_expires_at > now else now
    return base + timedelta(days=plan_days)
