import asyncio
import logging
import re
from datetime import date, datetime, timedelta, timezone
from typing import Literal, Optional

from pydantic import BaseModel, Field, ValidationError, field_validator

from ledroide_sdk import Module

log = logging.getLogger("ledroide.habit-tracker")

module = Module(__file__)
router = module.router

EXPORT_FORMAT = "ledroide-habits"
COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
MAX_VALUE = 100_000
MAX_IMPORT_HABITS = 500

_ready = False
_lock = asyncio.Lock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS habits (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT    NOT NULL,
    icon        TEXT    NOT NULL DEFAULT '✅',
    color       TEXT    NOT NULL DEFAULT '#6366f1',
    kind        TEXT    NOT NULL DEFAULT 'bool',
    target      INTEGER NOT NULL DEFAULT 1,
    unit        TEXT    NOT NULL DEFAULT '',
    schedule    TEXT    NOT NULL DEFAULT 'daily',
    per_week    INTEGER NOT NULL DEFAULT 3,
    days        TEXT    NOT NULL DEFAULT '',
    goal_streak INTEGER NOT NULL DEFAULT 0,
    reminder    TEXT    NOT NULL DEFAULT '',
    description TEXT    NOT NULL DEFAULT '',
    start_date  TEXT    NOT NULL,
    archived    INTEGER NOT NULL DEFAULT 0,
    position    INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT    NOT NULL
);
CREATE TABLE IF NOT EXISTS entries (
    habit_id INTEGER NOT NULL REFERENCES habits(id) ON DELETE CASCADE,
    date     TEXT    NOT NULL,
    value    INTEGER NOT NULL,
    PRIMARY KEY (habit_id, date)
);
CREATE TABLE IF NOT EXISTS day_notes (
    date TEXT PRIMARY KEY,
    note TEXT NOT NULL
);
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


def _int(v, default: int = 0) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _day(v) -> date:
    try:
        return date.fromisoformat(str(v)[:10])
    except (TypeError, ValueError):
        raise ValueError("Date invalide (AAAA-MM-JJ)")


def _today(payload: dict) -> date:
    """The UI sends its local date so the day boundary follows the user's clock."""
    try:
        return _day(payload.get("today")) if payload.get("today") else date.today()
    except ValueError:
        return date.today()


class HabitIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    icon: str = Field("✅", max_length=8)
    color: str = "#6366f1"
    kind: Literal["bool", "qty"] = "bool"
    target: int = Field(1, ge=1, le=MAX_VALUE)
    unit: str = Field("", max_length=20)
    schedule: Literal["daily", "weekly", "days"] = "daily"
    per_week: int = Field(3, ge=1, le=7)
    days: list[int] = Field(default_factory=list)
    goal_streak: int = Field(0, ge=0, le=3650)
    reminder: str = ""
    description: str = Field("", max_length=2000)
    start_date: Optional[str] = None

    @field_validator("name", "unit", "description", "icon")
    @classmethod
    def _strip(cls, v: str) -> str:
        return v.strip()

    @field_validator("color")
    @classmethod
    def _color(cls, v: str) -> str:
        if not COLOR_RE.match(v):
            raise ValueError("Couleur invalide (#rrggbb)")
        return v

    @field_validator("reminder")
    @classmethod
    def _time(cls, v: str) -> str:
        v = (v or "").strip()
        if v and not TIME_RE.match(v):
            raise ValueError("Heure invalide (HH:MM)")
        return v

    @field_validator("days")
    @classmethod
    def _days(cls, v: list[int]) -> list[int]:
        if any(d not in range(7) for d in v):
            raise ValueError("Jours invalides (0 = lundi … 6 = dimanche)")
        return sorted(set(v))

    @field_validator("start_date")
    @classmethod
    def _start(cls, v):
        return _day(v).isoformat() if v else None


def _check(h: HabitIn) -> Optional[str]:
    if not h.name:
        return "Le nom est requis"
    if h.schedule == "days" and not h.days:
        return "Choisissez au moins un jour"
    if h.kind == "bool":
        h.target = 1
        h.unit = ""
    if not h.icon:
        h.icon = "✅"
    return None


# ── Stats ─────────────────────────────────────────────────────────────────────

def _monday(d: date) -> date:
    return d - timedelta(days=d.weekday())


def _scheduled(h: dict, d: date) -> bool:
    if h["schedule"] == "daily":
        return True
    if h["schedule"] == "days":
        return str(d.weekday()) in h["days"].split(",")
    return False


def _stats(h: dict, entries: dict[str, int], today: date) -> dict:
    target = h["target"] if h["kind"] == "qty" else 1
    start = min(date.fromisoformat(h["start_date"]), today)
    done_dates = {d for d, v in entries.items() if v >= target}
    total_done = sum(1 for d in done_dates if d <= today.isoformat())
    out = {"total_done": total_done, "total_value": sum(entries.values()) if h["kind"] == "qty" else total_done}
    recent_from = today - timedelta(days=29)

    if h["schedule"] == "weekly":
        n = h["per_week"]
        week, cur, best, ok_weeks, weeks, recent_ok, recent_weeks = _monday(start), 0, 0, 0, 0, 0, 0
        this_monday = _monday(today)
        while week <= this_monday:
            count = sum(1 for i in range(7) if (week + timedelta(days=i)).isoformat() in done_dates)
            ok = count >= n
            current = week == this_monday
            if ok:
                cur += 1
                best = max(best, cur)
            elif not current:
                cur = 0
            if ok or not current:
                weeks += 1
                ok_weeks += ok
                if week >= _monday(today - timedelta(days=27)):
                    recent_weeks += 1
                    recent_ok += ok
            if current:
                out["week_count"] = count
            week += timedelta(days=7)
        out.update(streak=cur, best_streak=best, streak_unit="semaines",
                   rate_all=round(ok_weeks / weeks, 3) if weeks else None,
                   rate_30=round(recent_ok / recent_weeks, 3) if recent_weeks else None,
                   due_today=out.get("week_count", 0) < n)
    else:
        cur, best, sched, ok, r_sched, r_ok = 0, 0, 0, 0, 0, 0
        d = start
        while d <= today:
            if _scheduled(h, d):
                done = d.isoformat() in done_dates
                if done:
                    cur += 1
                    best = max(best, cur)
                elif d != today:
                    cur = 0
                if done or d != today:
                    sched += 1
                    ok += done
                    if d >= recent_from:
                        r_sched += 1
                        r_ok += done
            d += timedelta(days=1)
        out.update(streak=cur, best_streak=best, streak_unit="jours",
                   rate_all=round(ok / sched, 3) if sched else None,
                   rate_30=round(r_ok / r_sched, 3) if r_sched else None,
                   due_today=_scheduled(h, today))
    out["goal_progress"] = round(min(1.0, out["streak"] / h["goal_streak"]), 3) if h["goal_streak"] else None
    return out


def _public(h: dict) -> dict:
    return {**h, "archived": bool(h["archived"]),
            "days": [int(x) for x in h["days"].split(",") if x != ""]}


async def _entries(habit_ids: list[int], since: Optional[str] = None) -> dict[int, dict[str, int]]:
    if not habit_ids:
        return {}
    marks = ",".join("?" * len(habit_ids))
    sql = f"SELECT habit_id, date, value FROM entries WHERE habit_id IN ({marks})"
    params: list = list(habit_ids)
    if since:
        sql += " AND date >= ?"
        params.append(since)
    out: dict[int, dict[str, int]] = {i: {} for i in habit_ids}
    for r in await (await _db()).fetch_all(sql, tuple(params)):
        out[r["habit_id"]][r["date"]] = r["value"]
    return out


async def _habit(hid) -> Optional[dict]:
    return await (await _db()).fetch_one("SELECT * FROM habits WHERE id=?", (_int(hid),))


async def _full(h: dict, entries: dict[str, int], today: date, view: Optional[date] = None) -> dict:
    week_ago = (min(view or today, today) - timedelta(days=13)).isoformat()
    return {**_public(h), "stats": _stats(h, entries, today),
            "today_value": entries.get(today.isoformat(), 0),
            "recent": {d: v for d, v in entries.items() if d >= week_ago}}


# ── Habits ────────────────────────────────────────────────────────────────────

@module.on("habits.list")
async def habits_list(payload: dict):
    today = _today(payload)
    db = await _db()
    archived = 1 if payload.get("archived") else 0
    rows = await db.fetch_all("SELECT * FROM habits WHERE archived=? ORDER BY position, id", (archived,))
    entries = await _entries([r["id"] for r in rows])
    try:
        view = _day(payload["date"]) if payload.get("date") else today
    except ValueError:
        view = today
    habits = [await _full(r, entries[r["id"]], today, view) for r in rows]
    note = await db.fetch_one("SELECT note FROM day_notes WHERE date=?", (today.isoformat(),))
    archived_count = (await db.fetch_one("SELECT COUNT(*) AS n FROM habits WHERE archived=1"))["n"]
    return {"habits": habits, "today": today.isoformat(), "note": note["note"] if note else "", "archived_count": archived_count}


@module.on("habit.save")
async def habit_save(payload: dict):
    try:
        h = HabitIn.model_validate(payload.get("habit") or {})
    except ValidationError as exc:
        return _err(_validation_message(exc))
    if msg := _check(h):
        return _err(msg)
    today = _today(payload)
    db = await _db()
    hid = _int(payload.get("id"))
    days = ",".join(str(d) for d in h.days)
    vals = (h.name, h.icon, h.color, h.kind, h.target, h.unit, h.schedule, h.per_week, days, h.goal_streak,
            h.reminder, h.description)
    if hid:
        old = await _habit(hid)
        if not old:
            return _err("Habitude introuvable", "NOT_FOUND")
        await db.execute(
            "UPDATE habits SET name=?, icon=?, color=?, kind=?, target=?, unit=?, schedule=?, per_week=?, days=?, goal_streak=?,"
            " reminder=?, description=?, start_date=? WHERE id=?", (*vals, h.start_date or old["start_date"], hid))
        if h.kind == "bool":
            await db.execute("UPDATE entries SET value=1 WHERE habit_id=? AND value > 1", (hid,))
    else:
        pos = (await db.fetch_one("SELECT COALESCE(MAX(position)+1, 0) AS p FROM habits"))["p"]
        hid = (await db.fetch_one(
            "INSERT INTO habits (name, icon, color, kind, target, unit, schedule, per_week, days, goal_streak, reminder, description,"
            " start_date, position, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) RETURNING id",
            (*vals, h.start_date or today.isoformat(), pos, _now())))["id"]
    h_row = await _habit(hid)
    return {"habit": await _full(h_row, (await _entries([hid]))[hid], today)}


@module.on("habit.delete")
async def habit_delete(payload: dict):
    db = await _db()
    hid = _int(payload.get("id"))
    await db.execute("DELETE FROM entries WHERE habit_id=?", (hid,))
    if not await db.execute("DELETE FROM habits WHERE id=?", (hid,)):
        return _err("Habitude introuvable", "NOT_FOUND")
    return {"ok": True}


@module.on("habit.archive")
async def habit_archive(payload: dict):
    db = await _db()
    hid = _int(payload.get("id"))
    archived = 1 if payload.get("archived", True) else 0
    pos = (await db.fetch_one("SELECT COALESCE(MAX(position)+1, 0) AS p FROM habits"))["p"]
    if not await db.execute("UPDATE habits SET archived=?, position=? WHERE id=?", (archived, pos, hid)):
        return _err("Habitude introuvable", "NOT_FOUND")
    return {"ok": True}


@module.on("habit.reorder")
async def habit_reorder(payload: dict):
    db = await _db()
    for i, hid in enumerate(payload.get("order") or []):
        await db.execute("UPDATE habits SET position=? WHERE id=?", (i, _int(hid)))
    return {"ok": True}


# ── Entries ───────────────────────────────────────────────────────────────────

@module.on("entry.set")
async def entry_set(payload: dict):
    """Sets the value of a habit for a day (0 removes it). `delta` adds to the current value instead."""
    h = await _habit(payload.get("habit_id"))
    if not h:
        return _err("Habitude introuvable", "NOT_FOUND")
    today = _today(payload)
    try:
        d = _day(payload.get("date") or today.isoformat())
    except ValueError as exc:
        return _err(str(exc))
    if d > today:
        return _err("Impossible de cocher un jour futur")
    db = await _db()
    cur = await db.fetch_one("SELECT value FROM entries WHERE habit_id=? AND date=?", (h["id"], d.isoformat()))
    cur_v = cur["value"] if cur else 0
    if payload.get("toggle"):
        value = 0 if cur_v >= (h["target"] if h["kind"] == "qty" else 1) else (h["target"] if h["kind"] == "qty" else 1)
    elif payload.get("delta") is not None:
        value = cur_v + _int(payload.get("delta"))
    else:
        value = _int(payload.get("value"), -1)
        if value < 0:
            return _err("Valeur invalide")
    value = max(0, min(MAX_VALUE, value))
    if h["kind"] == "bool":
        value = min(1, value)
    if value and d.isoformat() < h["start_date"]:
        await db.execute("UPDATE habits SET start_date=? WHERE id=?", (d.isoformat(), h["id"]))
        h = await _habit(h["id"])
    if value:
        await db.execute("INSERT OR REPLACE INTO entries (habit_id, date, value) VALUES (?,?,?)", (h["id"], d.isoformat(), value))
    else:
        await db.execute("DELETE FROM entries WHERE habit_id=? AND date=?", (h["id"], d.isoformat()))
    return {"habit": await _full(h, (await _entries([h["id"]]))[h["id"]], today, d), "date": d.isoformat(), "value": value}


@module.on("habit.detail")
async def habit_detail(payload: dict):
    h = await _habit(payload.get("id"))
    if not h:
        return _err("Habitude introuvable", "NOT_FOUND")
    today = _today(payload)
    year = _int(payload.get("year"), today.year)
    entries = (await _entries([h["id"]]))[h["id"]]
    y = str(year)
    year_entries = {d: v for d, v in entries.items() if d.startswith(y)}
    months = {}
    for d, v in entries.items():
        if v >= (h["target"] if h["kind"] == "qty" else 1):
            months[d[:7]] = months.get(d[:7], 0) + 1
    return {"habit": await _full(h, entries, today), "year": year, "entries": year_entries,
            "years": sorted({int(d[:4]) for d in entries} | {today.year, int(h["start_date"][:4])}),
            "monthly_done": months}


@module.on("overview")
async def overview(payload: dict):
    """Year heatmap of all active habits: share of scheduled habits done each day, plus notes."""
    today = _today(payload)
    year = _int(payload.get("year"), today.year)
    db = await _db()
    habits = await db.fetch_all("SELECT * FROM habits WHERE archived=0 ORDER BY position, id")
    entries = await _entries([h["id"] for h in habits], f"{year - 1}-12-25")
    days: dict[str, list[int]] = {}
    d, end = date(year, 1, 1), min(date(year, 12, 31), today)
    while d <= end:
        iso = d.isoformat()
        done = due = 0
        for h in habits:
            if iso < h["start_date"]:
                continue
            target = h["target"] if h["kind"] == "qty" else 1
            hit = entries[h["id"]].get(iso, 0) >= target
            if h["schedule"] == "weekly":
                done += hit
                due += hit
            elif _scheduled(h, d):
                done += hit
                due += 1
        if due:
            days[iso] = [done, due]
        d += timedelta(days=1)
    notes = {r["date"]: r["note"] for r in await db.fetch_all(
        "SELECT date, note FROM day_notes WHERE date BETWEEN ? AND ?", (f"{year}-01-01", f"{year}-12-31"))}
    all_entries = await _entries([h["id"] for h in habits])
    summary = [{"id": h["id"], "name": h["name"], "icon": h["icon"], "color": h["color"],
                "stats": _stats(h, all_entries[h["id"]], today)} for h in habits]
    return {"year": year, "days": days, "notes": notes, "habits": summary}


# ── Notes ─────────────────────────────────────────────────────────────────────

@module.on("note.get")
async def note_get(payload: dict):
    try:
        d = _day(payload.get("date"))
    except ValueError as exc:
        return _err(str(exc))
    row = await (await _db()).fetch_one("SELECT note FROM day_notes WHERE date=?", (d.isoformat(),))
    return {"date": d.isoformat(), "note": row["note"] if row else ""}


@module.on("note.set")
async def note_set(payload: dict):
    try:
        d = _day(payload.get("date"))
    except ValueError as exc:
        return _err(str(exc))
    note = str(payload.get("note") or "")
    if len(note) > 10000:
        return _err("Note trop longue (10 000 caractères max.)")
    db = await _db()
    if note.strip():
        await db.execute("INSERT OR REPLACE INTO day_notes (date, note) VALUES (?,?)", (d.isoformat(), note))
    else:
        await db.execute("DELETE FROM day_notes WHERE date=?", (d.isoformat(),))
    return {"ok": True}


@module.on("notes.list")
async def notes_list(payload: dict):
    rows = await (await _db()).fetch_all("SELECT date, note FROM day_notes ORDER BY date DESC LIMIT ?",
                                         (max(1, min(1000, _int(payload.get("limit"), 100))),))
    return {"notes": rows}


# ── Export / import ───────────────────────────────────────────────────────────

FIELDS = ("name", "icon", "color", "kind", "target", "unit", "schedule", "per_week", "goal_streak", "reminder",
          "description", "start_date")


@module.on("export")
async def export(payload: dict):
    db = await _db()
    habits = await db.fetch_all("SELECT * FROM habits ORDER BY archived, position, id")
    entries = await _entries([h["id"] for h in habits])
    notes = await db.fetch_all("SELECT date, note FROM day_notes ORDER BY date")
    return {
        "format": EXPORT_FORMAT, "version": 1, "exported_at": _now(),
        "habits": [{**{k: h[k] for k in FIELDS}, "days": _public(h)["days"], "archived": bool(h["archived"]),
                    "entries": entries[h["id"]]} for h in habits],
        "notes": {n["date"]: n["note"] for n in notes},
    }


@module.on("import")
async def import_data(payload: dict):
    data = payload.get("data") or {}
    if not isinstance(data, dict) or data.get("format") != EXPORT_FORMAT:
        return _err("Fichier non reconnu (export Habitudes attendu)")
    raw = data.get("habits") or []
    if not isinstance(raw, list) or len(raw) > MAX_IMPORT_HABITS:
        return _err("Liste d'habitudes invalide")
    items = []
    for i, r in enumerate(raw, start=1):
        if not isinstance(r, dict):
            return _err(f"Habitude {i} invalide")
        try:
            h = HabitIn.model_validate({k: r[k] for k in (*FIELDS, "days") if k in r})
        except ValidationError as exc:
            return _err(f"Habitude {i} : {_validation_message(exc)}")
        if msg := _check(h):
            return _err(f"Habitude {i} : {msg}")
        ents = {}
        for d, v in (r.get("entries") or {}).items():
            try:
                ents[_day(d).isoformat()] = max(0, min(MAX_VALUE, int(v)))
            except (ValueError, TypeError):
                continue
        items.append((h, bool(r.get("archived")), ents))
    db = await _db()
    today = date.today().isoformat()
    pos = (await db.fetch_one("SELECT COALESCE(MAX(position)+1, 0) AS p FROM habits"))["p"]
    count = 0
    for i, (h, archived, ents) in enumerate(items):
        hid = (await db.fetch_one(
            "INSERT INTO habits (name, icon, color, kind, target, unit, schedule, per_week, days, goal_streak, reminder, description,"
            " start_date, archived, position, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) RETURNING id",
            (h.name, h.icon, h.color, h.kind, h.target, h.unit, h.schedule, h.per_week, ",".join(map(str, h.days)),
             h.goal_streak, h.reminder, h.description, h.start_date or today, int(archived), pos + i, _now())))["id"]
        for d, v in ents.items():
            if v:
                await db.execute("INSERT OR REPLACE INTO entries (habit_id, date, value) VALUES (?,?,?)",
                                 (hid, d, min(1, v) if h.kind == "bool" else v))
                count += 1
    notes = data.get("notes") or {}
    n_notes = 0
    if isinstance(notes, dict):
        for d, note in notes.items():
            try:
                iso = _day(d).isoformat()
            except ValueError:
                continue
            if isinstance(note, str) and note.strip() and len(note) <= 10000:
                if not await db.fetch_one("SELECT 1 AS x FROM day_notes WHERE date=?", (iso,)):
                    await db.execute("INSERT INTO day_notes (date, note) VALUES (?,?)", (iso, note))
                    n_notes += 1
    log.info("Import : %d habitude(s), %d pointage(s)", len(items), count)
    return {"habits": len(items), "entries": count, "notes": n_notes}


@module.on("demo.seed")
async def demo_seed(payload: dict):
    import random
    rnd = random.Random(7)
    today = _today(payload)
    start = (today - timedelta(days=120)).isoformat()
    samples = [
        {"name": "Boire de l'eau", "icon": "💧", "color": "#38bdf8", "kind": "qty", "target": 8, "unit": "verres", "reminder": "10:00"},
        {"name": "Méditer", "icon": "🧘", "color": "#a855f7", "goal_streak": 30},
        {"name": "Sport", "icon": "🏃", "color": "#22c55e", "schedule": "weekly", "per_week": 3},
        {"name": "Lire 20 pages", "icon": "📚", "color": "#f59e0b", "schedule": "days", "days": [0, 1, 2, 3, 4]},
    ]
    probs = [0.75, 0.8, 0.45, 0.7]
    for s, p in zip(samples, probs):
        r = await habit_save({"habit": {**s, "start_date": start}, "today": today.isoformat()})
        hid = r["habit"]["id"]
        d = today - timedelta(days=120)
        while d < today:
            if rnd.random() < p:
                value = s.get("target", 1) if s.get("kind") != "qty" else rnd.choice([5, 6, 8, 8, 9, 10])
                await (await _db()).execute("INSERT OR REPLACE INTO entries (habit_id, date, value) VALUES (?,?,?)",
                                            (hid, d.isoformat(), value))
            d += timedelta(days=1)
    return await habits_list({"today": today.isoformat()})
