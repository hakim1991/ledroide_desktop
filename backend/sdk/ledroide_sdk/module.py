import json
from pathlib import Path
from typing import Callable


class Module:
    """
    Loaded by a module's backend/main.py.
    Reads the manifest, registers lifecycle hooks, and wires SDK singletons.
    """

    def __init__(self, caller_file: str = None):
        start = Path(caller_file).resolve() if caller_file else Path(__file__).resolve()
        self.manifest = self._find_manifest(start)
        self._handlers: dict[str, list[Callable]] = {}
        # Per-instance SDK objects — never shared between modules
        from .events import _Events
        from .config import _Config
        from .db import _DB
        self._events = _Events()
        self._config = _Config()
        self._db = _DB()

    @property
    def id(self) -> str:
        return self.manifest["id"]

    @property
    def router(self):
        from fastapi import APIRouter
        if not hasattr(self, '_router'):
            self._router = APIRouter()
        return self._router

    def on(self, event: str):
        """Decorator — register an event handler triggered from the Electron UI."""
        def decorator(fn: Callable):
            self._handlers.setdefault(event, []).append(fn)
            return fn
        return decorator

    async def dispatch(self, event: str, payload: dict):
        """Called by the registry when a WebSocket 'call' message arrives."""
        result = None
        for handler in self._handlers.get(event, []):
            result = await handler(payload)
        return result

    @staticmethod
    def _find_manifest(start: Path) -> dict:
        current = start
        for _ in range(8):
            candidate = current.parent / "manifest.json"
            if candidate.exists():
                return json.loads(candidate.read_text(encoding="utf-8"))
            current = current.parent
        raise FileNotFoundError("manifest.json not found — is this module in modules/<id>/?")
