"""Dispatcher middlewares.

Throttling exists because the "I have paid — check now" button triggers a chain
poll. A user mashing it would burn the TronGrid rate limit for everyone, so each
user gets at most one update per ``rate`` seconds. Admins are never throttled.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, TelegramObject

from gatekit.texts import t

# Forget users idle longer than this, so the table cannot grow without bound.
_EVICT_AFTER_SECONDS = 600.0


class ThrottlingMiddleware(BaseMiddleware):
    def __init__(
        self,
        rate: float,
        exempt: set[int] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.rate = rate
        self.exempt = exempt or set()
        self._clock = clock
        self._last_seen: dict[int, float] = {}
        self._last_sweep = clock()

    def allow(self, user_id: int) -> bool:
        """Record a hit for ``user_id`` and say whether it should be processed."""
        if self.rate <= 0 or user_id in self.exempt:
            return True
        now = self._clock()
        if now - self._last_sweep > _EVICT_AFTER_SECONDS:
            cutoff = now - _EVICT_AFTER_SECONDS
            self._last_seen = {k: v for k, v in self._last_seen.items() if v >= cutoff}
            self._last_sweep = now
        last = self._last_seen.get(user_id)
        if last is not None and now - last < self.rate:
            return False
        self._last_seen[user_id] = now
        return True

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user = data.get("event_from_user")
        if user is None or self.allow(user.id):
            return await handler(event, data)
        if isinstance(event, CallbackQuery):
            # Always answer a callback, otherwise the button spins forever.
            lang = "ru" if (user.language_code or "").startswith(("ru", "uk", "be")) else "en"
            await event.answer(t(lang, "slow_down"))
        return None
