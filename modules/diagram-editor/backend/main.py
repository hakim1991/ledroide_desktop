import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from ledroide_sdk.http import JSONResponse
from pydantic import BaseModel
from ledroide_sdk import Module, data_path

module = Module(__file__)
router = module.router

DB_PATH = data_path(__file__, "diagram-editor.db")
EMPTY = {"nodes": [], "edges": []}


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
            CREATE TABLE IF NOT EXISTS diagrams (
                id         TEXT PRIMARY KEY,
                name       TEXT NOT NULL,
                data       TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)


_init()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _row(r: sqlite3.Row, with_data: bool = True) -> dict:
    d = {"id": r["id"], "name": r["name"], "created_at": r["created_at"], "updated_at": r["updated_at"]}
    if with_data:
        d["data"] = json.loads(r["data"])
    return d


def _not_found():
    return JSONResponse(status_code=404, content={"error": "Schéma introuvable"})


class DiagramIn(BaseModel):
    name: Optional[str] = None
    data: Optional[dict[str, Any]] = None


@router.get("/diagrams")
async def list_diagrams():
    with _db() as conn:
        rows = conn.execute(
            "SELECT id, name, created_at, updated_at FROM diagrams ORDER BY updated_at DESC"
        ).fetchall()
    return [_row(r, with_data=False) for r in rows]


@router.post("/diagrams")
async def create_diagram(body: DiagramIn):
    did, now = uuid.uuid4().hex, _now()
    name = (body.name or "").strip() or "Nouveau schéma"
    data = body.data if body.data is not None else EMPTY
    with _db() as conn:
        conn.execute(
            "INSERT INTO diagrams (id, name, data, created_at, updated_at) VALUES (?,?,?,?,?)",
            (did, name, json.dumps(data), now, now),
        )
        r = conn.execute("SELECT * FROM diagrams WHERE id=?", (did,)).fetchone()
    return _row(r)


@router.get("/diagrams/{did}")
async def get_diagram(did: str):
    with _db() as conn:
        r = conn.execute("SELECT * FROM diagrams WHERE id=?", (did,)).fetchone()
    return _row(r) if r else _not_found()


@router.put("/diagrams/{did}")
async def update_diagram(did: str, body: DiagramIn):
    with _db() as conn:
        r = conn.execute("SELECT * FROM diagrams WHERE id=?", (did,)).fetchone()
        if not r:
            return _not_found()
        name = r["name"] if body.name is None else (body.name.strip() or r["name"])
        data = r["data"] if body.data is None else json.dumps(body.data)
        conn.execute(
            "UPDATE diagrams SET name=?, data=?, updated_at=? WHERE id=?",
            (name, data, _now(), did),
        )
        r = conn.execute("SELECT * FROM diagrams WHERE id=?", (did,)).fetchone()
    return _row(r, with_data=False)


@router.post("/diagrams/{did}/duplicate")
async def duplicate_diagram(did: str):
    with _db() as conn:
        r = conn.execute("SELECT * FROM diagrams WHERE id=?", (did,)).fetchone()
        if not r:
            return _not_found()
        nid, now = uuid.uuid4().hex, _now()
        conn.execute(
            "INSERT INTO diagrams (id, name, data, created_at, updated_at) VALUES (?,?,?,?,?)",
            (nid, r["name"] + " (copie)", r["data"], now, now),
        )
        r2 = conn.execute("SELECT * FROM diagrams WHERE id=?", (nid,)).fetchone()
    return _row(r2)


@router.delete("/diagrams/{did}")
async def delete_diagram(did: str):
    with _db() as conn:
        cur = conn.execute("DELETE FROM diagrams WHERE id=?", (did,))
    return {"ok": True} if cur.rowcount else _not_found()
