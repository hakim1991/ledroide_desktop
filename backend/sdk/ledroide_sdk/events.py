from typing import Callable, Coroutine


class _Events:
    """Sends events to all connected Electron windows via the registry broadcast."""

    def __init__(self):
        self._broadcast_fn: Callable = None

    def _inject(self, fn: Callable):
        self._broadcast_fn = fn

    async def emit(self, event: str, payload: dict = None):
        if not self._broadcast_fn:
            return  # no-op when no Electron window connected
        await self._broadcast_fn(event, payload or {})


events = _Events()
