import asyncio
import hashlib
import json
import logging
import os
import re
from contextlib import AsyncExitStack
from datetime import timedelta
from pathlib import Path
from typing import Any, Awaitable, Callable

log = logging.getLogger("ledroide.litellm-agent.mcp")

CONNECT_TIMEOUT_S = 60
LOG_TAIL_CHARS = 2000


class McpError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def mcp_available() -> bool:
    try:
        import mcp  # noqa: F401
        return True
    except ImportError:
        return False


def _flatten_exc(exc: BaseException) -> str:
    while isinstance(exc, BaseExceptionGroup) and exc.exceptions:
        exc = exc.exceptions[0]
    msg = str(exc).strip() or type(exc).__name__
    return msg[:500]


def _fingerprint(cfg: dict) -> str:
    keys = ("transport", "command", "args", "env", "cwd", "url", "headers")
    return hashlib.sha1(json.dumps({k: cfg.get(k) for k in keys}, sort_keys=True).encode()).hexdigest()


def result_to_text(result: Any) -> str:
    parts: list[str] = []
    for c in getattr(result, "content", None) or []:
        ctype = getattr(c, "type", None)
        if ctype == "text":
            parts.append(c.text)
        elif ctype in ("image", "audio"):
            size = len(getattr(c, "data", "") or "") * 3 // 4
            parts.append(f"[{ctype} {getattr(c, 'mimeType', '')}, ~{size} octets — non transmis au modèle]")
        elif ctype == "resource":
            res = c.resource
            text = getattr(res, "text", None)
            parts.append(text if text is not None else f"[ressource binaire {getattr(res, 'uri', '')}]")
        elif ctype == "resource_link":
            parts.append(f"[ressource {getattr(c, 'uri', '')}]")
        else:
            parts.append(str(c))
    structured = getattr(result, "structuredContent", None)
    if not parts and structured is not None:
        parts.append(json.dumps(structured, ensure_ascii=False, indent=2))
    return "\n".join(parts) if parts else "(résultat vide)"


class McpConnection:
    def __init__(self, cfg: dict, log_dir: Path, on_status: Callable[[dict], Awaitable[None]]):
        self.cfg = cfg
        self.id = cfg["_id"]
        self.fingerprint = _fingerprint(cfg)
        self.log_path = log_dir / f"{self.id}.log"
        self.on_status = on_status
        self.status = "stopped"
        self.error: str | None = None
        self.tools: list[Any] = []
        self.session = None
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self._ready: asyncio.Future | None = None

    def snapshot(self) -> dict:
        return {"id": self.id, "status": self.status, "error": self.error,
                "tools": [{"name": t.name, "description": (t.description or "")[:300]} for t in self.tools]}

    def log_tail(self) -> str:
        try:
            data = self.log_path.read_text(encoding="utf-8", errors="replace")
            return data[-LOG_TAIL_CHARS:]
        except OSError:
            return ""

    async def _set(self, status: str, error: str | None = None):
        self.status = status
        self.error = error
        try:
            await self.on_status(self.snapshot())
        except Exception:
            log.debug("status callback failed", exc_info=True)

    async def start(self):
        if self._task and not self._task.done():
            return await self._wait_ready()
        self._stop = asyncio.Event()
        self._ready = asyncio.get_running_loop().create_future()
        self._task = asyncio.create_task(self._run())
        return await self._wait_ready()

    async def _wait_ready(self):
        timeout = max(5, int(self.cfg.get("timeout") or CONNECT_TIMEOUT_S))
        try:
            await asyncio.wait_for(asyncio.shield(self._ready), timeout)
        except asyncio.TimeoutError:
            await self.stop()
            await self._set("error", f"Délai de connexion dépassé ({timeout} s)")
            raise McpError("MCP_TIMEOUT", f"Serveur MCP « {self.cfg.get('name')} » : délai de connexion dépassé")

    async def _open_transport(self, stack: AsyncExitStack):
        cfg = self.cfg
        transport = cfg.get("transport", "stdio")
        if transport == "stdio":
            from mcp.client.stdio import StdioServerParameters, stdio_client
            if not (cfg.get("command") or "").strip():
                raise McpError("MCP_CONFIG", "Commande manquante")
            env = {**os.environ, **{k: str(v) for k, v in (cfg.get("env") or {}).items()}}
            params = StdioServerParameters(command=cfg["command"].strip(), args=list(cfg.get("args") or []),
                                           env=env, cwd=(cfg.get("cwd") or None),
                                           encoding_error_handler="replace")
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            errlog = stack.enter_context(open(self.log_path, "w", encoding="utf-8", errors="replace"))
            read, write = await stack.enter_async_context(stdio_client(params, errlog=errlog))
            return read, write
        url = (cfg.get("url") or "").strip()
        if not re.match(r"^https?://", url):
            raise McpError("MCP_CONFIG", "URL invalide (http:// ou https://)")
        headers = {k: str(v) for k, v in (cfg.get("headers") or {}).items()} or None
        if transport == "sse":
            from mcp.client.sse import sse_client
            read, write = await stack.enter_async_context(sse_client(url, headers=headers))
            return read, write
        from mcp.client.streamable_http import streamablehttp_client
        read, write, _sid = await stack.enter_async_context(streamablehttp_client(url, headers=headers))
        return read, write

    async def _run(self):
        await self._set("connecting")
        try:
            from mcp import ClientSession
            async with AsyncExitStack() as stack:
                read, write = await self._open_transport(stack)
                session = await stack.enter_async_context(ClientSession(read, write))
                await session.initialize()
                tools, cursor = [], None
                for _ in range(50):
                    res = await session.list_tools(cursor) if cursor else await session.list_tools()
                    tools.extend(res.tools)
                    cursor = getattr(res, "nextCursor", None)
                    if not cursor:
                        break
                self.tools = tools
                self.session = session
                await self._set("ready")
                if not self._ready.done():
                    self._ready.set_result(True)
                await self._stop.wait()
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            msg = exc.message if isinstance(exc, McpError) else _flatten_exc(exc)
            log.warning("MCP server %s failed: %s", self.cfg.get("name"), msg)
            await self._set("error", msg)
            if self._ready and not self._ready.done():
                self._ready.set_exception(McpError("MCP_CONNECT", f"Serveur MCP « {self.cfg.get('name')} » : {msg}"))
            return
        finally:
            self.session = None
        self.tools = []
        await self._set("stopped")

    async def stop(self):
        self._stop.set()
        task = self._task
        if task and not task.done():
            try:
                await asyncio.wait_for(asyncio.shield(task), 5)
            except (asyncio.TimeoutError, Exception):
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        if self._ready and not self._ready.done():
            self._ready.cancel()
        self.session = None

    async def call(self, tool: str, args: dict, timeout: float) -> tuple[str, bool]:
        if self.session is None or self.status != "ready":
            raise McpError("MCP_NOT_READY", f"Serveur MCP « {self.cfg.get('name')} » non connecté")
        try:
            res = await self.session.call_tool(tool, args, read_timeout_seconds=timedelta(seconds=timeout))
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            if type(exc).__name__ != "McpError":
                self.status = "error"
                self.error = _flatten_exc(exc)
                self._stop.set()
            raise McpError("MCP_CALL", _flatten_exc(exc))
        return result_to_text(res), bool(getattr(res, "isError", False))


class McpManager:
    def __init__(self, log_dir: Path, on_status: Callable[[dict], Awaitable[None]]):
        self.log_dir = log_dir
        self.on_status = on_status
        self.conns: dict[str, McpConnection] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    def get(self, server_id: str) -> McpConnection | None:
        return self.conns.get(server_id)

    async def ensure(self, cfg: dict, restart: bool = False) -> McpConnection:
        if not mcp_available():
            raise McpError("DEPENDENCY_MISSING", "La bibliothèque Python « mcp » n'est pas installée")
        sid = cfg["_id"]
        async with self._locks.setdefault(sid, asyncio.Lock()):
            conn = self.conns.get(sid)
            if conn and (restart or conn.fingerprint != _fingerprint(cfg) or conn.status in ("error", "stopped")):
                await conn.stop()
                conn = None
            if conn is None:
                conn = McpConnection(cfg, self.log_dir, self.on_status)
                self.conns[sid] = conn
            conn.cfg = cfg
            await conn.start()
            return conn

    async def stop(self, server_id: str):
        conn = self.conns.pop(server_id, None)
        if conn:
            await conn.stop()

    async def stop_all(self):
        conns = list(self.conns.values())
        self.conns.clear()
        await asyncio.gather(*(c.stop() for c in conns), return_exceptions=True)
