"""Background worker: read the chain, credit invoices, hand out access.

Loop shape: poll → match → settle → grant → notify. Every step is idempotent,
so a crash halfway through cannot double-credit or double-invite. The cursor is
only advanced after the batch is committed.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from aiogram import Bot
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from gatekit.chains.base import ChainClient, ChainTransfer
from gatekit.config import Settings
from gatekit.db.models import ChainCursor, Payment, PaymentStatus, User, utcnow
from gatekit.services import invoices as invoice_service
from gatekit.services.access import AccessManager, audit
from gatekit.services.matcher import Match, MatchPolicy, match_transfers
from gatekit.texts import t

log = logging.getLogger(__name__)

# On a cold start, look this far back so a payment made while the bot was down
# is still picked up.
COLD_START_LOOKBACK = timedelta(days=3)


class PaymentWatcher:
    def __init__(
        self,
        *,
        bot: Bot,
        settings: Settings,
        sessionmaker: async_sessionmaker,
        clients: list[ChainClient],
        access: AccessManager,
    ) -> None:
        self.bot = bot
        self.settings = settings
        self.sessionmaker = sessionmaker
        self.clients = clients
        self.access = access
        self.policy = MatchPolicy(
            clock_skew=timedelta(seconds=settings.clock_skew_seconds),
            late_tolerance=timedelta(minutes=settings.payment_late_tolerance_minutes),
        )
        self._stop = asyncio.Event()

    def stop(self) -> None:
        self._stop.set()

    async def run(self) -> None:
        log.info(
            "Payment watcher started for %s (every %ss)",
            ", ".join(c.currency for c in self.clients) or "no chain",
            self.settings.watcher_interval_seconds,
        )
        while not self._stop.is_set():
            try:
                await self.tick()
            except Exception:  # noqa: BLE001 - a worker must never die silently
                log.exception("Payment watcher tick failed; continuing")
            try:
                await asyncio.wait_for(
                    self._stop.wait(), timeout=self.settings.watcher_interval_seconds
                )
            except TimeoutError:
                continue

    async def tick(self) -> None:
        for client in self.clients:
            await self._poll_chain(client)

    # ── internals ───────────────────────────────────────────────────────────
    async def _poll_chain(self, client: ChainClient) -> None:
        chain_name = client.currency
        async with self.sessionmaker() as session:
            cursor = await session.get(ChainCursor, chain_name)
            if cursor is None:
                start_ms = int(
                    (datetime.now(tz=timezone.utc) - COLD_START_LOOKBACK).timestamp() * 1000
                )
                cursor = ChainCursor(chain=chain_name, last_ts_ms=start_ms)
                session.add(cursor)
                await session.commit()
            since_ms = cursor.last_ts_ms

        transfers, new_cursor = await client.fetch_incoming(since_ms)
        if not transfers:
            await self._save_cursor(chain_name, max(since_ms, new_cursor))
            return

        log.info("%s: %s incoming transfer(s) to inspect", chain_name, len(transfers))
        await self.process_transfers(transfers, chain_name=chain_name)
        # Re-poll from the newest seen timestamp minus the skew, so a transfer
        # sharing a timestamp with the cursor is not skipped.
        safe_cursor = max(since_ms, new_cursor - self.settings.clock_skew_seconds * 1000)
        await self._save_cursor(chain_name, safe_cursor)

    async def _save_cursor(self, chain: str, ts_ms: int) -> None:
        async with self.sessionmaker() as session:
            cursor = await session.get(ChainCursor, chain)
            if cursor is None:
                session.add(ChainCursor(chain=chain, last_ts_ms=ts_ms))
            else:
                cursor.last_ts_ms = max(cursor.last_ts_ms, ts_ms)
                cursor.updated_at = utcnow()
            await session.commit()

    async def process_transfers(
        self, transfers: list[ChainTransfer], *, chain_name: str = "manual"
    ) -> list[Match]:
        """Match a batch of transfers and act on it. Returns the matches made."""
        async with self.sessionmaker() as session:
            open_views = await invoice_service.open_invoice_views(session)
            known = set(
                (await session.scalars(select(Payment.tx_id))).all()
            )
            wallets = {
                currency: self.settings.wallet_for(currency)
                for currency in self.settings.enabled_currencies
            }

            result = match_transfers(
                transfers,
                open_views,
                known_tx_ids=known,
                wallets=wallets,
                policy=self.policy,
            )

            # Record every transfer we have not seen before, matched or not.
            for transfer in result.unmatched + result.ambiguous:
                if transfer.tx_id and transfer.tx_id not in known:
                    session.add(_payment_row(transfer, PaymentStatus.UNMATCHED))
                    await audit(
                        session,
                        "payment_unmatched",
                        tx=transfer.tx_id,
                        amount=str(transfer.amount),
                        currency=transfer.currency,
                        note="money arrived that no open invoice expected",
                    )
            await session.commit()

            granted: list[Match] = []
            for match in result.matches:
                if await self._settle_one(session, match):
                    granted.append(match)
            await session.commit()

        if result.ambiguous:
            await self._alert_admins(
                f"⚠️ {len(result.ambiguous)} payment(s) could match more than one invoice "
                "and were NOT credited automatically. Check /unmatched."
            )
        if result.unmatched:
            await self._alert_admins(
                f"💸 {len(result.unmatched)} incoming payment(s) matched no open invoice. "
                "See /unmatched — someone may have paid the wrong amount."
            )
        return granted

    async def _settle_one(self, session, match: Match) -> bool:
        transfer, invoice_view = match.transfer, match.invoice

        settled = await invoice_service.settle_invoice(
            session, invoice_view.id, tx_id=transfer.tx_id, paid_at=transfer.ts
        )
        if settled is None:
            log.info("Invoice %s already settled; skipping", invoice_view.code)
            return False

        session.add(
            _payment_row(transfer, PaymentStatus.MATCHED, invoice_id=invoice_view.id)
        )

        membership, invite = await self.access.grant(
            session,
            user_id=invoice_view.user_id,
            plan_code=settled.plan_code,
            plan_days=settled.plan_days,
            paid_at=transfer.ts,
            amount_usdt=settled.base_amount if transfer.currency == "USDT" else None,
        )
        await audit(
            session,
            "payment_matched",
            user_id=invoice_view.user_id,
            tx=transfer.tx_id,
            invoice=invoice_view.code,
            amount=str(transfer.amount),
            currency=transfer.currency,
            match_reason=match.reason,
        )

        user = await session.get(User, invoice_view.user_id)
        lang = user.lang if user else self.settings.default_lang
        await self._notify_user(invoice_view.user_id, lang, membership, invite)
        await self._alert_admins(
            f"✅ Payment received: {transfer.amount} {transfer.currency} "
            f"({invoice_view.code}) from {user.display if user else invoice_view.user_id}"
        )
        return True

    async def _notify_user(self, user_id: int, lang: str, membership, invite: str | None) -> None:
        expires = membership.expires_at
        if invite:
            text = t(lang, "payment_confirmed", invite=invite, expires=_fmt(expires))
        else:
            text = t(lang, "payment_confirmed_no_link", expires=_fmt(expires))
        try:
            await self.bot.send_message(user_id, text, disable_web_page_preview=True)
        except Exception as exc:  # noqa: BLE001 - user may have blocked the bot
            log.warning("Could not notify user %s: %s", user_id, exc)

    async def _alert_admins(self, text: str) -> None:
        for admin_id in self.settings.admin_id_set:
            try:
                await self.bot.send_message(admin_id, text, disable_web_page_preview=True)
            except Exception as exc:  # noqa: BLE001
                log.warning("Could not alert admin %s: %s", admin_id, exc)


def _payment_row(
    transfer: ChainTransfer, status: PaymentStatus, invoice_id: int | None = None
) -> Payment:
    return Payment(
        tx_id=transfer.tx_id,
        currency=transfer.currency,
        amount=str(transfer.amount),
        from_address=transfer.from_address,
        to_address=transfer.to_address,
        comment=transfer.comment,
        chain_ts=transfer.ts,
        status=status,
        invoice_id=invoice_id,
    )


def _fmt(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.strftime("%Y-%m-%d %H:%M UTC")
