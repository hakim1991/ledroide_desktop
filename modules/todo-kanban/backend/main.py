import asyncio
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

from ledroide_sdk.http import JSONResponse
from pydantic import BaseModel
from ledroide_sdk import Module, data_path

module = Module(__file__)
router = module.router

DB_PATH = data_path(__file__, "todo-kanban.db")
PRIORITIES = ("low", "medium", "high", "urgent")
DEFAULT_COLUMNS = ("À faire", "En cours", "Terminé")


@contextmanager
def _db():
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
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
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS boards (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                name       TEXT    NOT NULL,
                position   INTEGER NOT NULL DEFAULT 0,
                created_at TEXT    NOT NULL
            );
            CREATE TABLE IF NOT EXISTS columns (
                id       INTEGER PRIMARY KEY AUTOINCREMENT,
                board_id INTEGER NOT NULL REFERENCES boards(id) ON DELETE CASCADE,
                name     TEXT    NOT NULL,
                position INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS cards (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                column_id   INTEGER NOT NULL REFERENCES columns(id) ON DELETE CASCADE,
                title       TEXT    NOT NULL,
                description TEXT    NOT NULL DEFAULT '',
                priority    TEXT    NOT NULL DEFAULT 'medium',
                due_date    TEXT,
                tags        TEXT    NOT NULL DEFAULT '[]',
                position    INTEGER NOT NULL DEFAULT 0,
                created_at  TEXT    NOT NULL,
                updated_at  TEXT    NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_cols_board ON columns(board_id);
            CREATE INDEX IF NOT EXISTS idx_cards_col ON cards(column_id);
        """)


_init()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _err(msg: str, status: int = 400, code: Optional[str] = None):
    content = {"error": msg}
    if code:
        content["code"] = code
    return JSONResponse(status_code=status, content=content)


def _card(row) -> dict:
    d = dict(row)
    try:
        d["tags"] = json.loads(d.get("tags") or "[]")
    except Exception:
        d["tags"] = []
    return d


def _clean_tags(tags) -> list:
    out = []
    for t in tags or []:
        t = str(t).strip()[:40]
        if t and t not in out:
            out.append(t)
    return out[:20]


def _norm_due(due) -> Optional[str]:
    if not due:
        return None
    try:
        datetime.strptime(due, "%Y-%m-%d")
        return due
    except ValueError:
        raise ValueError("Date d'échéance invalide (format AAAA-MM-JJ)")


# ── Models ────────────────────────────────────────────────────────────────────

class NameIn(BaseModel):
    name: str


class BoardCreate(BaseModel):
    name: str
    default_columns: bool = True


class OrderIn(BaseModel):
    order: List[int]


class CardCreate(BaseModel):
    title: str
    description: str = ""
    priority: str = "medium"
    due_date: Optional[str] = None
    tags: List[str] = []


class CardUpdate(BaseModel):
    title: Optional[str] = None
    description: Optional[str] = None
    priority: Optional[str] = None
    due_date: Optional[str] = None   # "" clears
    tags: Optional[List[str]] = None


class CardMove(BaseModel):
    column_id: int
    position: int


# ── Sync helpers ──────────────────────────────────────────────────────────────

def _board_full(conn, board_id: int) -> Optional[dict]:
    b = conn.execute("SELECT * FROM boards WHERE id=?", (board_id,)).fetchone()
    if not b:
        return None
    board = dict(b)
    cols = conn.execute(
        "SELECT * FROM columns WHERE board_id=? ORDER BY position, id", (board_id,)
    ).fetchall()
    board["columns"] = []
    for c in cols:
        col = dict(c)
        col["cards"] = [_card(r) for r in conn.execute(
            "SELECT * FROM cards WHERE column_id=? ORDER BY position, id", (c["id"],)
        ).fetchall()]
        board["columns"].append(col)
    return board


def _renumber(conn, column_id: int, ids: List[int]):
    for i, cid in enumerate(ids):
        conn.execute("UPDATE cards SET position=?, column_id=? WHERE id=?", (i, column_id, cid))


# ── Routes: boards ────────────────────────────────────────────────────────────

@router.get("/health")
async def health():
    return {"ok": True}


@router.get("/boards")
async def list_boards():
    def _q():
        with _db() as conn:
            rows = conn.execute("""
                SELECT b.*, (SELECT COUNT(*) FROM cards ca JOIN columns co ON ca.column_id=co.id
                             WHERE co.board_id=b.id) AS card_count
                FROM boards b ORDER BY position, id
            """).fetchall()
            return [dict(r) for r in rows]
    return {"boards": await asyncio.to_thread(_q)}


@router.post("/boards")
async def create_board(payload: BoardCreate):
    name = payload.name.strip()
    if not name:
        return _err("Le nom du tableau est requis")

    def _q():
        with _db() as conn:
            pos = conn.execute("SELECT COALESCE(MAX(position)+1, 0) FROM boards").fetchone()[0]
            cur = conn.execute("INSERT INTO boards (name, position, created_at) VALUES (?,?,?)",
                               (name[:100], pos, _now()))
            bid = cur.lastrowid
            if payload.default_columns:
                for i, cn in enumerate(DEFAULT_COLUMNS):
                    conn.execute("INSERT INTO columns (board_id, name, position) VALUES (?,?,?)", (bid, cn, i))
            return _board_full(conn, bid)
    return await asyncio.to_thread(_q)


@router.get("/boards/{board_id}")
async def get_board(board_id: int):
    def _q():
        with _db() as conn:
            return _board_full(conn, board_id)
    b = await asyncio.to_thread(_q)
    if b is None:
        return _err("Tableau introuvable", 404)
    return b


@router.put("/boards/{board_id}")
async def rename_board(board_id: int, payload: NameIn):
    name = payload.name.strip()
    if not name:
        return _err("Le nom du tableau est requis")

    def _q():
        with _db() as conn:
            cur = conn.execute("UPDATE boards SET name=? WHERE id=?", (name[:100], board_id))
            return cur.rowcount > 0
    if not await asyncio.to_thread(_q):
        return _err("Tableau introuvable", 404)
    return {"ok": True}


@router.delete("/boards/{board_id}")
async def delete_board(board_id: int):
    def _q():
        with _db() as conn:
            return conn.execute("DELETE FROM boards WHERE id=?", (board_id,)).rowcount > 0
    if not await asyncio.to_thread(_q):
        return _err("Tableau introuvable", 404)
    return {"ok": True}


# ── Routes: columns ───────────────────────────────────────────────────────────

@router.post("/boards/{board_id}/columns")
async def create_column(board_id: int, payload: NameIn):
    name = payload.name.strip()
    if not name:
        return _err("Le nom de la colonne est requis")

    def _q():
        with _db() as conn:
            if not conn.execute("SELECT 1 FROM boards WHERE id=?", (board_id,)).fetchone():
                return None
            pos = conn.execute("SELECT COALESCE(MAX(position)+1, 0) FROM columns WHERE board_id=?",
                               (board_id,)).fetchone()[0]
            cur = conn.execute("INSERT INTO columns (board_id, name, position) VALUES (?,?,?)",
                               (board_id, name[:60], pos))
            col = dict(conn.execute("SELECT * FROM columns WHERE id=?", (cur.lastrowid,)).fetchone())
            col["cards"] = []
            return col
    col = await asyncio.to_thread(_q)
    if col is None:
        return _err("Tableau introuvable", 404)
    return col


@router.put("/columns/{column_id}")
async def rename_column(column_id: int, payload: NameIn):
    name = payload.name.strip()
    if not name:
        return _err("Le nom de la colonne est requis")

    def _q():
        with _db() as conn:
            return conn.execute("UPDATE columns SET name=? WHERE id=?", (name[:60], column_id)).rowcount > 0
    if not await asyncio.to_thread(_q):
        return _err("Colonne introuvable", 404)
    return {"ok": True}


@router.delete("/columns/{column_id}")
async def delete_column(column_id: int):
    def _q():
        with _db() as conn:
            return conn.execute("DELETE FROM columns WHERE id=?", (column_id,)).rowcount > 0
    if not await asyncio.to_thread(_q):
        return _err("Colonne introuvable", 404)
    return {"ok": True}


@router.post("/boards/{board_id}/columns/reorder")
async def reorder_columns(board_id: int, payload: OrderIn):
    def _q():
        with _db() as conn:
            existing = {r[0] for r in conn.execute("SELECT id FROM columns WHERE board_id=?", (board_id,))}
            if set(payload.order) != existing or len(payload.order) != len(existing):
                return False
            for i, cid in enumerate(payload.order):
                conn.execute("UPDATE columns SET position=? WHERE id=?", (i, cid))
            return True
    if not await asyncio.to_thread(_q):
        return _err("Ordre de colonnes invalide")
    return {"ok": True}


# ── Routes: cards ─────────────────────────────────────────────────────────────

@router.post("/columns/{column_id}/cards")
async def create_card(column_id: int, payload: CardCreate):
    title = payload.title.strip()
    if not title:
        return _err("Le titre de la carte est requis")
    if payload.priority not in PRIORITIES:
        return _err("Priorité invalide")
    try:
        due = _norm_due(payload.due_date)
    except ValueError as e:
        return _err(str(e))

    def _q():
        with _db() as conn:
            if not conn.execute("SELECT 1 FROM columns WHERE id=?", (column_id,)).fetchone():
                return None
            pos = conn.execute("SELECT COALESCE(MAX(position)+1, 0) FROM cards WHERE column_id=?",
                               (column_id,)).fetchone()[0]
            now = _now()
            cur = conn.execute(
                "INSERT INTO cards (column_id, title, description, priority, due_date, tags, position, created_at, updated_at)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                (column_id, title[:200], payload.description[:5000], payload.priority, due,
                 json.dumps(_clean_tags(payload.tags), ensure_ascii=False), pos, now, now))
            return _card(conn.execute("SELECT * FROM cards WHERE id=?", (cur.lastrowid,)).fetchone())
    card = await asyncio.to_thread(_q)
    if card is None:
        return _err("Colonne introuvable", 404)
    return card


@router.put("/cards/{card_id}")
async def update_card(card_id: int, payload: CardUpdate):
    ch = {}
    if payload.title is not None:
        if not payload.title.strip():
            return _err("Le titre de la carte est requis")
        ch["title"] = payload.title.strip()[:200]
    if payload.description is not None:
        ch["description"] = payload.description[:5000]
    if payload.priority is not None:
        if payload.priority not in PRIORITIES:
            return _err("Priorité invalide")
        ch["priority"] = payload.priority
    if payload.due_date is not None:
        try:
            ch["due_date"] = _norm_due(payload.due_date)
        except ValueError as e:
            return _err(str(e))
    if payload.tags is not None:
        ch["tags"] = json.dumps(_clean_tags(payload.tags), ensure_ascii=False)

    def _q():
        with _db() as conn:
            if ch:
                ch2 = {**ch, "updated_at": _now()}
                conn.execute(f"UPDATE cards SET {', '.join(k + '=?' for k in ch2)} WHERE id=?",
                             list(ch2.values()) + [card_id])
            row = conn.execute("SELECT * FROM cards WHERE id=?", (card_id,)).fetchone()
            return _card(row) if row else None
    card = await asyncio.to_thread(_q)
    if card is None:
        return _err("Carte introuvable", 404)
    return card


@router.delete("/cards/{card_id}")
async def delete_card(card_id: int):
    def _q():
        with _db() as conn:
            row = conn.execute("SELECT column_id FROM cards WHERE id=?", (card_id,)).fetchone()
            if not row:
                return False
            conn.execute("DELETE FROM cards WHERE id=?", (card_id,))
            ids = [r[0] for r in conn.execute(
                "SELECT id FROM cards WHERE column_id=? ORDER BY position, id", (row[0],))]
            _renumber(conn, row[0], ids)
            return True
    if not await asyncio.to_thread(_q):
        return _err("Carte introuvable", 404)
    return {"ok": True}


@router.post("/cards/{card_id}/move")
async def move_card(card_id: int, payload: CardMove):
    def _q():
        with _db() as conn:
            row = conn.execute("SELECT column_id FROM cards WHERE id=?", (card_id,)).fetchone()
            if not row:
                return "Carte introuvable"
            src = row[0]
            dst_board = conn.execute("SELECT board_id FROM columns WHERE id=?", (payload.column_id,)).fetchone()
            src_board = conn.execute("SELECT board_id FROM columns WHERE id=?", (src,)).fetchone()
            if not dst_board:
                return "Colonne introuvable"
            if dst_board[0] != src_board[0]:
                return "Déplacement entre tableaux non autorisé"
            src_ids = [r[0] for r in conn.execute(
                "SELECT id FROM cards WHERE column_id=? ORDER BY position, id", (src,))]
            src_ids.remove(card_id)
            if src != payload.column_id:
                _renumber(conn, src, src_ids)
                dst_ids = [r[0] for r in conn.execute(
                    "SELECT id FROM cards WHERE column_id=? ORDER BY position, id", (payload.column_id,))]
            else:
                dst_ids = src_ids
            pos = max(0, min(payload.position, len(dst_ids)))
            dst_ids.insert(pos, card_id)
            _renumber(conn, payload.column_id, dst_ids)
            conn.execute("UPDATE cards SET updated_at=? WHERE id=?", (_now(), card_id))
            return None
    err = await asyncio.to_thread(_q)
    if err:
        return _err(err, 404 if "introuvable" in err else 400)
    return {"ok": True}
