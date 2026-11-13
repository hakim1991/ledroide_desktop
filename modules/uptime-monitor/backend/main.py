import asyncio
import base64
import json
import logging
import os
import re
import socket
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.request
from typing import Literal, Optional
from urllib.parse import urlsplit

from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator

from ledroide_sdk import Module

log = logging.getLogger("ledroide.uptime-monitor")

module = Module(__file__)
router = module.router

EXPORT_FORMAT = "ledroide-uptime-monitor"
HOST_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9.\-_]{0,252}$|^\[?[0-9A-Fa-f:.]+\]?$")
STATUS_RE = re.compile(r"^\s*\d{3}(\s*-\s*\d{3})?(\s*,\s*\d{3}(\s*-\s*\d{3})?)*\s*$")
MAX_BODY = 1024 * 1024
MAX_PARALLEL = 16
HISTORY_POINTS = 120
DEFAULT_SETTINGS = {"retention_days": 30, "notify_windows": False, "notify_recovery": True}

_ready = False
_lock = asyncio.Lock()
_loop_task: Optional[asyncio.Task] = None
_wake: Optional[asyncio.Event] = None
_running: dict[int, asyncio.Task] = {}
_next_due: dict[int, float] = {}
_live: dict[int, dict] = {}
_monitors: dict[int, dict] = {}
_settings: dict = dict(DEFAULT_SETTINGS)
_sem: Optional[asyncio.Semaphore] = None
_last_purge = 0.0

SCHEMA = """
CREATE TABLE IF NOT EXISTS monitors (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT    NOT NULL,
    type       TEXT    NOT NULL,
    config     TEXT    NOT NULL,
    paused     INTEGER NOT NULL DEFAULT 0,
    position   INTEGER NOT NULL DEFAULT 0,
    created_at REAL    NOT NULL
);
CREATE TABLE IF NOT EXISTS checks (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    monitor_id INTEGER NOT NULL,
    ts         REAL    NOT NULL,
    ok         INTEGER NOT NULL,
    ms         REAL,
    message    TEXT    NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_checks_monitor_ts ON checks(monitor_id, ts);
CREATE INDEX IF NOT EXISTS idx_checks_ts ON checks(ts);
CREATE TABLE IF NOT EXISTS incidents (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    monitor_id INTEGER NOT NULL,
    started    REAL    NOT NULL,
    ended      REAL,
    cause      TEXT    NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_incidents_monitor ON incidents(monitor_id, started);
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


async def _db():
    global _ready
    if not _ready:
        async with _lock:
            if not _ready:
                await module.db.executescript(SCHEMA)
                await _load_all()
                _ready = True
    return module.db


def _err(message: str, code: str = "BAD_REQUEST") -> dict:
    return {"error": message, "code": code}


def _validation_message(exc: ValidationError) -> str:
    first = exc.errors()[0]
    loc = ".".join(str(p) for p in first.get("loc", ()))
    msg = str(first.get("msg", "Données invalides")).removeprefix("Value error, ")
    return f"{loc} : {msg}" if loc else msg


# ── Modèle ────────────────────────────────────────────────────────────────────

class MonitorIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    type: Literal["http", "tcp", "ping", "dns", "tls"]
    target: str = Field(min_length=1, max_length=500)
    port: Optional[int] = Field(None, ge=1, le=65535)
    interval: int = Field(60, ge=10, le=86400)
    timeout: float = Field(10, ge=1, le=60)
    method: Literal["GET", "HEAD", "POST", "PUT", "OPTIONS"] = "GET"
    expected_status: str = Field("200-399", max_length=100)
    keyword: str = Field("", max_length=200)
    keyword_mode: Literal["present", "absent"] = "present"
    max_ms: int = Field(0, ge=0, le=60000)
    verify_tls: bool = True
    follow_redirects: bool = True
    dns_type: Literal["A", "AAAA", "ANY"] = "A"
    dns_expected: str = Field("", max_length=200)
    warn_days: int = Field(14, ge=0, le=365)
    fail_threshold: int = Field(1, ge=1, le=10)
    paused: bool = False

    @field_validator("name", "target", "keyword", "dns_expected", "expected_status")
    @classmethod
    def _strip(cls, v: str) -> str:
        return v.strip()

    @model_validator(mode="after")
    def _check(self):
        if self.type == "http":
            p = urlsplit(self.target)
            if p.scheme not in ("http", "https") or not p.hostname:
                raise ValueError("URL http:// ou https:// attendue")
            if not STATUS_RE.match(self.expected_status or ""):
                raise ValueError("Statuts attendus invalides (ex. 200-399 ou 200,301)")
        else:
            host, port = _split_host(self.target)
            if not host or not HOST_RE.match(host) or host.startswith("-"):
                raise ValueError("Nom d'hôte ou adresse IP invalide")
            if port is not None:
                self.port = port
            self.target = host
            if self.type == "tcp" and not self.port:
                raise ValueError("Port requis pour une sonde TCP (hôte:port)")
            if self.type == "tls" and not self.port:
                self.port = 443
        return self


def _split_host(target: str) -> tuple[str, Optional[int]]:
    t = target.strip()
    if "://" in t:
        p = urlsplit(t)
        return p.hostname or "", p.port
    if t.startswith("["):
        m = re.match(r"^\[([^\]]+)\](?::(\d{1,5}))?$", t)
        return (m.group(1), int(m.group(2)) if m.group(2) else None) if m else ("", None)
    if t.count(":") == 1:
        h, p = t.split(":")
        if p.isdigit() and 0 < int(p) < 65536:
            return h, int(p)
        return "", None
    return t, None


def _row_to_monitor(row: dict) -> dict:
    cfg = json.loads(row["config"])
    cfg.update({"id": row["id"], "name": row["name"], "type": row["type"], "paused": bool(row["paused"]),
                "position": row["position"]})
    return cfg


# ── Sondes ────────────────────────────────────────────────────────────────────

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _status_ok(code: int, spec: str) -> bool:
    for part in spec.split(","):
        part = part.strip()
        if "-" in part:
            lo, hi = (int(x) for x in part.split("-"))
            if lo <= code <= hi:
                return True
        elif part and int(part) == code:
            return True
    return False


def _http_probe(m: dict) -> tuple[bool, Optional[float], str]:
    ctx = ssl.create_default_context()
    if not m.get("verify_tls", True):
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    handlers: list = [urllib.request.HTTPSHandler(context=ctx)]
    if not m.get("follow_redirects", True):
        handlers.append(_NoRedirect())
    opener = urllib.request.build_opener(*handlers)
    req = urllib.request.Request(m["target"], method=m.get("method", "GET"),
                                 headers={"User-Agent": "LedroideDesktop-Supervision/1.0", "Accept": "*/*"})
    t0 = time.perf_counter()
    body = b""
    want_body = bool(m.get("keyword")) and m.get("method") != "HEAD"
    try:
        with opener.open(req, timeout=m.get("timeout", 10)) as resp:
            code = resp.status
            if want_body:
                body = resp.read(MAX_BODY)
    except urllib.error.HTTPError as exc:
        code = exc.code
        if want_body:
            try:
                body = exc.read(MAX_BODY)
            except Exception:
                body = b""
    ms = (time.perf_counter() - t0) * 1000
    if not _status_ok(code, m.get("expected_status") or "200-399"):
        return False, ms, f"HTTP {code} (attendu {m.get('expected_status')})"
    if want_body:
        text = body.decode("utf-8", errors="replace")
        found = m["keyword"] in text
        if m.get("keyword_mode") == "absent" and found:
            return False, ms, f"HTTP {code} — mot-clé « {m['keyword']} » présent"
        if m.get("keyword_mode") != "absent" and not found:
            return False, ms, f"HTTP {code} — mot-clé « {m['keyword']} » introuvable"
    return True, ms, f"HTTP {code}"


async def _tcp_probe(host: str, port: int, timeout: float) -> tuple[bool, Optional[float], str]:
    t0 = time.perf_counter()
    try:
        _r, w = await asyncio.wait_for(asyncio.open_connection(host, port), timeout)
    except asyncio.TimeoutError:
        return False, None, f"Port {port} : délai dépassé"
    except OSError as exc:
        return False, None, f"Port {port} : {_oserr(exc)}"
    ms = (time.perf_counter() - t0) * 1000
    w.close()
    try:
        await asyncio.wait_for(w.wait_closed(), 2)
    except Exception:
        pass
    return True, ms, f"Port {port} ouvert"


def _oserr(exc: OSError) -> str:
    if isinstance(exc, socket.gaierror):
        return "nom d'hôte introuvable"
    if isinstance(exc, ConnectionRefusedError):
        return "connexion refusée"
    return exc.strerror or type(exc).__name__


PING_TIME_RE = re.compile(r"[=<]\s*(\d+(?:[.,]\d+)?)\s*ms", re.I)


def _ping_cmd(host: str, timeout: float) -> list[str]:
    if sys.platform == "win32":
        return ["ping", "-n", "1", "-w", str(int(timeout * 1000)), host]
    return ["ping", "-c", "1", "-W", str(max(1, int(timeout))), host]


def _run_ping(host: str, timeout: float) -> tuple[int, str]:
    kwargs = {"creationflags": subprocess.CREATE_NO_WINDOW} if sys.platform == "win32" else {}
    res = subprocess.run(_ping_cmd(host, timeout), capture_output=True, timeout=timeout + 5,
                         stdin=subprocess.DEVNULL, **kwargs)
    out = res.stdout.decode("utf-8", errors="replace") + res.stderr.decode("utf-8", errors="replace")
    return res.returncode, out


async def _ping_probe(host: str, timeout: float, port: Optional[int]) -> tuple[bool, Optional[float], str]:
    if host.startswith("-"):
        return False, None, "Hôte invalide"
    try:
        code, out = await asyncio.to_thread(_run_ping, host, timeout)
    except (FileNotFoundError, PermissionError):
        ok, ms, msg = await _tcp_probe(host, port or 443, timeout)
        return ok, ms, f"ICMP indisponible — TCP {msg}"
    except subprocess.TimeoutExpired:
        return False, None, "Ping : délai dépassé"
    m = PING_TIME_RE.search(out)
    if code == 0 and m and "ttl" in out.lower():
        return True, float(m.group(1).replace(",", ".")), "Réponse ICMP"
    return False, None, "Pas de réponse au ping"


async def _dns_probe(m: dict) -> tuple[bool, Optional[float], str]:
    family = {"A": socket.AF_INET, "AAAA": socket.AF_INET6}.get(m.get("dns_type", "A"), socket.AF_UNSPEC)
    t0 = time.perf_counter()
    try:
        infos = await asyncio.wait_for(asyncio.get_running_loop().getaddrinfo(m["target"], None, family=family, type=socket.SOCK_STREAM),
                                       m.get("timeout", 10))
    except asyncio.TimeoutError:
        return False, None, "Résolution DNS : délai dépassé"
    except socket.gaierror:
        return False, None, f"Aucun enregistrement {m.get('dns_type', 'A')}"
    ms = (time.perf_counter() - t0) * 1000
    addrs = list(dict.fromkeys(i[4][0] for i in infos))
    exp = (m.get("dns_expected") or "").strip()
    if exp and exp not in addrs:
        return False, ms, f"{', '.join(addrs[:4])} (attendu {exp})"
    return True, ms, ", ".join(addrs[:4]) + ("…" if len(addrs) > 4 else "")


async def _tls_probe(m: dict) -> tuple[bool, Optional[float], str, Optional[float]]:
    host, port = m["target"], m.get("port") or 443
    ctx = ssl.create_default_context()
    t0 = time.perf_counter()
    try:
        _r, w = await asyncio.wait_for(asyncio.open_connection(host, port, ssl=ctx, server_hostname=host), m.get("timeout", 10))
    except ssl.SSLCertVerificationError as exc:
        return False, None, f"Certificat invalide : {exc.verify_message or exc.reason}", None
    except asyncio.TimeoutError:
        return False, None, "Connexion TLS : délai dépassé", None
    except (OSError, ssl.SSLError) as exc:
        return False, None, f"Connexion TLS impossible : {_oserr(exc) if isinstance(exc, OSError) else exc}", None
    ms = (time.perf_counter() - t0) * 1000
    cert = w.get_extra_info("peercert") or {}
    w.close()
    try:
        await asyncio.wait_for(w.wait_closed(), 2)
    except Exception:
        pass
    not_after = cert.get("notAfter")
    if not not_after:
        return False, ms, "Certificat illisible", None
    expires = ssl.cert_time_to_seconds(not_after)
    days = (expires - time.time()) / 86400
    issuer = dict(x[0] for x in cert.get("issuer", ()) if x).get("organizationName", "")
    expiry = time.strftime("%d/%m/%Y", time.localtime(expires))
    if days < 0:
        return False, ms, f"Certificat expiré le {expiry}", expires
    if days < m.get("warn_days", 14):
        return False, ms, f"Certificat expire dans {int(days)} j ({expiry})", expires
    return True, ms, f"Valide jusqu'au {expiry} ({int(days)} j)" + (f" — {issuer}" if issuer else ""), expires


async def probe(m: dict) -> dict:
    t = m["type"]
    extra: dict = {}
    try:
        if t == "http":
            ok, ms, msg = await asyncio.to_thread(_http_probe, m)
        elif t == "tcp":
            ok, ms, msg = await _tcp_probe(m["target"], m["port"], m.get("timeout", 10))
        elif t == "ping":
            ok, ms, msg = await _ping_probe(m["target"], m.get("timeout", 10), m.get("port"))
        elif t == "dns":
            ok, ms, msg = await _dns_probe(m)
        else:
            ok, ms, msg, exp = await _tls_probe(m)
            extra["cert_expires"] = exp
    except urllib.error.URLError as exc:
        reason = exc.reason
        if isinstance(reason, ssl.SSLCertVerificationError):
            msg = f"Certificat invalide : {reason.verify_message or reason.reason}"
        elif isinstance(reason, (socket.timeout, TimeoutError)):
            msg = "Délai dépassé"
        elif isinstance(reason, OSError):
            msg = _oserr(reason)
        else:
            msg = str(reason)
        ok, ms = False, None
    except (TimeoutError, socket.timeout):
        ok, ms, msg = False, None, "Délai dépassé"
    except Exception as exc:
        log.debug("probe %s failed", t, exc_info=True)
        ok, ms, msg = False, None, f"{type(exc).__name__} : {exc}"[:300]
    if ok and m.get("max_ms") and ms is not None and ms > m["max_ms"]:
        ok, msg = False, f"Trop lent : {ms:.0f} ms (max {m['max_ms']} ms) — {msg}"
    return {"ok": ok, "ms": round(ms, 1) if ms is not None else None, "message": msg[:500], "ts": time.time(), **extra}


# ── Notifications Windows ─────────────────────────────────────────────────────

TOAST_PS = r"""
$ErrorActionPreference = 'Stop'
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] > $null
[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] > $null
$t = [Security.SecurityElement]::Escape($env:LDR_TOAST_TITLE)
$m = [Security.SecurityElement]::Escape($env:LDR_TOAST_TEXT)
$x = New-Object Windows.Data.Xml.Dom.XmlDocument
$x.LoadXml("<toast><visual><binding template='ToastGeneric'><text>$t</text><text>$m</text></binding></visual></toast>")
$n = [Windows.UI.Notifications.ToastNotification]::new($x)
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe').Show($n)
"""


def _toast_sync(title: str, text: str) -> tuple[bool, str]:
    if sys.platform != "win32":
        return False, "Notifications Windows indisponibles sur ce système"
    env = {**os.environ, "LDR_TOAST_TITLE": title[:120], "LDR_TOAST_TEXT": text[:300]}
    enc = base64.b64encode(TOAST_PS.encode("utf-16-le")).decode("ascii")
    try:
        res = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-EncodedCommand", enc],
                             capture_output=True, timeout=20, env=env, stdin=subprocess.DEVNULL,
                             creationflags=subprocess.CREATE_NO_WINDOW)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"PowerShell indisponible : {exc}"
    if res.returncode != 0:
        return False, res.stderr.decode("utf-8", errors="replace").strip()[:300] or "Échec de la notification"
    return True, ""


async def _notify(title: str, text: str):
    ok, msg = await asyncio.to_thread(_toast_sync, title, text)
    if not ok:
        log.info("toast failed: %s", msg[:120])
    return ok, msg


# ── État et planification ─────────────────────────────────────────────────────

async def _load_settings():
    rows = await module.db.fetch_all("SELECT key, value FROM settings")
    _settings.clear()
    _settings.update(DEFAULT_SETTINGS)
    for r in rows:
        try:
            _settings[r["key"]] = json.loads(r["value"])
        except ValueError:
            pass


async def _load_all():
    db = module.db
    await _load_settings()
    rows = await db.fetch_all("SELECT * FROM monitors ORDER BY position, id")
    _monitors.clear()
    now = time.time()
    for i, r in enumerate(rows):
        m = _row_to_monitor(r)
        _monitors[m["id"]] = m
        last = await db.fetch_one("SELECT ts, ok, ms, message FROM checks WHERE monitor_id=? ORDER BY ts DESC LIMIT 1", (m["id"],))
        inc = await db.fetch_one("SELECT id, started FROM incidents WHERE monitor_id=? AND ended IS NULL ORDER BY started DESC LIMIT 1", (m["id"],))
        _live[m["id"]] = {
            "status": "paused" if m["paused"] else ("down" if inc else ("up" if last and last["ok"] else ("down" if last else "pending"))),
            "last_ts": last["ts"] if last else None, "last_ms": last["ms"] if last else None,
            "message": last["message"] if last else "En attente de la première vérification",
            "fails": 0, "incident_id": inc["id"] if inc else None, "down_since": inc["started"] if inc else None,
        }
        due = (last["ts"] + m["interval"]) if last else now
        _next_due[m["id"]] = max(now + min(i * 0.3, 5), due)


def _ensure_started():
    global _loop_task, _sem
    if _loop_task is not None and not _loop_task.done():
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    _sem = asyncio.Semaphore(MAX_PARALLEL)
    _loop_task = loop.create_task(_scheduler())


def _kick():
    if _wake is not None:
        _wake.set()


async def _scheduler():
    global _wake, _last_purge
    _wake = asyncio.Event()
    try:
        await _db()
    except Exception:
        log.exception("uptime: init failed")
        return
    log.info("uptime scheduler started (%d monitors)", len(_monitors))
    while True:
        now = time.time()
        for mid, m in list(_monitors.items()):
            if m["paused"] or mid in _running:
                continue
            if _next_due.get(mid, now) <= now:
                _next_due[mid] = now + m["interval"]
                task = asyncio.create_task(_run_check(mid))
                _running[mid] = task
                task.add_done_callback(lambda _t, _id=mid: _running.pop(_id, None))
        if now - _last_purge > 3600:
            _last_purge = now
            asyncio.create_task(_purge())
        pending = [d for mid, d in _next_due.items() if mid in _monitors and not _monitors[mid]["paused"]]
        delay = max(0.2, min(5.0, (min(pending) - time.time()) if pending else 5.0))
        _wake.clear()
        try:
            await asyncio.wait_for(_wake.wait(), delay)
        except asyncio.TimeoutError:
            pass


async def _purge():
    try:
        days = int(_settings.get("retention_days") or 30)
        cutoff = time.time() - days * 86400
        n = await module.db.execute("DELETE FROM checks WHERE ts < ?", (cutoff,))
        await module.db.execute("DELETE FROM incidents WHERE ended IS NOT NULL AND ended < ?", (cutoff,))
        if n:
            log.info("uptime: purged %d checks", n)
    except Exception:
        log.exception("uptime purge failed")


def _fmt_duration(s: float) -> str:
    s = int(s)
    if s < 60:
        return f"{s} s"
    if s < 3600:
        return f"{s // 60} min {s % 60:02d} s"
    if s < 86400:
        return f"{s // 3600} h {(s % 3600) // 60:02d} min"
    return f"{s // 86400} j {(s % 86400) // 3600} h"


async def _run_check(mid: int) -> Optional[dict]:
    m = _monitors.get(mid)
    if m is None:
        return None
    async with (_sem or asyncio.Semaphore(1)):
        res = await probe(m)
    if mid not in _monitors:
        return res
    db = module.db
    await db.execute("INSERT INTO checks (monitor_id, ts, ok, ms, message) VALUES (?,?,?,?,?)",
                     (mid, res["ts"], int(res["ok"]), res["ms"], res["message"]))
    live = _live.setdefault(mid, {"status": "pending", "fails": 0, "incident_id": None, "down_since": None})
    prev = live["status"]
    live.update({"last_ts": res["ts"], "last_ms": res["ms"], "message": res["message"]})
    if "cert_expires" in res:
        live["cert_expires"] = res["cert_expires"]
    alert = None
    if res["ok"]:
        live["fails"] = 0
        if live.get("incident_id"):
            await db.execute("UPDATE incidents SET ended=? WHERE id=?", (res["ts"], live["incident_id"]))
            dur = res["ts"] - (live.get("down_since") or res["ts"])
            alert = {"kind": "up", "message": f"Rétabli après {_fmt_duration(dur)}", "duration": dur}
            live["incident_id"] = None
            live["down_since"] = None
        live["status"] = "paused" if _monitors[mid]["paused"] else "up"
    else:
        live["fails"] = live.get("fails", 0) + 1
        if live["fails"] >= m.get("fail_threshold", 1) and not live.get("incident_id"):
            row = await db.fetch_one("INSERT INTO incidents (monitor_id, started, cause) VALUES (?,?,?) RETURNING id",
                                     (mid, res["ts"], res["message"]))
            live["incident_id"] = row["id"]
            live["down_since"] = res["ts"]
            alert = {"kind": "down", "message": res["message"]}
        if live.get("incident_id"):
            live["status"] = "down"
        elif prev in ("pending", "paused"):
            live["status"] = "pending"
    await module.events.emit("monitor:update", {"id": mid, "live": _public_live(mid), "check": res})
    if alert:
        alert.update({"id": mid, "name": m["name"], "at": res["ts"]})
        await module.events.emit("alert", alert)
        if _settings.get("notify_windows") and (alert["kind"] == "down" or _settings.get("notify_recovery", True)):
            title = f"{'🔴' if alert['kind'] == 'down' else '🟢'} {m['name']} — {'hors service' if alert['kind'] == 'down' else 'rétabli'}"
            asyncio.create_task(_notify(title, alert["message"]))
    return res


def _public_live(mid: int) -> dict:
    lv = _live.get(mid) or {}
    return {k: lv.get(k) for k in ("status", "last_ts", "last_ms", "message", "down_since", "cert_expires")} | \
        {"next_ts": _next_due.get(mid), "running": mid in _running}


async def _uptime(mid: int, since: float) -> Optional[float]:
    r = await module.db.fetch_one("SELECT COUNT(*) AS n, SUM(ok) AS up FROM checks WHERE monitor_id=? AND ts>=?", (mid, since))
    return round(r["up"] * 100 / r["n"], 3) if r and r["n"] else None


# ── Handlers ──────────────────────────────────────────────────────────────────

@module.on("state")
async def state(payload: dict):
    db = await _db()
    _ensure_started()
    now = time.time()
    out = []
    for mid, m in _monitors.items():
        recent = await db.fetch_all("SELECT ts, ok, ms FROM checks WHERE monitor_id=? ORDER BY ts DESC LIMIT 40", (mid,))
        out.append({**m, "live": _public_live(mid),
                    "uptime": {"24h": await _uptime(mid, now - 86400), "7d": await _uptime(mid, now - 7 * 86400),
                               "30d": await _uptime(mid, now - 30 * 86400)},
                    "recent": [{"ok": bool(r["ok"]), "ms": r["ms"], "ts": r["ts"]} for r in reversed(recent)]})
    open_inc = await db.fetch_one("SELECT COUNT(*) AS n FROM incidents WHERE ended IS NULL")
    return {"monitors": out, "settings": dict(_settings), "open_incidents": open_inc["n"], "now": now}


@module.on("monitor.save")
async def monitor_save(payload: dict):
    try:
        m = MonitorIn.model_validate(payload.get("monitor") or {})
    except ValidationError as exc:
        return _err(_validation_message(exc))
    db = await _db()
    _ensure_started()
    cfg = m.model_dump(exclude={"name", "type", "paused"})
    mid = payload.get("id")
    if mid:
        mid = int(mid)
        if mid not in _monitors:
            return _err("Sonde introuvable", "NOT_FOUND")
        await db.execute("UPDATE monitors SET name=?, type=?, config=?, paused=? WHERE id=?",
                         (m.name, m.type, json.dumps(cfg), int(m.paused), mid))
    else:
        pos = (await db.fetch_one("SELECT COALESCE(MAX(position)+1, 0) AS p FROM monitors"))["p"]
        row = await db.fetch_one("INSERT INTO monitors (name, type, config, paused, position, created_at) VALUES (?,?,?,?,?,?) RETURNING id",
                                 (m.name, m.type, json.dumps(cfg), int(m.paused), pos, time.time()))
        mid = row["id"]
    row = await db.fetch_one("SELECT * FROM monitors WHERE id=?", (mid,))
    _monitors[mid] = _row_to_monitor(row)
    live = _live.setdefault(mid, {"status": "pending", "fails": 0, "incident_id": None, "down_since": None,
                                  "message": "En attente de la première vérification", "last_ts": None, "last_ms": None})
    if m.paused:
        live["status"] = "paused"
    elif live["status"] == "paused":
        live["status"] = "pending"
    live["fails"] = 0
    _next_due[mid] = time.time()
    _kick()
    return {"ok": True, "id": mid, "monitor": _monitors[mid]}


@module.on("monitor.delete")
async def monitor_delete(payload: dict):
    db = await _db()
    mid = int(payload.get("id") or 0)
    if mid not in _monitors:
        return _err("Sonde introuvable", "NOT_FOUND")
    _monitors.pop(mid, None)
    _live.pop(mid, None)
    _next_due.pop(mid, None)
    task = _running.pop(mid, None)
    if task:
        task.cancel()
    await db.execute("DELETE FROM monitors WHERE id=?", (mid,))
    await db.execute("DELETE FROM checks WHERE monitor_id=?", (mid,))
    await db.execute("DELETE FROM incidents WHERE monitor_id=?", (mid,))
    return {"ok": True}


async def _set_paused(mid: int, paused: bool):
    m = _monitors[mid]
    await module.db.execute("UPDATE monitors SET paused=? WHERE id=?", (int(paused), mid))
    m["paused"] = paused
    live = _live.setdefault(mid, {"fails": 0})
    if paused:
        live["status"] = "paused"
        if live.get("incident_id"):
            await module.db.execute("UPDATE incidents SET ended=? WHERE id=?", (time.time(), live["incident_id"]))
            live["incident_id"] = None
            live["down_since"] = None
    else:
        live["status"] = "pending"
        live["message"] = "Reprise de la surveillance"
        live["fails"] = 0
        _next_due[mid] = time.time()


@module.on("monitor.pause")
async def monitor_pause(payload: dict):
    await _db()
    _ensure_started()
    paused = bool(payload.get("paused"))
    ids = list(_monitors) if payload.get("all") else [int(payload.get("id") or 0)]
    for mid in ids:
        if mid not in _monitors:
            return _err("Sonde introuvable", "NOT_FOUND")
        await _set_paused(mid, paused)
        await module.events.emit("monitor:update", {"id": mid, "live": _public_live(mid)})
    _kick()
    return {"ok": True, "count": len(ids)}


@module.on("monitor.check_now")
async def monitor_check_now(payload: dict):
    await _db()
    _ensure_started()
    mid = int(payload.get("id") or 0)
    if mid not in _monitors:
        return _err("Sonde introuvable", "NOT_FOUND")
    if mid in _running:
        await asyncio.shield(_running[mid])
        return {"ok": True, "live": _public_live(mid)}
    _next_due[mid] = time.time() + _monitors[mid]["interval"]
    task = asyncio.create_task(_run_check(mid))
    _running[mid] = task
    task.add_done_callback(lambda _t, _id=mid: _running.pop(_id, None))
    res = await asyncio.shield(task)
    return {"ok": True, "check": res, "live": _public_live(mid)}


@module.on("monitor.test")
async def monitor_test(payload: dict):
    try:
        m = MonitorIn.model_validate(payload.get("monitor") or {})
    except ValidationError as exc:
        return _err(_validation_message(exc))
    return await probe(m.model_dump())


@module.on("monitor.reorder")
async def monitor_reorder(payload: dict):
    db = await _db()
    ids = [int(x) for x in payload.get("ids") or [] if int(x) in _monitors]
    for pos, mid in enumerate(ids):
        await db.execute("UPDATE monitors SET position=? WHERE id=?", (pos, mid))
        _monitors[mid]["position"] = pos
    ordered = sorted(_monitors.values(), key=lambda m: (m["position"], m["id"]))
    _monitors.clear()
    _monitors.update({m["id"]: m for m in ordered})
    return {"ok": True}


RANGES = {"1h": 3600, "24h": 86400, "7d": 7 * 86400, "30d": 30 * 86400}


@module.on("monitor.detail")
async def monitor_detail(payload: dict):
    db = await _db()
    mid = int(payload.get("id") or 0)
    if mid not in _monitors:
        return _err("Sonde introuvable", "NOT_FOUND")
    rng = payload.get("range") if payload.get("range") in RANGES else "24h"
    now = time.time()
    span = RANGES[rng]
    since = now - span
    bucket = max(1.0, span / HISTORY_POINTS)
    series = await db.fetch_all(
        "SELECT CAST((ts - ?) / ? AS INTEGER) AS b, COUNT(*) AS n, SUM(ok) AS up, AVG(CASE WHEN ok THEN ms END) AS avg, "
        "MIN(CASE WHEN ok THEN ms END) AS min, MAX(CASE WHEN ok THEN ms END) AS max "
        "FROM checks WHERE monitor_id=? AND ts>=? GROUP BY b ORDER BY b", (since, bucket, mid, since))
    stats = await db.fetch_one(
        "SELECT COUNT(*) AS n, SUM(ok) AS up, AVG(CASE WHEN ok THEN ms END) AS avg, MIN(CASE WHEN ok THEN ms END) AS min, "
        "MAX(CASE WHEN ok THEN ms END) AS max FROM checks WHERE monitor_id=? AND ts>=?", (mid, since))
    incidents = await db.fetch_all("SELECT id, started, ended, cause FROM incidents WHERE monitor_id=? ORDER BY started DESC LIMIT 100", (mid,))
    checks = await db.fetch_all("SELECT ts, ok, ms, message FROM checks WHERE monitor_id=? ORDER BY ts DESC LIMIT 60", (mid,))
    return {
        "monitor": _monitors[mid], "live": _public_live(mid), "range": rng, "since": since, "now": now, "bucket": bucket,
        "series": [{"t": since + r["b"] * bucket, "n": r["n"], "up": r["up"], "avg": r["avg"], "min": r["min"], "max": r["max"]} for r in series],
        "stats": {**stats, "uptime": round(stats["up"] * 100 / stats["n"], 3) if stats["n"] else None},
        "uptime": {"24h": await _uptime(mid, now - 86400), "7d": await _uptime(mid, now - 7 * 86400), "30d": await _uptime(mid, now - 30 * 86400)},
        "incidents": [{**i, "duration": (i["ended"] or now) - i["started"]} for i in incidents],
        "checks": [{**c, "ok": bool(c["ok"])} for c in checks],
    }


@module.on("incidents.list")
async def incidents_list(payload: dict):
    db = await _db()
    limit = max(1, min(500, int(payload.get("limit") or 200)))
    rows = await db.fetch_all(
        "SELECT i.id, i.monitor_id, i.started, i.ended, i.cause, m.name FROM incidents i JOIN monitors m ON m.id = i.monitor_id "
        "ORDER BY (i.ended IS NULL) DESC, i.started DESC LIMIT ?", (limit,))
    now = time.time()
    return {"incidents": [{**r, "duration": (r["ended"] or now) - r["started"]} for r in rows]}


@module.on("incidents.clear")
async def incidents_clear(payload: dict):
    db = await _db()
    n = await db.execute("DELETE FROM incidents WHERE ended IS NOT NULL")
    return {"ok": True, "deleted": n}


class SettingsIn(BaseModel):
    retention_days: int = Field(30, ge=1, le=365)
    notify_windows: bool = False
    notify_recovery: bool = True


@module.on("settings.save")
async def settings_save(payload: dict):
    try:
        s = SettingsIn.model_validate(payload.get("settings") or {})
    except ValidationError as exc:
        return _err(_validation_message(exc))
    db = await _db()
    for k, v in s.model_dump().items():
        await db.execute("INSERT INTO settings (key, value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                         (k, json.dumps(v)))
    old = _settings.get("retention_days")
    _settings.update(s.model_dump())
    if old != s.retention_days:
        await _purge()
    return {"ok": True, "settings": dict(_settings)}


@module.on("notify.test")
async def notify_test(payload: dict):
    ok, msg = await _notify("📡 Supervision de services", "Les notifications Windows fonctionnent.")
    return {"ok": True} if ok else _err(msg or "Notification impossible", "NOTIFY_FAILED")


@module.on("export")
async def export(payload: dict):
    await _db()
    mons = []
    for m in _monitors.values():
        mons.append({k: v for k, v in m.items() if k not in ("id", "position")})
    return {"format": EXPORT_FORMAT, "version": 1, "exported_at": time.time(), "monitors": mons}


@module.on("import")
async def import_(payload: dict):
    data = payload.get("data")
    if not isinstance(data, dict) or data.get("format") != EXPORT_FORMAT or not isinstance(data.get("monitors"), list):
        return _err("Fichier non reconnu (export de « Supervision de services » attendu)")
    if len(data["monitors"]) > 500:
        return _err("Trop de sondes dans le fichier (500 max.)")
    valid: list[MonitorIn] = []
    for i, raw in enumerate(data["monitors"]):
        try:
            valid.append(MonitorIn.model_validate(raw))
        except ValidationError as exc:
            return _err(f"Sonde n° {i + 1} : {_validation_message(exc)}")
    await _db()
    if payload.get("replace"):
        for mid in list(_monitors):
            await monitor_delete({"id": mid})
    existing = {(m["type"], m["target"], m.get("port")) for m in _monitors.values()}
    added = skipped = 0
    for m in valid:
        if (m.type, m.target, m.port) in existing:
            skipped += 1
            continue
        r = await monitor_save({"monitor": m.model_dump()})
        if r.get("ok"):
            added += 1
            existing.add((m.type, m.target, m.port))
    return {"ok": True, "added": added, "skipped": skipped}


@module.on_shutdown
async def _on_shutdown():
    global _loop_task
    if _loop_task is not None:
        _loop_task.cancel()
        _loop_task = None
    for task in list(_running.values()):
        task.cancel()
    _running.clear()


_ensure_started()
