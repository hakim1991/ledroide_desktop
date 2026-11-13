import asyncio
import json
import logging
import os
import re
import string
import sys
import time
from collections import OrderedDict
from datetime import datetime, timedelta
from typing import Literal, Optional

from pydantic import BaseModel, Field, ValidationError, field_validator

from ledroide_sdk import Module

import backup_engine as eng

log = logging.getLogger("ledroide.backup")

module = Module(__file__)
router = module.router

IS_WINDOWS = sys.platform == "win32"
TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
RETRY_AFTER_FAILURE = timedelta(hours=1)
SCHED_TICK_S = 30
MAX_RUNS = 500

_ready = False
_lock = asyncio.Lock()
_busy: dict[int, str] = {}
_manifests: "OrderedDict[tuple, dict]" = OrderedDict()
_sched_task: Optional[asyncio.Task] = None
_wake = asyncio.Event()
_sched_sem = asyncio.Semaphore(1)
_ctls: dict[int, eng.Ctl] = {}

SCHEMA = """
CREATE TABLE IF NOT EXISTS sets (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT NOT NULL,
    config        TEXT NOT NULL,
    enabled       INTEGER NOT NULL DEFAULT 1,
    created_at    TEXT NOT NULL,
    last_attempt  TEXT,
    last_status   TEXT,
    last_success  TEXT,
    last_message  TEXT NOT NULL DEFAULT '',
    last_snapshot TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    set_id      INTEGER,
    set_name    TEXT NOT NULL DEFAULT '',
    kind        TEXT NOT NULL,
    trigger     TEXT NOT NULL DEFAULT 'manuel',
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    status      TEXT NOT NULL DEFAULT 'running',
    message     TEXT NOT NULL DEFAULT '',
    result      TEXT NOT NULL DEFAULT '{}',
    log         TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_runs_set ON runs(set_id);
"""


async def _db():
    global _ready
    if not _ready:
        async with _lock:
            if not _ready:
                await module.db.executescript(SCHEMA)
                await module.db.execute(
                    "UPDATE runs SET status='error', message='Interrompue (application fermée)', finished_at=? "
                    "WHERE status='running'", (_now(),))
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


def _norm(p: str) -> str:
    return os.path.normcase(os.path.normpath(p))


def _inside(child: str, parent: str) -> bool:
    c, p = _norm(child), _norm(parent)
    return c == p or c.startswith(p.rstrip(os.sep) + os.sep)


def _slug(name: str) -> str:
    s = "".join(c if c.isalnum() or c in "-_ " else "_" for c in name).strip().replace(" ", "_")
    return (s or "sauvegarde")[:60]


# ── Models ────────────────────────────────────────────────────────────────────

class Retention(BaseModel):
    last: int = Field(3, ge=1, le=1000)
    daily: int = Field(7, ge=0, le=3650)
    weekly: int = Field(4, ge=0, le=520)
    monthly: int = Field(12, ge=0, le=600)


class Schedule(BaseModel):
    type: Literal["manual", "interval", "daily", "weekly"] = "manual"
    time: str = "12:00"
    weekdays: list[int] = Field(default_factory=lambda: [0])
    hours: int = Field(24, ge=1, le=720)

    @field_validator("time")
    @classmethod
    def _time(cls, v: str) -> str:
        if not TIME_RE.match(v or ""):
            raise ValueError("Heure invalide (HH:MM)")
        return v

    @field_validator("weekdays")
    @classmethod
    def _days(cls, v: list[int]) -> list[int]:
        v = sorted({int(d) for d in v if 0 <= int(d) <= 6})
        return v or [0]


class Sftp(BaseModel):
    host: str = Field("", max_length=253)
    port: int = Field(22, ge=1, le=65535)
    user: str = Field("", max_length=100)
    path: str = Field("/", max_length=500)
    key_file: str = Field("", max_length=1000)
    password_var: str = Field("BACKUP_SFTP_PASSWORD", max_length=100)
    passphrase_var: str = Field("", max_length=100)
    fingerprint: str = Field("", max_length=200)

    @field_validator("host", "user", "path", "key_file", "password_var", "passphrase_var")
    @classmethod
    def _strip(cls, v: str) -> str:
        return v.strip()

    @field_validator("password_var", "passphrase_var")
    @classmethod
    def _var(cls, v: str) -> str:
        if v and not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,99}", v):
            raise ValueError("Nom de variable invalide")
        return v


class SetIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    sources: list[str] = Field(min_length=1, max_length=50)
    excludes: list[str] = Field(default_factory=list)
    dest_type: Literal["local", "sftp"] = "local"
    dest_path: str = Field("", max_length=1000)
    folder: str = Field("", max_length=100)
    sftp: Sftp = Field(default_factory=Sftp)
    mode: Literal["auto", "hardlink", "dedup"] = "auto"
    compress: bool = False
    retention: Retention = Field(default_factory=Retention)
    schedule: Schedule = Field(default_factory=Schedule)
    enabled: bool = True

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("Le nom est requis")
        return v

    @field_validator("sources")
    @classmethod
    def _sources(cls, v: list[str]) -> list[str]:
        out = []
        for s in v:
            s = str(s).strip().strip('"')
            if not s:
                continue
            if not os.path.isabs(s):
                raise ValueError(f"Chemin source non absolu : {s}")
            s = os.path.normpath(s)
            if _norm(s) not in (_norm(x) for x in out):
                out.append(s)
        if not out:
            raise ValueError("Ajoutez au moins un dossier source")
        return out

    @field_validator("excludes")
    @classmethod
    def _excl(cls, v: list[str]) -> list[str]:
        return eng.clean_excludes(v)


def _check_set(s: SetIn) -> Optional[str]:
    for i, a in enumerate(s.sources):
        for b in s.sources[i + 1:]:
            if _inside(a, b) or _inside(b, a):
                return f"Sources imbriquées : {a} et {b}"
    if s.dest_type == "local":
        d = s.dest_path.strip().strip('"')
        if not d or not os.path.isabs(d):
            return "Indiquez un dossier de destination absolu (ex. E:\\Sauvegardes ou \\\\serveur\\partage)"
        s.dest_path = os.path.normpath(d)
        root = os.path.join(s.dest_path, s.folder or _slug(s.name))
        for src in s.sources:
            if _inside(root, src):
                return f"La destination ne peut pas être à l'intérieur d'une source ({src})"
            if _inside(src, root):
                return "Une source ne peut pas être à l'intérieur de la destination"
    else:
        if not s.sftp.host or not s.sftp.user:
            return "SFTP : hôte et utilisateur requis"
        if not re.fullmatch(r"[A-Za-z0-9.\-:\[\]]{1,253}", s.sftp.host):
            return "SFTP : hôte invalide"
        if not s.sftp.path.startswith("/"):
            return "SFTP : le chemin distant doit commencer par /"
        if s.sftp.key_file and not os.path.isabs(s.sftp.key_file):
            return "SFTP : chemin de clé privée non absolu"
    return None


# ── Store helpers ─────────────────────────────────────────────────────────────

def _store_for(cfg: dict):
    folder = cfg.get("folder") or _slug(cfg["name"])
    if cfg.get("dest_type") == "sftp":
        sc = {**cfg["sftp"], "folder": folder}
        pw = module.env(sc["password_var"]) if sc.get("password_var") else None
        pp = module.env(sc["passphrase_var"]) if sc.get("passphrase_var") else None
        return eng.SftpStore(sc, pw, pp)
    return eng.LocalStore(os.path.join(cfg["dest_path"], folder))


def _dest_label(cfg: dict) -> str:
    folder = cfg.get("folder") or _slug(cfg["name"])
    if cfg.get("dest_type") == "sftp":
        s = cfg["sftp"]
        return f"sftp://{s['user']}@{s['host']}:{s['port']}{s['path'].rstrip('/')}/{folder}"
    return os.path.join(cfg["dest_path"], folder)


def _with_store(cfg: dict, fn, create: bool = False):
    store = _store_for(cfg)
    store.open(create=create)
    try:
        return fn(store)
    finally:
        store.close()


async def _get_set(set_id) -> Optional[dict]:
    try:
        sid = int(set_id or 0)
    except (TypeError, ValueError):
        return None
    row = await (await _db()).fetch_one("SELECT * FROM sets WHERE id=?", (sid,))
    if row:
        row["config"] = json.loads(row["config"])
    return row


def _due(row: dict) -> Optional[datetime]:
    cfg = row["config"]
    if not row["enabled"] or cfg["schedule"]["type"] == "manual":
        return None
    if row["last_status"] == "error" and row["last_attempt"]:
        retry = datetime.fromisoformat(row["last_attempt"]) + RETRY_AFTER_FAILURE
        regular = eng.next_due(cfg["schedule"], datetime.fromisoformat(row["last_success"] or row["created_at"]))
        return min(retry, regular) if regular and regular > datetime.fromisoformat(row["last_attempt"]) else retry
    ref = datetime.fromisoformat(row["last_success"] or row["created_at"])
    return eng.next_due(cfg["schedule"], ref)


def _set_view(row: dict) -> dict:
    cfg = row["config"]
    due = _due(row)
    available = None
    if cfg["dest_type"] == "local":
        available = os.path.isdir(cfg["dest_path"])
    return {"id": row["id"], "name": row["name"], "config": cfg, "enabled": bool(row["enabled"]),
            "created_at": row["created_at"], "last_attempt": row["last_attempt"], "last_status": row["last_status"],
            "last_success": row["last_success"], "last_message": row["last_message"],
            "last_snapshot": row["last_snapshot"], "next_run": due.isoformat(timespec="seconds") if due else None,
            "busy": _busy.get(row["id"]), "dest_label": _dest_label(cfg), "dest_available": available}


# ── Execution ─────────────────────────────────────────────────────────────────

KIND_LABEL = {"backup": "Sauvegarde", "restore": "Restauration", "verify": "Vérification",
              "rotate": "Rotation", "export": "Export zip", "delete": "Suppression"}


async def _execute(row: dict, kind: str, trigger: str, fn, job=None) -> dict:
    """Runs fn(ctl, store) in a thread, journals it, relays progress, and on cancellation
    stops the worker cleanly (a partial snapshot is removed by the engine)."""
    sid = row["id"]
    if sid in _busy:
        raise eng.BackupError("BUSY", f"Une opération est déjà en cours sur ce jeu ({KIND_LABEL.get(_busy[sid], _busy[sid])})")
    _busy[sid] = kind
    db = await _db()
    run_id = (await db.fetch_one(
        "INSERT INTO runs (set_id, set_name, kind, trigger, started_at) VALUES (?,?,?,?,?) RETURNING id",
        (sid, row["name"], kind, trigger, _now())))["id"]
    ctl = eng.Ctl()
    _ctls[sid] = ctl
    await module.events.emit("run:started", {"set_id": sid, "kind": kind, "run_id": run_id, "trigger": trigger})

    def work(c):
        store = _store_for(row["config"])
        store.open(create=kind == "backup")
        try:
            return fn(c, store)
        finally:
            store.close()

    fut = asyncio.ensure_future(asyncio.to_thread(work, ctl))
    last = None
    status, message, result = "error", "", {}
    try:
        while not fut.done():
            await asyncio.wait({fut}, timeout=0.3)
            cur = (round(ctl.pct, 1), ctl.msg)
            if cur != last:
                last = cur
                if job:
                    await job.progress(cur[0], cur[1])
                await module.events.emit("run:progress", {"set_id": sid, "kind": kind, "percent": cur[0], "message": cur[1]})
        result = fut.result()
        r = result or {}
        bad = r.get("stats", {}).get("errors") or r.get("errors") or r.get("missing") or r.get("corrupt")
        status = "warning" if bad else "ok"
        message = _summary(kind, result)
        return result
    except asyncio.CancelledError:
        ctl.cancel.set()
        await asyncio.wait({fut})
        status, message = "cancelled", "Annulée par l'utilisateur"
        raise
    except eng.Cancelled:
        status, message = "cancelled", "Annulée"
        raise eng.BackupError("JOB_CANCELLED", "Opération annulée")
    except eng.BackupError as exc:
        message = exc.message
        raise
    except Exception as exc:
        log.exception("%s failed for set %s", kind, sid)
        message = str(exc) or exc.__class__.__name__
        raise
    finally:
        _busy.pop(sid, None)
        _ctls.pop(sid, None)
        await db.execute("UPDATE runs SET finished_at=?, status=?, message=?, result=?, log=? WHERE id=?",
                         (_now(), status, message[:2000], json.dumps(result or {}, ensure_ascii=False)[:200000],
                          "\n".join(ctl.log)[:200000], run_id))
        await db.execute(f"DELETE FROM runs WHERE id NOT IN (SELECT id FROM runs ORDER BY id DESC LIMIT {MAX_RUNS})")
        if kind == "backup":
            ok = status in ("ok", "warning")
            await db.execute(
                "UPDATE sets SET last_attempt=?, last_status=?, last_message=?, "
                "last_success=CASE WHEN ? THEN ? ELSE last_success END, "
                "last_snapshot=CASE WHEN ? THEN ? ELSE last_snapshot END WHERE id=?",
                (_now(), "ok" if ok else ("cancelled" if status == "cancelled" else "error"), message[:500],
                 int(ok), _now(), int(ok), (result or {}).get("snapshot", ""), sid))
        _manifests_drop(sid)
        await module.events.emit("run:finished", {"set_id": sid, "kind": kind, "run_id": run_id,
                                                  "status": status, "message": message})
        _wake.set()


def _summary(kind: str, r: dict) -> str:
    if kind == "backup":
        s = r["stats"]
        rot = r.get("rotation") or {}
        msg = (f"{r['snapshot']} : {s['files']} fichiers, {s['new']} nouveaux, {s['changed']} modifiés, "
               f"{s['unchanged']} inchangés, {_size(s['bytes_copied'])} copiés")
        if rot.get("deleted"):
            msg += f" ; rotation : {len(rot['deleted'])} instantané(s) supprimé(s)"
        if s["errors"]:
            msg += f" ; {s['errors']} erreur(s)"
        return msg
    if kind == "restore":
        return (f"{r['restored']} restauré(s), {r['identical']} identique(s), {r['skipped']} ignoré(s), "
                f"{r['renamed']} renommé(s), {r['errors']} erreur(s)")
    if kind == "verify":
        return f"{r['snapshots']} instantané(s), {r['files']} fichiers : {r['ok']} OK, {r['missing']} manquant(s), {r['corrupt']} corrompu(s)"
    if kind == "rotate":
        return f"{len(r['deleted'])} instantané(s) supprimé(s), {len(r['kept'])} conservé(s)"
    if kind == "export":
        return f"{r['files']} fichiers → {r['path']}"
    if kind == "delete":
        return f"Instantané {r['deleted']} supprimé"
    return ""


def _size(n: int) -> str:
    for unit in ("o", "Ko", "Mo", "Go"):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit == "o" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.2f} To"


def _backup_fn(cfg: dict):
    def fn(ctl, store):
        res = eng.run_backup(ctl, store, cfg)
        try:
            res["rotation"] = eng.run_rotation(eng.Ctl(), store, cfg["retention"])
        except Exception as exc:
            ctl.note(f"Rotation impossible : {exc}")
            res["rotation"] = {"error": str(exc)}
        return res
    return fn


def _as_error(exc: Exception) -> dict:
    if isinstance(exc, eng.BackupError):
        return _err(exc.message, exc.code)
    return _err(str(exc) or exc.__class__.__name__, "FAILED")


async def _job_wrap(coro):
    try:
        return await coro
    except eng.BackupError as exc:
        if exc.code == "JOB_CANCELLED":
            raise asyncio.CancelledError()
        raise RuntimeError(exc.message)


# ── Manifest cache ────────────────────────────────────────────────────────────

def _manifests_drop(sid: int):
    for k in [k for k in _manifests if k[0] == sid]:
        _manifests.pop(k, None)


async def _manifest(row: dict, snap: str) -> dict:
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}_\d{6}(-\d+)?", snap or ""):
        raise eng.BackupError("BAD_SNAPSHOT", "Instantané invalide")
    key = (row["id"], snap)
    if key in _manifests:
        _manifests.move_to_end(key)
        return _manifests[key]
    m = await asyncio.to_thread(_with_store, row["config"], lambda st: eng.load_manifest(st, snap))
    _manifests[key] = m
    while len(_manifests) > 4:
        _manifests.popitem(last=False)
    return m


# ── Handlers : sets ───────────────────────────────────────────────────────────

@module.on("info")
async def info(payload: dict):
    try:
        import asyncssh  # noqa: F401
        sftp = True
    except ImportError:
        sftp = False
    home = os.path.expanduser("~")
    return {"sftp_available": sftp, "home": home, "windows": IS_WINDOWS,
            "default_excludes": ["node_modules", ".git", "__pycache__", "*.tmp", "~$*", "Thumbs.db",
                                 "desktop.ini", "$RECYCLE.BIN", "System Volume Information"]}


@module.on("sets.list")
async def sets_list(payload: dict):
    rows = await (await _db()).fetch_all("SELECT * FROM sets ORDER BY name COLLATE NOCASE, id")
    out = []
    for r in rows:
        r["config"] = json.loads(r["config"])
        out.append(_set_view(r))
    return {"sets": out}


@module.on("set.save")
async def set_save(payload: dict):
    try:
        s = SetIn.model_validate(payload.get("set") or {})
    except ValidationError as exc:
        return _err(_validation_message(exc))
    sid = payload.get("id")
    old = await _get_set(sid) if sid else None
    if sid and not old:
        return _err("Jeu de sauvegarde introuvable", "NOT_FOUND")
    if old:
        s.folder = old["config"].get("folder") or _slug(old["name"])
        if old["config"]["dest_type"] == s.dest_type == "sftp":
            same_host = (old["config"]["sftp"]["host"], old["config"]["sftp"]["port"]) == (s.sftp.host, s.sftp.port)
            if same_host and not s.sftp.fingerprint:
                s.sftp.fingerprint = old["config"]["sftp"].get("fingerprint", "")
    else:
        s.folder = _slug(s.name)
    if msg := _check_set(s):
        return _err(msg)
    db = await _db()
    cfg = json.dumps(s.model_dump(), ensure_ascii=False)
    if old:
        if old["id"] in _busy:
            return _err("Une opération est en cours sur ce jeu", "BUSY")
        await db.execute("UPDATE sets SET name=?, config=?, enabled=? WHERE id=?", (s.name, cfg, int(s.enabled), old["id"]))
        new_id = old["id"]
        _manifests_drop(new_id)
    else:
        new_id = (await db.fetch_one(
            "INSERT INTO sets (name, config, enabled, created_at) VALUES (?,?,?,?) RETURNING id",
            (s.name, cfg, int(s.enabled), _now())))["id"]
    _wake.set()
    return _set_view(await _get_set(new_id))


@module.on("set.toggle")
async def set_toggle(payload: dict):
    row = await _get_set(payload.get("id"))
    if not row:
        return _err("Jeu de sauvegarde introuvable", "NOT_FOUND")
    enabled = bool(payload.get("enabled"))
    row["config"]["enabled"] = enabled
    await (await _db()).execute("UPDATE sets SET enabled=?, config=? WHERE id=?",
                                (int(enabled), json.dumps(row["config"], ensure_ascii=False), row["id"]))
    _wake.set()
    return _set_view(await _get_set(row["id"]))


@module.on("set.delete")
async def set_delete(payload: dict):
    row = await _get_set(payload.get("id"))
    if not row:
        return _err("Jeu de sauvegarde introuvable", "NOT_FOUND")
    if row["id"] in _busy:
        return _err("Une opération est en cours sur ce jeu", "BUSY")
    await (await _db()).execute("DELETE FROM sets WHERE id=?", (row["id"],))
    _manifests_drop(row["id"])
    return {"ok": True, "dest": _dest_label(row["config"])}


@module.on("dest.test")
async def dest_test(payload: dict):
    """Checks a destination before saving. SFTP: first contact returns the host key
    fingerprint (`need_trust`); calling again with `trust` accepts it."""
    try:
        s = SetIn.model_validate(payload.get("set") or {})
    except ValidationError as exc:
        return _err(_validation_message(exc))
    old = await _get_set(payload.get("id")) if payload.get("id") else None
    s.folder = (old["config"].get("folder") if old else None) or _slug(old["name"] if old else s.name)
    if msg := _check_set(s):
        return _err(msg)
    cfg = s.model_dump()
    if s.dest_type == "local":
        def check():
            parent = cfg["dest_path"]
            if not os.path.isdir(parent):
                return _err(f"Dossier indisponible : {parent}", "DEST_UNAVAILABLE")
            store = eng.LocalStore(os.path.join(parent, cfg["folder"]))
            store.open(create=True)
            meta = store.read_meta("backup.json") or {}
            usage = None
            try:
                import shutil
                du = shutil.disk_usage(parent)
                usage = {"total": du.total, "free": du.free}
            except OSError:
                pass
            return {"ok": True, "hardlink": store.can_hardlink(), "mode": meta.get("mode"),
                    "snapshots": len(store.list_snapshots()), "path": str(store.root), "disk": usage}
        try:
            return await asyncio.to_thread(check)
        except (OSError, eng.BackupError) as exc:
            return _as_error(exc) if isinstance(exc, eng.BackupError) else _err(f"Destination inutilisable : {exc}", "DEST_UNAVAILABLE")
    trust = str(payload.get("trust") or "")
    if trust:
        cfg["sftp"]["fingerprint"] = trust
    store = _store_for(cfg)

    def connect():
        try:
            store.open(create=True)
            return {"ok": True, "fingerprint": store.fingerprint, "snapshots": len(store.list_snapshots()),
                    "mode": (store.read_meta("backup.json") or {}).get("mode") or "dedup", "path": store.label()}
        finally:
            store.close()
    try:
        res = await asyncio.to_thread(connect)
    except eng.BackupError as exc:
        if exc.code == "HOSTKEY_UNKNOWN":
            fp = exc.message.split("(", 1)[1].split(")", 1)[0]
            return {"ok": False, "need_trust": True, "fingerprint": fp}
        return _as_error(exc)
    if old and trust:
        old["config"]["sftp"]["fingerprint"] = trust
        await (await _db()).execute("UPDATE sets SET config=? WHERE id=?",
                                    (json.dumps(old["config"], ensure_ascii=False), old["id"]))
    return res


@module.on("fs.list")
async def fs_list(payload: dict):
    """Folder picker: subfolders of `path` (drives when empty, on Windows)."""
    path = str(payload.get("path") or "").strip().strip('"')

    def ls():
        drives = []
        if IS_WINDOWS:
            try:
                drives = os.listdrives()
            except (AttributeError, OSError):
                drives = [f"{d}:\\" for d in string.ascii_uppercase if os.path.exists(f"{d}:\\")]
        if not path:
            return {"path": "", "parent": None, "dirs": [], "drives": drives, "home": os.path.expanduser("~")}
        if not os.path.isabs(path):
            return _err("Chemin non absolu", "BAD_PATH")
        p = os.path.normpath(path)
        if not os.path.isdir(p):
            return _err(f"Dossier introuvable : {p}", "NOT_FOUND")
        dirs = []
        try:
            with os.scandir(p) as it:
                for e in it:
                    try:
                        if e.is_dir(follow_symlinks=False) and not e.name.startswith("$"):
                            dirs.append(e.name)
                    except OSError:
                        continue
        except PermissionError:
            return _err("Accès refusé", "ACCESS_DENIED")
        up = os.path.dirname(p)
        parent = "" if up == p else up
        return {"path": p, "parent": parent, "dirs": sorted(dirs, key=str.lower)[:2000], "drives": drives,
                "home": os.path.expanduser("~")}
    return await asyncio.to_thread(ls)


@module.on("fs.mkdir")
async def fs_mkdir(payload: dict):
    parent = str(payload.get("path") or "").strip()
    name = str(payload.get("name") or "").strip()
    if not parent or not os.path.isabs(parent) or not os.path.isdir(parent):
        return _err("Dossier parent introuvable", "NOT_FOUND")
    if not name or any(c in name for c in '<>:"/\\|?*') or name in (".", ".."):
        return _err("Nom de dossier invalide")
    target = os.path.join(parent, name)
    try:
        await asyncio.to_thread(os.makedirs, target, exist_ok=False)
    except FileExistsError:
        return _err("Ce dossier existe déjà", "CONFLICT")
    except OSError as exc:
        return _err(f"Création impossible : {exc.strerror or exc}", "FAILED")
    return {"path": target}


# ── Handlers : operations ─────────────────────────────────────────────────────

@module.job("backup.run")
async def backup_run(payload: dict, job):
    row = await _get_set(payload.get("id"))
    if not row:
        raise RuntimeError("Jeu de sauvegarde introuvable")
    return await _job_wrap(_execute(row, "backup", "manuel", _backup_fn(row["config"]), job))


@module.on("op.cancel")
async def op_cancel(payload: dict):
    """Stops a running operation of a set, including scheduled ones (no job id)."""
    try:
        sid = int(payload.get("id") or 0)
    except (TypeError, ValueError):
        sid = 0
    ctl = _ctls.get(sid)
    if not ctl:
        return _err("Aucune opération en cours", "NOT_FOUND")
    ctl.cancel.set()
    return {"ok": True}


@module.on("snapshots.list")
async def snapshots_list(payload: dict):
    row = await _get_set(payload.get("id"))
    if not row:
        return _err("Jeu de sauvegarde introuvable", "NOT_FOUND")

    def read(store):
        out = []
        for n in store.list_snapshots():
            s = store.read_meta(f"snapshots/{n}/summary.json") or {"id": n}
            out.append(s)
        meta = store.read_meta("backup.json") or {}
        return {"snapshots": list(reversed(out)), "mode": meta.get("mode"), "dest": store.label()}
    try:
        return await asyncio.to_thread(_with_store, row["config"], read)
    except eng.BackupError as exc:
        if exc.code == "DEST_UNAVAILABLE":
            return {"snapshots": [], "mode": None, "dest": _dest_label(row["config"]), "unavailable": exc.message}
        return _as_error(exc)
    except OSError as exc:
        return _err(f"Destination illisible : {exc}", "DEST_UNAVAILABLE")


@module.on("snapshot.browse")
async def snapshot_browse(payload: dict):
    row = await _get_set(payload.get("id"))
    if not row:
        return _err("Jeu de sauvegarde introuvable", "NOT_FOUND")
    try:
        m = await _manifest(row, str(payload.get("snapshot") or ""))
    except (eng.BackupError, OSError) as exc:
        return _as_error(exc)
    q = str(payload.get("search") or "").strip().lower()
    if q:
        hits = [{"name": f["p"].rsplit("/", 1)[-1], "path": f["p"], "size": f["s"], "mtime": f["m"] / 1e9}
                for f in m["files"] if q in f["p"].lower()]
        return {"path": "", "search": q, "dirs": [], "files": hits[:1000], "truncated": len(hits) > 1000,
                "sources": m["sources"]}
    listing = eng.list_dir(m, str(payload.get("path") or ""))
    return {**listing, "sources": m["sources"]}


@module.on("restore.plan")
async def restore_plan(payload: dict):
    row = await _get_set(payload.get("id"))
    if not row:
        return _err("Jeu de sauvegarde introuvable", "NOT_FOUND")
    target = _restore_target_arg(payload)
    if isinstance(target, dict):
        return target
    try:
        m = await _manifest(row, str(payload.get("snapshot") or ""))
        return await asyncio.to_thread(eng.plan_restore, m, payload.get("paths") or None, target)
    except (eng.BackupError, OSError) as exc:
        return _as_error(exc)


def _restore_target_arg(payload: dict):
    target = str(payload.get("target") or "").strip().strip('"')
    if not target:
        return None
    if not os.path.isabs(target):
        return _err("Le dossier de restauration doit être un chemin absolu", "BAD_PATH")
    return os.path.normpath(target)


@module.job("restore.run")
async def restore_run(payload: dict, job):
    row = await _get_set(payload.get("id"))
    if not row:
        raise RuntimeError("Jeu de sauvegarde introuvable")
    target = _restore_target_arg(payload)
    if isinstance(target, dict):
        raise RuntimeError(target["error"])
    policy = payload.get("policy") or "ask"
    if policy not in ("ask", "skip", "overwrite", "rename"):
        raise RuntimeError("Politique de conflit invalide")
    try:
        m = await _manifest(row, str(payload.get("snapshot") or ""))
    except eng.BackupError as exc:
        raise RuntimeError(exc.message)
    paths = payload.get("paths") or None
    return await _job_wrap(_execute(row, "restore", "manuel",
                                    lambda c, st: eng.run_restore(c, st, m, paths, target, policy), job))


@module.job("verify.run")
async def verify_run(payload: dict, job):
    row = await _get_set(payload.get("id"))
    if not row:
        raise RuntimeError("Jeu de sauvegarde introuvable")
    snap = payload.get("snapshot")
    deep = payload.get("deep", True) is not False

    def fn(c, store):
        names = store.list_snapshots()
        sel = [snap] if snap else names
        if snap and snap not in names:
            raise eng.BackupError("NOT_FOUND", "Instantané introuvable")
        return eng.run_verify(c, store, sel, deep)
    return await _job_wrap(_execute(row, "verify", "manuel", fn, job))


@module.job("rotate.run")
async def rotate_run(payload: dict, job):
    row = await _get_set(payload.get("id"))
    if not row:
        raise RuntimeError("Jeu de sauvegarde introuvable")
    return await _job_wrap(_execute(row, "rotate", "manuel",
                                    lambda c, st: eng.run_rotation(c, st, row["config"]["retention"]), job))


@module.on("rotate.preview")
async def rotate_preview(payload: dict):
    row = await _get_set(payload.get("id"))
    if not row:
        return _err("Jeu de sauvegarde introuvable", "NOT_FOUND")
    try:
        ret = Retention.model_validate(payload.get("retention") or row["config"]["retention"]).model_dump()
    except ValidationError as exc:
        return _err(_validation_message(exc))
    try:
        names = await asyncio.to_thread(_with_store, row["config"], lambda st: st.list_snapshots())
    except eng.BackupError as exc:
        return _as_error(exc)
    keep = eng.rotation_keep(names, ret)
    return {"keep": sorted(keep, reverse=True), "delete": sorted((n for n in names if n not in keep), reverse=True)}


@module.on("snapshot.delete")
async def snapshot_delete(payload: dict):
    row = await _get_set(payload.get("id"))
    if not row:
        return _err("Jeu de sauvegarde introuvable", "NOT_FOUND")
    snap = str(payload.get("snapshot") or "")
    try:
        return await _execute(row, "delete", "manuel", lambda c, st: eng.delete_snapshot(st, snap))
    except (eng.BackupError, OSError) as exc:
        return _as_error(exc)


@module.job("snapshot.export")
async def snapshot_export(payload: dict, job):
    row = await _get_set(payload.get("id"))
    if not row:
        raise RuntimeError("Jeu de sauvegarde introuvable")
    out_dir = str(payload.get("out_dir") or "").strip().strip('"')
    if not out_dir or not os.path.isabs(out_dir) or not os.path.isdir(out_dir):
        raise RuntimeError("Dossier d'export introuvable")
    try:
        m = await _manifest(row, str(payload.get("snapshot") or ""))
    except eng.BackupError as exc:
        raise RuntimeError(exc.message)
    paths = payload.get("paths") or None
    return await _job_wrap(_execute(row, "export", "manuel",
                                    lambda c, st: eng.export_zip(c, st, m, paths, os.path.normpath(out_dir)), job))


# ── Journal ───────────────────────────────────────────────────────────────────

@module.on("runs.list")
async def runs_list(payload: dict):
    db = await _db()
    limit = max(1, min(500, int(payload.get("limit") or 100)))
    if payload.get("set_id"):
        rows = await db.fetch_all("SELECT id, set_id, set_name, kind, trigger, started_at, finished_at, status, message "
                                  "FROM runs WHERE set_id=? ORDER BY id DESC LIMIT ?", (int(payload["set_id"]), limit))
    else:
        rows = await db.fetch_all("SELECT id, set_id, set_name, kind, trigger, started_at, finished_at, status, message "
                                  "FROM runs ORDER BY id DESC LIMIT ?", (limit,))
    return {"runs": rows}


@module.on("run.get")
async def run_get(payload: dict):
    row = await (await _db()).fetch_one("SELECT * FROM runs WHERE id=?", (int(payload.get("id") or 0),))
    if not row:
        return _err("Exécution introuvable", "NOT_FOUND")
    row["result"] = json.loads(row["result"] or "{}")
    return row


@module.on("runs.clear")
async def runs_clear(payload: dict):
    db = await _db()
    if payload.get("set_id"):
        n = await db.execute("DELETE FROM runs WHERE set_id=? AND status!='running'", (int(payload["set_id"]),))
    else:
        n = await db.execute("DELETE FROM runs WHERE status!='running'")
    return {"deleted": n}


# ── Scheduler ─────────────────────────────────────────────────────────────────

async def _scheduled(row: dict, trigger: str):
    async with _sched_sem:
        fresh = await _get_set(row["id"])
        if not fresh or not fresh["enabled"] or fresh["id"] in _busy:
            return
        due = _due(fresh)
        if not due or due > datetime.now():
            return
        log.info("scheduled backup for set %s (%s)", fresh["id"], trigger)
        try:
            await _execute(fresh, "backup", trigger, _backup_fn(fresh["config"]))
        except eng.BackupError as exc:
            log.warning("scheduled backup %s failed: %s", fresh["id"], exc.message)
        except Exception:
            log.exception("scheduled backup %s failed", fresh["id"])


async def _scheduler():
    await asyncio.sleep(3)
    first = True
    pending: set[int] = set()
    while True:
        try:
            rows = await (await _db()).fetch_all("SELECT * FROM sets WHERE enabled=1")
            now = datetime.now()
            for r in rows:
                r["config"] = json.loads(r["config"])
                due = _due(r)
                if due and due <= now and r["id"] not in _busy and r["id"] not in pending:
                    trigger = "rattrapage" if first or (now - due) > timedelta(minutes=5) else "planifiée"
                    pending.add(r["id"])
                    t = asyncio.create_task(_scheduled(r, trigger))
                    t.add_done_callback(lambda _t, sid=r["id"]: pending.discard(sid))
            first = False
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("scheduler tick failed")
        _wake.clear()
        try:
            await asyncio.wait_for(_wake.wait(), timeout=SCHED_TICK_S)
        except asyncio.TimeoutError:
            pass


def _ensure_scheduler():
    global _sched_task
    if _sched_task is not None and not _sched_task.done():
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    _sched_task = loop.create_task(_scheduler())


_ensure_scheduler()


@module.on("scheduler.status")
async def scheduler_status(payload: dict):
    _ensure_scheduler()
    return {"running": bool(_sched_task and not _sched_task.done()), "busy": {str(k): v for k, v in _busy.items()}}


@module.on_shutdown
async def _shutdown():
    for ctl in list(_ctls.values()):
        ctl.cancel.set()
    if _sched_task:
        _sched_task.cancel()
