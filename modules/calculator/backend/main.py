import asyncio
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from fastapi import APIRouter
from pydantic import BaseModel

router  = APIRouter()
DB_PATH = Path(__file__).parent / "calculator.db"


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
            CREATE TABLE IF NOT EXISTS history (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                expression  TEXT    NOT NULL,
                result      TEXT    NOT NULL,
                ts          TEXT    NOT NULL DEFAULT (datetime('now', 'localtime'))
            )
        """)


_init()


class Calc(BaseModel):
    expression: str
    result:     str


@router.get("/history")
async def get_history():
    def _q():
        with _db() as conn:
            rows = conn.execute(
                "SELECT * FROM history ORDER BY id DESC LIMIT 100"
            ).fetchall()
            return [dict(r) for r in rows]

    return await asyncio.to_thread(_q)


@router.post("/history")
async def add_history(c: Calc):
    def _q():
        with _db() as conn:
            cur = conn.execute(
                "INSERT INTO history (expression, result) VALUES (?, ?)",
                (c.expression, c.result),
            )
            row = conn.execute(
                "SELECT * FROM history WHERE id=?", (cur.lastrowid,)
            ).fetchone()
            return dict(row)

    return await asyncio.to_thread(_q)


@router.delete("/history")
async def clear_history():
    def _q():
        with _db() as conn:
            n = conn.execute("SELECT COUNT(*) FROM history").fetchone()[0]
            conn.execute("DELETE FROM history")
            return n

    n = await asyncio.to_thread(_q)
    return {"cleared": n}
