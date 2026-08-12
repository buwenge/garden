"""小院子待展示事件的投递确认：失败必须留给下次重试。"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any


async def confirm_event_safely(
    event: dict | None,
    *,
    confirm: Callable[[str], Any],
    to_thread: Callable[..., Awaitable[bool]],
    warn: Callable[[str, Exception], None],
) -> bool:
    event_id = event.get("event_id") if isinstance(event, dict) else None
    delivery_token = event.get("delivery_token") if isinstance(event, dict) else None
    if not isinstance(event_id, str) or not event_id or not isinstance(delivery_token, str) or not delivery_token:
        return False
    try:
        return await to_thread(confirm, event_id, delivery_token)
    except Exception as exc:
        warn("小院子里程碑确认延后（pending 保留）: %s", exc)
        return False
