import asyncio
import csv
import http.client
import io
import json
import logging
import os
import re
import secrets
import ssl
import statistics
import threading
import time
from typing import Callable, Optional

from pydantic import BaseModel, Field, ValidationError

from ledroide_sdk import Module

log = logging.getLogger("ledroide.speedtest")

module = Module(__file__)
router = module.router

HOST = "speed.cloudflare.com"
USER_AGENT = "LedroideDesktop-Speedtest/1.0"
READ_CHUNK = 64 * 1024
MIN_CHUNK = 100_000
UPLOAD_BLOCK = os.urandom(256 * 1024)
SERVER_TIMING_RE = re.compile(r"dur=([\d.]+)")

_ready = False
_lock = asyncio.Lock()
_running = asyncio.Lock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS tests (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          REAL    NOT NULL,
    ping_ms     REAL,
    jitter_ms   REAL,
    down_mbps   REAL,
    up_mbps     REAL,
    down_bytes  INTEGER NOT NULL DEFAULT 0,
    up_bytes    INTEGER NOT NULL DEFAULT 0,
    ip          TEXT    NOT NULL DEFAULT '',
    isp         TEXT    NOT NULL DEFAULT '',
    asn         TEXT    NOT NULL DEFAULT '',
    server      TEXT    NOT NULL DEFAULT '',
    location    TEXT    NOT NULL DEFAULT '',
    connections INTEGER NOT NULL DEFAULT 1,
    duration    REAL    NOT NULL DEFAULT 0,
    note        TEXT    NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_tests_ts ON tests(ts);
"""


async def _db():
    global _ready
    if not _ready:
        async with _lock:
            if not _ready:
                await module.db.executescript(SCHEMA)
                _ready = True
    return module.db


def _err(message: str, code: str = "BAD_REQUEST") -> dict:
    return {"error": message, "code": code}


def _validation_message(exc: ValidationError) -> str:
    first = exc.errors()[0]
    loc = ".".join(str(p) for p in first.get("loc", ()))
    msg = str(first.get("msg", "Données invalides")).removeprefix("Value error, ")
    return f"{loc} : {msg}" if loc else msg


class RunIn(BaseModel):
    connections: int = Field(4, ge=1, le=8)
    duration: float = Field(10, ge=1, le=30)
    ping_count: int = Field(20, ge=3, le=50)
    limit_mb: float = Field(0, ge=0, le=5000)
    max_chunk_mb: float = Field(50, ge=0.1, le=100)
    download: bool = True
    upload: bool = True
    save: bool = True


_ctx = ssl.create_default_context()


def _conn() -> http.client.HTTPSConnection:
    return http.client.HTTPSConnection(HOST, 443, timeout=15, context=_ctx)


def _headers() -> dict:
    return {"User-Agent": USER_AGENT, "Accept": "*/*", "Connection": "keep-alive"}


def _meta_sync() -> dict:
    c = _conn()
    try:
        c.request("GET", "/cdn-cgi/trace", headers=_headers())
        r = c.getresponse()
        kv = dict(line.split("=", 1) for line in r.read(16 * 1024).decode("utf-8", "replace").splitlines() if "=" in line)
        c.request("GET", "/__down?bytes=0", headers=_headers())
        r = c.getresponse()
        r.read()
        h = {k.lower(): v for k, v in r.getheaders() if k.lower().startswith("cf-meta-")}
    finally:
        c.close()
    return {"ip": h.get("cf-meta-ip") or kv.get("ip", ""), "isp": h.get("cf-meta-asorganization", ""),
            "asn": h.get("cf-meta-asn", ""), "colo": h.get("cf-meta-colo") or kv.get("colo", ""),
            "city": h.get("cf-meta-city", ""), "region": h.get("cf-meta-region", ""),
            "country": h.get("cf-meta-country") or kv.get("loc", ""), "http": kv.get("http", ""),
            "tls": kv.get("tls", "")}


def _latency_sync(count: int, stop: threading.Event, on_sample: Callable[[float], None]) -> list[float]:
    out: list[float] = []
    c = _conn()
    try:
        c.request("GET", "/__down?bytes=0", headers=_headers())
        c.getresponse().read()
        for _ in range(count):
            if stop.is_set():
                break
            t0 = time.perf_counter()
            c.request("GET", f"/__down?bytes=0&r={secrets.token_hex(4)}", headers=_headers())
            r = c.getresponse()
            t1 = time.perf_counter()
            r.read()
            server = 0.0
            m = SERVER_TIMING_RE.search(r.getheader("Server-Timing") or "")
            if m:
                server = float(m.group(1))
            ms = max(0.1, (t1 - t0) * 1000 - server)
            out.append(ms)
            on_sample(ms)
    finally:
        c.close()
    return out


class _Transfer:
    """Parallel workers (dedicated threads) pulling or pushing data until stop."""

    def __init__(self, kind: str, connections: int, max_chunk: int, limit: int):
        self.kind = kind
        self.connections = connections
        self.max_chunk = max_chunk
        self.limit = limit
        self.bytes = 0
        self.errors = 0
        self.last_error = ""
        self.stop = threading.Event()
        self._lock = threading.Lock()
        self.threads: list[threading.Thread] = []
        self.active = 0

    def add(self, n: int):
        with self._lock:
            self.bytes += n
            if self.limit and self.bytes >= self.limit:
                self.stop.set()

    def start_worker(self):
        t = threading.Thread(target=self._worker, name=f"speedtest-{self.kind}", daemon=True)
        self.threads.append(t)
        t.start()

    def _worker(self):
        with self._lock:
            self.active += 1
        size = MIN_CHUNK
        conn = None
        try:
            while not self.stop.is_set():
                try:
                    if conn is None:
                        conn = _conn()
                    t0 = time.perf_counter()
                    if self.kind == "down":
                        self._download(conn, size)
                    else:
                        self._upload(conn, size)
                    took = time.perf_counter() - t0
                    if took < 1.0 and size < self.max_chunk:
                        size = min(self.max_chunk, size * 4 if took < 0.25 else size * 2)
                except (OSError, http.client.HTTPException) as exc:
                    with self._lock:
                        self.errors += 1
                        self.last_error = str(exc) or type(exc).__name__
                    if conn is not None:
                        conn.close()
                        conn = None
                    if self.errors > 20:
                        self.stop.set()
                    else:
                        self.stop.wait(0.3)
        finally:
            if conn is not None:
                conn.close()
            with self._lock:
                self.active -= 1

    def _download(self, conn: http.client.HTTPSConnection, size: int):
        conn.request("GET", f"/__down?bytes={size}&r={secrets.token_hex(4)}", headers=_headers())
        r = conn.getresponse()
        if r.status != 200:
            r.read()
            raise http.client.HTTPException(f"HTTP {r.status}")
        while True:
            if self.stop.is_set():
                conn.close()
                return
            chunk = r.read(READ_CHUNK)
            if not chunk:
                return
            self.add(len(chunk))

    def _upload(self, conn: http.client.HTTPSConnection, size: int):
        conn.putrequest("POST", f"/__up?r={secrets.token_hex(4)}", skip_accept_encoding=True)
        for k, v in {**_headers(), "Content-Type": "application/octet-stream", "Content-Length": str(size)}.items():
            conn.putheader(k, v)
        conn.endheaders()
        sent = 0
        block = UPLOAD_BLOCK
        while sent < size:
            if self.stop.is_set():
                conn.close()
                return
            n = min(len(block), size - sent)
            conn.send(block[:n] if n < len(block) else block)
            sent += n
            self.add(n)
        r = conn.getresponse()
        r.read()
        if r.status >= 400:
            raise http.client.HTTPException(f"HTTP {r.status}")


def _mbps(nbytes: float, seconds: float) -> float:
    return nbytes * 8 / seconds / 1e6 if seconds > 0 else 0.0


async def _measure(kind: str, opts: RunIn, job, base_pct: float, span_pct: float, label: str) -> dict:
    tr = _Transfer(kind, opts.connections, int(opts.max_chunk_mb * 1e6), int(opts.limit_mb * 1e6))
    samples: list[tuple[float, int]] = []
    t_start = time.perf_counter()
    stagger = min(0.6, opts.duration / (opts.connections * 4))
    launched = 0
    last_emit = 0.0
    try:
        while True:
            now = time.perf_counter()
            el = now - t_start
            while launched < opts.connections and el >= launched * stagger:
                tr.start_worker()
                launched += 1
            samples.append((el, tr.bytes))
            if el >= opts.duration or (tr.stop.is_set() and tr.active == 0 and launched == opts.connections):
                break
            if now - last_emit >= 0.25:
                last_emit = now
                win = [s for s in samples if s[0] >= el - 1.0]
                cur = _mbps(win[-1][1] - win[0][1], win[-1][0] - win[0][0]) if len(win) > 1 else 0.0
                pct = base_pct + span_pct * min(1.0, el / opts.duration)
                await job.progress(round(pct, 1), f"{label} : {cur:.1f} Mbit/s", phase=kind, mbps=round(cur, 2),
                                   bytes=tr.bytes, connections=launched)
            await asyncio.sleep(0.1)
    finally:
        tr.stop.set()
    elapsed = samples[-1][0] if samples else 0.0
    total = tr.bytes
    warm = elapsed * 0.25
    base = next((s for s in samples if s[0] >= warm), samples[0] if samples else (0.0, 0))
    steady = _mbps(total - base[1], elapsed - base[0]) if elapsed - base[0] > 0.3 and total - base[1] > 0 else _mbps(total, elapsed)
    if total == 0:
        raise RuntimeError(f"Aucune donnée transférée ({tr.last_error or 'serveur injoignable'})")
    return {"mbps": round(steady, 2), "bytes": total, "seconds": round(elapsed, 2), "errors": tr.errors}


@module.job("run")
async def run(payload: dict, job):
    try:
        opts = RunIn.model_validate(payload or {})
    except ValidationError as exc:
        raise ValueError(_validation_message(exc))
    if _running.locked():
        raise RuntimeError("Un test est déjà en cours")
    async with _running:
        stop = threading.Event()
        t0 = time.time()
        await job.progress(1, "Recherche du serveur…", phase="meta")
        try:
            meta = await asyncio.to_thread(_meta_sync)
        except (OSError, http.client.HTTPException) as exc:
            raise RuntimeError(f"Serveur de test injoignable ({HOST}) : {exc}")
        await job.progress(3, "Mesure de la latence…", phase="ping", meta=meta)
        loop = asyncio.get_running_loop()
        lat: list[float] = []

        def on_sample(ms: float):
            lat.append(ms)
            asyncio.run_coroutine_threadsafe(
                job.progress(round(3 + 12 * len(lat) / opts.ping_count, 1), f"Latence : {ms:.1f} ms", phase="ping", ping=round(ms, 2)), loop)

        try:
            await asyncio.to_thread(_latency_sync, opts.ping_count, stop, on_sample)
        except (OSError, http.client.HTTPException) as exc:
            if not lat:
                raise RuntimeError(f"Mesure de latence impossible : {exc}")
        except asyncio.CancelledError:
            stop.set()
            raise
        ping = round(statistics.median(lat), 2) if lat else None
        jitter = round(statistics.mean(abs(a - b) for a, b in zip(lat, lat[1:])), 2) if len(lat) > 1 else None
        await job.progress(15, f"Latence : {ping} ms", phase="ping", ping=ping, jitter=jitter)
        down = up = None
        if opts.download:
            down = await _measure("down", opts, job, 15, 45 if opts.upload else 85, "Descendant")
            await job.progress(60 if opts.upload else 100, f"Descendant : {down['mbps']} Mbit/s", phase="down", mbps=down["mbps"], done=True)
        if opts.upload:
            up = await _measure("up", opts, job, 60 if opts.download else 15, 40 if opts.download else 85, "Montant")
            await job.progress(100, f"Montant : {up['mbps']} Mbit/s", phase="up", mbps=up["mbps"], done=True)
        result = {
            "ts": t0, "ping_ms": ping, "jitter_ms": jitter,
            "down_mbps": down["mbps"] if down else None, "up_mbps": up["mbps"] if up else None,
            "down_bytes": down["bytes"] if down else 0, "up_bytes": up["bytes"] if up else 0,
            "ip": meta.get("ip", ""), "isp": meta.get("isp", ""), "asn": meta.get("asn", ""),
            "server": meta.get("colo", ""),
            "location": ", ".join(x for x in (meta.get("city"), meta.get("region"), meta.get("country")) if x),
            "connections": opts.connections, "duration": round(time.time() - t0, 1), "note": "",
        }
        if opts.save:
            db = await _db()
            row = await db.fetch_one(
                "INSERT INTO tests (ts, ping_ms, jitter_ms, down_mbps, up_mbps, down_bytes, up_bytes, ip, isp, asn, server, location, connections, duration, note)"
                " VALUES (:ts,:ping_ms,:jitter_ms,:down_mbps,:up_mbps,:down_bytes,:up_bytes,:ip,:isp,:asn,:server,:location,:connections,:duration,:note) RETURNING id",
                result)
            result["id"] = row["id"]
        log.info("speedtest done: down=%s up=%s ping=%s", result["down_mbps"], result["up_mbps"], ping)
        return result


@module.on("meta")
async def meta(payload: dict):
    try:
        return await asyncio.to_thread(_meta_sync)
    except (OSError, http.client.HTTPException) as exc:
        return _err(f"Serveur de test injoignable : {exc}", "NETWORK_ERROR")


@module.on("history")
async def history(payload: dict):
    db = await _db()
    limit = max(1, min(5000, int(payload.get("limit") or 500)))
    rows = await db.fetch_all("SELECT * FROM tests ORDER BY ts DESC LIMIT ?", (limit,))
    stats = await db.fetch_one(
        "SELECT COUNT(*) AS n, AVG(down_mbps) AS down, MAX(down_mbps) AS down_max, AVG(up_mbps) AS up, MAX(up_mbps) AS up_max, "
        "AVG(ping_ms) AS ping, MIN(ping_ms) AS ping_min FROM tests")
    return {"tests": rows, "stats": stats}


@module.on("note")
async def note(payload: dict):
    db = await _db()
    text = str(payload.get("note") or "").strip()[:200]
    n = await db.execute("UPDATE tests SET note=? WHERE id=?", (text, int(payload.get("id") or 0)))
    return {"ok": True} if n else _err("Test introuvable", "NOT_FOUND")


@module.on("delete")
async def delete(payload: dict):
    db = await _db()
    if payload.get("all"):
        await db.execute("DELETE FROM tests")
        return {"ok": True}
    n = await db.execute("DELETE FROM tests WHERE id=?", (int(payload.get("id") or 0),))
    return {"ok": True} if n else _err("Test introuvable", "NOT_FOUND")


CSV_COLUMNS = [("ts", "Date"), ("ping_ms", "Latence (ms)"), ("jitter_ms", "Gigue (ms)"), ("down_mbps", "Descendant (Mbit/s)"),
               ("up_mbps", "Montant (Mbit/s)"), ("down_bytes", "Octets reçus"), ("up_bytes", "Octets envoyés"), ("ip", "IP publique"),
               ("isp", "FAI"), ("asn", "ASN"), ("server", "Serveur Cloudflare"), ("location", "Localisation"),
               ("connections", "Connexions"), ("duration", "Durée (s)"), ("note", "Note")]


def _fr_num(v) -> str:
    if v is None:
        return ""
    if isinstance(v, float):
        return f"{v:.2f}".replace(".", ",")
    return str(v)


@module.on("export_csv")
async def export_csv(payload: dict):
    db = await _db()
    rows = await db.fetch_all("SELECT * FROM tests ORDER BY ts")
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";", lineterminator="\r\n")
    w.writerow([label for _, label in CSV_COLUMNS])
    for r in rows:
        line = []
        for key, _ in CSV_COLUMNS:
            v = r[key]
            if key == "ts":
                v = time.strftime("%d/%m/%Y %H:%M:%S", time.localtime(v))
            elif isinstance(v, str) and v[:1] in ("=", "+", "-", "@"):
                v = "'" + v
            line.append(_fr_num(v) if not isinstance(v, str) else v)
        w.writerow(line)
    return {"csv": "﻿" + buf.getvalue(), "count": len(rows)}
