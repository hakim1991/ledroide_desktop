import asyncio
import json
import logging
import os
import shutil
import time
import uuid
from pathlib import Path
from typing import Any, Awaitable, Callable

from .config import _Config
from .db import _DB
from .events import _Events

log = logging.getLogger("ledroide.sdk")

JOB_RETENTION_S = 600


def _module_data_dir(module_id: str, module_root: Path) -> Path:
    base = os.environ.get("LEDROIDE_DATA_DIR")
    path = Path(base) / "modules" / module_id if base else module_root / "data"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _resolve_data_path(data_dir: Path, name: str, legacy: str | Path | None) -> Path:
    target = data_dir / name
    if legacy is not None:
        legacy = Path(legacy)
        if legacy.exists() and not target.exists() and legacy.resolve() != target.resolve():
            shutil.move(str(legacy), str(target))
            for suffix in ("-wal", "-shm", "-journal"):
                side = legacy.with_name(legacy.name + suffix)
                if side.exists():
                    shutil.move(str(side), str(target.with_name(target.name + suffix)))
    return target


def data_path(caller_file: str, name: str) -> Path:
    """Writable per-module file path, usable without a Module instance:
    `DB_PATH = data_path(__file__, "notes.db")`. A file of the same name next to
    `caller_file` (old layout) is moved there on first use."""
    manifest, root = Module._find_manifest(Path(caller_file).resolve())
    legacy = Path(caller_file).resolve().parent / name
    return _resolve_data_path(_module_data_dir(manifest["id"], root), name, legacy)


class Job:
    """Handle passed to @module.job handlers to report progress."""

    def __init__(self, module: "Module", job_id: str):
        self.module = module
        self.id = job_id
        self.cancelled = False

    async def progress(self, percent: float | None = None, message: str | None = None, **extra):
        state = {"status": "running", "percent": percent, "message": message, **extra}
        self.module._job_state[self.id] = {**state, "updated": time.time()}
        await self.module._events.emit("job:progress", {"job_id": self.id, **state})


class Module:
    """Instantiated by a module's backend/main.py: `module = Module(__file__)`."""

    def __init__(self, caller_file: str = None):
        start = Path(caller_file).resolve() if caller_file else Path(__file__).resolve()
        self.manifest, self.root = self._find_manifest(start)
        self._handlers: dict[str, list[Callable]] = {}
        self._shutdown_hooks: list[Callable[[], Awaitable[None]]] = []
        self._jobs: dict[str, asyncio.Task] = {}
        self._job_state: dict[str, dict] = {}
        self._events = _Events()
        self._config = _Config(self._config_defaults())
        self._db = _DB(self)
        self._handlers["job:status"] = [self._job_status]
        self._handlers["job:cancel"] = [self._job_cancel]

    @property
    def id(self) -> str:
        return self.manifest["id"]

    @property
    def events(self) -> _Events:
        return self._events

    @property
    def config(self) -> _Config:
        return self._config

    @property
    def db(self) -> _DB:
        return self._db

    def env(self, key: str, default: str | None = None) -> str | None:
        """Variable defined in the app's « Variables » menu (or the process env)."""
        return os.environ.get(key, default)

    @property
    def router(self):
        from fastapi import APIRouter
        if not hasattr(self, "_router"):
            self._router = APIRouter()
        return self._router

    @property
    def data_dir(self) -> Path:
        """Per-module writable directory, outside the install dir."""
        return _module_data_dir(self.id, self.root)

    def data_path(self, name: str, legacy: str | Path | None = None) -> Path:
        """Path inside data_dir; moves a legacy file (e.g. backend/notes.db) there on first use."""
        return _resolve_data_path(self.data_dir, name, legacy)

    # ── Handlers ─────────────────────────────────────────────────────────────

    def on(self, action: str):
        """Register a handler for `window.ledroide.call(id, action, payload)`."""
        def decorator(fn: Callable):
            self._handlers.setdefault(action, []).append(fn)
            return fn
        return decorator

    def job(self, action: str):
        """Register a long-running handler `async def fn(payload, job)`.
        The call returns `{"job_id"}` at once; progress/result arrive as
        `job:progress`, `job:done`, `job:error` events (see `window.ledroide.run`)."""
        def decorator(fn: Callable):
            async def start(payload: dict):
                job = Job(self, uuid.uuid4().hex)
                self._job_state[job.id] = {"status": "running", "updated": time.time()}
                self._jobs[job.id] = asyncio.create_task(self._run_job(job, fn, payload))
                return {"job_id": job.id}
            self._handlers.setdefault(action, []).append(start)
            return fn
        return decorator

    def on_shutdown(self, fn: Callable[[], Awaitable[None]]):
        """Called when the module is deactivated or the backend stops."""
        self._shutdown_hooks.append(fn)
        return fn

    async def dispatch(self, action: str, payload: dict):
        result = None
        for handler in self._handlers.get(action, []):
            result = await handler(payload)
        return result

    def has_handler(self, action: str) -> bool:
        return action in self._handlers

    async def _shutdown(self):
        for task in list(self._jobs.values()):
            task.cancel()
        for hook in self._shutdown_hooks:
            try:
                await hook()
            except Exception:
                log.exception("[%s] shutdown hook failed", self.id)
        await self._db.close()

    # ── Jobs ─────────────────────────────────────────────────────────────────

    async def _run_job(self, job: Job, fn: Callable, payload: dict):
        try:
            result = await fn(payload, job)
            self._finish(job.id, {"status": "done", "result": result})
            await self._events.emit("job:done", {"job_id": job.id, "result": result})
        except asyncio.CancelledError:
            job.cancelled = True
            self._finish(job.id, {"status": "error", "error": "Tâche annulée", "code": "JOB_CANCELLED"})
            await self._events.emit("job:error", {"job_id": job.id, "error": "Tâche annulée", "code": "JOB_CANCELLED"})
        except Exception as exc:
            log.exception("[%s] job %s failed", self.id, job.id)
            self._finish(job.id, {"status": "error", "error": str(exc), "code": "JOB_FAILED"})
            await self._events.emit("job:error", {"job_id": job.id, "error": str(exc), "code": "JOB_FAILED"})
        finally:
            self._jobs.pop(job.id, None)

    def _finish(self, job_id: str, state: dict):
        now = time.time()
        self._job_state[job_id] = {**state, "updated": now}
        for jid, st in list(self._job_state.items()):
            if st["status"] != "running" and now - st["updated"] > JOB_RETENTION_S:
                self._job_state.pop(jid, None)

    async def _job_status(self, payload: dict):
        state = self._job_state.get(payload.get("job_id"))
        if state is None:
            return {"error": "Tâche inconnue", "code": "JOB_NOT_FOUND"}
        return state

    async def _job_cancel(self, payload: dict):
        task = self._jobs.get(payload.get("job_id"))
        if task is None:
            return {"error": "Tâche inconnue ou terminée", "code": "JOB_NOT_FOUND"}
        task.cancel()
        return {"ok": True}

    # ── Internal ─────────────────────────────────────────────────────────────

    def _config_defaults(self) -> dict:
        schema = self.manifest.get("config_schema") or {}
        return {k: v["default"] for k, v in schema.items() if isinstance(v, dict) and "default" in v}

    @staticmethod
    def _find_manifest(start: Path) -> tuple[dict, Path]:
        current = start
        for _ in range(8):
            candidate = current.parent / "manifest.json"
            if candidate.exists():
                return json.loads(candidate.read_text(encoding="utf-8")), candidate.parent
            current = current.parent
        raise FileNotFoundError("manifest.json not found — is this module in modules/<id>/?")
