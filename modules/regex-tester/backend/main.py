import asyncio
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from ledroide_sdk.http import JSONResponse
from pydantic import BaseModel
from ledroide_sdk import Module, data_path

module = Module(__file__)
router = module.router

DB_PATH = data_path(__file__, "regex-tester.db")
MAX_MATCHES = 5000
MAX_TEXT = 2_000_000

FLAG_MAP = {"i": re.IGNORECASE, "m": re.MULTILINE, "s": re.DOTALL, "x": re.VERBOSE}


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
            CREATE TABLE IF NOT EXISTS patterns (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                name        TEXT NOT NULL,
                pattern     TEXT NOT NULL,
                flags       TEXT NOT NULL DEFAULT '',
                replacement TEXT NOT NULL DEFAULT '',
                engine      TEXT NOT NULL DEFAULT 'python',
                created_at  TEXT NOT NULL
            )
        """)


_init()


def _compile(pattern: str, flags: str):
    bad = [f for f in flags if f not in FLAG_MAP]
    if bad:
        raise ValueError(f"Drapeau inconnu : {''.join(bad)}")
    fl = 0
    for f in flags:
        fl |= FLAG_MAP[f]
    return re.compile(pattern, fl)


def _regex_error(e: re.error):
    content = {"error": f"Expression invalide : {e.msg}", "code": "REGEX_ERROR"}
    if e.pos is not None:
        content["pos"] = e.pos
        content["line"] = e.lineno
        content["column"] = e.colno
    return JSONResponse(status_code=400, content=content)


class MatchIn(BaseModel):
    pattern: str
    flags: str = ""
    text: str = ""


class ReplaceIn(MatchIn):
    replacement: str = ""
    count: int = 0


class PatternIn(BaseModel):
    name: str
    pattern: str
    flags: str = ""
    replacement: str = ""
    engine: str = "python"


def _match_sync(rx, text: str):
    names = {v: k for k, v in rx.groupindex.items()}
    matches = []
    truncated = False
    for i, m in enumerate(rx.finditer(text)):
        if i >= MAX_MATCHES:
            truncated = True
            break
        groups = []
        for g in range(1, rx.groups + 1):
            s, e = m.span(g)
            groups.append({
                "index": g, "name": names.get(g),
                "start": s if s >= 0 else None, "end": e if e >= 0 else None,
                "text": m.group(g),
            })
        matches.append({"index": i, "start": m.start(), "end": m.end(), "text": m.group(0), "groups": groups})
    return {
        "matches": matches, "count": len(matches), "truncated": truncated,
        "group_count": rx.groups,
        "group_names": [names.get(g) for g in range(1, rx.groups + 1)],
    }


@router.get("/health")
async def health():
    return {"ok": True}


@router.post("/match")
async def match(body: MatchIn):
    if len(body.text) > MAX_TEXT:
        return JSONResponse(status_code=400, content={"error": "Texte trop long (max 2 Mo)."})
    try:
        rx = _compile(body.pattern, body.flags)
    except re.error as e:
        return _regex_error(e)
    except ValueError as e:
        return JSONResponse(status_code=400, content={"error": str(e)})
    return await asyncio.to_thread(_match_sync, rx, body.text)


@router.post("/replace")
async def replace(body: ReplaceIn):
    if len(body.text) > MAX_TEXT:
        return JSONResponse(status_code=400, content={"error": "Texte trop long (max 2 Mo)."})
    try:
        rx = _compile(body.pattern, body.flags)
    except re.error as e:
        return _regex_error(e)
    except ValueError as e:
        return JSONResponse(status_code=400, content={"error": str(e)})

    def _run():
        return rx.subn(body.replacement, body.text, count=max(0, body.count))

    try:
        result, n = await asyncio.to_thread(_run)
    except (re.error, IndexError) as e:
        msg = getattr(e, "msg", None) or str(e)
        return JSONResponse(status_code=400, content={"error": f"Remplacement invalide : {msg}", "code": "REPLACE_ERROR"})
    return {"result": result, "count": n}


@router.get("/patterns")
async def list_patterns():
    def _q():
        with _db() as conn:
            return [dict(r) for r in conn.execute("SELECT * FROM patterns ORDER BY name COLLATE NOCASE")]
    return {"patterns": await asyncio.to_thread(_q)}


@router.post("/patterns")
async def save_pattern(body: PatternIn):
    name = body.name.strip()
    if not name:
        return JSONResponse(status_code=400, content={"error": "Le nom est obligatoire."})
    if not body.pattern:
        return JSONResponse(status_code=400, content={"error": "Le motif est vide."})
    if body.engine not in ("python", "js"):
        return JSONResponse(status_code=400, content={"error": "Moteur inconnu."})

    def _q():
        with _db() as conn:
            existing = conn.execute("SELECT id FROM patterns WHERE name=?", (name,)).fetchone()
            if existing:
                conn.execute("UPDATE patterns SET pattern=?, flags=?, replacement=?, engine=? WHERE id=?",
                             (body.pattern, body.flags, body.replacement, body.engine, existing["id"]))
                pid = existing["id"]
            else:
                cur = conn.execute(
                    "INSERT INTO patterns (name, pattern, flags, replacement, engine, created_at) VALUES (?,?,?,?,?,?)",
                    (name, body.pattern, body.flags, body.replacement, body.engine, datetime.now(timezone.utc).isoformat()))
                pid = cur.lastrowid
            return dict(conn.execute("SELECT * FROM patterns WHERE id=?", (pid,)).fetchone())
    return await asyncio.to_thread(_q)


@router.delete("/patterns/{pid}")
async def delete_pattern(pid: int):
    def _q():
        with _db() as conn:
            return conn.execute("DELETE FROM patterns WHERE id=?", (pid,)).rowcount
    if not await asyncio.to_thread(_q):
        return JSONResponse(status_code=404, content={"error": "Motif introuvable."})
    return {"ok": True}
