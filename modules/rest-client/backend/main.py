import asyncio
import base64
import logging
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, Field, ValidationError

from ledroide_sdk import Module

log = logging.getLogger("ledroide.rest-client")

module = Module(__file__)
router = module.router

METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS")
USER_AGENT = "LedroideDesktop-REST/1.0"
TEXT_TYPES = ("text/", "json", "xml", "javascript", "html", "x-www-form-urlencoded", "yaml", "csv", "graphql")


class KeyValue(BaseModel):
    key: str = ""
    value: str = ""
    enabled: bool = True


class Auth(BaseModel):
    type: Literal["none", "bearer", "basic", "apikey"] = "none"
    token: str = ""
    username: str = ""
    password: str = ""
    key: str = ""
    value: str = ""
    location: Literal["header", "query"] = "header"


class SendPayload(BaseModel):
    method: str = "GET"
    url: str
    headers: list[KeyValue] = []
    body_type: Literal["none", "json", "text", "xml", "urlencoded"] = "none"
    body: str = ""
    form: list[KeyValue] = []
    auth: Auth = Auth()
    timeout: float = Field(30, gt=0, le=600)
    follow_redirects: bool = True
    verify_ssl: bool = True


class RequestError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


# ── HTTP ──────────────────────────────────────────────────────────────────────

class _Redirects(urllib.request.HTTPRedirectHandler):
    def __init__(self, follow: bool):
        self.follow = follow
        self.chain: list[dict] = []

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not self.follow:
            return None
        if urllib.parse.urlsplit(newurl).scheme.lower() not in ("http", "https"):
            raise RequestError("BAD_REDIRECT", f"Redirection refusée vers {newurl}")
        self.chain.append({"status": code, "url": newurl})
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _normalize_url(raw: str) -> str:
    url = raw.strip()
    if "://" not in url:
        url = "http://" + url
    parts = urllib.parse.urlsplit(url)
    if parts.scheme.lower() not in ("http", "https"):
        raise RequestError("BAD_URL", "Seuls les schémas http:// et https:// sont acceptés")
    if not parts.hostname:
        raise RequestError("BAD_URL", "URL invalide : hôte manquant")
    # Spaces and non-ASCII characters are percent-encoded; existing escapes are kept.
    return urllib.parse.quote(url, safe=":/?#[]@!$&'()*+,;=%~")


def _with_query(url: str, extra: list[tuple[str, str]]) -> str:
    if not extra:
        return url
    sep = "&" if urllib.parse.urlsplit(url).query else "?"
    return url + sep + urllib.parse.urlencode(extra)


def _decode_body(raw: bytes, encoding: str) -> bytes:
    enc = encoding.lower().strip()
    try:
        if enc in ("gzip", "x-gzip"):
            return zlib.decompress(raw, 16 + zlib.MAX_WBITS)
        if enc == "deflate":
            try:
                return zlib.decompress(raw)
            except zlib.error:
                return zlib.decompress(raw, -zlib.MAX_WBITS)
    except zlib.error:
        pass
    return raw


def _is_text(content_type: str) -> bool:
    ct = content_type.lower()
    return any(t in ct for t in TEXT_TYPES)


def _charset(content_type: str) -> str:
    for part in content_type.split(";")[1:]:
        k, _, v = part.strip().partition("=")
        if k.lower() == "charset" and v:
            return v.strip('"')
    return "utf-8"


def _build_request(p: SendPayload) -> tuple[urllib.request.Request, dict]:
    method = p.method.upper()
    if method not in METHODS:
        raise RequestError("BAD_METHOD", f"Méthode non prise en charge : {p.method}")
    url = _normalize_url(p.url)

    headers: dict[str, str] = {}
    for h in p.headers:
        if h.enabled and h.key.strip():
            headers[h.key.strip()] = h.value

    def has(name: str) -> bool:
        return any(k.lower() == name.lower() for k in headers)

    query: list[tuple[str, str]] = []
    a = p.auth
    if a.type == "bearer" and a.token:
        headers.setdefault("Authorization", f"Bearer {a.token}")
    elif a.type == "basic":
        cred = base64.b64encode(f"{a.username}:{a.password}".encode()).decode()
        headers.setdefault("Authorization", f"Basic {cred}")
    elif a.type == "apikey" and a.key:
        if a.location == "query":
            query.append((a.key, a.value))
        else:
            headers.setdefault(a.key, a.value)
    url = _with_query(url, query)

    data: bytes | None = None
    if p.body_type == "urlencoded":
        data = urllib.parse.urlencode([(f.key, f.value) for f in p.form if f.enabled and f.key]).encode()
        if not has("Content-Type"):
            headers["Content-Type"] = "application/x-www-form-urlencoded"
    elif p.body_type != "none":
        data = p.body.encode("utf-8")
        default_ct = {"json": "application/json", "xml": "application/xml", "text": "text/plain; charset=utf-8"}
        if not has("Content-Type"):
            headers["Content-Type"] = default_ct[p.body_type]
    if not has("User-Agent"):
        headers["User-Agent"] = USER_AGENT
    if not has("Accept"):
        headers["Accept"] = "*/*"

    req = urllib.request.Request(url, data=data, method=method)
    for k, v in headers.items():
        req.add_header(k, v)
    sent = {"method": method, "url": url, "headers": [[k, v] for k, v in headers.items()],
            "body_size": len(data) if data else 0}
    return req, sent


def _perform(p: SendPayload, max_bytes: int) -> dict:
    req, sent = _build_request(p)
    ctx = ssl.create_default_context()
    if not p.verify_ssl:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    redirects = _Redirects(p.follow_redirects)
    opener = urllib.request.build_opener(urllib.request.HTTPSHandler(context=ctx), redirects)

    started = time.perf_counter()
    try:
        try:
            resp = opener.open(req, timeout=p.timeout)
        except urllib.error.HTTPError as exc:
            resp = exc
        with resp:
            ttfb = time.perf_counter() - started
            raw = resp.read(max_bytes + 1)
            elapsed = time.perf_counter() - started
            status, reason = resp.status, resp.reason
            headers = list(resp.headers.items())
            final_url = resp.geturl()
    except RequestError:
        raise
    except urllib.error.URLError as exc:
        reason = exc.reason
        if isinstance(reason, ssl.SSLCertVerificationError):
            raise RequestError("SSL_ERROR", f"Certificat SSL invalide : {reason.verify_message}. "
                                            "Désactivez la vérification SSL dans Options pour forcer.")
        if isinstance(reason, TimeoutError):
            raise RequestError("TIMEOUT", f"Délai dépassé ({p.timeout:g} s)")
        raise RequestError("NETWORK_ERROR", f"Connexion impossible : {reason}")
    except TimeoutError:
        raise RequestError("TIMEOUT", f"Délai dépassé ({p.timeout:g} s)")
    except (ConnectionError, OSError) as exc:
        raise RequestError("NETWORK_ERROR", f"Erreur réseau : {exc}")
    except ValueError as exc:
        raise RequestError("BAD_REQUEST", f"Requête invalide : {exc}")

    truncated = len(raw) > max_bytes
    raw = raw[:max_bytes]
    wire_size = len(raw)
    content_type = next((v for k, v in headers if k.lower() == "content-type"), "")
    encoding = next((v for k, v in headers if k.lower() == "content-encoding"), "")
    if encoding and not truncated:
        raw = _decode_body(raw, encoding)

    body, body_base64, binary = "", None, False
    if raw:
        if _is_text(content_type) or not content_type:
            try:
                body = raw.decode(_charset(content_type))
            except (UnicodeDecodeError, LookupError):
                try:
                    body = raw.decode("utf-8")
                except UnicodeDecodeError:
                    binary = True
        else:
            binary = True
        if binary:
            body_base64 = base64.b64encode(raw).decode()

    return {
        "status": status,
        "reason": reason or "",
        "elapsed_ms": round(elapsed * 1000, 1),
        "ttfb_ms": round(ttfb * 1000, 1),
        "size": wire_size,
        "headers": [[k, v] for k, v in headers],
        "content_type": content_type,
        "body": body,
        "body_base64": body_base64,
        "binary": binary,
        "truncated": truncated,
        "final_url": final_url,
        "redirects": redirects.chain,
        "request": sent,
    }


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


async def _trim_history():
    keep = int(module.config.get("history_size", 100))
    entries = await module.db.query("history")
    for old in entries[keep:]:
        await module.db.delete("history", old["_id"])


@module.on("send")
async def send(payload: dict):
    try:
        p = SendPayload.model_validate(payload.get("request") or {})
    except ValidationError as exc:
        first = exc.errors()[0]
        return {"error": f"Requête invalide ({'.'.join(map(str, first['loc']))} : {first['msg']})", "code": "BAD_REQUEST"}

    max_bytes = int(module.config.get("max_body_mb", 10)) * 1024 * 1024
    try:
        result = await asyncio.to_thread(_perform, p, max_bytes)
    except RequestError as exc:
        log.info("%s %s -> %s", p.method, urllib.parse.urlsplit(p.url).netloc, exc.code)
        if payload.get("history", True):
            await module.db.save("history", {"request": p.model_dump(), "at": _now(), "error": exc.message})
            await _trim_history()
        return {"error": exc.message, "code": exc.code}

    log.info("%s %s -> %s", p.method, urllib.parse.urlsplit(result["request"]["url"]).netloc, result["status"])
    if payload.get("history", True):
        await module.db.save("history", {
            "request": p.model_dump(), "at": _now(),
            "status": result["status"], "elapsed_ms": result["elapsed_ms"],
        })
        await _trim_history()
    return result


# ── History / saved requests / environments ──────────────────────────────────

@module.on("history_list")
async def history_list(payload: dict):
    return {"items": await module.db.query("history")}


@module.on("history_delete")
async def history_delete(payload: dict):
    return await module.db.delete("history", str(payload.get("id", "")))


@module.on("history_clear")
async def history_clear(payload: dict):
    for item in await module.db.query("history"):
        await module.db.delete("history", item["_id"])
    return {"ok": True}


@module.on("saved_list")
async def saved_list(payload: dict):
    items = await module.db.query("saved")
    items.sort(key=lambda d: ((d.get("folder") or "").lower(), (d.get("name") or "").lower()))
    return {"items": items}


@module.on("saved_save")
async def saved_save(payload: dict):
    name = str(payload.get("name") or "").strip()
    if not name:
        return {"error": "Le nom est obligatoire", "code": "NAME_REQUIRED"}
    try:
        req = SendPayload.model_validate(payload.get("request") or {})
    except ValidationError:
        return {"error": "Requête invalide", "code": "BAD_REQUEST"}
    doc = {"name": name[:120], "folder": str(payload.get("folder") or "").strip()[:80],
           "request": req.model_dump(), "updated": _now()}
    if payload.get("id"):
        doc["_id"] = str(payload["id"])
    return await module.db.save("saved", doc)


@module.on("saved_delete")
async def saved_delete(payload: dict):
    return await module.db.delete("saved", str(payload.get("id", "")))


@module.on("env_get")
async def env_get(payload: dict):
    doc = await module.db.get("settings", "environments")
    return doc or {"_id": "environments", "active": None, "items": []}


@module.on("env_save")
async def env_save(payload: dict):
    items = []
    for env in payload.get("items") or []:
        if not isinstance(env, dict) or not str(env.get("name", "")).strip():
            continue
        items.append({
            "id": str(env.get("id") or "")[:40],
            "name": str(env["name"]).strip()[:60],
            "vars": [
                {"key": str(v.get("key", ""))[:100], "value": str(v.get("value", "")), "enabled": bool(v.get("enabled", True))}
                for v in env.get("vars") or [] if isinstance(v, dict) and str(v.get("key", "")).strip()
            ],
        })
    active = payload.get("active")
    if active not in {e["id"] for e in items}:
        active = None
    return await module.db.save("settings", {"_id": "environments", "active": active, "items": items})
