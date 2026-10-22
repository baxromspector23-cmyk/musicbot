"""Dispatcher-level middlewares."""
from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject, User

log = logging.getLogger(__name__)


class OwnerOnlyMiddleware(BaseMiddleware):
    """Silently ignore every update that does not come from the owner."""

    def __init__(self, owner_id: int) -> None:
        self.owner_id = owner_id

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user: User | None = data.get("event_from_user")
        if self.owner_id > 0:
            if user is None or user.id != self.owner_id:
                log.debug("ignored update from non-owner user id %s", user.id if user else None)
                return None
        return await handler(event, data)
