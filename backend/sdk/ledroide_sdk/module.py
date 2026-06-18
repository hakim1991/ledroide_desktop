import json
from pathlib import Path
from typing import Callable


class Module:
    """
    Loaded by a module's backend/main.py.
    Reads the manifest, registers lifecycle hooks, and wires SDK singletons.
    """

    def __init__(self):
        self.manifest = self._find_manifest()
        self._handlers: dict[str, list[Callable]] = {}
        # Expose singletons for injection by the registry
        from . import db, events, config
        self._db = db
        self._events = events
        self._config = config

    @property
    def id(self) -> str:
        return self.manifest["id"]

    def on(self, event: str):
        """Decorator — register an event handler triggered from the Electron UI."""
        def decorator(fn: Callable):
            self._handlers.setdefault(event, []).append(fn)
            return fn
        return decorator

    async def dispatch(self, event: str, payload: dict):
        """Called by the registry when a WebSocket 'call' message arrives."""
        for handler in self._handlers.get(event, []):
            await handler(payload)

    @staticmethod
    def _find_manifest() -> dict:
        current = Path(__file__).resolve()
        for _ in range(8):
            candidate = current.parent / "manifest.json"
            if candidate.exists():
                return json.loads(candidate.read_text(encoding="utf-8"))
            current = current.parent
        raise FileNotFoundError("manifest.json not found — is this module in modules/<id>/?")
