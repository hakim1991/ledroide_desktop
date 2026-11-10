import asyncio
import csv
import io
import json
import logging
import math
import os
import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from ledroide_sdk import Module

log = logging.getLogger("ledroide.sqlite-browser")

module = Module(__file__)
router = module.router

RECENT_MAX = 15
PAGE_SIZES = (25, 50, 100, 250, 500)
COUNT_BUDGET_S = 2.0
BLOB_PREVIEW = 16
DB_SUFFIXES = (".db", ".sqlite", ".sqlite3", ".db3")


class BrowserError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _err(exc: BrowserError) -> dict:
    return {"error": exc.message, "code": exc.code}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _q(name: str) -> str:
    return '"' + str(name).replace('"', '""') + '"'


def _clean_path(raw) -> Path:
    text = str(raw or "").strip().strip('"').strip("'").strip()
    if not text:
        raise BrowserError("PATH_REQUIRED", "Indiquez le chemin d'un fichier SQLite")
    path = Path(os.path.expandvars(os.path.expanduser(text)))
    try:
        path = path.resolve()
    except OSError as exc:
        raise BrowserError("BAD_PATH", f"Chemin invalide : {exc}")
    if not path.exists():
        raise BrowserError("NOT_FOUND", f"Fichier introuvable : {path}")
    if not path.is_file():
        raise BrowserError("NOT_A_FILE", f"Ce chemin n'est pas un fichier : {path}")
    return path


def _deny_attach(action, *_):
    # ATTACH would create/open other files despite the read-only main database.
    return sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_ATTACH else sqlite3.SQLITE_OK


def _connect(path: Path, readonly: bool) -> sqlite3.Connection:
    try:
        if readonly:
            conn = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, check_same_thread=False,
                                   isolation_level=None)
            conn.execute("PRAGMA query_only = ON")
            conn.set_authorizer(_deny_attach)
        else:
            conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        conn.execute("SELECT count(*) FROM sqlite_master").fetchone()
    except sqlite3.DatabaseError as exc:
        msg = str(exc)
        if "not a database" in msg or "malformed" in msg:
            raise BrowserError("NOT_SQLITE", "Ce fichier n'est pas une base SQLite valide")
        raise BrowserError("OPEN_FAILED", f"Ouverture impossible : {msg}")
    return conn


def _value(v):
    if isinstance(v, (bytes, bytearray, memoryview)):
        b = bytes(v)
        return {"$blob": len(b), "hex": b[:BLOB_PREVIEW].hex()}
    if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
        return str(v)
    if isinstance(v, int) and abs(v) > 2 ** 53:
        return str(v)
    return v


def _export_value(v):
    if isinstance(v, (bytes, bytearray, memoryview)):
        return bytes(v).hex()
    if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
        return str(v)
    return v


def _split_sql(script: str) -> list[str]:
    statements, buf = [], ""
    for ch in script:
        buf += ch
        if ch == ";" and sqlite3.complete_statement(buf):
            statements.append(buf.strip())
            buf = ""
    if buf.strip():
        statements.append(buf.strip())
    return [s for s in statements if s.strip().rstrip(";").strip()]


class Session:
    """The open database; all sqlite3 access happens in worker threads under `lock`."""

    def __init__(self):
        self.lock = threading.Lock()
        self.conn: sqlite3.Connection | None = None
        self.path: Path | None = None
        self.readonly = True

    def open(self, path: Path, readonly: bool):
        conn = _connect(path, readonly)
        with self.lock:
            if self.conn is not None:
                self.conn.close()
            self.conn, self.path, self.readonly = conn, path, readonly

    def close(self):
        with self.lock:
            if self.conn is not None:
                self.conn.close()
            self.conn, self.path = None, None

    def require(self) -> sqlite3.Connection:
        if self.conn is None:
            raise BrowserError("NOT_OPEN", "Aucune base ouverte")
        return self.conn

    def info(self) -> dict:
        if self.path is None:
            return {"open": False}
        try:
            size = self.path.stat().st_size
        except OSError:
            size = None
        return {"open": True, "path": str(self.path), "name": self.path.name, "size": size,
                "readonly": self.readonly, "sqlite_version": sqlite3.sqlite_version}


session = Session()


def _objects(conn: sqlite3.Connection) -> dict:
    rows = conn.execute(
        "SELECT type, name, tbl_name, sql FROM sqlite_master "
        "WHERE type IN ('table','view','index','trigger') ORDER BY type, name").fetchall()
    out = {"tables": [], "views": [], "indexes": [], "triggers": []}
    started = time.perf_counter()
    for typ, name, tbl, sql in rows:
        if typ == "table":
            count = None
            if time.perf_counter() - started < COUNT_BUDGET_S:
                try:
                    count = conn.execute(f"SELECT count(*) FROM {_q(name)}").fetchone()[0]
                except sqlite3.DatabaseError:
                    count = None
            out["tables"].append({"name": name, "rows": count, "sql": sql, "system": name.startswith("sqlite_")})
        elif typ == "view":
            out["views"].append({"name": name, "sql": sql})
        elif typ == "index":
            out["indexes"].append({"name": name, "table": tbl, "sql": sql, "auto": sql is None})
        else:
            out["triggers"].append({"name": name, "table": tbl, "sql": sql})
    return out


def _columns(conn: sqlite3.Connection, name: str) -> list[dict]:
    cols = []
    for r in conn.execute(f"PRAGMA table_xinfo({_q(name)})").fetchall():
        cid, cname, ctype, notnull, default, pk, hidden = r
        cols.append({"name": cname, "type": ctype or "", "notnull": bool(notnull), "default": default,
                     "pk": pk, "hidden": hidden})
    return cols


def _object_kind(conn: sqlite3.Connection, name: str) -> str:
    row = conn.execute("SELECT type FROM sqlite_master WHERE name = ? AND type IN ('table','view')",
                       (name,)).fetchone()
    if row is None:
        if name in ("sqlite_master", "sqlite_schema"):
            return "table"
        raise BrowserError("NO_SUCH_TABLE", f"Table ou vue inconnue : {name}")
    return row[0]


def _details(conn: sqlite3.Connection, name: str) -> dict:
    kind = _object_kind(conn, name)
    sql_row = conn.execute("SELECT sql FROM sqlite_master WHERE name = ?", (name,)).fetchone()
    detail = {"name": name, "kind": kind, "columns": _columns(conn, name), "sql": sql_row[0] if sql_row else None}
    if kind == "table":
        indexes = []
        for r in conn.execute(f"PRAGMA index_list({_q(name)})").fetchall():
            iname, unique, origin = r[1], bool(r[2]), r[3]
            icols = [c[2] for c in conn.execute(f"PRAGMA index_info({_q(iname)})").fetchall()]
            indexes.append({"name": iname, "unique": unique, "origin": origin, "columns": icols})
        detail["indexes"] = indexes
        detail["foreign_keys"] = [
            {"from": r[3], "table": r[2], "to": r[4], "on_update": r[5], "on_delete": r[6]}
            for r in conn.execute(f"PRAGMA foreign_key_list({_q(name)})").fetchall()
        ]
        try:
            detail["rows"] = conn.execute(f"SELECT count(*) FROM {_q(name)}").fetchone()[0]
        except sqlite3.DatabaseError:
            detail["rows"] = None
    return detail


def _where(columns: list[str], text: str) -> tuple[str, list]:
    text = (text or "").strip()
    if not text or not columns:
        return "", []
    pattern = "%" + text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
    clause = " OR ".join(f"CAST({_q(c)} AS TEXT) LIKE ? ESCAPE '\\'" for c in columns)
    return f" WHERE ({clause})", [pattern] * len(columns)


def _page(conn, name: str, page: int, size: int, sort: str | None, desc: bool, filt: str) -> dict:
    _object_kind(conn, name)
    cols = [c["name"] for c in _columns(conn, name) if not c["hidden"]]
    where, params = _where(cols, filt)
    total = conn.execute(f"SELECT count(*) FROM {_q(name)}{where}", params).fetchone()[0]
    order = ""
    if sort:
        if sort not in cols:
            raise BrowserError("BAD_SORT", f"Colonne de tri inconnue : {sort}")
        order = f" ORDER BY {_q(sort)} {'DESC' if desc else 'ASC'}"
    pages = max(1, math.ceil(total / size))
    page = min(max(1, page), pages)
    cur = conn.execute(f"SELECT * FROM {_q(name)}{where}{order} LIMIT ? OFFSET ?",
                       [*params, size, (page - 1) * size])
    columns = [d[0] for d in cur.description]
    rows = [[_value(v) for v in r] for r in cur.fetchall()]
    return {"columns": columns, "rows": rows, "total": total, "page": page, "pages": pages, "page_size": size}


def _execute(conn: sqlite3.Connection, script: str, max_rows: int) -> dict:
    statements = _split_sql(script)
    if not statements:
        raise BrowserError("EMPTY_SQL", "Aucune instruction SQL à exécuter")
    before = conn.total_changes
    started = time.perf_counter()
    result = None
    done = 0
    try:
        for stmt in statements:
            cur = conn.execute(stmt)
            done += 1
            if cur.description:
                rows = cur.fetchmany(max_rows + 1)
                result = {"columns": [d[0] for d in cur.description],
                          "rows": [[_value(v) for v in r] for r in rows[:max_rows]],
                          "truncated": len(rows) > max_rows}
    except sqlite3.Error as exc:
        msg = str(exc)
        code = "SQL_ERROR"
        if "readonly" in msg or "read-only" in msg or "query_only" in msg or "not authorized" in msg:
            code = "READ_ONLY"
            msg = "Base ouverte en lecture seule : passez en mode écriture pour modifier les données"
        elif "interrupted" in msg:
            code = "INTERRUPTED"
            msg = "Requête interrompue"
        where = f" (instruction {done + 1}/{len(statements)})" if len(statements) > 1 else ""
        raise BrowserError(code, msg + where)
    elapsed = (time.perf_counter() - started) * 1000
    return {"result": result, "statements": len(statements), "changes": conn.total_changes - before,
            "elapsed_ms": round(elapsed, 2), "in_transaction": conn.in_transaction}


async def _locked(fn, *args):
    def run():
        with session.lock:
            return fn(session.require(), *args)
    return await asyncio.to_thread(run)


async def _guard(coro):
    try:
        return await coro
    except BrowserError as exc:
        return _err(exc)
    except sqlite3.Error as exc:
        return {"error": f"Erreur SQLite : {exc}", "code": "SQL_ERROR"}


# ── Open / close ─────────────────────────────────────────────────────────────

async def _remember(path: Path):
    doc_id = os.path.normcase(str(path))
    await module.db.save("recent", {"_id": doc_id, "path": str(path), "name": path.name, "at": _now()})
    for old in (await module.db.query("recent"))[RECENT_MAX:]:
        await module.db.delete("recent", old["_id"])


@module.on("open")
async def open_db(payload: dict):
    async def run():
        path = _clean_path(payload.get("path"))
        readonly = payload.get("readonly", True) is not False
        await asyncio.to_thread(session.open, path, readonly)
        await _remember(path)
        log.info("opened %s (readonly=%s)", path.name, readonly)
        return {**session.info(), "objects": await _locked(_objects)}
    return await _guard(run())


@module.on("set_mode")
async def set_mode(payload: dict):
    async def run():
        if session.path is None:
            raise BrowserError("NOT_OPEN", "Aucune base ouverte")
        readonly = bool(payload.get("readonly", True))
        if session.conn is not None and session.conn.in_transaction and not payload.get("force"):
            raise BrowserError("IN_TRANSACTION", "Une transaction est en cours : faites COMMIT ou ROLLBACK avant de changer de mode")
        await asyncio.to_thread(session.open, session.path, readonly)
        log.info("mode readonly=%s", readonly)
        return session.info()
    return await _guard(run())


@module.on("close")
async def close_db(payload: dict):
    await asyncio.to_thread(session.close)
    return {"ok": True}


@module.on("status")
async def status(payload: dict):
    return session.info()


@module.on("interrupt")
async def interrupt(payload: dict):
    conn = session.conn
    if conn is not None:
        conn.interrupt()
    return {"ok": True}


@module.on("recent_list")
async def recent_list(payload: dict):
    items = await module.db.query("recent")
    for it in items:
        it["exists"] = os.path.isfile(it.get("path", ""))
    return {"items": items}


@module.on("recent_remove")
async def recent_remove(payload: dict):
    return await module.db.delete("recent", str(payload.get("id", "")))


def _module_dbs(modules_dir: Path) -> list[dict]:
    items = []
    if not modules_dir.is_dir():
        return items
    for sub in sorted(modules_dir.iterdir()):
        if not sub.is_dir():
            continue
        for f in sorted(sub.iterdir()):
            if f.is_file() and f.suffix.lower() in DB_SUFFIXES:
                try:
                    size = f.stat().st_size
                except OSError:
                    size = None
                items.append({"module": sub.name, "name": f.name, "path": str(f), "size": size})
    return items


@module.on("module_dbs")
async def module_dbs(payload: dict):
    base = os.environ.get("LEDROIDE_DATA_DIR")
    modules_dir = Path(base) / "modules" if base else module.data_dir.parent
    return {"dir": str(modules_dir), "items": await asyncio.to_thread(_module_dbs, modules_dir)}


# ── Schema / data / SQL ──────────────────────────────────────────────────────

@module.on("schema")
async def schema(payload: dict):
    return await _guard(_locked(lambda c: {**session.info(), "objects": _objects(c)}))


@module.on("details")
async def details(payload: dict):
    return await _guard(_locked(_details, str(payload.get("name", ""))))


@module.on("rows")
async def rows(payload: dict):
    size = int(payload.get("page_size") or 100)
    if size not in PAGE_SIZES:
        size = 100
    try:
        page = int(payload.get("page") or 1)
    except (TypeError, ValueError):
        page = 1
    return await _guard(_locked(_page, str(payload.get("name", "")), page, size,
                                payload.get("sort") or None, bool(payload.get("desc")),
                                str(payload.get("filter") or "")))


async def _add_history(entry: dict):
    await module.db.save("history", {**entry, "at": _now()})
    keep = int(module.config.get("history_size", 100))
    for old in (await module.db.query("history"))[keep:]:
        await module.db.delete("history", old["_id"])


@module.on("execute")
async def execute(payload: dict):
    sql = str(payload.get("sql") or "")
    max_rows = int(module.config.get("max_rows", 1000))
    try:
        out = await _locked(_execute, sql, max_rows)
    except BrowserError as exc:
        if exc.code not in ("NOT_OPEN", "EMPTY_SQL"):
            await _add_history({"sql": sql[:20000], "ok": False, "error": exc.message})
        return _err(exc)
    res = out["result"]
    await _add_history({"sql": sql[:20000], "ok": True, "elapsed_ms": out["elapsed_ms"],
                        "rows": len(res["rows"]) if res else None, "changes": out["changes"]})
    log.info("executed %d statement(s) in %.1f ms", out["statements"], out["elapsed_ms"])
    return out


@module.on("history_list")
async def history_list(payload: dict):
    return {"items": await module.db.query("history")}


@module.on("history_clear")
async def history_clear(payload: dict):
    for item in await module.db.query("history"):
        await module.db.delete("history", item["_id"])
    return {"ok": True}


# ── Export ───────────────────────────────────────────────────────────────────

def _export(path: Path, table: str | None, sql: str | None, fmt: str, limit: int,
            sort: str | None, desc: bool, filt: str) -> dict:
    conn = _connect(path, readonly=True)
    try:
        if table:
            _object_kind(conn, table)
            cols = [c["name"] for c in _columns(conn, table) if not c["hidden"]]
            where, params = _where(cols, filt)
            order = f" ORDER BY {_q(sort)} {'DESC' if desc else 'ASC'}" if sort in cols else ""
            cur = conn.execute(f"SELECT * FROM {_q(table)}{where}{order}", params)
        else:
            statements = _split_sql(sql or "")
            if not statements:
                raise BrowserError("EMPTY_SQL", "Aucune requête à exporter")
            try:
                cur = conn.execute(statements[-1])
            except sqlite3.Error as exc:
                msg = str(exc)
                if "readonly" in msg or "query_only" in msg or "not authorized" in msg:
                    raise BrowserError("READ_ONLY", "Seules les requêtes de lecture peuvent être exportées")
                raise BrowserError("SQL_ERROR", msg)
        if not cur.description:
            raise BrowserError("NO_RESULT", "Cette requête ne renvoie aucune ligne à exporter")
        columns = [d[0] for d in cur.description]
        rows = cur.fetchmany(limit + 1)
        truncated = len(rows) > limit
        rows = rows[:limit]
    finally:
        conn.close()

    if fmt == "json":
        content = json.dumps([{c: _export_value(v) for c, v in zip(columns, r)} for r in rows],
                             ensure_ascii=False, indent=2, default=str)
    else:
        buf = io.StringIO()
        writer = csv.writer(buf, lineterminator="\r\n")
        writer.writerow(columns)
        for r in rows:
            writer.writerow(["" if v is None else _export_value(v) for v in r])
        content = buf.getvalue()
    return {"content": content, "rows": len(rows), "truncated": truncated}


@module.on("export")
async def export(payload: dict):
    async def run():
        if session.path is None:
            raise BrowserError("NOT_OPEN", "Aucune base ouverte")
        fmt = "json" if payload.get("format") == "json" else "csv"
        table = payload.get("table") or None
        if not table and not payload.get("sql"):
            raise BrowserError("BAD_REQUEST", "Indiquez une table ou une requête à exporter")
        limit = int(module.config.get("export_max_rows", 200000))
        out = await asyncio.to_thread(_export, session.path, table, payload.get("sql"), fmt, limit,
                                      payload.get("sort") or None, bool(payload.get("desc")),
                                      str(payload.get("filter") or ""))
        base = table or "resultat"
        safe = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in base)[:60] or "export"
        return {**out, "filename": f"{safe}.{fmt}", "mime": "application/json" if fmt == "json" else "text/csv"}
    return await _guard(run())


@module.on_shutdown
async def shutdown():
    await asyncio.to_thread(session.close)
