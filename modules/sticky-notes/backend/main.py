import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import APIRouter
from pydantic import BaseModel
from ledroide_sdk import Module, events

router = APIRouter()
module = Module()

DB_PATH = Path(__file__).parent / "notes.db"


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
            CREATE TABLE IF NOT EXISTS notes (
                id         TEXT    PRIMARY KEY,
                title      TEXT    NOT NULL DEFAULT '',
                content    TEXT    NOT NULL DEFAULT '',
                color      TEXT    NOT NULL DEFAULT '#fef08a',
                pos_x      REAL    NOT NULL DEFAULT 0,
                pos_y      REAL    NOT NULL DEFAULT 0,
                created_at TEXT    NOT NULL,
                updated_at TEXT    NOT NULL
            )
        """)


_init()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _row_to_dict(row) -> dict:
    return dict(row)


class NoteCreate(BaseModel):
    title:   str   = ""
    content: str   = ""
    color:   str   = "#fef08a"
    pos_x:   float = 0
    pos_y:   float = 0


class NoteUpdate(BaseModel):
    title:   Optional[str]   = None
    content: Optional[str]   = None
    color:   Optional[str]   = None
    pos_x:   Optional[float] = None
    pos_y:   Optional[float] = None


@router.get("/notes")
async def list_notes():
    with _db() as conn:
        rows = conn.execute(
            "SELECT * FROM notes ORDER BY created_at DESC"
        ).fetchall()
    return {"notes": [_row_to_dict(r) for r in rows]}


@router.post("/notes")
async def create_note(payload: NoteCreate):
    import uuid
    note_id = str(uuid.uuid4())
    now = _now()
    with _db() as conn:
        conn.execute(
            "INSERT INTO notes (id, title, content, color, pos_x, pos_y, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (note_id, payload.title, payload.content, payload.color,
             payload.pos_x, payload.pos_y, now, now),
        )
        row = conn.execute("SELECT * FROM notes WHERE id=?", (note_id,)).fetchone()
    note = _row_to_dict(row)
    await events.emit("note_created", note)
    return note


@router.put("/notes/{note_id}")
async def update_note(note_id: str, payload: NoteUpdate):
    changes = {k: v for k, v in payload.model_dump().items() if v is not None}
    if not changes:
        with _db() as conn:
            row = conn.execute("SELECT * FROM notes WHERE id=?", (note_id,)).fetchone()
        if not row:
            return {"error": "Note introuvable", "code": "NOT_FOUND"}
        return _row_to_dict(row)

    changes["updated_at"] = _now()
    set_clause = ", ".join(f"{k}=?" for k in changes)
    values = list(changes.values()) + [note_id]

    with _db() as conn:
        conn.execute(f"UPDATE notes SET {set_clause} WHERE id=?", values)
        row = conn.execute("SELECT * FROM notes WHERE id=?", (note_id,)).fetchone()

    if not row:
        return {"error": "Note introuvable", "code": "NOT_FOUND"}
    note = _row_to_dict(row)
    await events.emit("note_updated", note)
    return note


@router.delete("/notes/{note_id}")
async def delete_note(note_id: str):
    with _db() as conn:
        existing = conn.execute(
            "SELECT id FROM notes WHERE id=?", (note_id,)
        ).fetchone()
        if not existing:
            return {"error": "Note introuvable", "code": "NOT_FOUND"}
        conn.execute("DELETE FROM notes WHERE id=?", (note_id,))
    await events.emit("note_deleted", {"id": note_id})
    return {"ok": True}
