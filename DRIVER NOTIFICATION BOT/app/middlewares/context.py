from __future__ import annotations

from typing import Any, Awaitable, Callable, Dict

from aiogram import BaseMiddleware


class ContextMiddleware(BaseMiddleware):
    def __init__(self, **payload: Any) -> None:
        self._payload = payload

    async def __call__(
        self,
        handler: Callable[[Any, Dict[str, Any]], Awaitable[Any]],
        event: Any,
        data: Dict[str, Any],
    ) -> Any:
        data.update(self._payload)
        return await handler(event, data)
