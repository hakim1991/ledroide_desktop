import asyncio
import csv
import io
import ipaddress
import json
import logging
import time
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field, ValidationError, field_validator

from ledroide_sdk import Module

import lan_probe as probe

log = logging.getLogger("ledroide.lan-inventory")

module = Module(__file__)
router = module.router

TYPES = ["inconnu", "ordinateur", "mobile", "routeur", "reseau", "imprimante", "nas", "serveur", "camera",
         "multimedia", "objet", "console", "apple"]
MAX_SCANS = 300
DEFAULT_SETTINGS = {"cidr": "", "interval_min": 0, "ping_timeout_ms": 700, "ping_concurrency": 32,
                    "resolve_names": True, "scan_ports": False, "alert_new": True}

_ready = False
_lock = asyncio.Lock()
_scan_lock = asyncio.Lock()
_auto_task: Optional[asyncio.Task] = None
_wake = asyncio.Event()
_progress: dict = {}

SCHEMA = """
CREATE TABLE IF NOT EXISTS devices (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    key         TEXT NOT NULL UNIQUE,
    mac         TEXT NOT NULL DEFAULT '',
    ip          TEXT NOT NULL DEFAULT '',
    hostname    TEXT NOT NULL DEFAULT '',
    netbios     TEXT NOT NULL DEFAULT '',
    mdns        TEXT NOT NULL DEFAULT '',
    vendor      TEXT NOT NULL DEFAULT '',
    name        TEXT NOT NULL DEFAULT '',
    notes       TEXT NOT NULL DEFAULT '',
    type        TEXT NOT NULL DEFAULT '',
    ports       TEXT NOT NULL DEFAULT '[]',
    ports_at    TEXT,
    rtt_ms      REAL,
    first_seen  TEXT NOT NULL,
    last_seen   TEXT NOT NULL,
    online      INTEGER NOT NULL DEFAULT 0,
    is_new      INTEGER NOT NULL DEFAULT 1,
    known       INTEGER NOT NULL DEFAULT 0,
    seen_count  INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS scans (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    cidr        TEXT NOT NULL,
    trigger     TEXT NOT NULL DEFAULT 'manuel',
    status      TEXT NOT NULL DEFAULT 'running',
    found       INTEGER NOT NULL DEFAULT 0,
    new_count   INTEGER NOT NULL DEFAULT 0,
    elapsed_s   REAL,
    message     TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS sightings (
    device_id   INTEGER NOT NULL,
    scan_id     INTEGER NOT NULL,
    ip          TEXT NOT NULL,
    PRIMARY KEY (device_id, scan_id)
);
CREATE INDEX IF NOT EXISTS idx_sight_scan ON sightings(scan_id);
CREATE TABLE IF NOT EXISTS settings (k TEXT PRIMARY KEY, v TEXT NOT NULL);
"""


async def _db():
    global _ready
    if not _ready:
        async with _lock:
            if not _ready:
                await module.db.executescript(SCHEMA)
                await module.db.execute("UPDATE scans SET status='error', message='Interrompu' WHERE status='running'")
                _ready = True
    return module.db


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _err(message: str, code: str = "BAD_REQUEST") -> dict:
    return {"error": message, "code": code}


def _validation_message(exc: ValidationError) -> str:
    first = exc.errors()[0]
    loc = ".".join(str(p) for p in first.get("loc", ()))
    msg = str(first.get("msg", "Données invalides")).removeprefix("Value error, ")
    return f"{loc} : {msg}" if loc else msg


class SettingsIn(BaseModel):
    cidr: str = Field("", max_length=50)
    interval_min: int = Field(0, ge=0, le=1440)
    ping_timeout_ms: int = Field(700, ge=200, le=3000)
    ping_concurrency: int = Field(32, ge=4, le=64)
    resolve_names: bool = True
    scan_ports: bool = False
    alert_new: bool = True

    @field_validator("cidr")
    @classmethod
    def _cidr(cls, v: str) -> str:
        v = v.strip()
        if v:
            probe.parse_cidr(v)
            v = str(ipaddress.ip_network(v, strict=False))
        return v

    @field_validator("interval_min")
    @classmethod
    def _interval(cls, v: int) -> int:
        if 0 < v < 5:
            raise ValueError("Intervalle minimal : 5 minutes")
        return v


class DeviceIn(BaseModel):
    name: str = Field("", max_length=100)
    notes: str = Field("", max_length=5000)
    type: str = ""
    known: bool = True

    @field_validator("name")
    @classmethod
    def _strip(cls, v: str) -> str:
        return v.strip()

    @field_validator("type")
    @classmethod
    def _type(cls, v: str) -> str:
        if v and v not in TYPES:
            raise ValueError("Type d'appareil inconnu")
        return v


async def _settings() -> dict:
    rows = await (await _db()).fetch_all("SELECT k, v FROM settings")
    out = dict(DEFAULT_SETTINGS)
    for r in rows:
        if r["k"] in out:
            out[r["k"]] = json.loads(r["v"])
    return out


_net_cache: dict = {"at": 0.0, "net": {}}


async def _network() -> dict:
    if time.time() - _net_cache["at"] > 60:
        _net_cache["net"] = await asyncio.to_thread(probe.detect_network)
        _net_cache["at"] = time.time()
    return _net_cache["net"]


def _device_view(d: dict, net: Optional[dict] = None) -> dict:
    ports = json.loads(d.get("ports") or "[]")
    net = net or _net_cache["net"] or {}
    flags = {"is_self": bool(d["ip"]) and d["ip"] == net.get("ip"), "is_gateway": bool(d["ip"]) and d["ip"] == net.get("gateway")}
    guessed = probe.guess_type({**d, **flags, "ports": ports})
    label = d["name"] or d["hostname"] or d["mdns"] or d["netbios"] or ""
    return {**d, **flags, "ports": ports, "online": bool(d["online"]), "is_new": bool(d["is_new"]), "known": bool(d["known"]),
            "type_effective": d["type"] or guessed, "type_guessed": guessed, "label": label}


# ── Scan ──────────────────────────────────────────────────────────────────────

async def _probe_ports(ip: str, ports: list[int], sem: asyncio.Semaphore, timeout: float = 0.5) -> list[int]:
    found = []

    async def one(port: int):
        async with sem:
            try:
                _, w = await asyncio.wait_for(asyncio.open_connection(ip, port), timeout)
            except (OSError, asyncio.TimeoutError):
                return
            found.append(port)
            w.close()
            try:
                await asyncio.wait_for(w.wait_closed(), 1)
            except (OSError, asyncio.TimeoutError):
                pass
    await asyncio.gather(*(one(p) for p in ports))
    return sorted(found)


async def _scan(settings: dict, trigger: str, report) -> dict:
    """Ping sweep (bounded), ARP table merge, name lookups, optional TCP probe of a
    short list of common ports on responding hosts only."""
    net = await asyncio.to_thread(probe.detect_network)
    _net_cache.update(at=time.time(), net=net)
    cidr = settings["cidr"] or net["cidr"]
    ips = probe.parse_cidr(cidr)
    db = await _db()
    scan_id = (await db.fetch_one("INSERT INTO scans (started_at, cidr, trigger) VALUES (?,?,?) RETURNING id",
                                  (_now(), cidr, trigger)))["id"]
    started = time.perf_counter()
    status, message = "error", ""
    try:
        timeout_ms = settings["ping_timeout_ms"]
        sem = asyncio.Semaphore(settings["ping_concurrency"])
        alive: dict[str, dict] = {}
        done = 0
        total = len(ips)
        last = 0.0

        async def ping_one(ip: str):
            nonlocal done, last
            async with sem:
                rtt = await asyncio.to_thread(probe.ping, ip, timeout_ms)
            done += 1
            if rtt is not None:
                alive[ip] = {"ip": ip, "rtt_ms": rtt}
            if time.perf_counter() - last > 0.3:
                last = time.perf_counter()
                await report(done * 70 / total, f"Ping {done}/{total} — {len(alive)} appareil(s)")
        await asyncio.gather(*(ping_one(ip) for ip in ips))

        await report(71, "Lecture de la table ARP…")
        arp = await asyncio.to_thread(probe.arp_table)
        wanted = set(ips)
        for ip in arp:
            if ip in wanted and ip not in alive:
                alive[ip] = {"ip": ip, "rtt_ms": None}
        for ip, e in alive.items():
            e["mac"] = arp.get(ip, "")
            if ip == net["ip"] and not e["mac"]:
                e["mac"] = net["mac"]
            e["vendor"] = probe.vendor_for(e["mac"]) if e["mac"] else ""
            e["is_self"] = ip == net["ip"]
            e["is_gateway"] = ip == net["gateway"]

        if settings["resolve_names"] and alive:
            nsem = asyncio.Semaphore(12)
            count = 0

            async def names(e: dict):
                nonlocal count
                async with nsem:
                    e["hostname"] = await asyncio.to_thread(probe.reverse_dns, e["ip"])
                    e["netbios"] = await asyncio.to_thread(probe.netbios_name, e["ip"], 0.8)
                    e["mdns"] = "" if e["is_self"] else await asyncio.to_thread(probe.mdns_name, e["ip"], 0.8)
                count += 1
                await report(72 + 18 * count / len(alive), f"Noms {count}/{len(alive)}")
            await asyncio.gather(*(names(e) for e in alive.values()))

        if settings["scan_ports"] and alive:
            psem = asyncio.Semaphore(48)
            count = 0

            async def ports(e: dict):
                nonlocal count
                e["ports"] = await _probe_ports(e["ip"], list(probe.COMMON_PORTS), psem)
                count += 1
                await report(90 + 8 * count / len(alive), f"Ports {count}/{len(alive)}")
            await asyncio.gather(*(ports(e) for e in alive.values()))

        await report(99, "Enregistrement…")
        new = await _store(scan_id, alive, settings["scan_ports"])
        status = "ok"
        elapsed = round(time.perf_counter() - started, 1)
        message = f"{len(alive)} appareil(s) en ligne, {len(new)} nouveau(x)"
        await db.execute("UPDATE scans SET found=?, new_count=?, elapsed_s=? WHERE id=?",
                         (len(alive), len(new), elapsed, scan_id))
        if new and settings["alert_new"]:
            await module.events.emit("devices:new", {"devices": new, "scan_id": scan_id})
        return {"scan_id": scan_id, "cidr": cidr, "found": len(alive), "new": new, "elapsed_s": elapsed,
                "scanned": total}
    except asyncio.CancelledError:
        status, message = "cancelled", "Annulé"
        raise
    except Exception as exc:
        message = str(exc)
        raise
    finally:
        await db.execute("UPDATE scans SET finished_at=?, status=?, message=? WHERE id=?",
                         (_now(), status, message, scan_id))
        old = await db.fetch_all("SELECT id FROM scans ORDER BY id DESC LIMIT -1 OFFSET ?", (MAX_SCANS,))
        if old:
            marks = ",".join("?" * len(old))
            ids = tuple(r["id"] for r in old)
            await db.execute(f"DELETE FROM sightings WHERE scan_id IN ({marks})", ids)
            await db.execute(f"DELETE FROM scans WHERE id IN ({marks})", ids)


async def _store(scan_id: int, alive: dict[str, dict], ports_scanned: bool) -> list[dict]:
    db = await _db()
    now = _now()
    first_scan = (await db.fetch_one("SELECT COUNT(*) AS n FROM devices"))["n"] == 0
    await db.execute("UPDATE devices SET online=0")
    new = []
    for ip, e in alive.items():
        key = e["mac"] or f"ip:{ip}"
        row = await db.fetch_one("SELECT * FROM devices WHERE key=?", (key,))
        if not row and e["mac"]:
            row = await db.fetch_one("SELECT * FROM devices WHERE key=?", (f"ip:{ip}",))
            if row:
                await db.execute("UPDATE devices SET key=?, mac=? WHERE id=?", (key, e["mac"], row["id"]))
        fields = {"ip": ip, "vendor": e.get("vendor") or (row or {}).get("vendor", ""), "rtt_ms": e.get("rtt_ms")}
        for k in ("hostname", "netbios", "mdns"):
            if e.get(k):
                fields[k] = e[k]
        if ports_scanned:
            fields["ports"] = json.dumps(e.get("ports") or [])
            fields["ports_at"] = now
        if row:
            sets = ", ".join(f"{k}=?" for k in fields)
            await db.execute(f"UPDATE devices SET {sets}, last_seen=?, online=1, seen_count=seen_count+1 WHERE id=?",
                             (*fields.values(), now, row["id"]))
            did = row["id"]
        else:
            cols = ["key", "mac", *fields.keys(), "first_seen", "last_seen", "online", "is_new", "known", "seen_count"]
            vals = [key, e["mac"], *fields.values(), now, now, 1, 0 if first_scan else 1, 1 if e.get("is_self") else 0, 1]
            did = (await db.fetch_one(
                f"INSERT INTO devices ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))}) RETURNING id", tuple(vals)))["id"]
            if not first_scan and not e.get("is_self"):
                new.append({"id": did, "ip": ip, "mac": e["mac"], "vendor": fields["vendor"],
                            "label": e.get("hostname") or e.get("mdns") or e.get("netbios") or ""})
        await db.execute("INSERT OR REPLACE INTO sightings (device_id, scan_id, ip) VALUES (?,?,?)", (did, scan_id, ip))
    return new


async def _run_scan(trigger: str, job=None) -> dict:
    if _scan_lock.locked():
        raise RuntimeError("Un balayage est déjà en cours")
    async with _scan_lock:
        settings = await _settings()

        async def report(pct, msg):
            _progress.update(percent=round(pct, 1), message=msg, trigger=trigger)
            if job:
                await job.progress(round(pct, 1), msg)
            await module.events.emit("scan:progress", dict(_progress))
        _progress.clear()
        _progress.update(percent=0, message="Démarrage…", trigger=trigger)
        await module.events.emit("scan:started", {"trigger": trigger})
        try:
            res = await _scan(settings, trigger, report)
            await module.events.emit("scan:finished", {"ok": True, **res})
            return res
        except Exception as exc:
            await module.events.emit("scan:finished", {"ok": False, "error": str(exc)})
            raise
        finally:
            _progress.clear()


# ── Handlers ──────────────────────────────────────────────────────────────────

@module.on("info")
async def info(payload: dict):
    net = await asyncio.to_thread(probe.detect_network)
    _net_cache.update(at=time.time(), net=net)
    db = await _db()
    last = await db.fetch_one("SELECT * FROM scans ORDER BY id DESC LIMIT 1")
    return {"network": net, "settings": await _settings(), "types": TYPES, "last_scan": last,
            "scanning": dict(_progress) if _scan_lock.locked() else None,
            "ports": [{"port": p, "service": s} for p, s in probe.COMMON_PORTS.items()]}


@module.on("settings.save")
async def settings_save(payload: dict):
    try:
        s = SettingsIn.model_validate(payload.get("settings") or {})
    except ValidationError as exc:
        return _err(_validation_message(exc))
    db = await _db()
    for k, v in s.model_dump().items():
        await db.execute("INSERT OR REPLACE INTO settings (k, v) VALUES (?,?)", (k, json.dumps(v)))
    _wake.set()
    return {"settings": await _settings()}


@module.job("scan")
async def scan(payload: dict, job):
    try:
        return await _run_scan("manuel", job)
    except ValueError as exc:
        raise RuntimeError(str(exc))


@module.on("devices.list")
async def devices_list(payload: dict):
    db = await _db()
    net = await _network()
    rows = await db.fetch_all("SELECT * FROM devices")
    rows.sort(key=lambda d: (ipaddress.IPv4Address(d["ip"]) if d["ip"] else ipaddress.IPv4Address("255.255.255.255")))
    total_scans = (await db.fetch_one("SELECT COUNT(*) AS n FROM scans WHERE status='ok'"))["n"]
    return {"devices": [_device_view(r, net) for r in rows], "total_scans": total_scans}


@module.on("device.get")
async def device_get(payload: dict):
    db = await _db()
    row = await db.fetch_one("SELECT * FROM devices WHERE id=?", (int(payload.get("id") or 0),))
    if not row:
        return _err("Appareil introuvable", "NOT_FOUND")
    recent = await db.fetch_all("SELECT id FROM scans WHERE status='ok' ORDER BY id DESC LIMIT 30")
    seen = {r["scan_id"]: r["ip"] for r in await db.fetch_all(
        "SELECT scan_id, ip FROM sightings WHERE device_id=?", (row["id"],))}
    timeline = []
    for r in reversed(recent):
        timeline.append({"scan_id": r["id"], "seen": r["id"] in seen})
    ips = await db.fetch_all(
        "SELECT s.ip, MIN(c.started_at) AS first, MAX(c.started_at) AS last, COUNT(*) AS n FROM sightings s "
        "JOIN scans c ON c.id = s.scan_id WHERE s.device_id=? GROUP BY s.ip ORDER BY last DESC", (row["id"],))
    return {"device": _device_view(row, await _network()), "timeline": timeline, "ips": ips,
            "port_names": {str(p): s for p, s in probe.COMMON_PORTS.items()}}


@module.on("device.update")
async def device_update(payload: dict):
    try:
        d = DeviceIn.model_validate(payload.get("device") or {})
    except ValidationError as exc:
        return _err(_validation_message(exc))
    db = await _db()
    did = int(payload.get("id") or 0)
    n = await db.execute("UPDATE devices SET name=?, notes=?, type=?, known=?, is_new=CASE WHEN ? THEN 0 ELSE is_new END WHERE id=?",
                         (d.name, d.notes, d.type, int(d.known), int(d.known), did))
    if not n:
        return _err("Appareil introuvable", "NOT_FOUND")
    return await device_get({"id": did})


@module.on("device.delete")
async def device_delete(payload: dict):
    db = await _db()
    did = int(payload.get("id") or 0)
    await db.execute("DELETE FROM sightings WHERE device_id=?", (did,))
    if not await db.execute("DELETE FROM devices WHERE id=?", (did,)):
        return _err("Appareil introuvable", "NOT_FOUND")
    return {"ok": True}


@module.on("devices.ack")
async def devices_ack(payload: dict):
    """Marks new devices as seen (`ids` or all); `known` also approves them."""
    db = await _db()
    known = bool(payload.get("known"))
    ids = [int(i) for i in payload.get("ids") or []]
    extra = ", known=1" if known else ""
    if ids:
        marks = ",".join("?" * len(ids))
        n = await db.execute(f"UPDATE devices SET is_new=0{extra} WHERE id IN ({marks})", tuple(ids))
    else:
        n = await db.execute(f"UPDATE devices SET is_new=0{extra} WHERE is_new=1")
    return {"updated": n}


@module.on("devices.purge")
async def devices_purge(payload: dict):
    days = max(1, min(3650, int(payload.get("days") or 90)))
    db = await _db()
    cutoff = datetime.fromtimestamp(time.time() - days * 86400).isoformat(timespec="seconds")
    old = await db.fetch_all("SELECT id FROM devices WHERE last_seen < ? AND known=0 AND name=''", (cutoff,))
    for r in old:
        await db.execute("DELETE FROM sightings WHERE device_id=?", (r["id"],))
        await db.execute("DELETE FROM devices WHERE id=?", (r["id"],))
    return {"deleted": len(old)}


@module.job("device.ports")
async def device_ports(payload: dict, job):
    db = await _db()
    row = await db.fetch_one("SELECT * FROM devices WHERE id=?", (int(payload.get("id") or 0),))
    if not row:
        raise RuntimeError("Appareil introuvable")
    await job.progress(10, f"Test de {len(probe.COMMON_PORTS)} ports sur {row['ip']}…")
    ports = await _probe_ports(row["ip"], list(probe.COMMON_PORTS), asyncio.Semaphore(16), 0.8)
    await db.execute("UPDATE devices SET ports=?, ports_at=? WHERE id=?", (json.dumps(ports), _now(), row["id"]))
    return await device_get({"id": row["id"]})


@module.on("scans.list")
async def scans_list(payload: dict):
    rows = await (await _db()).fetch_all("SELECT * FROM scans ORDER BY id DESC LIMIT ?",
                                         (max(1, min(300, int(payload.get("limit") or 50))),))
    return {"scans": rows}


@module.on("export.csv")
async def export_csv(payload: dict):
    rows = (await devices_list({}))["devices"]
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";", lineterminator="\r\n")
    w.writerow(["Nom", "Type", "IP", "MAC", "Fabricant", "Nom DNS", "NetBIOS", "mDNS", "Ports ouverts", "En ligne",
                "Connu", "Première vue", "Dernière vue", "Vu (balayages)", "Notes"])
    for d in rows:
        w.writerow([d["name"], d["type_effective"], d["ip"], d["mac"], d["vendor"], d["hostname"], d["netbios"],
                    d["mdns"], " ".join(str(p) for p in d["ports"]), "oui" if d["online"] else "non",
                    "oui" if d["known"] else "non", d["first_seen"], d["last_seen"], d["seen_count"],
                    d["notes"].replace("\r", " ").replace("\n", " ")])
    return {"csv": "﻿" + buf.getvalue(), "filename": f"inventaire-reseau-{datetime.now():%Y-%m-%d}.csv"}


# ── Periodic scan ─────────────────────────────────────────────────────────────

async def _auto_loop():
    await asyncio.sleep(5)
    while True:
        try:
            s = await _settings()
            interval = s["interval_min"]
            wait = 60.0
            if interval:
                last = await (await _db()).fetch_one(
                    "SELECT started_at FROM scans WHERE status IN ('ok','error') ORDER BY id DESC LIMIT 1")
                due = (datetime.fromisoformat(last["started_at"]).timestamp() + interval * 60) if last else 0
                if due <= time.time() and not _scan_lock.locked():
                    try:
                        await _run_scan("périodique")
                    except Exception as exc:
                        log.warning("periodic scan failed: %s", exc)
                    continue
                wait = max(5.0, min(60.0, due - time.time()))
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("auto scan loop error")
        _wake.clear()
        try:
            await asyncio.wait_for(_wake.wait(), timeout=wait)
        except asyncio.TimeoutError:
            pass


def _ensure_loop():
    global _auto_task
    if _auto_task is not None and not _auto_task.done():
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    _auto_task = loop.create_task(_auto_loop())


_ensure_loop()


@module.on_shutdown
async def _shutdown():
    if _auto_task:
        _auto_task.cancel()
