import asyncio
import json
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

from ledroide_sdk.http import JSONResponse
from pydantic import BaseModel, Field
from ledroide_sdk import Module, data_path

module = Module(__file__)
router = module.router

DB_PATH = data_path(__file__, "pomodoro.db")
TYPES = ("work", "short", "long")

DEFAULT_SETTINGS = {
    "work_min": 25, "short_min": 5, "long_min": 15, "cycles": 4,
    "auto_breaks": False, "auto_work": False, "sound": True, "volume": 0.6, "daily_goal": 8,
}


@contextmanager
def _db():
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _init():
    with _db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS sessions (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                day          TEXT    NOT NULL,
                started_at   TEXT    NOT NULL,
                ended_at     TEXT    NOT NULL,
                task         TEXT    NOT NULL DEFAULT '',
                type         TEXT    NOT NULL,
                planned_sec  INTEGER NOT NULL DEFAULT 0,
                duration_sec INTEGER NOT NULL DEFAULT 0,
                completed    INTEGER NOT NULL DEFAULT 1
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_sessions_day ON sessions(day)")
        conn.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)")


_init()


def _parse_local(s: Optional[str]) -> datetime:
    if not s:
        return datetime.now()
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError("Date invalide")
    if dt.tzinfo is not None:
        dt = dt.astimezone().replace(tzinfo=None)  # to local time
    return dt


# ── Settings ──────────────────────────────────────────────────────────────────

class SettingsIn(BaseModel):
    work_min: float = Field(25, ge=1, le=180)
    short_min: float = Field(5, ge=1, le=60)
    long_min: float = Field(15, ge=1, le=120)
    cycles: int = Field(4, ge=1, le=12)
    auto_breaks: bool = False
    auto_work: bool = False
    sound: bool = True
    volume: float = Field(0.6, ge=0, le=1)
    daily_goal: int = Field(8, ge=1, le=50)


@router.get("/settings")
async def get_settings():
    def _q():
        with _db() as conn:
            row = conn.execute("SELECT value FROM settings WHERE key='settings'").fetchone()
        data = dict(DEFAULT_SETTINGS)
        if row:
            try:
                data.update(json.loads(row["value"]))
            except ValueError:
                pass
        return data
    return await asyncio.to_thread(_q)


@router.put("/settings")
async def put_settings(body: SettingsIn):
    data = body.model_dump()

    def _q():
        with _db() as conn:
            conn.execute("INSERT INTO settings(key, value) VALUES('settings', ?) "
                         "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (json.dumps(data),))
    await asyncio.to_thread(_q)
    return data


# ── Sessions ──────────────────────────────────────────────────────────────────

class SessionIn(BaseModel):
    type: str
    task: str = ""
    started_at: Optional[str] = None   # local ISO (no tz) or ISO with offset
    ended_at: Optional[str] = None
    planned_sec: int = Field(0, ge=0, le=86400)
    duration_sec: int = Field(..., ge=0, le=86400)
    completed: bool = True


@router.post("/sessions")
async def add_session(body: SessionIn):
    if body.type not in TYPES:
        return JSONResponse(status_code=400, content={"error": "Type de session invalide."})
    try:
        end = _parse_local(body.ended_at)
        start = _parse_local(body.started_at) if body.started_at else end - timedelta(seconds=body.duration_sec)
    except ValueError as e:
        return JSONResponse(status_code=400, content={"error": str(e)})
    task = body.task.strip()[:200]

    def _q():
        with _db() as conn:
            cur = conn.execute(
                "INSERT INTO sessions (day, started_at, ended_at, task, type, planned_sec, duration_sec, completed)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (start.date().isoformat(), start.isoformat(timespec="seconds"), end.isoformat(timespec="seconds"),
                 task, body.type, body.planned_sec, body.duration_sec, int(body.completed)))
            return dict(conn.execute("SELECT * FROM sessions WHERE id=?", (cur.lastrowid,)).fetchone())
    return await asyncio.to_thread(_q)


@router.get("/sessions")
async def list_sessions(limit: int = 50, day: Optional[str] = None):
    limit = max(1, min(limit, 1000))

    def _q():
        with _db() as conn:
            if day:
                rows = conn.execute("SELECT * FROM sessions WHERE day=? ORDER BY started_at DESC LIMIT ?", (day, limit))
            else:
                rows = conn.execute("SELECT * FROM sessions ORDER BY started_at DESC LIMIT ?", (limit,))
            return [dict(r) for r in rows]
    return {"sessions": await asyncio.to_thread(_q)}


@router.delete("/sessions/{sid}")
async def delete_session(sid: int):
    def _q():
        with _db() as conn:
            return conn.execute("DELETE FROM sessions WHERE id=?", (sid,)).rowcount
    if not await asyncio.to_thread(_q):
        return JSONResponse(status_code=404, content={"error": "Session introuvable."})
    return {"ok": True}


@router.delete("/sessions")
async def clear_sessions(confirm: bool = False):
    if not confirm:
        return JSONResponse(status_code=400, content={"error": "Confirmation requise (confirm=true)."})

    def _q():
        with _db() as conn:
            return conn.execute("DELETE FROM sessions").rowcount
    return {"ok": True, "deleted": await asyncio.to_thread(_q)}


# ── Stats ─────────────────────────────────────────────────────────────────────

@router.get("/stats")
async def stats(days: int = 7, weeks: int = 8, today: Optional[str] = None):
    days = max(1, min(days, 366))
    weeks = max(1, min(weeks, 104))
    try:
        ref = date.fromisoformat(today) if today else date.today()
    except ValueError:
        return JSONResponse(status_code=400, content={"error": "Date invalide."})
    week_start = ref - timedelta(days=ref.weekday())  # Monday
    first = min(ref - timedelta(days=days - 1), week_start - timedelta(weeks=weeks - 1))

    def _q():
        with _db() as conn:
            rows = conn.execute(
                "SELECT day, type, SUM(duration_sec) AS sec, COUNT(*) AS n, SUM(completed) AS done "
                "FROM sessions WHERE day BETWEEN ? AND ? GROUP BY day, type",
                (first.isoformat(), ref.isoformat())).fetchall()
            tasks = conn.execute(
                "SELECT task, SUM(duration_sec) AS sec, COUNT(*) AS n FROM sessions "
                "WHERE type='work' AND day BETWEEN ? AND ? GROUP BY task ORDER BY sec DESC LIMIT 8",
                ((ref - timedelta(days=days - 1)).isoformat(), ref.isoformat())).fetchall()
            total = conn.execute(
                "SELECT COUNT(*) AS n, COALESCE(SUM(duration_sec),0) AS sec FROM sessions WHERE type='work' AND completed=1"
            ).fetchone()
            work_days = {r["day"] for r in conn.execute(
                "SELECT DISTINCT day FROM sessions WHERE type='work' AND completed=1 AND day <= ?", (ref.isoformat(),))}
        return rows, tasks, total, work_days

    rows, tasks, total, work_days = await asyncio.to_thread(_q)
    per_day: dict = {}
    for r in rows:
        d = per_day.setdefault(r["day"], {"work_sec": 0, "work_count": 0, "break_sec": 0})
        if r["type"] == "work":
            d["work_sec"] += r["sec"] or 0
            d["work_count"] += r["done"] or 0
        else:
            d["break_sec"] += r["sec"] or 0

    daily = []
    for i in range(days - 1, -1, -1):
        d = ref - timedelta(days=i)
        daily.append({"day": d.isoformat(), **per_day.get(d.isoformat(), {"work_sec": 0, "work_count": 0, "break_sec": 0})})

    weekly = []
    for i in range(weeks - 1, -1, -1):
        ws = week_start - timedelta(weeks=i)
        agg = {"work_sec": 0, "work_count": 0, "break_sec": 0}
        for k in range(7):
            v = per_day.get((ws + timedelta(days=k)).isoformat())
            if v:
                for key in agg:
                    agg[key] += v[key]
        iso = ws.isocalendar()
        weekly.append({"week_start": ws.isoformat(), "week": f"{iso[0]}-S{iso[1]:02d}", **agg})

    streak = 0
    d = ref if ref.isoformat() in work_days else ref - timedelta(days=1)
    while d.isoformat() in work_days:
        streak += 1
        d -= timedelta(days=1)

    return {
        "today": daily[-1],
        "this_week": weekly[-1],
        "daily": daily,
        "weekly": weekly,
        "top_tasks": [{"task": t["task"] or "(sans tâche)", "work_sec": t["sec"], "count": t["n"]} for t in tasks],
        "total": {"work_count": total["n"], "work_sec": total["sec"]},
        "streak": streak,
    }
