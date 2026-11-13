import asyncio
import copy
import json
import logging
import random
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections import deque
from datetime import datetime, timezone
from http import HTTPStatus
from typing import Literal, Optional

from pydantic import BaseModel, Field, ValidationError, field_validator

from ledroide_sdk import Module

log = logging.getLogger("ledroide.mock-server")

module = Module(__file__)
router = module.router

EXPORT_FORMAT = "ledroide-mock-server"
METHODS = ("ANY", "GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS")
HOSTS = ("127.0.0.1", "0.0.0.0")
MAX_HEADER = 64 * 1024
MAX_BODY = 10 * 1024 * 1024
LOG_SIZE = 500
LOG_BODY = 64 * 1024
REPLAY_BODY = 1024 * 1024
HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailers",
       "transfer-encoding", "upgrade", "content-length", "host", "accept-encoding"}
TYPES = {"json": "application/json; charset=utf-8", "text": "text/plain; charset=utf-8",
         "html": "text/html; charset=utf-8", "xml": "application/xml; charset=utf-8"}
TPL_RE = re.compile(r"\{\{\s*([^{}]+?)\s*\}\}")
TOKEN_RE = re.compile(r":([A-Za-z_]\w*)|\{([A-Za-z_][\w-]*)\}|\*")
HEADER_NAME_RE = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")


def _err(message: str, code: str = "BAD_REQUEST") -> dict:
    return {"error": message, "code": code}


def _validation_message(exc: ValidationError) -> str:
    first = exc.errors()[0]
    loc = ".".join(str(p) for p in first.get("loc", ()))
    msg = str(first.get("msg", "Données invalides")).removeprefix("Value error, ")
    return f"{loc} : {msg}" if loc else msg


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── Modèles ──────────────────────────────────────────────────────────────────

class HeaderIn(BaseModel):
    k: str = Field(max_length=200)
    v: str = Field("", max_length=8000)

    @field_validator("k")
    @classmethod
    def _name(cls, v: str) -> str:
        v = v.strip()
        if v and not HEADER_NAME_RE.match(v):
            raise ValueError("Nom d'en-tête invalide")
        return v

    @field_validator("v")
    @classmethod
    def _value(cls, v: str) -> str:
        if "\r" in v or "\n" in v:
            raise ValueError("Valeur d'en-tête sur une seule ligne")
        return v


class ResponseIn(BaseModel):
    status: int = Field(200, ge=100, le=599)
    type: Literal["json", "text", "html", "xml"] = "json"
    headers: list[HeaderIn] = Field(default_factory=list, max_length=50)
    body: str = Field("", max_length=2_000_000)
    label: str = Field("", max_length=100)


class RouteIn(BaseModel):
    id: str = ""
    enabled: bool = True
    method: str = "GET"
    path: str = Field(min_length=1, max_length=500)
    description: str = Field("", max_length=300)
    mode: Literal["first", "sequential", "random"] = "first"
    delay: int = Field(0, ge=0, le=60000)
    delay_max: int = Field(0, ge=0, le=60000)
    responses: list[ResponseIn] = Field(default_factory=lambda: [ResponseIn()], min_length=1, max_length=20)

    @field_validator("method")
    @classmethod
    def _method(cls, v: str) -> str:
        v = v.strip().upper()
        if v not in METHODS:
            raise ValueError("Méthode invalide")
        return v

    @field_validator("path")
    @classmethod
    def _path(cls, v: str) -> str:
        v = v.strip()
        if not v.startswith("/"):
            v = "/" + v
        if any(c in v for c in " \t\r\n?#"):
            raise ValueError("Chemin invalide (sans espace, ? ni #)")
        compile_path(v)
        return v


class ServerIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    host: str = "127.0.0.1"
    port: int = Field(3000, ge=1, le=65535)
    cors: bool = True
    proxy: str = Field("", max_length=500)
    routes: list[RouteIn] = Field(default_factory=list, max_length=500)

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("Le nom est requis")
        return v

    @field_validator("host")
    @classmethod
    def _host(cls, v: str) -> str:
        if v not in HOSTS:
            raise ValueError("Adresse d'écoute invalide (127.0.0.1 ou 0.0.0.0)")
        return v

    @field_validator("proxy")
    @classmethod
    def _proxy(cls, v: str) -> str:
        v = v.strip().rstrip("/")
        if v:
            u = urllib.parse.urlsplit(v)
            if u.scheme not in ("http", "https") or not u.netloc:
                raise ValueError("URL de proxy invalide (http:// ou https://)")
        return v


# ── Routage / gabarits ───────────────────────────────────────────────────────

def compile_path(path: str) -> tuple[re.Pattern, list[str]]:
    p = path.rstrip("/") or "/"
    out, names, pos, wild = ["^"], [], 0, 0
    for m in TOKEN_RE.finditer(p):
        out.append(re.escape(p[pos:m.start()]))
        if m.group(0) == "*":
            name = "_wild" if wild == 0 else f"_wild{wild}"
            wild += 1
            out.append(f"(?P<{name}>.*)")
        else:
            name = m.group(1) or m.group(2)
            safe = re.sub(r"\W", "_", name)
            if safe in names:
                raise ValueError(f"Paramètre en double : {name}")
            names.append(safe)
            out.append(f"(?P<{safe}>[^/]+)")
        pos = m.end()
    out.append(re.escape(p[pos:]))
    regex = "".join(out) + ("$" if p == "/" else "/?$")
    try:
        return re.compile(regex), names
    except re.error as exc:
        raise ValueError(f"Chemin invalide : {exc}")


_path_cache: dict[str, re.Pattern] = {}


def match_route(routes: list[dict], method: str, path: str) -> tuple[dict | None, dict]:
    for r in routes:
        if not r.get("enabled", True):
            continue
        rm = r.get("method", "ANY")
        if rm != "ANY" and rm != method and not (method == "HEAD" and rm == "GET"):
            continue
        rx = _path_cache.get(r["path"])
        if rx is None:
            rx = _path_cache[r["path"]] = compile_path(r["path"])[0]
        m = rx.match(path)
        if m:
            params = {}
            for k, v in m.groupdict().items():
                v = urllib.parse.unquote(v or "")
                params["*" if k == "_wild" else k] = v
            return r, params
    return None, {}


def _lookup(obj, parts: list[str]):
    for p in parts:
        if isinstance(obj, dict):
            if p in obj:
                obj = obj[p]
            else:
                low = {str(k).lower(): v for k, v in obj.items()}
                if p.lower() not in low:
                    return None
                obj = low[p.lower()]
        elif isinstance(obj, list) and p.lstrip("-").isdigit():
            try:
                obj = obj[int(p)]
            except IndexError:
                return None
        else:
            return None
    return obj


def render_template(text: str, ctx: dict, json_mode: bool = False) -> str:
    def value(expr: str):
        expr = expr.strip()
        if expr == "now" or expr == "now.iso":
            return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
        if expr == "now.date":
            return datetime.now().date().isoformat()
        if expr == "now.local":
            return datetime.now().isoformat(timespec="seconds")
        if expr == "timestamp":
            return int(time.time())
        if expr == "timestamp.ms":
            return int(time.time() * 1000)
        if expr == "uuid":
            return str(uuid.uuid4())
        m = re.fullmatch(r"randomInt(?:\s*\(\s*(-?\d+)\s*,\s*(-?\d+)\s*\))?", expr)
        if m:
            lo, hi = (int(m.group(1)), int(m.group(2))) if m.group(1) else (0, 1000)
            return random.randint(min(lo, hi), max(lo, hi))
        if expr == "method":
            return ctx.get("method", "")
        if expr == "path":
            return ctx.get("path", "")
        if expr == "body":
            return ctx.get("body_text", "")
        root, _, rest = expr.partition(".")
        if root in ("params", "query", "headers", "body") and rest:
            src = ctx.get("body_json") if root == "body" else ctx.get(root, {})
            if root == "headers":
                return (src or {}).get(rest.lower(), "")
            v = _lookup(src, rest.split("."))
            return "" if v is None else v
        return None

    def sub(m: re.Match) -> str:
        v = value(m.group(1))
        if v is None:
            return m.group(0)
        if isinstance(v, (dict, list)):
            s = json.dumps(v, ensure_ascii=False)
            return s
        if isinstance(v, bool):
            s = "true" if v else "false"
        else:
            s = str(v)
        return json.dumps(s, ensure_ascii=False)[1:-1] if json_mode else s

    return TPL_RE.sub(sub, text)


# ── Serveur HTTP ─────────────────────────────────────────────────────────────

class Runtime:
    def __init__(self, conf: dict):
        self.conf = conf
        self.server: asyncio.base_events.Server | None = None
        self.log: deque = deque(maxlen=LOG_SIZE)
        self.replay: dict[str, dict] = {}
        self.counters: dict[str, int] = {}
        self.conns: set[asyncio.Task] = set()
        self.started = time.time()
        self.hits = 0


_configs: dict[str, dict] = {}
_runtimes: dict[str, Runtime] = {}
_loaded = False
_load_lock = asyncio.Lock()


def _reason(status: int) -> str:
    try:
        return HTTPStatus(status).phrase
    except ValueError:
        return "Unknown"


def _decode_body(body: bytes) -> tuple[str, bool]:
    if not body:
        return "", False
    try:
        return body.decode("utf-8"), False
    except UnicodeDecodeError:
        return f"<binaire, {len(body)} octets>", True


async def _read_request(reader: asyncio.StreamReader):
    head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 60)
    lines = head.decode("latin-1").split("\r\n")
    parts = lines[0].split(" ")
    if len(parts) != 3 or not parts[2].startswith("HTTP/"):
        raise ValueError("bad request line")
    method, target, version = parts
    headers: list[tuple[str, str]] = []
    for line in lines[1:]:
        if not line:
            continue
        k, sep, v = line.partition(":")
        if not sep:
            raise ValueError("bad header")
        headers.append((k.strip(), v.strip()))
    hmap = {k.lower(): v for k, v in headers}
    body = b""
    if "chunked" in hmap.get("transfer-encoding", "").lower():
        chunks = []
        total = 0
        while True:
            size_line = await asyncio.wait_for(reader.readuntil(b"\r\n"), 60)
            size = int(size_line.split(b";")[0].strip() or b"0", 16)
            if size == 0:
                while (await asyncio.wait_for(reader.readuntil(b"\r\n"), 60)) != b"\r\n":
                    pass
                break
            total += size
            if total > MAX_BODY:
                raise OverflowError()
            chunks.append(await asyncio.wait_for(reader.readexactly(size), 60))
            await reader.readexactly(2)
        body = b"".join(chunks)
    elif hmap.get("content-length"):
        length = int(hmap["content-length"])
        if length < 0:
            raise ValueError("bad length")
        if length > MAX_BODY:
            raise OverflowError()
        body = await asyncio.wait_for(reader.readexactly(length), 60)
    return method.upper(), target, version, headers, hmap, body


def _cors_headers(conf: dict, hmap: dict) -> list[tuple[str, str]]:
    if not conf.get("cors"):
        return []
    origin = hmap.get("origin")
    out = [("Access-Control-Allow-Origin", origin or "*")]
    if origin:
        out += [("Access-Control-Allow-Credentials", "true"), ("Vary", "Origin")]
    out.append(("Access-Control-Expose-Headers", "*"))
    return out


def _proxy_sync(base: str, method: str, target: str, headers: list[tuple[str, str]], body: bytes):
    url = base + target
    req = urllib.request.Request(url, data=body if body or method in ("POST", "PUT", "PATCH") else None, method=method)
    for k, v in headers:
        if k.lower() not in HOP and k.lower() != "origin":
            req.add_header(k, v)
    opener = urllib.request.build_opener(urllib.request.HTTPRedirectHandler())
    try:
        with opener.open(req, timeout=30) as resp:
            return resp.status, list(resp.headers.items()), resp.read(MAX_BODY + 1)[:MAX_BODY]
    except urllib.error.HTTPError as exc:
        return exc.code, list(exc.headers.items()), exc.read(MAX_BODY)


async def _process(rt: Runtime, method: str, target: str, headers, hmap: dict, body: bytes, client: str):
    conf = rt.conf
    split = urllib.parse.urlsplit(target)
    path = urllib.parse.unquote(split.path or "/")
    query = {k: v[0] if len(v) == 1 else v for k, v in urllib.parse.parse_qs(split.query, keep_blank_values=True).items()}
    route, params = match_route(conf.get("routes", []), method, split.path or "/")
    resp_headers: list[tuple[str, str]] = []
    proxied = False
    label = ""
    if route is None and method == "OPTIONS" and conf.get("cors") and hmap.get("access-control-request-method"):
        status, rbody = 204, b""
        resp_headers = [("Access-Control-Allow-Methods", "GET, POST, PUT, PATCH, DELETE, HEAD, OPTIONS"),
                        ("Access-Control-Allow-Headers", hmap.get("access-control-request-headers", "*")),
                        ("Access-Control-Max-Age", "86400")]
        label = "Pré-vérification CORS"
    elif route is not None:
        resps = route.get("responses") or [{"status": 200, "type": "text", "body": ""}]
        mode = route.get("mode", "first")
        if mode == "sequential":
            idx = rt.counters.get(route["id"], 0)
            rt.counters[route["id"]] = idx + 1
            resp = resps[idx % len(resps)]
        elif mode == "random":
            resp = random.choice(resps)
        else:
            resp = resps[0]
        delay = int(route.get("delay") or 0)
        dmax = int(route.get("delay_max") or 0)
        if dmax > delay:
            delay = random.randint(delay, dmax)
        if delay:
            await asyncio.sleep(delay / 1000)
        body_text, binary = _decode_body(body)
        body_json = None
        if body_text and not binary:
            try:
                body_json = json.loads(body_text)
            except ValueError:
                body_json = {k: v[0] if len(v) == 1 else v for k, v in urllib.parse.parse_qs(body_text).items()} \
                    if "x-www-form-urlencoded" in hmap.get("content-type", "") else None
        ctx = {"params": params, "query": query, "headers": hmap, "body_json": body_json, "body_text": body_text,
               "method": method, "path": path}
        rtype = resp.get("type", "json")
        status = int(resp.get("status", 200))
        rbody = render_template(resp.get("body", ""), ctx, json_mode=rtype == "json").encode("utf-8")
        has_ct = False
        for h in resp.get("headers", []):
            if h.get("k"):
                resp_headers.append((h["k"], render_template(h.get("v", ""), ctx)))
                has_ct = has_ct or h["k"].lower() == "content-type"
        if not has_ct and (rbody or status not in (204, 304)):
            resp_headers.append(("Content-Type", TYPES.get(rtype, TYPES["text"])))
        label = f"{route.get('method')} {route.get('path')}"
    elif conf.get("proxy"):
        proxied = True
        label = "Proxy → " + conf["proxy"]
        try:
            status, ph, rbody = await asyncio.to_thread(_proxy_sync, conf["proxy"], method, target, headers, body)
            resp_headers = [(k, v) for k, v in ph if k.lower() not in HOP and not k.lower().startswith("access-control-")]
        except Exception as exc:
            status = 502
            rbody = json.dumps({"error": f"Proxy injoignable : {exc}", "code": "PROXY_ERROR"}, ensure_ascii=False).encode()
            resp_headers = [("Content-Type", TYPES["json"])]
    else:
        status = 404
        rbody = json.dumps({"error": "Aucune route simulée ne correspond", "method": method, "path": path},
                           ensure_ascii=False).encode()
        resp_headers = [("Content-Type", TYPES["json"])]
        label = "Aucune route"
    resp_headers = _cors_headers(conf, hmap) + resp_headers
    return status, resp_headers, rbody, route, proxied, label, path, query


async def _handle_conn(rt: Runtime, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
    task = asyncio.current_task()
    rt.conns.add(task)
    peer = writer.get_extra_info("peername")
    client = peer[0] if peer else ""
    try:
        while True:
            try:
                method, target, version, headers, hmap, body = await _read_request(reader)
            except (asyncio.IncompleteReadError, asyncio.TimeoutError, ConnectionError):
                break
            except asyncio.LimitOverrunError:
                await _write_simple(writer, 431, "Request Header Fields Too Large")
                break
            except OverflowError:
                await _write_simple(writer, 413, "Payload Too Large")
                break
            except ValueError:
                await _write_simple(writer, 400, "Bad Request")
                break
            started = time.perf_counter()
            if hmap.get("expect", "").lower() == "100-continue":
                pass
            try:
                status, rh, rbody, route, proxied, label, path, query = await _process(rt, method, target, headers, hmap, body, client)
            except Exception as exc:
                log.exception("mock handler failed")
                status, rh, rbody, route, proxied, label = 500, [("Content-Type", TYPES["json"])], \
                    json.dumps({"error": str(exc)}).encode(), None, False, "Erreur interne"
                path, query = target, {}
            keep = version == "HTTP/1.1" and hmap.get("connection", "").lower() != "close" \
                or version == "HTTP/1.0" and hmap.get("connection", "").lower() == "keep-alive"
            out = [f"HTTP/1.1 {status} {_reason(status)}"]
            names = {k.lower() for k, _ in rh}
            out += [f"{k}: {v}" for k, v in rh]
            if "date" not in names:
                out.append("Date: " + datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S GMT"))
            out.append("Server: Ledroide-Mock")
            out.append(f"Content-Length: {len(rbody)}")
            out.append("Connection: " + ("keep-alive" if keep else "close"))
            writer.write(("\r\n".join(out) + "\r\n\r\n").encode("latin-1", "replace"))
            if method != "HEAD":
                writer.write(rbody)
            await writer.drain()
            duration = round((time.perf_counter() - started) * 1000, 1)
            await _record(rt, method, target, path, query, headers, body, client, status, rh, rbody, route, proxied, label, duration)
            if not keep:
                break
    except (ConnectionError, asyncio.CancelledError):
        pass
    except Exception:
        log.exception("connection error")
    finally:
        rt.conns.discard(task)
        try:
            writer.close()
        except Exception:
            pass


async def _write_simple(writer, status: int, text: str):
    try:
        writer.write(f"HTTP/1.1 {status} {text}\r\nContent-Length: 0\r\nConnection: close\r\n\r\n".encode())
        await writer.drain()
    except Exception:
        pass


async def _record(rt, method, target, path, query, headers, body, client, status, rh, rbody, route, proxied, label, duration):
    rt.hits += 1
    req_text, req_bin = _decode_body(body[:LOG_BODY])
    resp_text, resp_bin = _decode_body(rbody[:LOG_BODY])
    entry = {
        "id": uuid.uuid4().hex[:12], "ts": _now(), "method": method, "target": target, "path": path, "query": query,
        "headers": [[k, v] for k, v in headers], "body": req_text, "body_size": len(body), "body_truncated": len(body) > LOG_BODY,
        "client": client, "status": status, "resp_headers": [[k, v] for k, v in rh], "resp_body": resp_text,
        "resp_size": len(rbody), "route_id": route.get("id") if route else None, "label": label, "proxied": proxied,
        "duration": duration,
    }
    if any(k.lower() == "x-ledroide-replay" for k, _ in headers):
        entry["label"] = "↻ Rejeu · " + label
    rt.log.append(entry)
    if len(body) <= REPLAY_BODY:
        rt.replay[entry["id"]] = {"method": method, "target": target, "headers": headers, "body": body}
    if len(rt.replay) > LOG_SIZE:
        alive = {e["id"] for e in rt.log}
        for k in [k for k in rt.replay if k not in alive]:
            rt.replay.pop(k, None)
    await module.events.emit("mock:request", {"server_id": rt.conf["_id"], "entry": entry})


# ── Persistance ──────────────────────────────────────────────────────────────

async def _ensure_loaded():
    global _loaded
    if _loaded:
        return
    async with _load_lock:
        if not _loaded:
            for doc in await module.db.query("servers"):
                _configs[doc["_id"]] = doc
            _loaded = True


def _normalize(model: ServerIn, sid: str, existing: dict | None) -> dict:
    data = model.model_dump()
    for r in data["routes"]:
        r["id"] = r.get("id") or uuid.uuid4().hex[:10]
    seen = set()
    for r in data["routes"]:
        while r["id"] in seen:
            r["id"] = uuid.uuid4().hex[:10]
        seen.add(r["id"])
    return {"_id": sid, **data, "created_at": (existing or {}).get("created_at") or _now(), "updated_at": _now()}


def _public(conf: dict) -> dict:
    rt = _runtimes.get(conf["_id"])
    return {**{k: v for k, v in conf.items()}, "id": conf["_id"], "running": rt is not None,
            "hits": rt.hits if rt else 0, "started": rt.started if rt else None}


def _check_port_clash(sid: str, port: int) -> str | None:
    for other, rt in _runtimes.items():
        if other != sid and rt.conf.get("port") == port:
            return f"Le port {port} est déjà utilisé par « {rt.conf.get('name')} »"
    return None


async def _start(conf: dict) -> Runtime:
    clash = _check_port_clash(conf["_id"], conf["port"])
    if clash:
        raise RuntimeError(clash)
    rt = Runtime(conf)
    try:
        rt.server = await asyncio.start_server(lambda r, w: _handle_conn(rt, r, w), conf["host"], conf["port"],
                                               limit=MAX_HEADER, reuse_address=False)
    except OSError as exc:
        if exc.errno in (98, 48, 10048, 10013) or "address already in use" in str(exc).lower():
            raise RuntimeError(f"Le port {conf['port']} est déjà utilisé ou interdit sur {conf['host']}")
        raise RuntimeError(f"Démarrage impossible : {exc.strerror or exc}")
    _runtimes[conf["_id"]] = rt
    log.info("mock server %s listening on %s:%s", conf["name"], conf["host"], conf["port"])
    return rt


async def _stop(sid: str) -> Runtime | None:
    rt = _runtimes.pop(sid, None)
    if rt is None:
        return None
    if rt.server:
        rt.server.close()
    for t in list(rt.conns):
        t.cancel()
    if rt.server:
        try:
            await asyncio.wait_for(rt.server.wait_closed(), 3)
        except asyncio.TimeoutError:
            pass
    log.info("mock server %s stopped", rt.conf.get("name"))
    return rt


# ── Handlers ─────────────────────────────────────────────────────────────────

@module.on("servers.list")
async def servers_list(payload: dict):
    await _ensure_loaded()
    servers = sorted((_public(c) for c in _configs.values()), key=lambda c: c.get("created_at", ""))
    return {"servers": servers}


@module.on("server.save")
async def server_save(payload: dict):
    await _ensure_loaded()
    try:
        model = ServerIn(**(payload.get("server") or {}))
    except ValidationError as exc:
        return _err(_validation_message(exc), "VALIDATION")
    sid = str(payload.get("id") or "") or uuid.uuid4().hex[:12]
    existing = _configs.get(sid)
    if payload.get("id") and existing is None:
        return _err("Serveur introuvable", "NOT_FOUND")
    conf = _normalize(model, sid, existing)
    rt = _runtimes.get(sid)
    if rt and (rt.conf["host"], rt.conf["port"]) != (conf["host"], conf["port"]):
        clash = _check_port_clash(sid, conf["port"])
        if clash:
            return _err(clash, "PORT_IN_USE")
        old_log, old_replay = rt.log, rt.replay
        await _stop(sid)
        try:
            new_rt = await _start(conf)
            new_rt.log, new_rt.replay = old_log, old_replay
        except RuntimeError as exc:
            await module.db.save("servers", conf)
            _configs[sid] = conf
            await module.events.emit("mock:status", {"server_id": sid, "running": False})
            return _err(str(exc), "PORT_IN_USE")
    elif rt:
        rt.conf = conf
        rt.counters = {k: v for k, v in rt.counters.items() if any(r["id"] == k for r in conf["routes"])}
    await module.db.save("servers", conf)
    _configs[sid] = conf
    return _public(conf)


@module.on("server.delete")
async def server_delete(payload: dict):
    await _ensure_loaded()
    sid = str(payload.get("id") or "")
    if sid not in _configs:
        return _err("Serveur introuvable", "NOT_FOUND")
    await _stop(sid)
    await module.db.delete("servers", sid)
    _configs.pop(sid, None)
    return {"ok": True}


@module.on("server.start")
async def server_start(payload: dict):
    await _ensure_loaded()
    sid = str(payload.get("id") or "")
    conf = _configs.get(sid)
    if conf is None:
        return _err("Serveur introuvable", "NOT_FOUND")
    if sid in _runtimes:
        return _public(conf)
    try:
        await _start(conf)
    except RuntimeError as exc:
        return _err(str(exc), "PORT_IN_USE")
    await module.events.emit("mock:status", {"server_id": sid, "running": True})
    return _public(conf)


@module.on("server.stop")
async def server_stop(payload: dict):
    await _ensure_loaded()
    sid = str(payload.get("id") or "")
    if sid not in _configs:
        return _err("Serveur introuvable", "NOT_FOUND")
    await _stop(sid)
    await module.events.emit("mock:status", {"server_id": sid, "running": False})
    return _public(_configs[sid])


@module.on("log.list")
async def log_list(payload: dict):
    rt = _runtimes.get(str(payload.get("id") or ""))
    return {"entries": list(rt.log) if rt else []}


@module.on("log.clear")
async def log_clear(payload: dict):
    rt = _runtimes.get(str(payload.get("id") or ""))
    if rt:
        rt.log.clear()
        rt.replay.clear()
    return {"ok": True}


def _replay_sync(port: int, req: dict):
    url = f"http://127.0.0.1:{port}{req['target']}"
    r = urllib.request.Request(url, data=req["body"] or None, method=req["method"])
    for k, v in req["headers"]:
        if k.lower() not in HOP:
            r.add_header(k, v)
    r.add_header("X-Ledroide-Replay", "1")
    try:
        with urllib.request.urlopen(r, timeout=70) as resp:
            return resp.status
    except urllib.error.HTTPError as exc:
        return exc.code


@module.on("log.replay")
async def log_replay(payload: dict):
    rt = _runtimes.get(str(payload.get("id") or ""))
    if rt is None:
        return _err("Le serveur n'est pas démarré", "NOT_RUNNING")
    req = rt.replay.get(str(payload.get("entry_id") or ""))
    if req is None:
        return _err("Requête introuvable (journal vidé ou corps trop volumineux)", "NOT_FOUND")
    try:
        status = await asyncio.to_thread(_replay_sync, rt.conf["port"], req)
    except Exception as exc:
        return _err(f"Rejeu impossible : {exc}", "REPLAY_FAILED")
    return {"status": status}


@module.on("config.export")
async def config_export(payload: dict):
    await _ensure_loaded()
    ids = payload.get("ids")
    servers = [c for c in _configs.values() if not ids or c["_id"] in ids]
    clean = [{k: v for k, v in c.items() if k not in ("_id", "created_at", "updated_at")} for c in servers]
    return {"format": EXPORT_FORMAT, "version": 1, "exported_at": _now(), "servers": clean}


def _parse_text(text: str):
    text = text.strip()
    if not text:
        raise ValueError("Contenu vide")
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        json_error = exc
    try:
        import yaml
    except ImportError:
        raise ValueError(f"JSON invalide : {json_error}")
    try:
        return yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ValueError(f"Ni JSON ni YAML valide : {str(exc).splitlines()[0]}")


def _resolve(spec: dict, node, depth: int = 0):
    while isinstance(node, dict) and "$ref" in node and depth < 20:
        ref = node["$ref"]
        if not isinstance(ref, str) or not ref.startswith("#/"):
            return {}
        cur = spec
        for part in ref[2:].split("/"):
            part = part.replace("~1", "/").replace("~0", "~")
            cur = cur.get(part) if isinstance(cur, dict) else None
            if cur is None:
                return {}
        node = cur
        depth += 1
    return node if isinstance(node, dict) else {}


def sample_from_schema(spec: dict, schema, depth: int = 0):
    schema = _resolve(spec, schema)
    if depth > 6 or not schema:
        return None
    for key in ("example", "default"):
        if key in schema:
            return schema[key]
    if isinstance(schema.get("examples"), list) and schema["examples"]:
        return schema["examples"][0]
    if schema.get("enum"):
        return schema["enum"][0]
    if "allOf" in schema:
        out = {}
        for sub in schema["allOf"]:
            v = sample_from_schema(spec, sub, depth + 1)
            if isinstance(v, dict):
                out.update(v)
        return out
    for key in ("oneOf", "anyOf"):
        if schema.get(key):
            return sample_from_schema(spec, schema[key][0], depth + 1)
    t = schema.get("type")
    if isinstance(t, list):
        t = next((x for x in t if x != "null"), "string")
    if t == "object" or (t is None and "properties" in schema):
        return {k: sample_from_schema(spec, v, depth + 1) for k, v in (schema.get("properties") or {}).items()}
    if t == "array":
        item = sample_from_schema(spec, schema.get("items") or {}, depth + 1)
        return [item] if item is not None else []
    if t == "integer":
        return schema.get("minimum", 1)
    if t == "number":
        return schema.get("minimum", 1.5)
    if t == "boolean":
        return True
    if t == "string":
        fmt = schema.get("format", "")
        return {"date-time": "{{now}}", "date": "{{now.date}}", "uuid": "{{uuid}}", "email": "utilisateur@exemple.fr",
                "uri": "https://exemple.fr", "url": "https://exemple.fr", "ipv4": "192.0.2.1"}.get(fmt, "texte")
    return None


def openapi_to_server(spec: dict, name: str | None = None) -> dict:
    if not isinstance(spec.get("paths"), dict):
        raise ValueError("Document OpenAPI sans « paths »")
    prefix = ""
    if spec.get("swagger"):
        prefix = str(spec.get("basePath") or "").rstrip("/")
    elif spec.get("servers"):
        url = str((spec["servers"][0] or {}).get("url") or "")
        prefix = urllib.parse.urlsplit(url).path.rstrip("/") if "{" not in url else ""
    routes = []
    for path, item in spec["paths"].items():
        if not isinstance(item, dict):
            continue
        for method, op in item.items():
            if method.upper() not in METHODS or not isinstance(op, dict):
                continue
            responses = op.get("responses") or {}
            codes = sorted(responses.keys(), key=lambda c: (not str(c).startswith("2"), str(c) == "default", str(c)))
            out = []
            for code in codes[:4]:
                resp = _resolve(spec, responses[code])
                status = int(code) if str(code).isdigit() else 200
                body, rtype = "", "json"
                content = resp.get("content") or {}
                media = content.get("application/json") or next(iter(content.values()), None) if content else None
                example = None
                if isinstance(media, dict):
                    if "example" in media:
                        example = media["example"]
                    elif isinstance(media.get("examples"), dict) and media["examples"]:
                        example = _resolve(spec, next(iter(media["examples"].values()))).get("value")
                    elif media.get("schema"):
                        example = sample_from_schema(spec, media["schema"])
                elif spec.get("swagger"):
                    ex = resp.get("examples") or {}
                    example = ex.get("application/json") if ex else sample_from_schema(spec, resp.get("schema")) if resp.get("schema") else None
                if example is not None:
                    body = example if isinstance(example, str) else json.dumps(example, ensure_ascii=False, indent=2)
                    if isinstance(example, str) and not example.strip().startswith(("{", "[")):
                        rtype = "text"
                out.append({"status": status, "type": rtype, "headers": [], "body": body,
                            "label": str(resp.get("description") or "")[:100]})
            if not out:
                out = [{"status": 200, "type": "json", "headers": [], "body": "{}", "label": ""}]
            mock_path = re.sub(r"\{([^}/]+)\}", lambda m: ":" + re.sub(r"\W", "_", m.group(1)), prefix + path)
            routes.append({"method": method.upper(), "path": mock_path or "/",
                           "description": str(op.get("summary") or op.get("operationId") or "")[:300],
                           "mode": "first", "delay": 0, "delay_max": 0, "enabled": True, "responses": out})
    title = name or str((spec.get("info") or {}).get("title") or "API importée")
    return {"name": title[:100], "host": "127.0.0.1", "port": 3000, "cors": True, "proxy": "", "routes": routes[:500]}


def _free_port(start: int) -> int:
    used = {c.get("port") for c in _configs.values()}
    port = start
    while port in used and port < 65535:
        port += 1
    return port


@module.on("config.import")
async def config_import(payload: dict):
    await _ensure_loaded()
    data = payload.get("data")
    try:
        if isinstance(payload.get("text"), str):
            data = _parse_text(payload["text"])
        if not isinstance(data, dict):
            raise ValueError("Format non reconnu")
        if data.get("openapi") or data.get("swagger"):
            servers = [openapi_to_server(data)]
            kind = "openapi"
        elif data.get("format") == EXPORT_FORMAT:
            servers = data.get("servers") or []
            kind = "export"
        elif "routes" in data and "name" in data:
            servers = [data]
            kind = "server"
        else:
            raise ValueError("Format non reconnu (export de ce module ou OpenAPI 2/3)")
    except ValueError as exc:
        return _err(str(exc), "BAD_IMPORT")
    created = []
    for raw in servers[:50]:
        if not isinstance(raw, dict):
            continue
        raw = copy.deepcopy(raw)
        raw.pop("_id", None)
        for r in raw.get("routes") or []:
            if isinstance(r, dict):
                r.pop("id", None)
        try:
            model = ServerIn(**{**raw, "port": _free_port(int(raw.get("port") or 3000))})
        except (ValidationError, TypeError, ValueError) as exc:
            msg = _validation_message(exc) if isinstance(exc, ValidationError) else str(exc)
            return _err(f"« {raw.get('name', '?')} » : {msg}", "VALIDATION")
        conf = _normalize(model, uuid.uuid4().hex[:12], None)
        await module.db.save("servers", conf)
        _configs[conf["_id"]] = conf
        created.append(_public(conf))
    return {"kind": kind, "servers": created}


@module.on("server.duplicate")
async def server_duplicate(payload: dict):
    await _ensure_loaded()
    src = _configs.get(str(payload.get("id") or ""))
    if src is None:
        return _err("Serveur introuvable", "NOT_FOUND")
    raw = {k: copy.deepcopy(v) for k, v in src.items() if k not in ("_id", "created_at", "updated_at")}
    raw["name"] = (raw["name"] + " (copie)")[:100]
    raw["port"] = _free_port(raw["port"] + 1)
    for r in raw["routes"]:
        r["id"] = ""
    model = ServerIn(**raw)
    conf = _normalize(model, uuid.uuid4().hex[:12], None)
    await module.db.save("servers", conf)
    _configs[conf["_id"]] = conf
    return _public(conf)


@module.on("template.preview")
async def template_preview(payload: dict):
    text = str(payload.get("body") or "")[:200_000]
    route_path = str(payload.get("path") or "/")
    sample = str(payload.get("sample") or route_path)
    params = {}
    try:
        rx, _ = compile_path(route_path)
        m = rx.match(urllib.parse.urlsplit(sample).path)
        if m:
            params = {("*" if k == "_wild" else k): v for k, v in m.groupdict().items()}
    except ValueError:
        pass
    query = {k: v[0] for k, v in urllib.parse.parse_qs(urllib.parse.urlsplit(sample).query).items()}
    ctx = {"params": params, "query": query, "headers": {}, "body_json": None, "body_text": "", "method": "GET", "path": sample}
    out = render_template(text, ctx, json_mode=payload.get("type") == "json")
    valid = None
    if payload.get("type") == "json" and out.strip():
        try:
            json.loads(out)
            valid = True
        except ValueError as exc:
            valid = False
            return {"body": out, "valid": valid, "message": str(exc)}
    return {"body": out, "valid": valid}


@module.on_shutdown
async def cleanup():
    for sid in list(_runtimes):
        await _stop(sid)
