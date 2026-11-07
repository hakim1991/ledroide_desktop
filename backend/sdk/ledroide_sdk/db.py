import asyncio
import json
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class SQLiteAdapter:
    """Document store + raw SQL on a per-module SQLite file, run off the event loop."""

    def __init__(self, path: Path):
        self.path = path
        self._conn: sqlite3.Connection | None = None
        self._lock = threading.Lock()
        self._collections: set[str] = set()

    def _connect(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA journal_mode=WAL")
        return self._conn

    async def _run(self, fn, *args):
        def locked():
            with self._lock:
                conn = self._connect()
                try:
                    result = fn(conn, *args)
                    conn.commit()
                    return result
                except Exception:
                    conn.rollback()
                    raise
        return await asyncio.to_thread(locked)

    # ── Raw SQL ──────────────────────────────────────────────────────────────

    async def execute(self, sql: str, params: tuple | dict = ()) -> int:
        return await self._run(lambda c: c.execute(sql, params).rowcount)

    async def executescript(self, script: str) -> None:
        await self._run(lambda c: c.executescript(script))

    async def fetch_all(self, sql: str, params: tuple | dict = ()) -> list[dict]:
        return await self._run(lambda c: [dict(r) for r in c.execute(sql, params).fetchall()])

    async def fetch_one(self, sql: str, params: tuple | dict = ()) -> dict | None:
        def q(c):
            row = c.execute(sql, params).fetchone()
            return dict(row) if row else None
        return await self._run(q)

    # ── Document API ─────────────────────────────────────────────────────────

    @staticmethod
    def _table(collection: str) -> str:
        if not collection.replace("_", "").isalnum():
            raise ValueError(f"Nom de collection invalide : {collection!r}")
        return f"doc_{collection}"

    def _ensure(self, c: sqlite3.Connection, collection: str) -> str:
        table = self._table(collection)
        if collection not in self._collections:
            c.execute(f"CREATE TABLE IF NOT EXISTS {table} (id TEXT PRIMARY KEY, data TEXT NOT NULL, updated_at TEXT NOT NULL)")
            self._collections.add(collection)
        return table

    async def save(self, collection: str, document: dict) -> dict:
        doc = {**document, "_id": document.get("_id") or uuid.uuid4().hex}
        now = datetime.now(timezone.utc).isoformat()

        def q(c):
            table = self._ensure(c, collection)
            c.execute(
                f"INSERT INTO {table}(id, data, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET data=excluded.data, updated_at=excluded.updated_at",
                (doc["_id"], json.dumps(doc), now),
            )
            return doc
        return await self._run(q)

    async def get(self, collection: str, doc_id: str) -> dict | None:
        def q(c):
            table = self._ensure(c, collection)
            row = c.execute(f"SELECT data FROM {table} WHERE id=?", (doc_id,)).fetchone()
            return json.loads(row["data"]) if row else None
        return await self._run(q)

    async def query(self, collection: str, filters: dict | None = None) -> list[dict]:
        filters = filters or {}

        def q(c):
            table = self._ensure(c, collection)
            rows = c.execute(f"SELECT data FROM {table} ORDER BY updated_at DESC").fetchall()
            docs = [json.loads(r["data"]) for r in rows]
            return [d for d in docs if all(d.get(k) == v for k, v in filters.items())]
        return await self._run(q)

    async def delete(self, collection: str, doc_id: str) -> dict:
        def q(c):
            table = self._ensure(c, collection)
            return c.execute(f"DELETE FROM {table} WHERE id=?", (doc_id,)).rowcount
        deleted = await self._run(q)
        return {"ok": bool(deleted), "id": doc_id}

    async def close(self):
        if self._conn is not None:
            await asyncio.to_thread(self._conn.close)
            self._conn = None


class _DB:
    """`module.db` — adapter chosen from manifest `database.type`.
    Only "sqlite" is implemented; couchdb/postgresql are reserved."""

    def __init__(self, module=None):
        self._module = module
        self._adapter: Any = None

    def _inject(self, adapter):
        self._adapter = adapter

    def _get(self):
        if self._adapter is not None:
            return self._adapter
        if self._module is None:
            raise RuntimeError("DB indisponible : utilisez module.db (instance Module)")
        db_conf = self._module.manifest.get("database") or {}
        db_type = db_conf.get("type", "none")
        if db_type == "sqlite":
            name = db_conf.get("file", f"{self._module.id}.db")
            self._adapter = SQLiteAdapter(self._module.data_dir / name)
            return self._adapter
        if db_type in ("none", None):
            raise RuntimeError(f"Le module '{self._module.id}' ne déclare aucune base (database.type = none)")
        raise NotImplementedError(f"Adaptateur DB '{db_type}' non implémenté — utilisez 'sqlite'")

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        return getattr(self._get(), name)

    async def close(self):
        if self._adapter is not None:
            await self._adapter.close()


db = _DB()
