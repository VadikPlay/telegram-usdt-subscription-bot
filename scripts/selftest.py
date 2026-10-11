#!/usr/bin/env python3
"""End-to-end dry run with a fake chain and a fake Telegram.

Why this exists
───────────────
You cannot test a payment bot by reading the code. This script walks the whole
path — invoice → on-chain transfer → access granted → reminder → expiry →
removal — against a temporary database, a fake TronGrid and a fake Bot API.

No bot token, no real channel and no real money are involved, so you can run it
on any machine, and so can a customer who wants proof before they deploy.

    python scripts/selftest.py
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

# Configure before importing gatekit: Settings reads the environment at import.
TMP_DIR = Path(tempfile.mkdtemp(prefix="gatekit-selftest-"))
os.environ.update(
    BOT_TOKEN="123456789:AAselftestselftestselftestselftest",
    CHANNEL_ID="-1001234567890",
    ADMIN_IDS="999000999",
    PLANS="month:30:39.00,year:365:349.00",
    PLAN_LABELS="month=1 month|year=1 year",
    TRON_ENABLED="true",
    TRON_WALLET="TSelfTestWallet0000000000000000000",
    TRON_NETWORK="mainnet",
    TON_ENABLED="false",
    SAFE_MODE="false",
    GRACE_PERIOD_DAYS="2",
    PAYMENT_PROTECTION_HOURS="48",
    REMINDER_DAYS="3,1",
    DATABASE_URL=f"sqlite+aiosqlite:///{TMP_DIR / 'selftest.db'}",
    LOG_LEVEL="WARNING",
)

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from gatekit.chains.base import ChainTransfer  # noqa: E402
from gatekit.config import Settings  # noqa: E402
from gatekit.db.base import init_db  # noqa: E402
from gatekit.db.models import (  # noqa: E402
    Invoice,
    InvoiceStatus,
    Membership,
    MembershipState,
    utcnow,
)
from gatekit.services import invoices as invoice_service  # noqa: E402
from gatekit.services.access import AccessManager  # noqa: E402
from gatekit.workers.payment_watcher import PaymentWatcher  # noqa: E402
from gatekit.workers.scheduler import SubscriptionScheduler  # noqa: E402

PASS = "\033[32m✓\033[0m"
FAIL = "\033[31m✗\033[0m"
results: list[tuple[bool, str]] = []


def check(condition: bool, label: str) -> None:
    results.append((bool(condition), label))
    print(f"  {PASS if condition else FAIL} {label}")


# ── fakes ───────────────────────────────────────────────────────────────────
class FakeInviteLink:
    def __init__(self, link: str) -> None:
        self.invite_link = link


class FakeBot:
    """Records what would have been sent to Telegram."""

    def __init__(self) -> None:
        self.messages: list[tuple[int, str]] = []
        self.invites: list[int] = []
        self.banned: list[int] = []
        self.unbanned: list[int] = []

    async def send_message(self, chat_id: int, text: str, **_: object) -> None:
        self.messages.append((chat_id, text))

    async def create_chat_invite_link(self, chat_id: int, **kwargs: object) -> FakeInviteLink:
        user = str(kwargs.get("name", "")).removeprefix("gk-")
        self.invites.append(int(user) if user.isdigit() else 0)
        return FakeInviteLink(f"https://t.me/+fake_{user}")

    async def ban_chat_member(self, chat_id: int, user_id: int, **_: object) -> None:
        self.banned.append(user_id)

    async def unban_chat_member(self, chat_id: int, user_id: int, **_: object) -> None:
        self.unbanned.append(user_id)

    def messages_to(self, user_id: int) -> list[str]:
        return [text for uid, text in self.messages if uid == user_id]


class FakeTronClient:
    """A chain you control. Feed it transfers, it hands them to the watcher."""

    currency = "USDT"

    def __init__(self) -> None:
        self.queue: list[ChainTransfer] = []

    def push(self, transfer: ChainTransfer) -> None:
        self.queue.append(transfer)

    async def fetch_incoming(self, since_ts_ms: int, limit: int = 200):
        batch, self.queue = self.queue, []
        cursor = max(
            [int(t.ts.timestamp() * 1000) for t in batch] + [since_ts_ms]
        )
        return batch, cursor

    async def fetch_by_tx_id(self, tx_id: str) -> ChainTransfer | None:
        return next((t for t in self.queue if t.tx_id == tx_id), None)

    async def close(self) -> None:
        return None


def transfer_for(invoice: Invoice, *, tx_id: str, wallet: str, delta_minutes: int = 2):
    return ChainTransfer(
        tx_id=tx_id,
        currency=invoice.currency,
        amount=invoice.amount_dec,
        to_address=wallet,
        from_address="TFakePayerAddress",
        ts=datetime.now(tz=timezone.utc) + timedelta(minutes=delta_minutes),
    )


# ── the run ─────────────────────────────────────────────────────────────────
async def main() -> int:
    settings = Settings()  # type: ignore[call-arg]
    from gatekit.db.base import get_sessionmaker

    sessionmaker = get_sessionmaker(settings.database_url)
    await init_db(settings.database_url)

    bot = FakeBot()
    chain = FakeTronClient()
    access = AccessManager(bot, settings)  # type: ignore[arg-type]
    watcher = PaymentWatcher(
        bot=bot,  # type: ignore[arg-type]
        settings=settings,
        sessionmaker=sessionmaker,
        clients=[chain],  # type: ignore[list-item]
        access=access,
    )
    scheduler = SubscriptionScheduler(
        bot=bot, settings=settings, sessionmaker=sessionmaker, access=access  # type: ignore[arg-type]
    )

    print("\n\033[1mGatekit self-test\033[0m")
    print(f"  workspace: {TMP_DIR}\n")

    # ── 1. configuration ────────────────────────────────────────────────────
    print("1. Configuration")
    check(len(settings.plan_list) == 2, "two plans parsed from PLANS")
    check(settings.plan_by_code("month").days == 30, "plan 'month' is 30 days")
    check(settings.plan_by_code("month").label == "1 month", "human label applied")
    check(settings.enabled_currencies == ["USDT"], "only the USDT rail is active")
    check(settings.admin_id_set == {999000999}, "admin id parsed")
    check(settings.reminder_day_list == [3, 1], "reminder days parsed in order")

    # ── 2. invoice creation ─────────────────────────────────────────────────
    print("\n2. Invoice")
    user_id = 424242
    async with sessionmaker() as session:
        await invoice_service.ensure_user(
            session,
            user_id=user_id,
            username="selftest_user",
            full_name="Self Test",
            default_lang="en",
        )
        invoice = await invoice_service.create_invoice(
            session,
            settings,
            user_id=user_id,
            plan=settings.plan_by_code("month"),
            currency="USDT",
        )
        await session.commit()
        invoice_id, invoice_code = invoice.id, invoice.code
        expected_amount = invoice.amount_dec

    check(expected_amount != Decimal("39.00"), "amount carries a unique tag")
    check(
        Decimal("39.00") < expected_amount < Decimal("39.01"),
        f"surcharge is under a cent ({expected_amount})",
    )
    check(invoice_code.startswith("GK-"), f"invoice code issued ({invoice_code})")

    # A second invoice for the same user must retire the first one.
    async with sessionmaker() as session:
        second = await invoice_service.create_invoice(
            session, settings, user_id=user_id, plan=settings.plan_by_code("month"),
            currency="USDT",
        )
        await session.commit()
        first_again = await session.get(Invoice, invoice_id)
        check(
            first_again.status == InvoiceStatus.CANCELLED,
            "creating a new invoice cancels the previous open one",
        )
        invoice_id, expected_amount = second.id, second.amount_dec

    # ── 3. wrong amount is refused ──────────────────────────────────────────
    print("\n3. A rounded payment must NOT unlock access")
    wrong = ChainTransfer(
        tx_id="tx-rounded",
        currency="USDT",
        amount=Decimal("39.000000"),
        to_address=settings.tron_wallet,
        from_address="TFakePayerAddress",
        ts=datetime.now(tz=timezone.utc),
    )
    granted = await watcher.process_transfers([wrong])
    check(not granted, "39.00 did not settle a 39.00xxxx invoice")
    async with sessionmaker() as session:
        check(
            (await session.get(Invoice, invoice_id)).status == InvoiceStatus.OPEN,
            "invoice is still open",
        )
        check(await session.get(Membership, user_id) is None, "no membership was created")
    check(
        any("matched no open invoice" in m for _, m in bot.messages),
        "the owner was alerted about unmatched money",
    )

    # ── 4. correct payment grants access ────────────────────────────────────
    print("\n4. The exact payment unlocks access")
    async with sessionmaker() as session:
        invoice = await session.get(Invoice, invoice_id)
    good = transfer_for(invoice, tx_id="tx-correct", wallet=settings.tron_wallet)
    granted = await watcher.process_transfers([good])
    check(len(granted) == 1, "payment matched exactly one invoice")

    async with sessionmaker() as session:
        settled = await session.get(Invoice, invoice_id)
        membership = await session.get(Membership, user_id)
        check(settled.status == InvoiceStatus.PAID, "invoice marked paid")
        check(settled.paid_tx_id == "tx-correct", "transaction id recorded on the invoice")
        check(membership is not None, "membership created")
        check(membership.payments_count == 1, "payment counted")
        check(membership.total_paid_usdt == "39.000000", "revenue recorded at the list price")
        expires = membership.expires_at
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        days = (expires - datetime.now(tz=timezone.utc)).days
        check(29 <= days <= 30, f"access runs ~30 days ({days})")
    check(user_id in bot.invites, "single-use invite link issued")
    check(
        any("Payment confirmed" in m for m in bot.messages_to(user_id)),
        "subscriber was told they are in",
    )

    # ── 5. replay protection ────────────────────────────────────────────────
    print("\n5. The same transaction cannot be credited twice")
    before = len(bot.invites)
    granted = await watcher.process_transfers([good])
    check(not granted, "replayed transaction was ignored")
    check(len(bot.invites) == before, "no second invite link was issued")

    # ── 6. join tracking ────────────────────────────────────────────────────
    print("\n6. Joining the channel")
    async with sessionmaker() as session:
        await access.mark_joined(session, user_id)
        await session.commit()
        membership = await session.get(Membership, user_id)
        check(membership.state == MembershipState.ACTIVE, "state moved to active")
        check(membership.joined_at is not None, "join time recorded")

    # ── 7. renewal stacks ───────────────────────────────────────────────────
    print("\n7. Renewing early adds days instead of resetting them")
    async with sessionmaker() as session:
        membership = await session.get(Membership, user_id)
        before_expiry = membership.expires_at
        if before_expiry.tzinfo is None:
            before_expiry = before_expiry.replace(tzinfo=timezone.utc)
        await access.grant(
            session, user_id=user_id, plan_code="month", plan_days=30, amount_usdt="39.00"
        )
        await session.commit()
        after = await session.get(Membership, user_id)
        after_expiry = after.expires_at
        if after_expiry.tzinfo is None:
            after_expiry = after_expiry.replace(tzinfo=timezone.utc)
        check(
            abs((after_expiry - before_expiry).days - 30) <= 1,
            "30 days added on top of the remaining time",
        )
        check(after.payments_count == 2, "second payment counted")
        check(after.total_paid_usdt == "78.000000", "running revenue total is right")

    # ── 8. reminders ────────────────────────────────────────────────────────
    print("\n8. Renewal reminder before expiry")
    async with sessionmaker() as session:
        membership = await session.get(Membership, user_id)
        membership.expires_at = utcnow() + timedelta(days=2, hours=12)
        membership.reminders_sent = ""
        await session.commit()
    summary = await scheduler.tick()
    check(summary["reminded"] == 1, "one reminder sent")
    check(
        any("expires in" in m for m in bot.messages_to(user_id)),
        "the subscriber was warned before expiry",
    )
    summary = await scheduler.tick()
    check(summary["reminded"] == 0, "the same reminder is not sent twice")

    # ── 9. grace period ─────────────────────────────────────────────────────
    print("\n9. Expiry enters a grace period before anyone is removed")
    async with sessionmaker() as session:
        membership = await session.get(Membership, user_id)
        membership.expires_at = utcnow() - timedelta(hours=6)
        await session.commit()
    summary = await scheduler.tick()
    check(summary["grace"] == 1, "membership moved into grace, not removed")
    check(not bot.banned, "nobody was removed from the channel")

    # ── 10. payment protection ──────────────────────────────────────────────
    print("\n10. A recent payment blocks removal even when the dates say otherwise")
    async with sessionmaker() as session:
        membership = await session.get(Membership, user_id)
        membership.expires_at = utcnow() - timedelta(days=30)
        membership.last_payment_at = utcnow() - timedelta(hours=2)
        membership.state = MembershipState.ACTIVE
        await session.commit()
    summary = await scheduler.tick()
    check(summary["revoked"] == 0, "removal refused: paid within the protection window")
    check(not bot.banned, "still nobody removed")

    # ── 11. real removal ────────────────────────────────────────────────────
    print("\n11. Removal happens only when every guard agrees")
    async with sessionmaker() as session:
        membership = await session.get(Membership, user_id)
        membership.expires_at = utcnow() - timedelta(days=30)
        membership.last_payment_at = utcnow() - timedelta(days=40)
        membership.state = MembershipState.ACTIVE
        await session.commit()
    summary = await scheduler.tick()
    check(summary["revoked"] == 1, "lapsed member removed")
    check(bot.banned == [user_id], "ban issued")
    check(bot.unbanned == [user_id], "unban issued straight after, so they can return")
    async with sessionmaker() as session:
        membership = await session.get(Membership, user_id)
        check(membership.state == MembershipState.REVOKED, "state recorded as revoked")
    summary = await scheduler.tick()
    check(summary["revoked"] == 0, "removal is idempotent — never repeated")

    # ── 12. safe mode ───────────────────────────────────────────────────────
    print("\n12. SAFE_MODE removes nobody at all")
    safe_settings = Settings(safe_mode=True)  # type: ignore[call-arg]
    safe_scheduler = SubscriptionScheduler(
        bot=bot, settings=safe_settings, sessionmaker=sessionmaker, access=access  # type: ignore[arg-type]
    )
    async with sessionmaker() as session:
        membership = await session.get(Membership, user_id)
        membership.state = MembershipState.ACTIVE
        membership.expires_at = utcnow() - timedelta(days=30)
        membership.last_payment_at = utcnow() - timedelta(days=40)
        membership.revoked_at = None
        await session.commit()
    bans_before = len(bot.banned)
    summary = await safe_scheduler.tick()
    check(summary["warned"] == 1, "owner warned instead of a removal")
    check(len(bot.banned) == bans_before, "SAFE_MODE removed nobody")

    # ── 13. audit trail ─────────────────────────────────────────────────────
    print("\n13. Everything is on the record")
    from sqlalchemy import select

    from gatekit.db.models import AuditLog

    async with sessionmaker() as session:
        actions = [
            row for row in (await session.scalars(select(AuditLog.action))).all()
        ]
    for expected in ("granted", "payment_matched", "payment_unmatched", "joined", "revoked"):
        check(expected in actions, f"audit log contains '{expected}'")

    # ── 14. stats and export ────────────────────────────────────────────────
    print("\n14. Owner-facing numbers")
    from gatekit.services import stats as stats_service

    async with sessionmaker() as session:
        stats = await stats_service.collect(session)
        # Channel revenue counts invoices actually settled on-chain. Step 7's
        # renewal was granted directly (as an admin grant would be), so it
        # raises the member's own lifetime total but is NOT channel revenue —
        # a manual grant must never inflate the owner's income figure.
        check(
            stats.revenue_total == Decimal("39.00"),
            f"revenue counts on-chain payments only ({stats.revenue_total})",
        )
        check(stats.revenue_ton_total == Decimal("0"), "no TON revenue on a USDT-only instance")
        check(stats.payments_30d == 1, "payments in the last 30 days counted")
        check(stats.unmatched_payments == 1, "the rounded payment is flagged for the owner")
        path = await stats_service.export_csv(session, directory=str(TMP_DIR / "exports"))
        content = path.read_text(encoding="utf-8")
        check(path.exists(), f"CSV written ({path.name})")
        check("user_id,username" in content, "CSV has a header row")
        check(str(user_id) in content, "CSV contains the subscriber")

    # ── summary ─────────────────────────────────────────────────────────────
    passed = sum(1 for ok, _ in results if ok)
    failed = len(results) - passed
    print("\n" + "─" * 62)
    if failed:
        print(f"\033[31m{failed} check(s) FAILED\033[0m, {passed} passed")
        for ok, label in results:
            if not ok:
                print(f"  {FAIL} {label}")
        return 1
    print(f"\033[32mAll {passed} checks passed.\033[0m")
    print(
        "\nThe full money path works: invoice → exact on-chain payment → access,\n"
        "and the safety rails hold: rounded amounts are refused, replays ignored,\n"
        "recent payers protected, and SAFE_MODE removes nobody.\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
