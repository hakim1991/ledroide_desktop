import io
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

router  = APIRouter()
DB_PATH = Path(__file__).parent / "timetracker.db"


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


@router.post("/entries")
async def create_entry(e: EntryIn):
    with _db() as conn:
        cur = conn.execute(
            "INSERT INTO entries (date, project, task, description, hours) VALUES (?,?,?,?,?)",
            (e.date, e.project, e.task, e.description, e.hours),
        )
        eid = cur.lastrowid
        row = conn.execute("SELECT * FROM entries WHERE id=?", (eid,)).fetchone()
    return dict(row)


@router.put("/entries/{eid}")
async def update_entry(eid: int, e: EntryIn):
    with _db() as conn:
        conn.execute(
            "UPDATE entries SET date=?,project=?,task=?,description=?,hours=? WHERE id=?",
            (e.date, e.project, e.task, e.description, e.hours, eid),
        )
        row = conn.execute("SELECT * FROM entries WHERE id=?", (eid,)).fetchone()
    if not row:
        raise HTTPException(404, "entry not found")
    return dict(row)


@router.delete("/entries/{eid}")
async def delete_entry(eid: int):
    with _db() as conn:
        conn.execute("DELETE FROM entries WHERE id=?", (eid,))
    return {"deleted": True}


# ── Autocomplete ──────────────────────────────────────────────────────────────

@router.get("/projects")
async def list_projects():
    with _db() as conn:
        rows = conn.execute(
            "SELECT DISTINCT project FROM entries WHERE project!='' ORDER BY project"
        ).fetchall()
    return [r["project"] for r in rows]


@router.get("/tasks")
async def list_tasks(project: Optional[str] = None):
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


# ── Export Excel ──────────────────────────────────────────────────────────────

@router.get("/export/excel")
async def export_excel(project: Optional[str] = None):
    try:
        import openpyxl
        from openpyxl.styles import Alignment, Font, PatternFill
    except ImportError:
        raise HTTPException(500, "openpyxl requis — exécutez : pip install openpyxl")

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

    with _db() as conn:
        if project:
            rows = conn.execute(
                "SELECT date,project,task,description,hours FROM entries"
                " WHERE project=? ORDER BY date DESC, id DESC",
                (project,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT date,project,task,description,hours FROM entries ORDER BY date DESC, id DESC"
            ).fetchall()

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
