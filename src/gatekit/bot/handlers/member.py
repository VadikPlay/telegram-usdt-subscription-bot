"""Channel membership events.

Telegram tells us when someone joins or leaves the protected channel. Recording
it is what makes "paid but never joined" visible to the owner, and it is also
the safety net: if a member vanishes from the channel we know it, rather than
assuming our database is reality.
"""

from __future__ import annotations

import logging

from aiogram import Router
from aiogram.filters import IS_MEMBER, IS_NOT_MEMBER, ChatMemberUpdatedFilter
from aiogram.types import ChatMemberUpdated
from sqlalchemy.ext.asyncio import async_sessionmaker

from gatekit.config import Settings
from gatekit.services.access import AccessManager

log = logging.getLogger(__name__)
router = Router(name="member")


@router.chat_member(ChatMemberUpdatedFilter(member_status_changed=IS_NOT_MEMBER >> IS_MEMBER))
async def on_join(
    event: ChatMemberUpdated,
    settings: Settings,
    sessionmaker: async_sessionmaker,
    access: AccessManager,
    **_: object,
) -> None:
    if event.chat.id != settings.channel_id:
        return
    user_id = event.new_chat_member.user.id
    async with sessionmaker() as session:
        await access.mark_joined(session, user_id)
        await session.commit()
    log.info("User %s joined the protected channel", user_id)


@router.chat_member(ChatMemberUpdatedFilter(member_status_changed=IS_MEMBER >> IS_NOT_MEMBER))
async def on_leave(
    event: ChatMemberUpdated,
    settings: Settings,
    sessionmaker: async_sessionmaker,
    access: AccessManager,
    **_: object,
) -> None:
    if event.chat.id != settings.channel_id:
        return
    user_id = event.old_chat_member.user.id
    async with sessionmaker() as session:
        await access.mark_left(session, user_id)
        await session.commit()
    log.info("User %s left the protected channel", user_id)
