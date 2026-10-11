"""Access manager: issue invite links, remove lapsed members, log everything.

Every method here writes an AuditLog row. That log is the difference between
"the bot did something weird" and "here is exactly what happened at 03:14 and
why" — the single most useful thing to have when a customer's paying subscriber
complains.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from gatekit.config import Settings
from gatekit.db.models import AuditLog, Membership, MembershipState, utcnow
from gatekit.money import quantize
from gatekit.services.access_rules import next_expiry

log = logging.getLogger(__name__)


async def audit(
    session: AsyncSession,
    action: str,
    *,
    user_id: int | None = None,
    actor: str = "system",
    **details: object,
) -> None:
    session.add(
        AuditLog(
            actor=actor,
            action=action,
            user_id=user_id,
            details=json.dumps(details, default=str, ensure_ascii=False) if details else None,
        )
    )
    await session.flush()


class AccessManager:
    def __init__(self, bot: Bot, settings: Settings) -> None:
        self.bot = bot
        self.settings = settings

    # ── granting ────────────────────────────────────────────────────────────
    async def create_invite(self, user_id: int) -> str:
        """Single-use, short-lived invite link.

        member_limit=1 means a leaked link cannot be resold to a crowd, and the
        expiry means a link found in an old chat is already dead.
        """
        expire_at = datetime.now(tz=timezone.utc) + timedelta(
            minutes=self.settings.invite_link_ttl_minutes
        )
        link = await self.bot.create_chat_invite_link(
            chat_id=self.settings.channel_id,
            name=f"gk-{user_id}"[:32],
            expire_date=expire_at,
            member_limit=1,
        )
        return link.invite_link

    async def grant(
        self,
        session: AsyncSession,
        *,
        user_id: int,
        plan_code: str,
        plan_days: int,
        paid_at: datetime | None = None,
        actor: str = "system",
        amount_usdt: str | None = None,
    ) -> tuple[Membership, str | None]:
        """Activate or extend a membership and hand back an invite link."""
        now = utcnow()
        membership = await session.get(Membership, user_id)
        previous_expiry = None

        if membership is None:
            membership = Membership(
                user_id=user_id,
                plan_code=plan_code,
                state=MembershipState.PENDING,
                started_at=now,
                expires_at=next_expiry(None, now, plan_days),
            )
            session.add(membership)
        else:
            previous_expiry = _aware(membership.expires_at)
            membership.plan_code = plan_code
            membership.expires_at = next_expiry(previous_expiry, now, plan_days)
            # A renewal re-opens the reminder cycle for the new period.
            membership.reminders_sent = ""
            membership.pending_kick = False
            membership.revoked_at = None
            if membership.state == MembershipState.REVOKED:
                membership.state = MembershipState.PENDING
            elif membership.state == MembershipState.IN_GRACE:
                membership.state = MembershipState.ACTIVE

        membership.last_payment_at = paid_at or now
        membership.payments_count = (membership.payments_count or 0) + 1
        if amount_usdt:
            running_total = Decimal(str(membership.total_paid_usdt or "0")) + Decimal(amount_usdt)
            membership.total_paid_usdt = str(quantize(running_total, 6))

        invite: str | None = None
        try:
            invite = await self.create_invite(user_id)
            membership.invite_link = invite
        except TelegramAPIError as exc:
            # Do not lose the payment because Telegram hiccuped: the membership
            # is recorded, and the user can ask for the link again with /access.
            log.error("Could not create invite link for %s: %s", user_id, exc)
            await audit(
                session,
                "invite_failed",
                user_id=user_id,
                actor=actor,
                error=str(exc),
            )

        await audit(
            session,
            "granted",
            user_id=user_id,
            actor=actor,
            plan=plan_code,
            days=plan_days,
            previous_expiry=previous_expiry,
            new_expiry=membership.expires_at,
            invite_issued=bool(invite),
        )
        await session.flush()
        return membership, invite

    async def mark_joined(self, session: AsyncSession, user_id: int) -> None:
        membership = await session.get(Membership, user_id)
        if membership is None:
            await audit(session, "joined_without_membership", user_id=user_id)
            return
        membership.joined_at = utcnow()
        if membership.state == MembershipState.PENDING:
            membership.state = MembershipState.ACTIVE
        await audit(session, "joined", user_id=user_id, state=membership.state.value)

    async def mark_left(self, session: AsyncSession, user_id: int) -> None:
        await audit(session, "left_channel", user_id=user_id)

    # ── revoking ────────────────────────────────────────────────────────────
    async def revoke(
        self,
        session: AsyncSession,
        *,
        user_id: int,
        reason: str,
        actor: str = "system",
    ) -> bool:
        """Remove a member from the channel.

        Ban immediately followed by unban: Telegram's only "kick" primitive is a
        ban, and leaving it in place would block the person from ever coming
        back — the opposite of what a lapsed subscriber needs.
        """
        membership = await session.get(Membership, user_id)
        try:
            await self.bot.ban_chat_member(chat_id=self.settings.channel_id, user_id=user_id)
            await self.bot.unban_chat_member(
                chat_id=self.settings.channel_id, user_id=user_id, only_if_banned=True
            )
        except TelegramAPIError as exc:
            log.error("Failed to remove %s: %s", user_id, exc)
            await audit(session, "revoke_failed", user_id=user_id, actor=actor, error=str(exc))
            return False

        if membership is not None:
            membership.state = MembershipState.REVOKED
            membership.revoked_at = utcnow()
            membership.pending_kick = False
        await audit(session, "revoked", user_id=user_id, actor=actor, reason=reason)
        log.info("Removed user %s (%s, by %s)", user_id, reason, actor)
        return True

    async def queue_kick(self, session: AsyncSession, user_id: int, reason: str) -> None:
        membership = await session.get(Membership, user_id)
        if membership is None:
            return
        membership.pending_kick = True
        await audit(session, "kick_queued", user_id=user_id, reason=reason)

    async def set_grace(self, session: AsyncSession, user_id: int) -> None:
        membership = await session.get(Membership, user_id)
        if membership is None or membership.state == MembershipState.IN_GRACE:
            return
        membership.state = MembershipState.IN_GRACE
        await audit(session, "entered_grace", user_id=user_id, expires_at=membership.expires_at)

    async def extend(
        self, session: AsyncSession, *, user_id: int, days: int, actor: str
    ) -> Membership | None:
        membership = await session.get(Membership, user_id)
        if membership is None:
            return None
        before = _aware(membership.expires_at)
        membership.expires_at = before + timedelta(days=days)
        membership.pending_kick = False
        membership.reminders_sent = ""
        if membership.state in {MembershipState.IN_GRACE, MembershipState.REVOKED}:
            membership.state = MembershipState.ACTIVE
            membership.revoked_at = None
        await audit(
            session,
            "extended",
            user_id=user_id,
            actor=actor,
            days=days,
            before=before,
            after=membership.expires_at,
        )
        return membership


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
