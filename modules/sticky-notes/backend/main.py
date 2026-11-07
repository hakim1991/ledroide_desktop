import asyncio
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from ledroide_sdk.http import APIRouter
from pydantic import BaseModel
from ledroide_sdk import Module, events, data_path

router = APIRouter()
module = Module(__file__)

DB_PATH = data_path(__file__, "notes.db")


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


# ── WebSocket handlers ────────────────────────────────────────────────────────

@module.on('list_notes')
async def handle_list_notes(payload: dict):
    def _q():
        with _db() as conn:
            rows = conn.execute(
                "SELECT * FROM notes ORDER BY created_at DESC"
            ).fetchall()
            return [_row_to_dict(r) for r in rows]

    notes = await asyncio.to_thread(_q)
    return {"notes": notes}


@module.on('create_note')
async def handle_create_note(payload: dict):
    import uuid
    note_id = str(uuid.uuid4())
    now = _now()

    def _q():
        with _db() as conn:
            conn.execute(
                "INSERT INTO notes (id, title, content, color, pos_x, pos_y, width, height, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (note_id, payload.get('title', ''), payload.get('content', ''),
                 payload.get('color', '#fef08a'), payload.get('pos_x', 0), payload.get('pos_y', 0),
                 payload.get('width', 220), payload.get('height', 200), now, now),
            )
            row = conn.execute("SELECT * FROM notes WHERE id=?", (note_id,)).fetchone()
            return _row_to_dict(row)

    note = await asyncio.to_thread(_q)
    await events.emit("note_created", note)
    return note


@module.on('update_note')
async def handle_update_note(payload: dict):
    note_id = payload.get('note_id')
    p = payload.get('payload', {})
    changes = {k: v for k, v in p.items() if v is not None}

    def _q():
        with _db() as conn:
            if not changes:
                row = conn.execute("SELECT * FROM notes WHERE id=?", (note_id,)).fetchone()
                return _row_to_dict(row) if row else None
            ch = {**changes, "updated_at": _now()}
            set_clause = ", ".join(f"{k}=?" for k in ch)
            values = list(ch.values()) + [note_id]
            conn.execute(f"UPDATE notes SET {set_clause} WHERE id=?", values)
            row = conn.execute("SELECT * FROM notes WHERE id=?", (note_id,)).fetchone()
            return _row_to_dict(row) if row else None

    note = await asyncio.to_thread(_q)
    if note is None:
        return {"error": "Note introuvable", "code": "NOT_FOUND"}
    await events.emit("note_updated", note)
    return note


@module.on('delete_all_notes')
async def handle_delete_all_notes(payload: dict):
    await asyncio.to_thread(lambda: _delete_all_sync())
    await events.emit("all_notes_deleted", {})
    return {"ok": True}


def _delete_all_sync():
    with _db() as conn:
        conn.execute("DELETE FROM notes")


@module.on('delete_note')
async def handle_delete_note(payload: dict):
    note_id = payload.get('note_id')

    def _q():
        with _db() as conn:
            existing = conn.execute(
                "SELECT id FROM notes WHERE id=?", (note_id,)
            ).fetchone()
            if not existing:
                return False
            conn.execute("DELETE FROM notes WHERE id=?", (note_id,))
            return True

    deleted = await asyncio.to_thread(_q)
    if not deleted:
        return {"error": "Note introuvable", "code": "NOT_FOUND"}
    await events.emit("note_deleted", {"id": note_id})
    return {"ok": True}


# ── REST endpoints ────────────────────────────────────────────────────────────

@router.get("/notes/{note_id}")
async def get_note(note_id: str):
    def _q():
        with _db() as conn:
            row = conn.execute("SELECT * FROM notes WHERE id=?", (note_id,)).fetchone()
            return _row_to_dict(row) if row else None

    note = await asyncio.to_thread(_q)
    if note is None:
        from ledroide_sdk.http import HTTPException
        raise HTTPException(404, detail={"error": "Note introuvable", "code": "NOT_FOUND"})
    return note


@router.get("/notes")
async def list_notes():
    def _q():
        with _db() as conn:
            rows = conn.execute(
                "SELECT * FROM notes ORDER BY created_at DESC"
            ).fetchall()
            return [_row_to_dict(r) for r in rows]

    return {"notes": await asyncio.to_thread(_q)}


@router.post("/notes")
async def create_note(payload: NoteCreate):
    import uuid
    note_id = str(uuid.uuid4())
    now = _now()

    def _q():
        with _db() as conn:
            conn.execute(
                "INSERT INTO notes (id, title, content, color, pos_x, pos_y, width, height, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (note_id, payload.title, payload.content, payload.color,
                 payload.pos_x, payload.pos_y, payload.width, payload.height, now, now),
            )
            row = conn.execute("SELECT * FROM notes WHERE id=?", (note_id,)).fetchone()
            return _row_to_dict(row)

    note = await asyncio.to_thread(_q)
    await events.emit("note_created", note)
    return note


@router.put("/notes/{note_id}")
async def update_note(note_id: str, payload: NoteUpdate):
    changes = {k: v for k, v in payload.model_dump().items() if v is not None}

    def _q():
        with _db() as conn:
            if not changes:
                row = conn.execute("SELECT * FROM notes WHERE id=?", (note_id,)).fetchone()
                return _row_to_dict(row) if row else None
            ch = {**changes, "updated_at": _now()}
            set_clause = ", ".join(f"{k}=?" for k in ch)
            values = list(ch.values()) + [note_id]
            conn.execute(f"UPDATE notes SET {set_clause} WHERE id=?", values)
            row = conn.execute("SELECT * FROM notes WHERE id=?", (note_id,)).fetchone()
            return _row_to_dict(row) if row else None

    note = await asyncio.to_thread(_q)
    if note is None:
        return {"error": "Note introuvable", "code": "NOT_FOUND"}
    await events.emit("note_updated", note)
    return note


@router.delete("/notes/{note_id}")
async def delete_note(note_id: str):
    def _q():
        with _db() as conn:
            existing = conn.execute(
                "SELECT id FROM notes WHERE id=?", (note_id,)
            ).fetchone()
            if not existing:
                return False
            conn.execute("DELETE FROM notes WHERE id=?", (note_id,))
            return True

    deleted = await asyncio.to_thread(_q)
    if not deleted:
        return {"error": "Note introuvable", "code": "NOT_FOUND"}
    await events.emit("note_deleted", {"id": note_id})
    return {"ok": True}
