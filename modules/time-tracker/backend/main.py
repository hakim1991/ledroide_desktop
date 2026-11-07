import asyncio
import io
import sqlite3
import time as _time
from contextlib import contextmanager
from pathlib import Path

from ledroide_sdk import Module, data_path
from typing import Optional

from ledroide_sdk.http import HTTPException, Query
from ledroide_sdk.http import StreamingResponse
from pydantic import BaseModel

module = Module(__file__)
router = module.router
DB_PATH = data_path(__file__, "timetracker.db")


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
            CREATE TABLE IF NOT EXISTS entries (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                date        TEXT    NOT NULL,
                project     TEXT    NOT NULL DEFAULT '',
                task        TEXT    NOT NULL DEFAULT '',
                description TEXT    NOT NULL DEFAULT '',
                hours       REAL    NOT NULL DEFAULT 0.0,
                created_at  TEXT    NOT NULL DEFAULT (datetime('now'))
            )
        """)
        # Single-row timer state — id=1 is the only row ever used
        conn.execute("""
            CREATE TABLE IF NOT EXISTS timer_state (
                id         INTEGER PRIMARY KEY CHECK (id = 1),
                state      TEXT    NOT NULL DEFAULT 'idle',
                elapsed    INTEGER NOT NULL DEFAULT 0,
                started_at INTEGER          -- Unix timestamp in seconds, NULL when not running
            )
        """)
        conn.execute("INSERT OR IGNORE INTO timer_state (id) VALUES (1)")


_init()


class EntryIn(BaseModel):
    date:        str
    project:     str   = ""
    task:        str   = ""
    description: str   = ""
    hours:       float = 0.0


# ── CRUD ─────────────────────────────────────────────────────────────────────

@router.get("/entries")
async def list_entries(
    project: Optional[str] = None,
    limit:   int = Query(default=500, le=1000),
):
    def _q():
        with _db() as conn:
            if project:
                rows = conn.execute(
                    "SELECT * FROM entries WHERE project=? ORDER BY date DESC, id DESC LIMIT ?",
                    (project, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM entries ORDER BY date DESC, id DESC LIMIT ?", (limit,)
                ).fetchall()
            return [dict(r) for r in rows]

    return await asyncio.to_thread(_q)


@router.post("/entries")
async def create_entry(e: EntryIn):
    def _q():
        with _db() as conn:
            cur = conn.execute(
                "INSERT INTO entries (date, project, task, description, hours) VALUES (?,?,?,?,?)",
                (e.date, e.project, e.task, e.description, e.hours),
            )
            eid = cur.lastrowid
            row = conn.execute("SELECT * FROM entries WHERE id=?", (eid,)).fetchone()
            return dict(row)

    return await asyncio.to_thread(_q)


@router.put("/entries/{eid}")
async def update_entry(eid: int, e: EntryIn):
    def _q():
        with _db() as conn:
            conn.execute(
                "UPDATE entries SET date=?,project=?,task=?,description=?,hours=? WHERE id=?",
                (e.date, e.project, e.task, e.description, e.hours, eid),
            )
            row = conn.execute("SELECT * FROM entries WHERE id=?", (eid,)).fetchone()
            return dict(row) if row else None

    row = await asyncio.to_thread(_q)
    if not row:
        raise HTTPException(404, "entry not found")
    return row


@router.delete("/entries/{eid}")
async def delete_entry(eid: int):
    await asyncio.to_thread(lambda: _delete_entry_sync(eid))
    return {"deleted": True}


def _delete_entry_sync(eid: int):
    with _db() as conn:
        conn.execute("DELETE FROM entries WHERE id=?", (eid,))


# ── Autocomplete ──────────────────────────────────────────────────────────────

@router.get("/projects")
async def list_projects():
    def _q():
        with _db() as conn:
            rows = conn.execute(
                "SELECT DISTINCT project FROM entries WHERE project!='' ORDER BY project"
            ).fetchall()
            return [r["project"] for r in rows]

    return await asyncio.to_thread(_q)


@router.get("/tasks")
async def list_tasks(project: Optional[str] = None):
    def _q():
        with _db() as conn:
            if project:
                rows = conn.execute(
                    "SELECT DISTINCT task FROM entries WHERE task!='' AND project=? ORDER BY task",
                    (project,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT DISTINCT task FROM entries WHERE task!='' ORDER BY task"
                ).fetchall()
            return [r["task"] for r in rows]

    return await asyncio.to_thread(_q)


# ── Timer state ───────────────────────────────────────────────────────────────

def _timer_row() -> dict:
    with _db() as conn:
        row = conn.execute("SELECT * FROM timer_state WHERE id=1").fetchone()
        return dict(row)


@router.get("/timer")
async def get_timer():
    return await asyncio.to_thread(_timer_row)


@router.post("/timer/start")
async def timer_start():
    now = int(_time.time())

    def _q():
        with _db() as conn:
            conn.execute(
                "UPDATE timer_state SET state='running', started_at=?, elapsed=0 WHERE id=1",
                (now,),
            )

    await asyncio.to_thread(_q)
    return {"state": "running", "started_at": now, "elapsed": 0}


@router.post("/timer/pause")
async def timer_pause():
    now = int(_time.time())

    def _q():
        with _db() as conn:
            row = dict(conn.execute("SELECT * FROM timer_state WHERE id=1").fetchone())
            elapsed = row["elapsed"]
            if row["state"] == "running" and row["started_at"]:
                elapsed += now - row["started_at"]
            conn.execute(
                "UPDATE timer_state SET state='paused', started_at=NULL, elapsed=? WHERE id=1",
                (elapsed,),
            )
            return elapsed

    elapsed = await asyncio.to_thread(_q)
    return {"state": "paused", "elapsed": elapsed, "started_at": None}


@router.post("/timer/resume")
async def timer_resume():
    now = int(_time.time())

    def _q():
        with _db() as conn:
            conn.execute(
                "UPDATE timer_state SET state='running', started_at=? WHERE id=1",
                (now,),
            )

    await asyncio.to_thread(_q)
    return {"state": "running", "started_at": now}


@router.post("/timer/stop")
async def timer_stop():
    now = int(_time.time())

    def _q():
        with _db() as conn:
            row = dict(conn.execute("SELECT * FROM timer_state WHERE id=1").fetchone())
            elapsed = row["elapsed"]
            if row["state"] == "running" and row["started_at"]:
                elapsed += now - row["started_at"]
            conn.execute(
                "UPDATE timer_state SET state='done', started_at=NULL, elapsed=? WHERE id=1",
                (elapsed,),
            )
            return elapsed

    elapsed = await asyncio.to_thread(_q)
    return {"state": "done", "elapsed": elapsed, "started_at": None}


@router.delete("/timer")
async def timer_reset():
    def _q():
        with _db() as conn:
            conn.execute(
                "UPDATE timer_state SET state='idle', started_at=NULL, elapsed=0 WHERE id=1"
            )

    await asyncio.to_thread(_q)
    return {"state": "idle", "elapsed": 0, "started_at": None}


# ── Export Excel ──────────────────────────────────────────────────────────────

@router.get("/export/excel")
async def export_excel(project: Optional[str] = None):
    try:
        import openpyxl
        from openpyxl.styles import Alignment, Font, PatternFill
    except ImportError:
        raise HTTPException(500, "openpyxl requis — exécutez : pip install openpyxl")

    def _q():
        with _db() as conn:
            if project:
                return conn.execute(
                    "SELECT date,project,task,description,hours FROM entries"
                    " WHERE project=? ORDER BY date DESC, id DESC",
                    (project,),
                ).fetchall()
            return conn.execute(
                "SELECT date,project,task,description,hours FROM entries ORDER BY date DESC, id DESC"
            ).fetchall()

    rows = await asyncio.to_thread(_q)

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Tâches"

    HEADERS = ["Date", "Projet", "Tâche", "Description", "Heures"]
    hfill  = PatternFill(start_color="6366F1", end_color="6366F1", fill_type="solid")
    hfont  = Font(bold=True, color="FFFFFF")
    center = Alignment(horizontal="center")

    for col, h in enumerate(HEADERS, 1):
        c = ws.cell(row=1, column=col, value=h)
        c.fill, c.font, c.alignment = hfill, hfont, center

    for ri, row in enumerate(rows, 2):
        ws.cell(ri, 1, row["date"])
        ws.cell(ri, 2, row["project"])
        ws.cell(ri, 3, row["task"])
        ws.cell(ri, 4, row["description"])
        h_cell = ws.cell(ri, 5, row["hours"])
        h_cell.number_format = "0.00"

    ws.column_dimensions["A"].width = 13
    ws.column_dimensions["B"].width = 22
    ws.column_dimensions["C"].width = 28
    ws.column_dimensions["D"].width = 45
    ws.column_dimensions["E"].width = 10

    if rows:
        n  = len(rows) + 1
        tr = n + 1
        ws.cell(tr, 4, "TOTAL").font = Font(bold=True)
        tc = ws.cell(tr, 5, f"=SUM(E2:E{n})")
        tc.font, tc.number_format = Font(bold=True), "0.00"

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)

    fname = f"taches-{project}.xlsx" if project else "taches.xlsx"
    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename={fname}"},
    )
