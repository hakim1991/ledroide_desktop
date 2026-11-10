"""Planificateur interne : boucle asyncio, tâches persistées (module.db), historique des exécutions."""
import asyncio
import ctypes
import logging
import os
import re
import subprocess
import sys
import time
import uuid
from datetime import datetime, timedelta
from typing import Callable, Literal

from pydantic import BaseModel, Field, ValidationError

from ledroide_sdk import Module

log = logging.getLogger("ledroide.task-scheduler")

module = Module(__file__)
router = module.router

NOWIN = subprocess.CREATE_NO_WINDOW


# ── System actions (injectable: tests replace the callables) ──────────────────

def _shutdown_pc():
    subprocess.run(["shutdown.exe", "/s", "/t", "0"], creationflags=NOWIN, check=False)


def _restart_pc():
    subprocess.run(["shutdown.exe", "/r", "/t", "0"], creationflags=NOWIN, check=False)


def _sleep_pc():
    ctypes.windll.powrprof.SetSuspendState(False, False, False)


def _lock_pc():
    if not ctypes.windll.user32.LockWorkStation():
        raise OSError("LockWorkStation a échoué")


SYSTEM_ACTIONS: dict[str, Callable[[], None]] = {
    "shutdown": _shutdown_pc, "restart": _restart_pc, "sleep": _sleep_pc, "lock": _lock_pc,
}
SYSTEM_LABELS = {"shutdown": "Extinction", "restart": "Redémarrage", "sleep": "Mise en veille", "lock": "Verrouillage"}


# ── Models ─────────────────────────────────────────────────────────────────

class Trigger(BaseModel):
    type: Literal["once", "interval", "daily", "cron"]
    at: str = ""
    minutes: int = Field(60, ge=1, le=525600)
    time: str = "08:00"
    days: list[int] = [0, 1, 2, 3, 4, 5, 6]
    cron: str = ""


class Action(BaseModel):
    type: Literal["cmd", "powershell", "python", "system"]
    command: str = Field("", max_length=20000)
    system: Literal["shutdown", "restart", "sleep", "lock"] = "lock"
    cwd: str = ""
    timeout: int = Field(300, ge=1, le=86400)
    countdown: int = Field(60, ge=0, le=3600)


class TaskIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    enabled: bool = True
    trigger: Trigger
    action: Action


class SchedError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


# ── Cron ───────────────────────────────────────────────────────────────────

CRON_ALIASES = {"@hourly": "0 * * * *", "@daily": "0 0 * * *", "@midnight": "0 0 * * *",
                "@weekly": "0 0 * * 0", "@monthly": "0 0 1 * *", "@yearly": "0 0 1 1 *", "@annually": "0 0 1 1 *"}
CRON_NAMES = {**{d: str(i) for i, d in enumerate(["sun", "mon", "tue", "wed", "thu", "fri", "sat"])},
              **{m: str(i + 1) for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug",
                                                       "sep", "oct", "nov", "dec"])}}


def _cron_field(spec: str, lo: int, hi: int, label: str) -> set[int]:
    spec = re.sub(r"[a-z]{3}", lambda mm: CRON_NAMES.get(mm.group(0), mm.group(0)), spec.lower())
    values: set[int] = set()
    for part in spec.split(","):
        step = 1
        if "/" in part:
            part, s = part.split("/", 1)
            if not s.isdigit() or int(s) == 0:
                raise SchedError("BAD_CRON", f"Pas invalide dans le champ {label}")
            step = int(s)
        if part == "*":
            a, b = lo, hi
        elif "-" in part:
            x, _, y = part.partition("-")
            if not (x.isdigit() and y.isdigit()):
                raise SchedError("BAD_CRON", f"Intervalle invalide dans le champ {label} : « {part} »")
            a, b = int(x), int(y)
        elif part.isdigit():
            a = int(part)
            b = hi if step > 1 else a
        else:
            raise SchedError("BAD_CRON", f"Valeur invalide dans le champ {label} : « {part} »")
        if not (lo <= a <= hi and lo <= b <= hi and a <= b):
            raise SchedError("BAD_CRON", f"Valeur hors limites dans le champ {label} ({lo}–{hi})")
        values.update(range(a, b + 1, step))
    if label == "jour de la semaine" and 7 in values:
        values.discard(7)
        values.add(0)
    return values


class Cron:
    def __init__(self, expr: str):
        expr = CRON_ALIASES.get(expr.strip().lower(), expr.strip())
        parts = expr.split()
        if len(parts) != 5:
            raise SchedError("BAD_CRON", "Une expression cron comporte 5 champs : minute heure jour mois jour-de-semaine")
        self.minutes = sorted(_cron_field(parts[0], 0, 59, "minute"))
        self.hours = sorted(_cron_field(parts[1], 0, 23, "heure"))
        self.dom = _cron_field(parts[2], 1, 31, "jour du mois")
        self.months = _cron_field(parts[3], 1, 12, "mois")
        self.dow = _cron_field(parts[4], 0, 7, "jour de la semaine")
        self.dom_any = parts[2] == "*"
        self.dow_any = parts[4] == "*"

    def _day_ok(self, d: datetime) -> bool:
        if d.month not in self.months:
            return False
        dom_ok = d.day in self.dom
        dow_ok = (d.weekday() + 1) % 7 in self.dow
        if self.dom_any and self.dow_any:
            return True
        if self.dom_any:
            return dow_ok
        if self.dow_any:
            return dom_ok
        return dom_ok or dow_ok

    def next(self, after: datetime) -> datetime | None:
        t = after.replace(second=0, microsecond=0) + timedelta(minutes=1)
        day = t.replace(hour=0, minute=0)
        for _ in range(366 * 5):
            if self._day_ok(day):
                for h in self.hours:
                    for mi in self.minutes:
                        cand = day.replace(hour=h, minute=mi)
                        if cand >= t:
                            return cand
            day += timedelta(days=1)
        return None


# ── Trigger evaluation ─────────────────────────────────────────────────────

def _parse_hhmm(s: str) -> tuple[int, int]:
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", s.strip())
    if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
        raise SchedError("BAD_TIME", "Heure invalide (format HH:MM)")
    return int(m.group(1)), int(m.group(2))


def _parse_at(s: str) -> datetime:
    try:
        dt = datetime.fromisoformat(s.strip())
    except ValueError:
        raise SchedError("BAD_DATE", "Date/heure invalide")
    return dt.replace(tzinfo=None) if dt.tzinfo is None else dt.astimezone().replace(tzinfo=None)


def validate_trigger(tr: Trigger):
    if tr.type == "once":
        _parse_at(tr.at)
    elif tr.type == "daily":
        _parse_hhmm(tr.time)
        if not tr.days or any(d not in range(7) for d in tr.days):
            raise SchedError("BAD_DAYS", "Choisissez au moins un jour")
    elif tr.type == "cron":
        Cron(tr.cron)


def next_run(tr: Trigger, after: datetime) -> datetime | None:
    """Next occurrence strictly after `after` (local naive time)."""
    if tr.type == "once":
        at = _parse_at(tr.at)
        return at if at > after else None
    if tr.type == "interval":
        return after + timedelta(minutes=tr.minutes)
    if tr.type == "daily":
        h, mi = _parse_hhmm(tr.time)
        for i in range(8):
            cand = (after + timedelta(days=i)).replace(hour=h, minute=mi, second=0, microsecond=0)
            if cand > after and cand.weekday() in tr.days:
                return cand
        return None
    return Cron(tr.cron).next(after)


def describe(tr: Trigger) -> str:
    days = ["lun", "mar", "mer", "jeu", "ven", "sam", "dim"]
    if tr.type == "once":
        return "Une fois, le " + _parse_at(tr.at).strftime("%d/%m/%Y à %H:%M")
    if tr.type == "interval":
        m = tr.minutes
        return f"Toutes les {m} min" if m < 60 or m % 60 else f"Toutes les {m // 60} h"
    if tr.type == "daily":
        sel = sorted(set(tr.days))
        when = "tous les jours" if len(sel) == 7 else "en semaine" if sel == [0, 1, 2, 3, 4] else ", ".join(days[d] for d in sel)
        return f"À {tr.time} ({when})"
    return f"Cron : {tr.cron}"


# ── State ──────────────────────────────────────────────────────────────────

_tasks: dict[str, dict] = {}
_next: dict[str, datetime | None] = {}
_running: dict[str, dict] = {}
_countdowns: dict[str, dict] = {}
_exec_tasks: set[asyncio.Task] = set()
_loop_task: asyncio.Task | None = None
_wake: asyncio.Event | None = None
_schema_ready = False
_loaded = False


async def _schema():
    global _schema_ready
    if _schema_ready:
        return
    await module.db.executescript(
        "CREATE TABLE IF NOT EXISTS runs (id TEXT PRIMARY KEY, task_id TEXT NOT NULL, task_name TEXT, "
        "trigger TEXT, started_at TEXT NOT NULL, duration_ms INTEGER, status TEXT, code INTEGER, output TEXT);"
        "CREATE INDEX IF NOT EXISTS runs_task ON runs(task_id, started_at);"
    )
    _schema_ready = True


def _model(doc: dict) -> TaskIn:
    return TaskIn.model_validate({k: doc[k] for k in ("name", "enabled", "trigger", "action")})


def _compute_next(doc: dict, now: datetime) -> datetime | None:
    if not doc.get("enabled"):
        return None
    try:
        return next_run(_model(doc).trigger, now)
    except (SchedError, ValidationError):
        return None


async def _load_tasks():
    global _loaded
    _loaded = True
    await _schema()
    now = datetime.now()
    _tasks.clear()
    _next.clear()
    for doc in await module.db.query("tasks"):
        _tasks[doc["_id"]] = doc
        _next[doc["_id"]] = _compute_next(doc, now)


def _poke():
    if _wake is not None:
        _wake.set()


async def _loop():
    global _wake
    _wake = asyncio.Event()
    if not _loaded:
        await _load_tasks()
    log.info("scheduler started with %d task(s)", len(_tasks))
    while True:
        now = datetime.now()
        for tid, due in list(_next.items()):
            if due is None or due > now:
                continue
            doc = _tasks.get(tid)
            if not doc:
                _next.pop(tid, None)
                continue
            task = _model(doc)
            if task.trigger.type == "interval":
                nxt = due + timedelta(minutes=task.trigger.minutes)
                if nxt <= now:
                    nxt = now + timedelta(minutes=task.trigger.minutes)
            else:
                nxt = next_run(task.trigger, now)
            _next[tid] = nxt
            if tid in _running:
                log.info("task %s still running, scheduled run skipped", tid)
            else:
                _spawn(doc, "planifiée")
            if task.trigger.type == "once":
                doc = {**doc, "enabled": False}
                _tasks[tid] = await module.db.save("tasks", doc)
                _next[tid] = None
                await module.events.emit("tasks:changed", {"id": tid})
        upcoming = [d for d in _next.values() if d is not None]
        wait = min(((d - datetime.now()).total_seconds() for d in upcoming), default=30.0)
        _wake.clear()
        try:
            await asyncio.wait_for(_wake.wait(), timeout=max(0.05, min(wait, 30.0)))
        except asyncio.TimeoutError:
            pass


def _ensure_started():
    global _loop_task
    if _loop_task is not None and not _loop_task.done():
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    _loop_task = loop.create_task(_loop())
    _loop_task.add_done_callback(_loop_done)


def _loop_done(t: asyncio.Task):
    if not t.cancelled() and t.exception():
        log.error("scheduler loop stopped: %s", t.exception())


_ensure_started()


# ── Execution ──────────────────────────────────────────────────────────────

def _oem_decode(raw: bytes) -> str:
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        try:
            return raw.decode(f"cp{ctypes.windll.kernel32.GetOEMCP()}", "replace")
        except LookupError:
            return raw.decode("latin-1")


def _truncate(text: str) -> str:
    limit = int(module.config.get("output_max_kb", 16)) * 1024
    if len(text) <= limit:
        return text
    head = limit * 2 // 3
    return text[:head] + f"\n… [{len(text) - limit} caractères tronqués] …\n" + text[-(limit - head):]


def _command_args(a: Action) -> list[str]:
    cmd = a.command.strip()
    if not cmd:
        raise SchedError("EMPTY_COMMAND", "La commande est vide")
    if a.type == "cmd":
        return ["cmd.exe", "/d", "/s", "/c", cmd]
    if a.type == "powershell":
        return ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", cmd]
    path = os.path.expandvars(cmd.strip('"'))
    if "\n" not in cmd and path.lower().endswith(".py") and os.path.isfile(path):
        return [sys.executable, path]
    return [sys.executable, "-c", cmd]


def _kill_tree(pid: int):
    subprocess.run(["taskkill.exe", "/T", "/F", "/PID", str(pid)], creationflags=NOWIN, capture_output=True, check=False)


def _run_process(a: Action, slot: dict) -> tuple[str, int | None, str]:
    args = _command_args(a)
    cwd = os.path.expandvars(a.cwd.strip()) or None
    if cwd and not os.path.isdir(cwd):
        return "error", None, f"Dossier de travail introuvable : {cwd}"
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}
    try:
        proc = subprocess.Popen(args, cwd=cwd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, creationflags=NOWIN, env=env)
    except OSError as exc:
        return "error", None, f"Lancement impossible : {exc}"
    slot["pid"] = proc.pid
    try:
        out, _ = proc.communicate(timeout=a.timeout)
        status = "stopped" if slot.get("stopped") else ("ok" if proc.returncode == 0 else "failed")
        return status, proc.returncode, _oem_decode(out or b"")
    except subprocess.TimeoutExpired:
        _kill_tree(proc.pid)
        try:
            out, _ = proc.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            out = b""
        text = _oem_decode(out or b"")
        return "timeout", proc.returncode, text + f"\n[Délai de {a.timeout} s dépassé : processus arrêté]"


async def _system_action(doc: dict, a: Action, run_id: str) -> tuple[str, int | None, str]:
    label = SYSTEM_LABELS[a.system]
    ev = asyncio.Event()
    info = {"run_id": run_id, "task_id": doc["_id"], "name": doc["name"], "action": a.system, "label": label,
            "seconds": a.countdown, "deadline": time.time() + a.countdown}
    _countdowns[run_id] = {"event": ev, "info": info}
    await module.events.emit("countdown", info)
    try:
        try:
            await asyncio.wait_for(ev.wait(), timeout=a.countdown)
            cancelled = True
        except asyncio.TimeoutError:
            cancelled = False
    finally:
        _countdowns.pop(run_id, None)
    await module.events.emit("countdown:end", {"run_id": run_id, "cancelled": cancelled})
    if cancelled:
        return "cancelled", None, f"{label} annulé(e) pendant le compte à rebours"
    fn = SYSTEM_ACTIONS[a.system]
    log.info("system action %s", a.system)
    await asyncio.to_thread(fn)
    return "ok", 0, f"{label} déclenché(e)"


async def _execute(doc: dict, trigger: str):
    tid = doc["_id"]
    run_id = uuid.uuid4().hex
    task = _model(doc)
    started = datetime.now()
    t0 = time.perf_counter()
    slot = {"run_id": run_id, "started": started.isoformat(timespec="seconds")}
    _running[tid] = slot
    await module.events.emit("run:start", {"task_id": tid, "run_id": run_id, "name": doc["name"]})
    try:
        if task.action.type == "system":
            status, code, output = await _system_action(doc, task.action, run_id)
        else:
            status, code, output = await asyncio.to_thread(_run_process, task.action, slot)
    except SchedError as exc:
        status, code, output = "error", None, exc.message
    except asyncio.CancelledError:
        status, code, output = "cancelled", None, "Interrompue : arrêt du module"
        if slot.get("pid"):
            await asyncio.to_thread(_kill_tree, slot["pid"])
        _running.pop(tid, None)
        await _record(run_id, doc, trigger, started, t0, status, code, output)
        raise
    except Exception as exc:
        log.exception("task %s failed", tid)
        status, code, output = "error", None, str(exc)
    _running.pop(tid, None)
    await _record(run_id, doc, trigger, started, t0, status, code, output)
    log.info("task %s finished: %s (%s)", tid, status, code)
    await module.events.emit("run:end", {"task_id": tid, "run_id": run_id, "status": status, "code": code,
                                         "name": doc["name"]})


async def _record(run_id, doc, trigger, started, t0, status, code, output):
    await _schema()
    await module.db.execute(
        "INSERT INTO runs(id, task_id, task_name, trigger, started_at, duration_ms, status, code, output) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (run_id, doc["_id"], doc["name"], trigger, started.isoformat(timespec="seconds"),
         int((time.perf_counter() - t0) * 1000), status, code, _truncate(output.strip())),
    )
    keep = int(module.config.get("history_per_task", 50))
    await module.db.execute(
        "DELETE FROM runs WHERE task_id = ? AND id NOT IN "
        "(SELECT id FROM runs WHERE task_id = ? ORDER BY started_at DESC, rowid DESC LIMIT ?)",
        (doc["_id"], doc["_id"], keep),
    )


def _spawn(doc: dict, trigger: str) -> None:
    t = asyncio.get_running_loop().create_task(_execute(doc, trigger))
    _exec_tasks.add(t)
    t.add_done_callback(_exec_tasks.discard)


# ── Handlers ───────────────────────────────────────────────────────────────

def _err(exc: SchedError) -> dict:
    return {"error": exc.message, "code": exc.code}


def _validation_message(exc: ValidationError) -> str:
    first = exc.errors()[0]
    field = ".".join(str(x) for x in first["loc"])
    labels = {"name": "Nom", "action.command": "Commande", "action.timeout": "Délai maximum",
              "trigger.minutes": "Intervalle", "action.countdown": "Compte à rebours"}
    return f"{labels.get(field, field)} : {first['msg']}"


async def _ready():
    if not _loaded:
        await _load_tasks()
    _ensure_started()


def _view(doc: dict) -> dict:
    nxt = _next.get(doc["_id"])
    try:
        desc = describe(_model(doc).trigger)
    except (SchedError, ValidationError):
        desc = "?"
    return {**doc, "next_run": nxt.isoformat(timespec="seconds") if nxt else None, "trigger_text": desc,
            "running": doc["_id"] in _running}


@module.on("list")
async def list_tasks(payload: dict):
    await _ready()
    last =await module.db.fetch_all(
        "SELECT r.task_id, r.status, r.code, r.started_at, r.duration_ms FROM runs r "
        "JOIN (SELECT task_id, MAX(started_at) m FROM runs GROUP BY task_id) x "
        "ON x.task_id = r.task_id AND x.m = r.started_at")
    last_by = {r["task_id"]: r for r in last}
    items = [{**_view(d), "last_run": last_by.get(tid)} for tid, d in _tasks.items()]
    items.sort(key=lambda d: d["name"].lower())
    return {"items": items, "now": datetime.now().isoformat(timespec="seconds"),
            "countdowns": [c["info"] for c in _countdowns.values()], "scheduler_running": bool(_loop_task and not _loop_task.done())}


@module.on("save")
async def save(payload: dict):
    await _ready()
    try:
        task = TaskIn.model_validate(payload.get("task") or {})
        validate_trigger(task.trigger)
        if task.action.type != "system" and not task.action.command.strip():
            raise SchedError("EMPTY_COMMAND", "La commande est obligatoire")
    except ValidationError as exc:
        return {"error": _validation_message(exc), "code": "BAD_TASK"}
    except SchedError as exc:
        return _err(exc)
    doc = task.model_dump()
    doc["name"] = doc["name"].strip()
    tid = str(payload.get("id") or "")
    if tid:
        if tid not in _tasks:
            return {"error": "Tâche introuvable", "code": "NOT_FOUND"}
        doc["_id"] = tid
        doc["created"] = _tasks[tid].get("created")
    else:
        doc["created"] = datetime.now().isoformat(timespec="seconds")
    saved = await module.db.save("tasks", doc)
    _tasks[saved["_id"]] = saved
    _next[saved["_id"]] = _compute_next(saved, datetime.now())
    _poke()
    warning = None
    if saved["enabled"] and _next[saved["_id"]] is None:
        warning = "Aucune exécution à venir (date passée ?)"
    return {**_view(saved), "warning": warning}


@module.on("delete")
async def delete(payload: dict):
    await _ready()
    tid = str(payload.get("id") or "")
    if tid not in _tasks:
        return {"error": "Tâche introuvable", "code": "NOT_FOUND"}
    await module.db.delete("tasks", tid)
    _tasks.pop(tid, None)
    _next.pop(tid, None)
    if payload.get("history", True):
        await module.db.execute("DELETE FROM runs WHERE task_id = ?", (tid,))
    _poke()
    return {"ok": True}


@module.on("toggle")
async def toggle(payload: dict):
    await _ready()
    tid = str(payload.get("id") or "")
    doc = _tasks.get(tid)
    if not doc:
        return {"error": "Tâche introuvable", "code": "NOT_FOUND"}
    doc = await module.db.save("tasks", {**doc, "enabled": bool(payload.get("enabled"))})
    _tasks[tid] = doc
    _next[tid] = _compute_next(doc, datetime.now())
    _poke()
    return _view(doc)


@module.on("run_now")
async def run_now(payload: dict):
    await _ready()
    tid = str(payload.get("id") or "")
    doc = _tasks.get(tid)
    if not doc:
        return {"error": "Tâche introuvable", "code": "NOT_FOUND"}
    if tid in _running:
        return {"error": "Cette tâche est déjà en cours d'exécution", "code": "ALREADY_RUNNING"}
    _spawn(doc, "manuelle")
    return {"ok": True}


@module.on("stop")
async def stop(payload: dict):
    tid = str(payload.get("id") or "")
    slot = _running.get(tid)
    if not slot:
        return {"error": "Cette tâche n'est pas en cours", "code": "NOT_RUNNING"}
    for cd in _countdowns.values():
        if cd["info"]["task_id"] == tid:
            cd["event"].set()
            return {"ok": True}
    if not slot.get("pid"):
        return {"error": "Processus pas encore démarré, réessayez", "code": "NOT_READY"}
    slot["stopped"] = True
    await asyncio.to_thread(_kill_tree, slot["pid"])
    return {"ok": True}


@module.on("cancel_countdown")
async def cancel_countdown(payload: dict):
    cd = _countdowns.get(str(payload.get("run_id") or ""))
    if not cd:
        return {"error": "Compte à rebours introuvable (déjà terminé ?)", "code": "NOT_FOUND"}
    cd["event"].set()
    return {"ok": True}


@module.on("countdowns")
async def countdowns(payload: dict):
    return {"items": [c["info"] for c in _countdowns.values()]}


@module.on("history")
async def history(payload: dict):
    await _ready()
    limit = max(1, min(int(payload.get("limit") or 200), 1000))
    tid = payload.get("task_id")
    if tid:
        rows = await module.db.fetch_all(
            "SELECT * FROM runs WHERE task_id = ? ORDER BY started_at DESC, rowid DESC LIMIT ?", (str(tid), limit))
    else:
        rows = await module.db.fetch_all("SELECT * FROM runs ORDER BY started_at DESC, rowid DESC LIMIT ?", (limit,))
    return {"items": rows}


@module.on("clear_history")
async def clear_history(payload: dict):
    await _ready()
    tid = payload.get("task_id")
    if tid:
        n = await module.db.execute("DELETE FROM runs WHERE task_id = ?", (str(tid),))
    else:
        n = await module.db.execute("DELETE FROM runs")
    return {"ok": True, "deleted": n}


@module.on("preview")
async def preview(payload: dict):
    try:
        tr = Trigger.model_validate(payload.get("trigger") or {})
        validate_trigger(tr)
    except ValidationError as exc:
        return {"error": _validation_message(exc), "code": "BAD_TRIGGER"}
    except SchedError as exc:
        return _err(exc)
    out, cur = [], datetime.now()
    count = max(1, min(int(payload.get("count") or 5), 20))
    for _ in range(count):
        cur = next_run(tr, cur)
        if cur is None:
            break
        out.append(cur.isoformat(timespec="seconds"))
        if tr.type == "once":
            break
    return {"items": out, "text": describe(tr)}


@module.on_shutdown
async def _on_shutdown():
    global _loop_task
    if _loop_task is not None:
        _loop_task.cancel()
        _loop_task = None
    for t in list(_exec_tasks):
        t.cancel()
    if _exec_tasks:
        await asyncio.wait(list(_exec_tasks), timeout=5)
    _countdowns.clear()
