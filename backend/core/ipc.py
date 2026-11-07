import asyncio
import logging
from typing import Any, Literal, Optional

from fastapi import WebSocket
from pydantic import BaseModel, Field, ValidationError

log = logging.getLogger("ledroide.ipc")


class WsCall(BaseModel):
    type: Literal["call"] = "call"
    module_id: str
    action: str
    payload: dict[str, Any] = Field(default_factory=dict)
    request_id: Optional[str] = None


class WsResponse(BaseModel):
    type: Literal["response"] = "response"
    module_id: Optional[str]
    action: Optional[str]
    request_id: Optional[str]
    payload: Any


class WsEvent(BaseModel):
    type: Literal["event"] = "event"
    module_id: str
    action: str
    payload: Any


class Connection:
    """One WebSocket; sends are serialized because calls are handled concurrently."""

    def __init__(self, ws: WebSocket):
        self.ws = ws
        self._lock = asyncio.Lock()
        self._tasks: set[asyncio.Task] = set()

    async def send(self, data: dict):
        async with self._lock:
            await self.ws.send_json(data)

    def spawn(self, coro):
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def cancel_all(self):
        for task in list(self._tasks):
            task.cancel()


def parse_call(data: Any) -> WsCall | None:
    try:
        return WsCall.model_validate(data)
    except ValidationError as exc:
        log.warning("invalid WS message: %s", exc.errors()[:1])
        return None
