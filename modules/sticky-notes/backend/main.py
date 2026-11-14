import asyncio
import logging
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Optional

from pydantic import BaseModel, Field, ValidationError

from ledroide_sdk import Module, data_path

log = logging.getLogger("ledroide.sticky-notes")

module = Module(__file__)
router = module.router

DB_PATH = data_path(__file__, "notes.db")
DEFAULT_BOARD = "default"
DEFAULT_COLOR = "#fef08a"
COLOR_RE = r"^#[0-9a-fA-F]{6}$"
ID_RE = r"^[A-Za-z0-9_-]{1,64}$"
EXPORT_FORMAT = "ledroide-sticky-notes"
MAX_IMPORT = 5000

NOTE_COLUMNS = ("id", "board_id", "title", "content", "color", "pos_x", "pos_y", "width", "height",
                "z", "pinned", "collapsed", "font", "created_at", "updated_at", "deleted_at")

_lock = asyncio.Lock()
_ready = False


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


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


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
        conn.execute("""
            CREATE TABLE IF NOT EXISTS boards (
                id         TEXT    PRIMARY KEY,
                name       TEXT    NOT NULL,
                position   INTEGER NOT NULL DEFAULT 0,
                created_at TEXT    NOT NULL
            )
        """)
        existing = {r["name"] for r in conn.execute("PRAGMA table_info(notes)")}
        for col, ddl in (
            ("width", "REAL NOT NULL DEFAULT 220"),
            ("height", "REAL NOT NULL DEFAULT 200"),
            ("board_id", f"TEXT NOT NULL DEFAULT '{DEFAULT_BOARD}'"),
            ("z", "INTEGER NOT NULL DEFAULT 0"),
            ("pinned", "INTEGER NOT NULL DEFAULT 0"),
            ("collapsed", "INTEGER NOT NULL DEFAULT 0"),
            ("font", "INTEGER NOT NULL DEFAULT 1"),
            ("deleted_at", "TEXT"),
        ):
            if col not in existing:
                conn.execute(f"ALTER TABLE notes ADD COLUMN {col} {ddl}")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_notes_board ON notes(board_id)")
        if not conn.execute("SELECT 1 FROM boards LIMIT 1").fetchone():
            conn.execute("INSERT INTO boards (id, name, position, created_at) VALUES (?, ?, 0, ?)",
                         (DEFAULT_BOARD, "Tableau 1", _now()))


async def _run(fn, *args):
    """Runs a blocking DB function in a thread, one at a time (single SQLite writer)."""
    global _ready
    async with _lock:
        if not _ready:
            await asyncio.to_thread(_init)
            _ready = True
        return await asyncio.to_thread(fn, *args)


def _err(message: str, code: str = "BAD_REQUEST") -> dict:
    return {"error": message, "code": code}


def _validation_message(exc: ValidationError) -> str:
    first = exc.errors()[0]
    loc = ".".join(str(p) for p in first.get("loc", ()))
    msg = str(first.get("msg", "Données invalides")).removeprefix("Value error, ")
    return f"{loc} : {msg}" if loc else msg


def _parse(model, payload):
    try:
        return model.model_validate(payload or {}), None
    except ValidationError as exc:
        return None, _err(_validation_message(exc), "VALIDATION_ERROR")


def _note(row) -> dict:
    d = dict(row)
    d["pinned"] = bool(d["pinned"])
    d["collapsed"] = bool(d["collapsed"])
    return d


def _get_note(conn, note_id: str):
    row = conn.execute("SELECT * FROM notes WHERE id = ?", (note_id,)).fetchone()
    return _note(row) if row else None


def _boards(conn) -> list[dict]:
    return [dict(r) for r in conn.execute("SELECT * FROM boards ORDER BY position, created_at")]


def _board_exists(conn, board_id: str) -> bool:
    return conn.execute("SELECT 1 FROM boards WHERE id = ?", (board_id,)).fetchone() is not None


def _next_z(conn, board_id: str) -> int:
    row = conn.execute("SELECT COALESCE(MAX(z), 0) + 1 AS z FROM notes WHERE board_id = ?", (board_id,)).fetchone()
    return row["z"]


# ── Models ────────────────────────────────────────────────────────────────────

class NoteFields(BaseModel):
    title: Optional[str] = Field(None, max_length=300)
    content: Optional[str] = Field(None, max_length=200_000)
    color: Optional[str] = Field(None, pattern=COLOR_RE)
    pos_x: Optional[float] = Field(None, ge=0, le=100_000)
    pos_y: Optional[float] = Field(None, ge=0, le=100_000)
    width: Optional[float] = Field(None, ge=140, le=1600)
    height: Optional[float] = Field(None, ge=60, le=1600)
    pinned: Optional[bool] = None
    collapsed: Optional[bool] = None
    font: Optional[int] = Field(None, ge=0, le=2)
    board_id: Optional[str] = Field(None, pattern=ID_RE)


class NoteCreateIn(NoteFields):
    pass


class NoteUpdateIn(BaseModel):
    note_id: str = Field(pattern=ID_RE)
    payload: NoteFields = Field(default_factory=NoteFields)


class NoteIdIn(BaseModel):
    note_id: str = Field(pattern=ID_RE)


class NoteDeleteIn(NoteIdIn):
    permanent: bool = False


class RestoreIn(NoteIdIn):
    board_id: Optional[str] = Field(None, pattern=ID_RE)


class BoardIdIn(BaseModel):
    board_id: str = Field(DEFAULT_BOARD, pattern=ID_RE)


class BoardSaveIn(BaseModel):
    id: Optional[str] = Field(None, pattern=ID_RE)
    name: str = Field(min_length=1, max_length=60)


class Position(BaseModel):
    id: str = Field(pattern=ID_RE)
    pos_x: float = Field(ge=0, le=100_000)
    pos_y: float = Field(ge=0, le=100_000)


class ArrangeIn(BaseModel):
    positions: list[Position] = Field(max_length=MAX_IMPORT)


class BoardOrderIn(BaseModel):
    ids: list[str] = Field(max_length=200)


class ImportIn(BaseModel):
    data: dict[str, Any]


# ── State ─────────────────────────────────────────────────────────────────────

@module.on("state.get")
async def state_get(payload: dict):
    def q():
        with _db() as conn:
            notes = [_note(r) for r in conn.execute(
                "SELECT * FROM notes WHERE deleted_at IS NULL ORDER BY z, created_at")]
            trash = conn.execute("SELECT COUNT(*) AS n FROM notes WHERE deleted_at IS NOT NULL").fetchone()["n"]
            return {"boards": _boards(conn), "notes": notes, "trash_count": trash}
    return await _run(q)


@module.on("list_notes")
async def list_notes(payload: dict):
    board_id = (payload or {}).get("board_id")

    def q():
        with _db() as conn:
            sql, args = "SELECT * FROM notes WHERE deleted_at IS NULL", []
            if isinstance(board_id, str) and board_id:
                sql += " AND board_id = ?"
                args.append(board_id)
            return {"notes": [_note(r) for r in conn.execute(sql + " ORDER BY z, created_at", args)]}
    return await _run(q)


# ── Notes ─────────────────────────────────────────────────────────────────────

@module.on("create_note")
async def create_note(payload: dict):
    # Historical clients nest the fields under "payload".
    raw = payload.get("payload") if isinstance((payload or {}).get("payload"), dict) else payload
    p, err = _parse(NoteCreateIn, raw)
    if err:
        return err

    def q():
        with _db() as conn:
            board_id = p.board_id or DEFAULT_BOARD
            if not _board_exists(conn, board_id):
                return None
            note_id, now = str(uuid.uuid4()), _now()
            conn.execute(
                "INSERT INTO notes (id, board_id, title, content, color, pos_x, pos_y, width, height, z, pinned,"
                " collapsed, font, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (note_id, board_id, p.title or "", p.content or "", p.color or DEFAULT_COLOR,
                 p.pos_x if p.pos_x is not None else 40, p.pos_y if p.pos_y is not None else 40,
                 p.width or 220, p.height or 200, _next_z(conn, board_id), int(bool(p.pinned)),
                 int(bool(p.collapsed)), p.font if p.font is not None else 1, now, now),
            )
            return _get_note(conn, note_id)

    note = await _run(q)
    if note is None:
        return _err("Tableau introuvable", "NOT_FOUND")
    await module.events.emit("note_created", note)
    return note


@module.on("update_note")
async def update_note(payload: dict):
    p, err = _parse(NoteUpdateIn, payload)
    if err:
        return err
    changes = p.payload.model_dump(exclude_none=True)
    for key in ("pinned", "collapsed"):
        if key in changes:
            changes[key] = int(changes[key])
    # Moving or resizing is not an edit: the note keeps its "modifiée le" date.
    touches = any(k in changes for k in ("title", "content", "color", "board_id"))

    def q():
        with _db() as conn:
            current = _get_note(conn, p.note_id)
            if current is None:
                return None
            if "board_id" in changes:
                if not _board_exists(conn, changes["board_id"]):
                    return False
                if changes["board_id"] != current["board_id"]:
                    changes["z"] = _next_z(conn, changes["board_id"])
            if changes:
                if touches:
                    changes["updated_at"] = _now()
                cols = ", ".join(f"{k} = ?" for k in changes)
                conn.execute(f"UPDATE notes SET {cols} WHERE id = ?", [*changes.values(), p.note_id])
            return _get_note(conn, p.note_id)

    note = await _run(q)
    if note is None:
        return _err("Note introuvable", "NOT_FOUND")
    if note is False:
        return _err("Tableau introuvable", "NOT_FOUND")
    await module.events.emit("note_updated", note)
    return note


@module.on("raise_note")
async def raise_note(payload: dict):
    p, err = _parse(NoteIdIn, payload)
    if err:
        return err

    def q():
        with _db() as conn:
            current = _get_note(conn, p.note_id)
            if current is None:
                return None
            top = conn.execute("SELECT COALESCE(MAX(z), 0) AS z FROM notes WHERE board_id = ? AND deleted_at IS NULL",
                               (current["board_id"],)).fetchone()["z"]
            if current["z"] < top or top == 0:
                conn.execute("UPDATE notes SET z = ? WHERE id = ?", (top + 1, p.note_id))
            return _get_note(conn, p.note_id)

    note = await _run(q)
    return note if note is not None else _err("Note introuvable", "NOT_FOUND")


@module.on("duplicate_note")
async def duplicate_note(payload: dict):
    p, err = _parse(NoteIdIn, payload)
    if err:
        return err

    def q():
        with _db() as conn:
            src = _get_note(conn, p.note_id)
            if src is None:
                return None
            note_id, now = str(uuid.uuid4()), _now()
            conn.execute(
                "INSERT INTO notes (id, board_id, title, content, color, pos_x, pos_y, width, height, z, pinned,"
                " collapsed, font, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?)",
                (note_id, src["board_id"], src["title"], src["content"], src["color"], src["pos_x"] + 24,
                 src["pos_y"] + 24, src["width"], src["height"], _next_z(conn, src["board_id"]),
                 int(src["collapsed"]), src["font"], now, now),
            )
            return _get_note(conn, note_id)

    note = await _run(q)
    if note is None:
        return _err("Note introuvable", "NOT_FOUND")
    await module.events.emit("note_created", note)
    return note


@module.on("arrange_notes")
async def arrange_notes(payload: dict):
    p, err = _parse(ArrangeIn, payload)
    if err:
        return err

    def q():
        with _db() as conn:
            conn.executemany("UPDATE notes SET pos_x = ?, pos_y = ? WHERE id = ?",
                             [(x.pos_x, x.pos_y, x.id) for x in p.positions])
    await _run(q)
    return {"ok": True, "count": len(p.positions)}


@module.on("delete_note")
async def delete_note(payload: dict):
    p, err = _parse(NoteDeleteIn, payload)
    if err:
        return err

    def q():
        with _db() as conn:
            if p.permanent:
                return conn.execute("DELETE FROM notes WHERE id = ?", (p.note_id,)).rowcount
            return conn.execute("UPDATE notes SET deleted_at = ? WHERE id = ? AND deleted_at IS NULL",
                                (_now(), p.note_id)).rowcount

    if not await _run(q):
        return _err("Note introuvable", "NOT_FOUND")
    await module.events.emit("note_deleted", {"id": p.note_id})
    return {"ok": True}


@module.on("delete_all_notes")
async def delete_all_notes(payload: dict):
    """Moves every note of a board to the trash (all boards when no board_id is given)."""
    board_id = (payload or {}).get("board_id")
    if board_id is not None:
        p, err = _parse(BoardIdIn, payload)
        if err:
            return err
        board_id = p.board_id

    def q():
        with _db() as conn:
            sql, args = "UPDATE notes SET deleted_at = ? WHERE deleted_at IS NULL", [_now()]
            if board_id:
                sql += " AND board_id = ?"
                args.append(board_id)
            return conn.execute(sql, args).rowcount

    count = await _run(q)
    await module.events.emit("all_notes_deleted", {"board_id": board_id})
    return {"ok": True, "count": count}


# ── Trash ─────────────────────────────────────────────────────────────────────

@module.on("trash.list")
async def trash_list(payload: dict):
    def q():
        with _db() as conn:
            return {"notes": [_note(r) for r in conn.execute(
                "SELECT * FROM notes WHERE deleted_at IS NOT NULL ORDER BY deleted_at DESC")]}
    return await _run(q)


@module.on("restore_note")
async def restore_note(payload: dict):
    p, err = _parse(RestoreIn, payload)
    if err:
        return err

    def q():
        with _db() as conn:
            current = _get_note(conn, p.note_id)
            if current is None or current["deleted_at"] is None:
                return None
            board_id = current["board_id"]
            if not _board_exists(conn, board_id):
                board_id = p.board_id if p.board_id and _board_exists(conn, p.board_id) else _boards(conn)[0]["id"]
            conn.execute("UPDATE notes SET deleted_at = NULL, board_id = ?, z = ? WHERE id = ?",
                         (board_id, _next_z(conn, board_id), p.note_id))
            return _get_note(conn, p.note_id)

    note = await _run(q)
    if note is None:
        return _err("Note introuvable dans la corbeille", "NOT_FOUND")
    await module.events.emit("note_created", note)
    return note


@module.on("trash.empty")
async def trash_empty(payload: dict):
    def q():
        with _db() as conn:
            return conn.execute("DELETE FROM notes WHERE deleted_at IS NOT NULL").rowcount
    return {"ok": True, "count": await _run(q)}


# ── Boards ────────────────────────────────────────────────────────────────────

@module.on("board.save")
async def board_save(payload: dict):
    p, err = _parse(BoardSaveIn, payload)
    if err:
        return err
    name = p.name.strip()
    if not name:
        return _err("name : le nom est vide", "VALIDATION_ERROR")

    def q():
        with _db() as conn:
            if p.id:
                if not conn.execute("UPDATE boards SET name = ? WHERE id = ?", (name, p.id)).rowcount:
                    return None
                board_id = p.id
            else:
                board_id = str(uuid.uuid4())
                pos = conn.execute("SELECT COALESCE(MAX(position), -1) + 1 AS p FROM boards").fetchone()["p"]
                conn.execute("INSERT INTO boards (id, name, position, created_at) VALUES (?, ?, ?, ?)",
                             (board_id, name, pos, _now()))
            return dict(conn.execute("SELECT * FROM boards WHERE id = ?", (board_id,)).fetchone())

    board = await _run(q)
    return board if board is not None else _err("Tableau introuvable", "NOT_FOUND")


@module.on("board.delete")
async def board_delete(payload: dict):
    p, err = _parse(BoardIdIn, payload)
    if err:
        return err

    def q():
        with _db() as conn:
            if not _board_exists(conn, p.board_id):
                return None
            if conn.execute("SELECT COUNT(*) AS n FROM boards").fetchone()["n"] <= 1:
                return False
            trashed = conn.execute("UPDATE notes SET deleted_at = ? WHERE board_id = ? AND deleted_at IS NULL",
                                   (_now(), p.board_id)).rowcount
            conn.execute("DELETE FROM boards WHERE id = ?", (p.board_id,))
            return {"ok": True, "trashed": trashed}

    res = await _run(q)
    if res is None:
        return _err("Tableau introuvable", "NOT_FOUND")
    if res is False:
        return _err("Impossible de supprimer le dernier tableau", "LAST_BOARD")
    return res


@module.on("board.reorder")
async def board_reorder(payload: dict):
    p, err = _parse(BoardOrderIn, payload)
    if err:
        return err

    def q():
        with _db() as conn:
            conn.executemany("UPDATE boards SET position = ? WHERE id = ?", [(i, b) for i, b in enumerate(p.ids)])
            return {"boards": _boards(conn)}
    return await _run(q)


# ── Export / import ───────────────────────────────────────────────────────────

@module.on("export")
async def export_all(payload: dict):
    def q():
        with _db() as conn:
            notes = [_note(r) for r in conn.execute(
                "SELECT * FROM notes WHERE deleted_at IS NULL ORDER BY board_id, z")]
            return {"format": EXPORT_FORMAT, "version": 2, "exported_at": _now(),
                    "boards": _boards(conn), "notes": notes}
    return await _run(q)


@module.on("import")
async def import_all(payload: dict):
    """Appends the boards and notes of an export (new ids: nothing is overwritten)."""
    p, err = _parse(ImportIn, payload)
    if err:
        return err
    data = p.data
    if data.get("format") != EXPORT_FORMAT:
        return _err("Fichier non reconnu (export Sticky Notes attendu)", "BAD_FORMAT")
    raw_notes = data.get("notes")
    raw_boards = data.get("boards") or []
    if not isinstance(raw_notes, list) or not isinstance(raw_boards, list) or len(raw_notes) > MAX_IMPORT:
        return _err("Fichier d'export invalide", "BAD_FORMAT")

    notes: list[tuple[str, NoteFields]] = []
    for raw in raw_notes:
        if not isinstance(raw, dict):
            return _err("Fichier d'export invalide", "BAD_FORMAT")
        fields = {k: raw.get(k) for k in NoteFields.model_fields if k != "board_id"}
        try:
            notes.append((str(raw.get("board_id") or ""), NoteFields.model_validate(fields)))
        except ValidationError as exc:
            return _err(f"Note invalide dans le fichier — {_validation_message(exc)}", "VALIDATION_ERROR")

    def q():
        with _db() as conn:
            existing = {b["name"]: b["id"] for b in _boards(conn)}
            pos = conn.execute("SELECT COALESCE(MAX(position), -1) + 1 AS p FROM boards").fetchone()["p"]
            mapping: dict[str, str] = {}
            created = 0
            for b in raw_boards:
                if not isinstance(b, dict) or not isinstance(b.get("id"), str):
                    continue
                name = str(b.get("name") or "Tableau importé").strip()[:60] or "Tableau importé"
                if name in existing:
                    mapping[b["id"]] = existing[name]
                    continue
                new_id = str(uuid.uuid4())
                conn.execute("INSERT INTO boards (id, name, position, created_at) VALUES (?, ?, ?, ?)",
                             (new_id, name, pos, _now()))
                existing[name] = mapping[b["id"]] = new_id
                pos += 1
                created += 1
            fallback = _boards(conn)[0]["id"]
            now = _now()
            for old_board, n in notes:
                board_id = mapping.get(old_board, fallback)
                conn.execute(
                    "INSERT INTO notes (id, board_id, title, content, color, pos_x, pos_y, width, height, z, pinned,"
                    " collapsed, font, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (str(uuid.uuid4()), board_id, n.title or "", n.content or "", n.color or DEFAULT_COLOR,
                     n.pos_x or 40, n.pos_y or 40, n.width or 220, n.height or 200, _next_z(conn, board_id),
                     int(bool(n.pinned)), int(bool(n.collapsed)), n.font if n.font is not None else 1, now, now),
                )
            return {"ok": True, "notes": len(notes), "boards": created}

    res = await _run(q)
    log.info("imported %d note(s), %d board(s)", res["notes"], res["boards"])
    return res
