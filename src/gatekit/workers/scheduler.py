"""Background worker: reminders, grace handling, removals, reconciliation.

Runs the pure rules from ``services.access_rules`` against every membership and
performs only what they authorise. Nothing here decides policy — this file is
just the hands.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from aiogram import Bot
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from gatekit.config import Settings
from gatekit.db.models import Membership, MembershipState, User
from gatekit.services import invoices as invoice_service
from gatekit.services.access import AccessManager
from gatekit.services.access_rules import AccessAction, AccessPolicy, MembershipView, decide
from gatekit.texts import t

log = logging.getLogger(__name__)


class SubscriptionScheduler:
    def __init__(
        self,
        *,
        bot: Bot,
        settings: Settings,
        sessionmaker: async_sessionmaker,
        access: AccessManager,
    ) -> None:
        self.bot = bot
        self.settings = settings
        self.sessionmaker = sessionmaker
        self.access = access
        self.policy = AccessPolicy(
            grace_period=timedelta(days=settings.grace_period_days),
            payment_protection=timedelta(hours=settings.payment_protection_hours),
            reminder_days=tuple(settings.reminder_day_list),
            safe_mode=settings.safe_mode,
            require_manual_kick=settings.require_manual_kick_confirmation,
        )
        self._stop = asyncio.Event()

    def stop(self) -> None:
        self._stop.set()

    async def run(self) -> None:
        log.info(
            "Scheduler started (every %ss, safe_mode=%s, grace=%sd, protection=%sh)",
            self.settings.scheduler_interval_seconds,
            self.settings.safe_mode,
            self.settings.grace_period_days,
            self.settings.payment_protection_hours,
        )
        while not self._stop.is_set():
            try:
                await self.tick()
            except Exception:  # noqa: BLE001 - never let the sweep die
                log.exception("Scheduler tick failed; continuing")
            try:
                await asyncio.wait_for(
                    self._stop.wait(), timeout=self.settings.scheduler_interval_seconds
                )
            except TimeoutError:
                continue

    async def tick(self) -> dict[str, int]:
        """One sweep. Returns a small summary, which the self-test asserts on."""
        summary = {"reminded": 0, "grace": 0, "revoked": 0, "warned": 0, "queued": 0}
        now = datetime.now(tz=timezone.utc)

        async with self.sessionmaker() as session:
            await invoice_service.expire_stale_invoices(session, self.settings)
            await session.commit()

            memberships = list(
                (
                    await session.scalars(
                        select(Membership).where(Membership.state != MembershipState.REVOKED)
                    )
                ).all()
            )

            for membership in memberships:
                view = _to_view(membership)
                decision = decide(view, now, self.policy)
                if decision.action is AccessAction.NONE:
                    continue

                user = await session.get(User, membership.user_id)
                lang = user.lang if user else self.settings.default_lang

                if decision.action is AccessAction.REMIND:
                    day = decision.reminder_day or 0
                    await self._dm(
                        membership.user_id,
                        t(
                            lang,
                            "reminder",
                            days=day,
                            expires=_fmt(view.expires_at),
                        ),
                    )
                    membership.mark_reminder(day)
                    summary["reminded"] += 1

                elif decision.action is AccessAction.WARN_GRACE:
                    if membership.state is not MembershipState.IN_GRACE:
                        await self.access.set_grace(session, membership.user_id)
                        await self._dm(
                            membership.user_id,
                            t(lang, "grace_warning", expires=_fmt(view.expires_at)),
                        )
                        summary["grace"] += 1

                elif decision.action is AccessAction.WARN_ONLY:
                    # SAFE_MODE: tell the owner what *would* have happened.
                    await self._alert_admins(
                        f"🟡 SAFE_MODE: user {membership.user_id} is past the grace period "
                        f"(expired {_fmt(view.expires_at)}). Nobody was removed. "
                        "Set SAFE_MODE=false to enforce."
                    )
                    summary["warned"] += 1

                elif decision.action is AccessAction.QUEUE_KICK:
                    await self.access.queue_kick(session, membership.user_id, decision.reason)
                    await self._alert_admins(
                        f"🔔 Removal awaiting your confirmation: user {membership.user_id} "
                        f"(expired {_fmt(view.expires_at)}). Use /pending in the admin panel."
                    )
                    summary["queued"] += 1

                elif decision.action is AccessAction.REVOKE:
                    ok = await self.access.revoke(
                        session, user_id=membership.user_id, reason=decision.reason
                    )
                    if ok:
                        await self._dm(membership.user_id, t(lang, "revoked_notice"))
                        summary["revoked"] += 1

            await session.commit()

        if any(summary.values()):
            log.info("Scheduler sweep: %s", summary)
        return summary

    async def _dm(self, user_id: int, text: str) -> None:
        try:
            await self.bot.send_message(user_id, text, disable_web_page_preview=True)
        except Exception as exc:  # noqa: BLE001 - blocked bot, deleted account, etc.
            log.info("Could not DM %s: %s", user_id, exc)

    async def _alert_admins(self, text: str) -> None:
        for admin_id in self.settings.admin_id_set:
            try:
                await self.bot.send_message(admin_id, text, disable_web_page_preview=True)
            except Exception as exc:  # noqa: BLE001
                log.warning("Could not alert admin %s: %s", admin_id, exc)


def _to_view(membership: Membership) -> MembershipView:
    state = (
        membership.state.value if hasattr(membership.state, "value") else str(membership.state)
    )
    return MembershipView(
        user_id=membership.user_id,
        state=state,
        expires_at=_aware(membership.expires_at),
        last_payment_at=_aware(membership.last_payment_at) if membership.last_payment_at else None,
        reminders_sent=frozenset(membership.reminder_set),
        pending_kick=bool(membership.pending_kick),
    )


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _fmt(value: datetime) -> str:
    return _aware(value).strftime("%Y-%m-%d %H:%M UTC")
