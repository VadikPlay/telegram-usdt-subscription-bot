"""Wire everything together and run.

Startup order matters: validate config loudly → prepare the database → verify
the bot can actually administer the channel → only then start polling and the
background workers. Failing at startup with a clear message beats discovering a
misconfiguration when a subscriber's money is already on the chain.
"""

from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError, TelegramUnauthorizedError
from pydantic import ValidationError

from gatekit import __version__
from gatekit.bot.handlers import admin as admin_handlers
from gatekit.bot.handlers import member as member_handlers
from gatekit.bot.handlers import user as user_handlers
from gatekit.chains.base import ChainClient
from gatekit.chains.ton import TonClient
from gatekit.chains.tron import TronClient
from gatekit.config import Settings, get_settings
from gatekit.db.base import dispose_engine, get_sessionmaker, init_db
from gatekit.services.access import AccessManager
from gatekit.workers.payment_watcher import PaymentWatcher
from gatekit.workers.scheduler import SubscriptionScheduler

log = logging.getLogger("gatekit")


def setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    # aiogram's polling chatter is not useful at INFO.
    logging.getLogger("aiogram.event").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)


def build_chain_clients(settings: Settings) -> list[ChainClient]:
    clients: list[ChainClient] = []
    if settings.tron_enabled and settings.tron_wallet:
        clients.append(
            TronClient(
                base_url=settings.tron_base_url,
                wallet=settings.tron_wallet,
                contract=settings.tron_usdt_contract,
                decimals=settings.decimals_for("USDT"),
                api_key=settings.tron_api_key,
            )
        )
        log.info(
            "USDT rail on TRON %s, watching %s", settings.tron_network, settings.tron_wallet
        )
    if settings.ton_enabled and settings.ton_wallet:
        clients.append(TonClient(wallet=settings.ton_wallet, api_key=settings.ton_api_key))
        log.info("TON rail enabled, watching %s", settings.ton_wallet)
    return clients


async def verify_channel_rights(bot: Bot, settings: Settings) -> None:
    """Fail fast if the bot cannot do the one thing it exists to do.

    The token and the channel are checked separately so the error message names
    the actual problem instead of sending the installer down the wrong path.
    """
    try:
        me = await bot.get_me()
    except TelegramUnauthorizedError as exc:
        raise SystemExit(
            "Telegram rejected the bot token.\n"
            "Fix: copy BOT_TOKEN from @BotFather again — it looks like "
            "123456789:AA... with no spaces or quotes."
        ) from exc
    except TelegramAPIError as exc:
        raise SystemExit(
            f"Could not reach the Telegram API: {exc}\n"
            "Fix: check this server's outbound internet access."
        ) from exc

    try:
        member = await bot.get_chat_member(settings.channel_id, me.id)
    except TelegramAPIError as exc:
        raise SystemExit(
            f"The bot cannot see channel {settings.channel_id}: {exc}\n"
            "Fix, in order: (1) add @"
            f"{me.username} to the channel as an ADMIN; (2) re-check CHANNEL_ID — "
            "it must be the numeric id starting with -100, not the @username."
        ) from exc

    status = getattr(member, "status", None)
    if status not in {"administrator", "creator"}:
        raise SystemExit(
            f"The bot is in the channel as '{status}', not an administrator.\n"
            "Fix: promote it and enable 'Invite users via link' and 'Ban users'."
        )
    if getattr(member, "can_invite_users", True) is False:
        raise SystemExit(
            "The bot is an admin but cannot invite users.\n"
            "Fix: enable the 'Invite users via link' permission."
        )
    if getattr(member, "can_restrict_members", True) is False:
        log.warning(
            "The bot cannot restrict members — issuing access will work, removing "
            "lapsed members will not. Enable 'Ban users' to fix."
        )
    log.info("Channel rights verified for @%s", me.username)


def load_settings_or_explain() -> Settings:
    """Turn a pydantic validation dump into instructions a human can act on."""
    try:
        return get_settings()
    except ValidationError as exc:
        lines = ["Gatekit cannot start: the configuration is incomplete.", ""]
        for error in exc.errors():
            field = ".".join(str(p) for p in error.get("loc", ())) or "?"
            lines.append(f"  • {field.upper()}: {error.get('msg')}")
        lines += [
            "",
            "Fix: copy .env.example to .env and fill in BOT_TOKEN, CHANNEL_ID,",
            "ADMIN_IDS and TRON_WALLET. Every setting is documented in that file.",
        ]
        raise SystemExit("\n".join(lines)) from exc


async def run_bot() -> None:
    settings = load_settings_or_explain()
    setup_logging(settings.log_level)
    log.info("Gatekit %s starting", __version__)

    problems = settings.sanity_check()
    for problem in problems:
        log.warning("CONFIG: %s", problem)

    sessionmaker = get_sessionmaker(settings.database_url)
    await init_db(settings.database_url)

    bot = Bot(
        token=settings.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    try:
        await verify_channel_rights(bot, settings)
    except BaseException:
        # Close the HTTP session before bailing out, otherwise a failed startup
        # leaves an unclosed aiohttp session and a confusing second error on top
        # of the real one.
        await bot.session.close()
        raise

    access = AccessManager(bot, settings)
    clients = build_chain_clients(settings)
    if not clients:
        log.error(
            "No payment rail is active — subscribers can create invoices but nothing "
            "will ever confirm. Set TRON_ENABLED/TRON_WALLET (and/or TON)."
        )

    watcher = PaymentWatcher(
        bot=bot,
        settings=settings,
        sessionmaker=sessionmaker,
        clients=clients,
        access=access,
    )
    scheduler = SubscriptionScheduler(
        bot=bot, settings=settings, sessionmaker=sessionmaker, access=access
    )

    dp = Dispatcher()
    dp.include_router(admin_handlers.router)
    dp.include_router(user_handlers.router)
    dp.include_router(member_handlers.router)

    # Everything handlers need arrives as a keyword argument.
    dp.workflow_data.update(
        settings=settings,
        sessionmaker=sessionmaker,
        access=access,
        watcher=watcher,
    )

    if settings.safe_mode:
        log.warning(
            "SAFE_MODE is ON: nobody will be removed from the channel, you will only "
            "be notified. Set SAFE_MODE=false once you trust the numbers."
        )

    tasks = [asyncio.create_task(watcher.run()), asyncio.create_task(scheduler.run())]
    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        watcher.stop()
        scheduler.stop()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for client in clients:
            await client.close()
        await bot.session.close()
        await dispose_engine()
        log.info("Gatekit stopped")
