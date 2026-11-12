import asyncio
import logging
import re
from datetime import date, datetime, timedelta, timezone
from typing import Optional

from pydantic import BaseModel, Field, ValidationError, field_validator

from ledroide_sdk import Module

log = logging.getLogger("ledroide.gantt-planner")

module = Module(__file__)
router = module.router

EXPORT_FORMAT = "ledroide-gantt-planner"
COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
MAX_SCHEDULE_PASSES = 2000

_ready = False
_lock = asyncio.Lock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    name         TEXT    NOT NULL,
    color        TEXT    NOT NULL DEFAULT '#6366f1',
    auto_schedule INTEGER NOT NULL DEFAULT 1,
    position     INTEGER NOT NULL DEFAULT 0,
    created_at   TEXT    NOT NULL
);
CREATE TABLE IF NOT EXISTS tasks (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id  INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    parent_id   INTEGER REFERENCES tasks(id) ON DELETE CASCADE,
    name        TEXT    NOT NULL,
    start       TEXT    NOT NULL,
    end         TEXT    NOT NULL,
    progress    INTEGER NOT NULL DEFAULT 0,
    milestone   INTEGER NOT NULL DEFAULT 0,
    color       TEXT    NOT NULL DEFAULT '',
    assignee    TEXT    NOT NULL DEFAULT '',
    notes       TEXT    NOT NULL DEFAULT '',
    position    INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS deps (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id  INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    from_id     INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    to_id       INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    lag         INTEGER NOT NULL DEFAULT 0,
    UNIQUE(from_id, to_id)
);
CREATE INDEX IF NOT EXISTS idx_tasks_project ON tasks(project_id);
CREATE INDEX IF NOT EXISTS idx_deps_project ON deps(project_id);
"""


async def _db():
    global _ready
    if not _ready:
        async with _lock:
            if not _ready:
                await module.db.executescript(SCHEMA)
                _ready = True
    await module.db.execute("PRAGMA foreign_keys = ON")
    return module.db


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _err(message: str, code: str = "BAD_REQUEST") -> dict:
    return {"error": message, "code": code}


def _validation_message(exc: ValidationError) -> str:
    first = exc.errors()[0]
    loc = ".".join(str(p) for p in first.get("loc", ()))
    msg = str(first.get("msg", "Données invalides")).removeprefix("Value error, ")
    return f"{loc} : {msg}" if loc else msg


def _d(s: str) -> date:
    return date.fromisoformat(s)


# ── Models ────────────────────────────────────────────────────────────────────

class ProjectIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    color: str = "#6366f1"
    auto_schedule: bool = True

    @field_validator("name")
    @classmethod
    def _strip(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("Le nom est requis")
        return v

    @field_validator("color")
    @classmethod
    def _color(cls, v: str) -> str:
        if not COLOR_RE.match(v):
            raise ValueError("Couleur invalide (#rrggbb)")
        return v


class TaskIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    start: str
    end: str
    progress: int = Field(0, ge=0, le=100)
    milestone: bool = False
    color: str = ""
    assignee: str = Field("", max_length=100)
    notes: str = Field("", max_length=10000)
    parent_id: Optional[int] = None

    @field_validator("name", "assignee")
    @classmethod
    def _strip(cls, v: str) -> str:
        return v.strip()

    @field_validator("start", "end")
    @classmethod
    def _date(cls, v: str) -> str:
        try:
            return date.fromisoformat(v).isoformat()
        except (TypeError, ValueError):
            raise ValueError("Date invalide (AAAA-MM-JJ)")

    @field_validator("color")
    @classmethod
    def _color(cls, v: str) -> str:
        if v and not COLOR_RE.match(v):
            raise ValueError("Couleur invalide (#rrggbb)")
        return v


def _check_task(t: TaskIn) -> Optional[str]:
    if not t.name:
        return "Le nom de la tâche est requis"
    if t.milestone:
        t.end = t.start
    if _d(t.end) < _d(t.start):
        return "La date de fin précède la date de début"
    return None


# ── Scheduling ────────────────────────────────────────────────────────────────

def _children_map(tasks: dict[int, dict]) -> dict[int, list[int]]:
    children: dict[int, list[int]] = {}
    for t in tasks.values():
        if t["parent_id"] in tasks:
            children.setdefault(t["parent_id"], []).append(t["id"])
    return children


def _leaves(tid: int, children: dict[int, list[int]]) -> list[int]:
    out, stack = [], [tid]
    while stack:
        cur = stack.pop()
        kids = children.get(cur)
        if kids:
            stack.extend(kids)
        else:
            out.append(cur)
    return out


def _ancestors(tid: int, tasks: dict[int, dict]) -> set[int]:
    out, cur = set(), tasks[tid]["parent_id"]
    while cur in tasks and cur not in out:
        out.add(cur)
        cur = tasks[cur]["parent_id"]
    return out


def _span(tid: int, tasks: dict[int, dict], children: dict[int, list[int]]) -> tuple[date, date]:
    leaves = _leaves(tid, children)
    return (min(_d(tasks[i]["start"]) for i in leaves), max(_d(tasks[i]["end"]) for i in leaves))


def _schedule(tasks: dict[int, dict], deps: list[dict]) -> set[int]:
    """Pushes successors so they start after their predecessor ends (+ lag). Returns moved leaf ids."""
    children = _children_map(tasks)
    moved: set[int] = set()
    for _ in range(MAX_SCHEDULE_PASSES):
        changed = False
        for dep in deps:
            a, b = dep["from_id"], dep["to_id"]
            if a not in tasks or b not in tasks:
                continue
            _, end_a = _span(a, tasks, children)
            start_b, _ = _span(b, tasks, children)
            gap = 0 if tasks[a]["milestone"] else 1
            earliest = end_a + timedelta(days=gap + dep.get("lag", 0))
            if start_b < earliest:
                delta = earliest - start_b
                for leaf in _leaves(b, children):
                    t = tasks[leaf]
                    t["start"] = (_d(t["start"]) + delta).isoformat()
                    t["end"] = (_d(t["end"]) + delta).isoformat()
                    moved.add(leaf)
                changed = True
        if not changed:
            break
    return moved


async def _load(project_id: int) -> tuple[dict[int, dict], list[dict]]:
    db = await _db()
    rows = await db.fetch_all("SELECT * FROM tasks WHERE project_id=? ORDER BY position, id", (project_id,))
    deps = await db.fetch_all("SELECT * FROM deps WHERE project_id=?", (project_id,))
    return {r["id"]: r for r in rows}, deps


async def _apply_schedule(project_id: int) -> int:
    db = await _db()
    proj = await db.fetch_one("SELECT auto_schedule FROM projects WHERE id=?", (project_id,))
    if not proj or not proj["auto_schedule"]:
        return 0
    tasks, deps = await _load(project_id)
    moved = _schedule(tasks, deps)
    for tid in moved:
        await db.execute("UPDATE tasks SET start=?, end=? WHERE id=?",
                         (tasks[tid]["start"], tasks[tid]["end"], tid))
    return len(moved)


def _would_cycle(tasks: dict[int, dict], deps: list[dict], a: int, b: int) -> bool:
    # Edges = "moving X may move Y": a dep pushes its target and the target's subtree; a child resizes its parent.
    children = _children_map(tasks)

    def subtree(tid: int) -> list[int]:
        out, stack = [], [tid]
        while stack:
            cur = stack.pop()
            out.append(cur)
            stack.extend(children.get(cur, []))
        return out

    graph: dict[int, list[int]] = {}
    for d in deps:
        if d["to_id"] in tasks:
            graph.setdefault(d["from_id"], []).extend(subtree(d["to_id"]))
    for tid in tasks:
        p = tasks[tid]["parent_id"]
        if p in tasks:
            graph.setdefault(tid, []).append(p)
    seen, stack = set(), subtree(b)
    while stack:
        cur = stack.pop()
        if cur == a:
            return True
        if cur in seen:
            continue
        seen.add(cur)
        stack.extend(graph.get(cur, []))
    return False


def _project_view(project: dict, tasks: dict[int, dict], deps: list[dict]) -> dict:
    children = _children_map(tasks)
    out = []
    for t in tasks.values():
        item = {**t, "milestone": bool(t["milestone"]), "summary": t["id"] in children}
        if item["summary"]:
            leaves = _leaves(t["id"], children)
            s, e = _span(t["id"], tasks, children)
            item["start"], item["end"] = s.isoformat(), e.isoformat()
            weights = [(max(1, (_d(tasks[i]["end"]) - _d(tasks[i]["start"])).days + 1), tasks[i]["progress"]) for i in leaves]
            total = sum(w for w, _ in weights)
            item["progress"] = round(sum(w * p for w, p in weights) / total) if total else 0
        out.append(item)
    return {"project": {**project, "auto_schedule": bool(project["auto_schedule"])},
            "tasks": out,
            "deps": [{"id": d["id"], "from_id": d["from_id"], "to_id": d["to_id"], "lag": d["lag"]} for d in deps]}


async def _view(project_id: int) -> dict:
    db = await _db()
    project = await db.fetch_one("SELECT * FROM projects WHERE id=?", (project_id,))
    if not project:
        return _err("Projet introuvable", "NOT_FOUND")
    tasks, deps = await _load(project_id)
    return _project_view(project, tasks, deps)


async def _task_project(task_id) -> Optional[dict]:
    db = await _db()
    return await db.fetch_one("SELECT * FROM tasks WHERE id=?", (int(task_id),))


# ── Projects ──────────────────────────────────────────────────────────────────

@module.on("projects.list")
async def projects_list(payload: dict):
    db = await _db()
    rows = await db.fetch_all("""
        SELECT p.*, COUNT(t.id) AS task_count, MIN(t.start) AS start, MAX(t.end) AS end
        FROM projects p LEFT JOIN tasks t ON t.project_id = p.id
        GROUP BY p.id ORDER BY p.position, p.id
    """)
    return {"projects": [{**r, "auto_schedule": bool(r["auto_schedule"])} for r in rows]}


@module.on("project.save")
async def project_save(payload: dict):
    try:
        p = ProjectIn.model_validate(payload.get("project") or {})
    except ValidationError as exc:
        return _err(_validation_message(exc))
    db = await _db()
    pid = payload.get("id")
    if pid:
        n = await db.execute("UPDATE projects SET name=?, color=?, auto_schedule=? WHERE id=?",
                             (p.name, p.color, int(p.auto_schedule), int(pid)))
        if not n:
            return _err("Projet introuvable", "NOT_FOUND")
        await _apply_schedule(int(pid))
        return await _view(int(pid))
    pos = (await db.fetch_one("SELECT COALESCE(MAX(position)+1, 0) AS p FROM projects"))["p"]
    row = await db.fetch_one(
        "INSERT INTO projects (name, color, auto_schedule, position, created_at) VALUES (?,?,?,?,?) RETURNING id",
        (p.name, p.color, int(p.auto_schedule), pos, _now()))
    return await _view(row["id"])


@module.on("project.get")
async def project_get(payload: dict):
    return await _view(int(payload.get("id") or 0))


@module.on("project.delete")
async def project_delete(payload: dict):
    db = await _db()
    pid = int(payload.get("id") or 0)
    await db.execute("DELETE FROM deps WHERE project_id=?", (pid,))
    await db.execute("DELETE FROM tasks WHERE project_id=?", (pid,))
    if not await db.execute("DELETE FROM projects WHERE id=?", (pid,)):
        return _err("Projet introuvable", "NOT_FOUND")
    return {"ok": True}


# ── Tasks ─────────────────────────────────────────────────────────────────────

async def _check_parent(project_id: int, task_id: Optional[int], parent_id: Optional[int]) -> Optional[str]:
    if parent_id is None:
        return None
    tasks, _ = await _load(project_id)
    if parent_id not in tasks:
        return "Tâche parente introuvable"
    if task_id is not None:
        if parent_id == task_id or task_id in _ancestors(parent_id, tasks):
            return "Une tâche ne peut pas être sa propre sous-tâche"
    if tasks[parent_id]["milestone"]:
        return "Un jalon ne peut pas contenir de sous-tâches"
    return None


@module.on("task.save")
async def task_save(payload: dict):
    try:
        t = TaskIn.model_validate(payload.get("task") or {})
    except ValidationError as exc:
        return _err(_validation_message(exc))
    if msg := _check_task(t):
        return _err(msg)
    db = await _db()
    tid = payload.get("id")
    if tid:
        old = await _task_project(tid)
        if not old:
            return _err("Tâche introuvable", "NOT_FOUND")
        pid = old["project_id"]
        if msg := await _check_parent(pid, old["id"], t.parent_id):
            return _err(msg)
        if t.parent_id != old["parent_id"]:
            pos = (await db.fetch_one("SELECT COALESCE(MAX(position)+1, 0) AS p FROM tasks WHERE project_id=?", (pid,)))["p"]
        else:
            pos = old["position"]
        await db.execute(
            "UPDATE tasks SET name=?, start=?, end=?, progress=?, milestone=?, color=?, assignee=?, notes=?, parent_id=?, position=? WHERE id=?",
            (t.name, t.start, t.end, t.progress, int(t.milestone), t.color, t.assignee, t.notes, t.parent_id, pos, old["id"]))
    else:
        pid = int(payload.get("project_id") or 0)
        if not await db.fetch_one("SELECT 1 AS x FROM projects WHERE id=?", (pid,)):
            return _err("Projet introuvable", "NOT_FOUND")
        if msg := await _check_parent(pid, None, t.parent_id):
            return _err(msg)
        after = payload.get("after_id")
        if after:
            ref = await db.fetch_one("SELECT position FROM tasks WHERE id=? AND project_id=?", (int(after), pid))
            pos = ref["position"] + 1 if ref else 0
            await db.execute("UPDATE tasks SET position = position + 1 WHERE project_id=? AND position >= ?", (pid, pos))
        else:
            pos = (await db.fetch_one("SELECT COALESCE(MAX(position)+1, 0) AS p FROM tasks WHERE project_id=?", (pid,)))["p"]
        await db.execute(
            "INSERT INTO tasks (project_id, parent_id, name, start, end, progress, milestone, color, assignee, notes, position)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (pid, t.parent_id, t.name, t.start, t.end, t.progress, int(t.milestone), t.color, t.assignee, t.notes, pos))
    await _apply_schedule(pid)
    return await _view(pid)


@module.on("task.shift")
async def task_shift(payload: dict):
    """Moves/resizes a bar (drag in the chart). For a summary task, shifts all its leaves."""
    old = await _task_project(payload.get("id") or 0)
    if not old:
        return _err("Tâche introuvable", "NOT_FOUND")
    pid = old["project_id"]
    try:
        start = _d(payload["start"])
        end = _d(payload["end"])
    except (KeyError, TypeError, ValueError):
        return _err("Date invalide (AAAA-MM-JJ)")
    if end < start:
        return _err("La date de fin précède la date de début")
    db = await _db()
    tasks, _ = await _load(pid)
    children = _children_map(tasks)
    if old["id"] in children:
        cur_start, _ = _span(old["id"], tasks, children)
        delta = start - cur_start
        for leaf in _leaves(old["id"], children):
            lt = tasks[leaf]
            await db.execute("UPDATE tasks SET start=?, end=? WHERE id=?",
                             ((_d(lt["start"]) + delta).isoformat(), (_d(lt["end"]) + delta).isoformat(), leaf))
    else:
        if old["milestone"]:
            end = start
        await db.execute("UPDATE tasks SET start=?, end=? WHERE id=?", (start.isoformat(), end.isoformat(), old["id"]))
    await _apply_schedule(pid)
    return await _view(pid)


@module.on("task.progress")
async def task_progress(payload: dict):
    old = await _task_project(payload.get("id") or 0)
    if not old:
        return _err("Tâche introuvable", "NOT_FOUND")
    value = max(0, min(100, int(payload.get("progress") or 0)))
    await (await _db()).execute("UPDATE tasks SET progress=? WHERE id=?", (value, old["id"]))
    return await _view(old["project_id"])


@module.on("task.delete")
async def task_delete(payload: dict):
    old = await _task_project(payload.get("id") or 0)
    if not old:
        return _err("Tâche introuvable", "NOT_FOUND")
    pid = old["project_id"]
    tasks, _ = await _load(pid)
    doomed = [old["id"]] + [t for t in tasks if old["id"] in _ancestors(t, tasks)]
    db = await _db()
    marks = ",".join("?" * len(doomed))
    await db.execute(f"DELETE FROM deps WHERE from_id IN ({marks}) OR to_id IN ({marks})", (*doomed, *doomed))
    await db.execute(f"DELETE FROM tasks WHERE id IN ({marks})", tuple(doomed))
    return await _view(pid)


@module.on("task.reorder")
async def task_reorder(payload: dict):
    """`order` = every task id of the project in display order; `parents` = {id: parent_id|null}."""
    pid = int(payload.get("project_id") or 0)
    order = [int(i) for i in payload.get("order") or []]
    tasks, _ = await _load(pid)
    if set(order) != set(tasks) or len(order) != len(tasks):
        return _err("Ordre de tâches invalide")
    parents = {int(k): (int(v) if v is not None else None) for k, v in (payload.get("parents") or {}).items()}
    for tid, parent in parents.items():
        if tid not in tasks or (parent is not None and parent not in tasks):
            return _err("Tâche parente introuvable")
        tasks[tid]["parent_id"] = parent
    for tid in parents:
        cur, seen = tasks[tid]["parent_id"], {tid}
        while cur is not None:
            if cur in seen:
                return _err("Une tâche ne peut pas être sa propre sous-tâche")
            if tasks[cur]["milestone"]:
                return _err("Un jalon ne peut pas contenir de sous-tâches")
            seen.add(cur)
            cur = tasks[cur]["parent_id"]
    db = await _db()
    for i, tid in enumerate(order):
        await db.execute("UPDATE tasks SET position=?, parent_id=? WHERE id=?", (i, tasks[tid]["parent_id"], tid))
    await _apply_schedule(pid)
    return await _view(pid)


# ── Dependencies ──────────────────────────────────────────────────────────────

@module.on("dep.add")
async def dep_add(payload: dict):
    a, b = int(payload.get("from_id") or 0), int(payload.get("to_id") or 0)
    lag = max(-365, min(365, int(payload.get("lag") or 0)))
    ta = await _task_project(a)
    tb = await _task_project(b)
    if not ta or not tb:
        return _err("Tâche introuvable", "NOT_FOUND")
    pid = ta["project_id"]
    if tb["project_id"] != pid:
        return _err("Les deux tâches doivent appartenir au même projet")
    if a == b:
        return _err("Une tâche ne peut pas dépendre d'elle-même")
    tasks, deps = await _load(pid)
    if a in _ancestors(b, tasks) or b in _ancestors(a, tasks):
        return _err("Pas de dépendance entre une tâche et son groupe")
    if any(d["from_id"] == a and d["to_id"] == b for d in deps):
        return _err("Cette dépendance existe déjà", "CONFLICT")
    if _would_cycle(tasks, deps, a, b):
        return _err("Cette dépendance créerait un cycle", "CYCLE")
    db = await _db()
    await db.execute("INSERT INTO deps (project_id, from_id, to_id, lag) VALUES (?,?,?,?)", (pid, a, b, lag))
    await _apply_schedule(pid)
    return await _view(pid)


@module.on("dep.update")
async def dep_update(payload: dict):
    db = await _db()
    dep = await db.fetch_one("SELECT * FROM deps WHERE id=?", (int(payload.get("id") or 0),))
    if not dep:
        return _err("Dépendance introuvable", "NOT_FOUND")
    lag = max(-365, min(365, int(payload.get("lag") or 0)))
    await db.execute("UPDATE deps SET lag=? WHERE id=?", (lag, dep["id"]))
    await _apply_schedule(dep["project_id"])
    return await _view(dep["project_id"])


@module.on("dep.delete")
async def dep_delete(payload: dict):
    db = await _db()
    dep = await db.fetch_one("SELECT * FROM deps WHERE id=?", (int(payload.get("id") or 0),))
    if not dep:
        return _err("Dépendance introuvable", "NOT_FOUND")
    await db.execute("DELETE FROM deps WHERE id=?", (dep["id"],))
    return await _view(dep["project_id"])


# ── Export / import ───────────────────────────────────────────────────────────

@module.on("project.export")
async def project_export(payload: dict):
    view = await _view(int(payload.get("id") or 0))
    if "error" in view:
        return view
    p = view["project"]
    index = {t["id"]: i for i, t in enumerate(view["tasks"])}
    return {
        "format": EXPORT_FORMAT, "version": 1,
        "project": {"name": p["name"], "color": p["color"], "auto_schedule": p["auto_schedule"]},
        "tasks": [{"ref": index[t["id"]], "parent": index.get(t["parent_id"]), "name": t["name"],
                   "start": t["start"], "end": t["end"], "progress": t["progress"], "milestone": t["milestone"],
                   "color": t["color"], "assignee": t["assignee"], "notes": t["notes"]} for t in view["tasks"]],
        "deps": [{"from": index[d["from_id"]], "to": index[d["to_id"]], "lag": d["lag"]} for d in view["deps"]],
    }


@module.on("project.import")
async def project_import(payload: dict):
    data = payload.get("data") or {}
    if data.get("format") != EXPORT_FORMAT:
        return _err("Fichier non reconnu (export Planning Gantt attendu)")
    try:
        p = ProjectIn.model_validate(data.get("project") or {})
        items = [(int(raw.get("ref", i)), raw.get("parent"), TaskIn.model_validate(raw))
                 for i, raw in enumerate(data.get("tasks") or [])]
    except (ValidationError, TypeError, ValueError, AttributeError) as exc:
        return _err(_validation_message(exc) if isinstance(exc, ValidationError) else "Fichier invalide")
    for _, _, t in items:
        if msg := _check_task(t):
            return _err(f"{t.name} : {msg}")
    db = await _db()
    pos = (await db.fetch_one("SELECT COALESCE(MAX(position)+1, 0) AS p FROM projects"))["p"]
    pid = (await db.fetch_one(
        "INSERT INTO projects (name, color, auto_schedule, position, created_at) VALUES (?,?,?,?,?) RETURNING id",
        (p.name, p.color, int(p.auto_schedule), pos, _now())))["id"]
    ids: dict[int, int] = {}
    for i, (ref, _, t) in enumerate(items):
        ids[ref] = (await db.fetch_one(
            "INSERT INTO tasks (project_id, name, start, end, progress, milestone, color, assignee, notes, position)"
            " VALUES (?,?,?,?,?,?,?,?,?,?) RETURNING id",
            (pid, t.name, t.start, t.end, t.progress, int(t.milestone), t.color, t.assignee, t.notes, i)))["id"]
    for ref, parent, _ in items:
        if parent is not None and int(parent) in ids and int(parent) != ref:
            await db.execute("UPDATE tasks SET parent_id=? WHERE id=?", (ids[int(parent)], ids[ref]))
    tasks, deps = await _load(pid)
    for raw in data.get("deps") or []:
        try:
            a, b = ids[int(raw["from"])], ids[int(raw["to"])]
        except (KeyError, TypeError, ValueError):
            continue
        if a == b or any(d["from_id"] == a and d["to_id"] == b for d in deps) or _would_cycle(tasks, deps, a, b):
            continue
        lag = max(-365, min(365, int(raw.get("lag") or 0)))
        await db.execute("INSERT INTO deps (project_id, from_id, to_id, lag) VALUES (?,?,?,?)", (pid, a, b, lag))
        deps.append({"from_id": a, "to_id": b, "lag": lag})
    await _apply_schedule(pid)
    return await _view(pid)


@module.on("project.seed")
async def project_seed(payload: dict):
    """Creates a sample project so the chart is not empty on first use."""
    today = date.today()
    monday = today - timedelta(days=today.weekday())
    d = lambda n: (monday + timedelta(days=n)).isoformat()
    data = {
        "format": EXPORT_FORMAT,
        "project": {"name": "Exemple — Site web", "color": "#6366f1", "auto_schedule": True},
        "tasks": [
            {"ref": 0, "name": "Conception", "start": d(0), "end": d(9)},
            {"ref": 1, "parent": 0, "name": "Recueil du besoin", "start": d(0), "end": d(2), "progress": 100, "assignee": "Alice"},
            {"ref": 2, "parent": 0, "name": "Maquettes", "start": d(3), "end": d(9), "progress": 60, "assignee": "Bruno"},
            {"ref": 3, "name": "Validation des maquettes", "start": d(10), "end": d(10), "milestone": True},
            {"ref": 4, "name": "Réalisation", "start": d(11), "end": d(25)},
            {"ref": 5, "parent": 4, "name": "Intégration", "start": d(11), "end": d(18), "progress": 10, "assignee": "Chloé"},
            {"ref": 6, "parent": 4, "name": "Back-office", "start": d(11), "end": d(22), "assignee": "David"},
            {"ref": 7, "parent": 4, "name": "Recette", "start": d(23), "end": d(25), "assignee": "Alice", "color": "#f59e0b"},
            {"ref": 8, "name": "Mise en production", "start": d(26), "end": d(26), "milestone": True},
        ],
        "deps": [{"from": 1, "to": 2}, {"from": 2, "to": 3}, {"from": 3, "to": 5}, {"from": 3, "to": 6},
                 {"from": 5, "to": 7}, {"from": 6, "to": 7}, {"from": 7, "to": 8}],
    }
    return await project_import({"data": data})
