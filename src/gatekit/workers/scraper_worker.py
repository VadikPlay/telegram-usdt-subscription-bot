"""Background worker: polls 999.md for new ads, checks SQLite, and alerts subscribers."""

from __future__ import annotations

import asyncio
import html
import logging
from datetime import datetime, timedelta, timezone

import httpx
from aiogram import Bot
from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from gatekit.bot.keyboards import ad_inline_keyboard
from gatekit.config import Settings
from gatekit.db.models import Membership, MembershipState, SeenAd, User, utcnow
from gatekit.services.scraper_999 import ScrapedAd, fetch_latest_ads
from gatekit.texts import t

log = logging.getLogger(__name__)


class ScraperWorker:
    """Async background worker for 999.md ad monitoring."""

    def __init__(
        self,
        *,
        bot: Bot,
        settings: Settings,
        sessionmaker: async_sessionmaker,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.bot = bot
        self.settings = settings
        self.sessionmaker = sessionmaker
        self._custom_client = client
        self._internal_client: httpx.AsyncClient | None = None
        self._stop = asyncio.Event()

    @property
    def client(self) -> httpx.AsyncClient:
        if self._custom_client is not None:
            return self._custom_client
        if self._internal_client is None:
            self._internal_client = httpx.AsyncClient()
        return self._internal_client

    def stop(self) -> None:
        self._stop.set()

    async def close(self) -> None:
        self.stop()
        if self._internal_client is not None and not self._internal_client.is_closed:
            await self._internal_client.aclose()

    async def run(self) -> None:
        log.info(
            "999.md scraper worker started (url: %s, every %ss)",
            self.settings.scraper_url,
            self.settings.scraper_interval_seconds,
        )
        while not self._stop.is_set():
            try:
                await self.tick()
            except Exception:  # noqa: BLE001 - worker loop must never crash
                log.exception("999.md scraper worker tick failed; continuing")
            try:
                await asyncio.wait_for(
                    self._stop.wait(), timeout=self.settings.scraper_interval_seconds
                )
            except TimeoutError:
                continue

    async def tick(self) -> dict[str, int]:
        """Poll 999.md once, record new ads, and broadcast them to active subscribers."""
        summary = {"scraped": 0, "new": 0, "sent": 0}
        url = self.settings.scraper_url
        if not url:
            return summary

        ads = await fetch_latest_ads(self.client, url)
        summary["scraped"] = len(ads)
        if not ads:
            return summary

        ad_ids = [ad.id for ad in ads]
        async with self.sessionmaker() as session:
            existing_ids = set(
                (await session.scalars(select(SeenAd.id).where(SeenAd.id.in_(ad_ids)))).all()
            )
            new_ads = [ad for ad in ads if ad.id not in existing_ids]
            summary["new"] = len(new_ads)

            if not new_ads:
                return summary

            # Persist newly seen ads (safely truncate long titles/prices)
            for ad in new_ads:
                session.add(
                    SeenAd(
                        id=ad.id,
                        title=ad.title[:500] if ad.title else None,
                        price=ad.price[:120] if ad.price else None,
                        url=ad.url,
                    )
                )

            # Find active subscribers whose membership is still valid (including grace period)
            # and who have not been blocked.
            now = datetime.now(tz=timezone.utc)
            grace_days = getattr(self.settings, "grace_period_days", 2)
            grace_cutoff = now - timedelta(days=grace_days)
            memberships = list(
                (
                    await session.scalars(
                        select(Membership)
                        .join(User, Membership.user_id == User.id)
                        .where(
                            User.is_blocked.is_(False),
                            or_(
                                and_(
                                    Membership.state.in_(
                                        [MembershipState.ACTIVE, MembershipState.PENDING]
                                    ),
                                    Membership.expires_at > now,
                                ),
                                and_(
                                    Membership.state == MembershipState.IN_GRACE,
                                    Membership.expires_at > grace_cutoff,
                                ),
                            ),
                        )
                    )
                ).all()
            )

            user_ids = [m.user_id for m in memberships]
            if not user_ids:
                await session.commit()
                log.info("Scraped %s new ads, but no active subscribers to notify", len(new_ads))
                return summary

            # Fetch user languages
            users = list(
                (await session.scalars(select(User).where(User.id.in_(user_ids)))).all()
            )
            lang_by_user = {u.id: u.lang for u in users}

            await session.commit()

        # Broadcast new ads to active subscribers
        # Send oldest new ad first so chronological order is preserved
        for ad in reversed(new_ads):
            for uid in user_ids:
                lang = lang_by_user.get(uid, self.settings.default_lang)
                sent = await self._send_ad_alert(uid, ad, lang)
                if sent:
                    summary["sent"] += 1

        log.info(
            "999.md scraper sweep: %s scraped, %s new, %s messages sent",
            summary["scraped"],
            summary["new"],
            summary["sent"],
        )
        return summary

    async def _send_ad_alert(self, user_id: int, ad: ScrapedAd, lang: str) -> bool:
        escaped_title = html.escape(ad.title) if ad.title else f"Объявление #{ad.id}"
        escaped_price = html.escape(ad.price) if ad.price else "Договорная"
        escaped_id = html.escape(ad.id)
        text = t(
            lang,
            "ad_notification",
            title=escaped_title,
            price=escaped_price,
            id=escaped_id,
        )
        button_text = t(lang, "ad_open_button")
        keyboard = ad_inline_keyboard(url=ad.url.strip(), text=button_text)
        try:
            await self.bot.send_message(
                chat_id=user_id,
                text=text,
                reply_markup=keyboard,
                disable_web_page_preview=False,
            )
            return True
        except Exception as exc:  # noqa: BLE001 - blocked by user, chat not found, etc.
            log.info("Could not deliver ad %s to user %s: %s", ad.id, user_id, exc)
            return False
