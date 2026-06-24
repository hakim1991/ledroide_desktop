import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import APIRouter
from pydantic import BaseModel
from ledroide_sdk import Module, events

router = APIRouter()
module = Module(__file__)

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
                width      REAL    NOT NULL DEFAULT 220,
                height     REAL    NOT NULL DEFAULT 200,
                created_at TEXT    NOT NULL,
                updated_at TEXT    NOT NULL
            )
        """)
        for col, default in [('width', 220), ('height', 200)]:
            try:
                conn.execute(f"ALTER TABLE notes ADD COLUMN {col} REAL NOT NULL DEFAULT {default}")
            except Exception:
                pass


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
    width:   float = 220
    height:  float = 200


class NoteUpdate(BaseModel):
    title:   Optional[str]   = None
    content: Optional[str]   = None
    color:   Optional[str]   = None
    pos_x:   Optional[float] = None
    pos_y:   Optional[float] = None
    width:   Optional[float] = None
    height:  Optional[float] = None


@module.on('list_notes')
async def handle_list_notes(payload: dict):
    print("[sticky-notes] list_notes")
    with _db() as conn:
        rows = conn.execute(
            "SELECT * FROM notes ORDER BY created_at DESC"
        ).fetchall()
    result = {"notes": [_row_to_dict(r) for r in rows]}
    print(f"[sticky-notes] list_notes → {len(result['notes'])} note(s)")
    return result


@module.on('create_note')
async def handle_create_note(payload: dict):
    print(f"[sticky-notes] create_note payload={payload}")
    import uuid
    p = payload.get('payload', payload)
    note_id = str(uuid.uuid4())
    now = _now()
    with _db() as conn:
        conn.execute(
            "INSERT INTO notes (id, title, content, color, pos_x, pos_y, width, height, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (note_id, p.get('title', ''), p.get('content', ''),
             p.get('color', '#fef08a'), p.get('pos_x', 0), p.get('pos_y', 0),
             p.get('width', 220), p.get('height', 200), now, now),
        )
        row = conn.execute("SELECT * FROM notes WHERE id=?", (note_id,)).fetchone()
    note = _row_to_dict(row)
    print(f"[sticky-notes] create_note → {note}")
    await events.emit("note_created", note)
    return note


@module.on('update_note')
async def handle_update_note(payload: dict):
    note_id = payload.get('note_id')
    p = payload.get('payload', {})
    changes = {k: v for k, v in p.items() if v is not None}
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


@module.on('delete_all_notes')
async def handle_delete_all_notes(payload: dict):
    with _db() as conn:
        conn.execute("DELETE FROM notes")
    await events.emit("all_notes_deleted", {})
    return {"ok": True}


@module.on('delete_note')
async def handle_delete_note(payload: dict):
    note_id = payload.get('note_id')
    with _db() as conn:
        existing = conn.execute(
            "SELECT id FROM notes WHERE id=?", (note_id,)
        ).fetchone()
        if not existing:
            return {"error": "Note introuvable", "code": "NOT_FOUND"}
        conn.execute("DELETE FROM notes WHERE id=?", (note_id,))
    await events.emit("note_deleted", {"id": note_id})
    return {"ok": True}


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
