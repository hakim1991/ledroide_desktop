from typing import Callable


class _Events:
    """Emits events to every connected Electron window via the registry broadcast."""

    def __init__(self):
        self._broadcast_fn: Callable | None = None

    def _inject(self, fn: Callable):
        self._broadcast_fn = fn

    async def emit(self, event: str, payload: dict = None):
        if not self._broadcast_fn:
            return
        await self._broadcast_fn(event, payload or {})


events = _Events()
