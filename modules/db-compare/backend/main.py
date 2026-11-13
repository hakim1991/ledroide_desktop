import asyncio
import datetime as dt
import decimal
import json
import logging
import math
import re
import sqlite3
import time
import uuid
from collections import OrderedDict
from pathlib import Path
from typing import Any, Literal, Optional
from urllib.parse import urlsplit, urlunsplit

from pydantic import BaseModel, Field, ValidationError, field_validator

from ledroide_sdk import Module

log = logging.getLogger("ledroide.db-compare")

module = Module(__file__)
router = module.router

SQLITE_HEADER = b"SQLite format 3\x00"
PG_SCHEMA_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]{0,62}$")
MAX_CACHE = 6
MAX_TEXT = 500

_ready = False
_lock = asyncio.Lock()
_schema_cache: "OrderedDict[str, dict]" = OrderedDict()
_data_cache: "OrderedDict[str, dict]" = OrderedDict()

SCHEMA = """
CREATE TABLE IF NOT EXISTS recents (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    kind      TEXT NOT NULL,
    value     TEXT NOT NULL,
    pg_schema TEXT NOT NULL DEFAULT 'public',
    used_at   TEXT NOT NULL,
    UNIQUE(kind, value, pg_schema)
);
"""


async def _db():
    global _ready
    if not _ready:
        async with _lock:
            if not _ready:
                await module.db.executescript(SCHEMA)
                _ready = True
    return module.db


def _err(message: str, code: str = "BAD_REQUEST") -> dict:
    return {"error": message, "code": code}


def _validation_message(exc: ValidationError) -> str:
    first = exc.errors()[0]
    loc = ".".join(str(p) for p in first.get("loc", ()))
    msg = str(first.get("msg", "Données invalides")).removeprefix("Value error, ")
    return f"{loc} : {msg}" if loc else msg


def _cache_put(cache: OrderedDict, value: dict) -> str:
    key = uuid.uuid4().hex
    cache[key] = value
    while len(cache) > MAX_CACHE:
        cache.popitem(last=False)
    return key


# ── Connexions ────────────────────────────────────────────────────────────────

def redact_url(url: str) -> str:
    try:
        parts = urlsplit(url)
    except ValueError:
        return "postgresql://…"
    if parts.password is None:
        return url
    netloc = parts.netloc.rsplit("@", 1)
    user = netloc[0].split(":", 1)[0]
    return urlunsplit((parts.scheme, f"{user}@{netloc[1]}", parts.path, parts.query, parts.fragment))


def _strip_password(url: str) -> str:
    return redact_url(url)


class ConnIn(BaseModel):
    kind: Literal["sqlite", "postgresql"]
    path: str = Field("", max_length=1000)
    url: str = Field("", max_length=2000)
    password: str = Field("", max_length=1000)
    pg_schema: str = Field("public", max_length=63)

    @field_validator("path", "url", "pg_schema")
    @classmethod
    def _strip(cls, v: str) -> str:
        return v.strip().strip('"')

    def check(self) -> Optional[str]:
        if self.kind == "sqlite":
            if not self.path:
                return "Chemin du fichier SQLite requis"
            p = Path(self.path)
            if not p.is_absolute():
                return "Le chemin du fichier doit être absolu (ex. C:\\dossier\\base.db)"
            if not p.is_file():
                return f"Fichier introuvable : {self.path}"
            try:
                with p.open("rb") as fh:
                    head = fh.read(16)
            except OSError as exc:
                return f"Lecture impossible : {exc.strerror or exc}"
            if head and head != SQLITE_HEADER:
                return "Ce fichier n'est pas une base SQLite"
            return None
        if not re.match(r"^postgres(ql)?://", self.url, re.I):
            return "URL PostgreSQL attendue : postgresql://utilisateur@hôte:5432/base"
        if not PG_SCHEMA_RE.match(self.pg_schema or ""):
            return "Nom de schéma PostgreSQL invalide"
        return None

    def label(self) -> str:
        if self.kind == "sqlite":
            return Path(self.path).name
        try:
            p = urlsplit(self.url)
            return f"{p.hostname or 'localhost'}/{(p.path or '/').lstrip('/') or '?'} ({self.pg_schema})"
        except ValueError:
            return "PostgreSQL"


def _parse_conn(raw: Any) -> ConnIn:
    conn = ConnIn.model_validate(raw or {})
    if msg := conn.check():
        raise ValueError(msg)
    return conn


def _qi(name: str) -> str:
    return '"' + str(name).replace('"', '""') + '"'


def _sanitize(msg: str, conn: Optional[ConnIn]) -> str:
    msg = str(msg).strip()
    if conn is not None and conn.kind == "postgresql":
        secrets = [conn.password] if conn.password else []
        try:
            if urlsplit(conn.url).password:
                secrets.append(urlsplit(conn.url).password)
        except ValueError:
            pass
        for s in secrets:
            if s:
                msg = msg.replace(s, "***")
        msg = msg.replace(conn.url, redact_url(conn.url))
    return msg[:600]


def _psycopg():
    try:
        import psycopg
        return psycopg
    except ImportError:
        return None


class Source:
    dialect = ""

    def schema(self) -> dict:
        raise NotImplementedError

    def count(self, table: str) -> int:
        raise NotImplementedError

    def fetch(self, table: str, keys: list[str], cols: list[str], text_keys: set[str], after: Optional[tuple], limit: int) -> list[tuple]:
        raise NotImplementedError

    def close(self):
        pass


class SQLiteSource(Source):
    dialect = "sqlite"

    def __init__(self, path: str):
        uri = Path(path).resolve().as_uri() + "?mode=ro"
        self.conn = sqlite3.connect(uri, uri=True, check_same_thread=False, timeout=5)
        self.conn.execute("PRAGMA query_only = ON")
        self.version = "SQLite " + sqlite3.sqlite_version

    def qt(self, table: str) -> str:
        return _qi(table)

    def schema(self) -> dict:
        c = self.conn
        rows = c.execute(
            "SELECT name, sql FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite\\_%' ESCAPE '\\' ORDER BY name"
        ).fetchall()
        tables = {}
        for name, sql in rows:
            cols = []
            pk = []
            for cid, cname, ctype, notnull, dflt, pkpos in c.execute(f"PRAGMA table_info({_qi(name)})").fetchall():
                cols.append({"name": cname, "type": ctype or "", "nullable": not notnull and not pkpos,
                             "default": dflt, "auto": False, "identity": ""})
                if pkpos:
                    pk.append((pkpos, cname))
            pk = [n for _, n in sorted(pk)]
            if len(pk) == 1:
                col = next(x for x in cols if x["name"] == pk[0])
                if (col["type"] or "").strip().upper() == "INTEGER":
                    col["auto"] = True
                    col["nullable"] = False
            fks: dict[int, dict] = {}
            for fid, seq, ref, frm, to, on_upd, on_del, _match in c.execute(f"PRAGMA foreign_key_list({_qi(name)})").fetchall():
                fk = fks.setdefault(fid, {"name": "", "columns": [], "ref_table": ref, "ref_columns": [],
                                          "on_update": on_upd, "on_delete": on_del})
                fk["columns"].append(frm)
                fk["ref_columns"].append(to)
            indexes = []
            for _seq, iname, unique, origin, partial in c.execute(f"PRAGMA index_list({_qi(name)})").fetchall():
                if origin == "pk":
                    continue
                icols = []
                for _sn, _cid, cname in c.execute(f"PRAGMA index_info({_qi(iname)})").fetchall():
                    icols.append(cname if cname is not None else "(expression)")
                isql = c.execute("SELECT sql FROM sqlite_master WHERE type='index' AND name=?", (iname,)).fetchone()
                indexes.append({"name": iname, "columns": icols, "unique": bool(unique),
                                "constraint": origin == "u", "partial": bool(partial),
                                "sql": isql[0] if isql else None})
            tables[name] = {"name": name, "columns": cols, "pk": pk, "pk_name": "", "fks": list(fks.values()),
                            "indexes": indexes, "sql": sql}
        for t in tables.values():
            for fk in t["fks"]:
                if any(x is None for x in fk["ref_columns"]):
                    ref = _find(tables, fk["ref_table"])
                    fk["ref_columns"] = list(ref["pk"]) if ref else fk["columns"]
        return {"dialect": "sqlite", "tables": tables}

    def count(self, table: str) -> int:
        return self.conn.execute(f"SELECT COUNT(*) FROM {self.qt(table)}").fetchone()[0]

    def fetch(self, table, keys, cols, text_keys, after, limit):
        sel = ", ".join(_qi(k) for k in keys + cols)
        where = [f"{_qi(k)} IS NOT NULL" for k in keys]
        params: list = []
        if after is not None:
            ors = []
            for i in range(len(keys)):
                terms = [f"{_qi(keys[j])} = ? COLLATE BINARY" for j in range(i)]
                terms.append(f"{_qi(keys[i])} > ? COLLATE BINARY")
                params.extend(after[: i + 1])
                ors.append("(" + " AND ".join(terms) + ")")
            where.append("(" + " OR ".join(ors) + ")")
        order = ", ".join(f"{_qi(k)} COLLATE BINARY" for k in keys)
        sql = f"SELECT {sel} FROM {self.qt(table)} WHERE {' AND '.join(where)} ORDER BY {order} LIMIT ?"
        params.append(limit)
        return [tuple(r) for r in self.conn.execute(sql, params).fetchall()]

    def close(self):
        self.conn.close()


PG_FK_ACTIONS = {"a": "NO ACTION", "r": "RESTRICT", "c": "CASCADE", "n": "SET NULL", "d": "SET DEFAULT"}

PG_COLUMNS = """
SELECT c.relname, a.attname, format_type(a.atttypid, a.atttypmod), NOT a.attnotnull,
       pg_get_expr(d.adbin, d.adrelid), a.attidentity
FROM pg_attribute a
JOIN pg_class c ON c.oid = a.attrelid
JOIN pg_namespace n ON n.oid = c.relnamespace
LEFT JOIN pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum
WHERE n.nspname = %s AND c.relkind IN ('r', 'p') AND a.attnum > 0 AND NOT a.attisdropped
  AND NOT c.relispartition
ORDER BY c.relname, a.attnum
"""

PG_CONSTRAINTS = """
SELECT con.conname, c.relname, con.contype,
       ARRAY(SELECT a.attname FROM unnest(con.conkey) WITH ORDINALITY k(n, o)
             JOIN pg_attribute a ON a.attrelid = con.conrelid AND a.attnum = k.n ORDER BY k.o),
       fc.relname,
       ARRAY(SELECT a.attname FROM unnest(con.confkey) WITH ORDINALITY k(n, o)
             JOIN pg_attribute a ON a.attrelid = con.confrelid AND a.attnum = k.n ORDER BY k.o),
       con.confupdtype, con.confdeltype
FROM pg_constraint con
JOIN pg_class c ON c.oid = con.conrelid
JOIN pg_namespace n ON n.oid = c.relnamespace
LEFT JOIN pg_class fc ON fc.oid = con.confrelid
WHERE n.nspname = %s AND con.contype IN ('p', 'f', 'u')
ORDER BY c.relname, con.conname
"""

PG_INDEXES = """
SELECT ic.relname, c.relname, i.indisunique, i.indpred IS NOT NULL,
       ARRAY(SELECT CASE WHEN i.indkey[k - 1] = 0 THEN '(' || pg_get_indexdef(i.indexrelid, k, true) || ')'
                         ELSE (SELECT attname FROM pg_attribute WHERE attrelid = i.indrelid AND attnum = i.indkey[k - 1]) END
             FROM generate_series(1, i.indnkeyatts) k ORDER BY k),
       EXISTS (SELECT 1 FROM pg_constraint con WHERE con.conindid = i.indexrelid AND con.contype = 'u'),
       pg_get_indexdef(i.indexrelid)
FROM pg_index i
JOIN pg_class ic ON ic.oid = i.indexrelid
JOIN pg_class c ON c.oid = i.indrelid
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = %s AND c.relkind IN ('r', 'p') AND NOT i.indisprimary
ORDER BY c.relname, ic.relname
"""


class PGSource(Source):
    dialect = "postgresql"

    def __init__(self, conn: ConnIn):
        psycopg = _psycopg()
        if psycopg is None:
            raise RuntimeError("Le pilote psycopg n'est pas installé : réinstallez les dépendances du module")
        kwargs: dict[str, Any] = {"connect_timeout": 10, "application_name": "LedroideDesktop db-compare"}
        if conn.password:
            kwargs["password"] = conn.password
        self.conn = psycopg.connect(conn.url, **kwargs)
        self.conn.read_only = True
        self.schema_name = conn.pg_schema or "public"
        with self.conn.cursor() as cur:
            cur.execute("SET statement_timeout = 300000")
            cur.execute("SHOW server_version")
            self.version = "PostgreSQL " + str(cur.fetchone()[0]).split(" ")[0]
            cur.execute("SELECT 1 FROM pg_namespace WHERE nspname = %s", (self.schema_name,))
            if cur.fetchone() is None:
                raise ValueError(f"Schéma « {self.schema_name} » introuvable dans cette base")

    def qt(self, table: str) -> str:
        return f"{_qi(self.schema_name)}.{_qi(table)}"

    def _all(self, sql: str, params=()) -> list[tuple]:
        with self.conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchall()

    def schema(self) -> dict:
        s = self.schema_name
        tables: dict[str, dict] = {}
        for tname, cname, ctype, nullable, dflt, ident in self._all(PG_COLUMNS, (s,)):
            t = tables.setdefault(tname, {"name": tname, "columns": [], "pk": [], "pk_name": "", "fks": [],
                                          "indexes": [], "sql": None})
            auto = bool(ident) or (dflt or "").startswith("nextval(")
            t["columns"].append({"name": cname, "type": ctype, "nullable": bool(nullable), "default": dflt,
                                 "auto": auto, "identity": ident or ""})
        for cname, tname, ctype, cols, ref, ref_cols, upd, dele in self._all(PG_CONSTRAINTS, (s,)):
            t = tables.get(tname)
            if not t:
                continue
            if ctype == "p":
                t["pk"] = list(cols)
                t["pk_name"] = cname
            elif ctype == "f":
                t["fks"].append({"name": cname, "columns": list(cols), "ref_table": ref, "ref_columns": list(ref_cols),
                                 "on_update": PG_FK_ACTIONS.get(upd, "NO ACTION"),
                                 "on_delete": PG_FK_ACTIONS.get(dele, "NO ACTION")})
        for iname, tname, unique, partial, cols, is_con, idef in self._all(PG_INDEXES, (s,)):
            t = tables.get(tname)
            if t:
                t["indexes"].append({"name": iname, "columns": list(cols), "unique": bool(unique), "constraint": bool(is_con),
                                     "partial": bool(partial), "sql": idef})
        return {"dialect": "postgresql", "tables": dict(sorted(tables.items()))}

    def count(self, table: str) -> int:
        return self._all(f"SELECT COUNT(*) FROM {self.qt(table)}")[0][0]

    def fetch(self, table, keys, cols, text_keys, after, limit):
        def k(name: str) -> str:
            return _qi(name) + (' COLLATE "C"' if name in text_keys else "")
        sel = ", ".join(_qi(c) for c in keys + cols)
        where = [f"{_qi(c)} IS NOT NULL" for c in keys]
        params: list = []
        if after is not None:
            ors = []
            for i in range(len(keys)):
                terms = [f"{k(keys[j])} = %s" for j in range(i)]
                terms.append(f"{k(keys[i])} > %s")
                params.extend(after[: i + 1])
                ors.append("(" + " AND ".join(terms) + ")")
            where.append("(" + " OR ".join(ors) + ")")
        order = ", ".join(k(c) for c in keys)
        params.append(limit)
        return [tuple(r) for r in self._all(
            f"SELECT {sel} FROM {self.qt(table)} WHERE {' AND '.join(where)} ORDER BY {order} LIMIT %s", params)]

    def close(self):
        try:
            self.conn.rollback()
        finally:
            self.conn.close()


def _open_sync(conn: ConnIn) -> Source:
    if conn.kind == "sqlite":
        return SQLiteSource(conn.path)
    return PGSource(conn)


async def _open(conn: ConnIn) -> Source:
    return await asyncio.to_thread(_open_sync, conn)


async def _close(src: Optional[Source]):
    if src is not None:
        try:
            await asyncio.to_thread(src.close)
        except Exception:
            log.debug("close failed", exc_info=True)


async def _remember(conn: ConnIn):
    value = conn.path if conn.kind == "sqlite" else _strip_password(conn.url)
    db = await _db()
    await db.execute(
        "INSERT INTO recents (kind, value, pg_schema, used_at) VALUES (?,?,?,?) "
        "ON CONFLICT(kind, value, pg_schema) DO UPDATE SET used_at=excluded.used_at",
        (conn.kind, value, conn.pg_schema if conn.kind == "postgresql" else "", dt.datetime.now(dt.timezone.utc).isoformat()))
    await db.execute("DELETE FROM recents WHERE id NOT IN (SELECT id FROM recents ORDER BY used_at DESC LIMIT 30)")


def _friendly(exc: Exception, conn: Optional[ConnIn]) -> str:
    msg = _sanitize(str(exc) or type(exc).__name__, conn)
    low = msg.lower()
    if "password authentication failed" in low:
        return "Authentification refusée : mot de passe ou utilisateur incorrect"
    if "no password supplied" in low:
        return "Le serveur exige un mot de passe"
    if "connection refused" in low or "actively refused" in low:
        return "Connexion refusée : aucun serveur PostgreSQL n'écoute à cette adresse"
    if "does not exist" in low and "database" in low:
        return "La base de données indiquée n'existe pas"
    if "timeout expired" in low:
        return "Délai de connexion dépassé"
    if isinstance(exc, sqlite3.DatabaseError) and "not a database" in low:
        return "Ce fichier n'est pas une base SQLite valide"
    return msg


# ── Comparaison de schéma ─────────────────────────────────────────────────────

def _find(tables: dict, name: str) -> Optional[dict]:
    if name in tables:
        return tables[name]
    low = str(name).lower()
    for k, v in tables.items():
        if k.lower() == low:
            return v
    return None


TYPE_SYNONYMS = {
    "int": "integer", "int4": "integer", "integer": "integer", "serial": "integer", "serial4": "integer",
    "mediumint": "integer", "int8": "bigint", "bigint": "bigint", "bigserial": "bigint", "serial8": "bigint",
    "int2": "smallint", "smallint": "smallint", "smallserial": "smallint", "tinyint": "smallint",
    "bool": "boolean", "boolean": "boolean", "float8": "double precision", "double": "double precision",
    "double precision": "double precision", "float": "double precision", "float4": "real", "real": "real",
    "timestamp without time zone": "timestamp", "timestamp with time zone": "timestamptz",
    "time without time zone": "time", "time with time zone": "timetz", "character varying": "varchar",
    "varchar": "varchar", "character": "char", "char": "char", "bpchar": "char", "decimal": "numeric",
    "numeric": "numeric", "datetime": "timestamp",
}


def norm_type(t: str) -> str:
    t = re.sub(r"\s+", " ", str(t or "").strip().lower())
    m = re.match(r"^([a-z_ ][a-z0-9_ ]*?)\s*(\(.*\))?\s*(\[\])?$", t)
    if not m:
        return t
    base = TYPE_SYNONYMS.get(m.group(1).strip(), m.group(1).strip())
    args = (m.group(2) or "").replace(" ", "")
    return base + args + (m.group(3) or "")


def type_family(t: str) -> str:
    n = norm_type(t)
    if not n:
        return "blob"
    if n.endswith("[]") or any(x in n for x in ("uuid", "json", "xml", "inet", "cidr", "macaddr", "interval", "enum")):
        return "text"
    if "int" in n:
        return "integer"
    if any(x in n for x in ("char", "clob", "text")):
        return "text"
    if "blob" in n or "bytea" in n:
        return "blob"
    if any(x in n for x in ("real", "floa", "doub")):
        return "real"
    if "bool" in n:
        return "boolean"
    if "timestamp" in n or "date" in n or n.startswith("time"):
        return "datetime"
    return "numeric"


CAST_RE = re.compile(r"(::\s*(\"[^\"]+\"|[a-z_][\w ]*?)(\s*\(\d+(\s*,\s*\d+)?\))?(\[\])?)+$", re.I)
NOW_DEFAULTS = {"current_timestamp", "now()", "datetime('now')", "datetime(\"now\")", "localtimestamp",
                "transaction_timestamp()", "statement_timestamp()", "datetime('now','localtime')"}


def norm_default(col: dict) -> Optional[str]:
    if col.get("auto"):
        return "<auto>"
    d = col.get("default")
    if d is None:
        return None
    s = str(d).strip()
    while s.startswith("(") and s.endswith(")") and _balanced(s[1:-1]):
        s = s[1:-1].strip()
    s = CAST_RE.sub("", s).strip()
    while s.startswith("(") and s.endswith(")") and _balanced(s[1:-1]):
        s = s[1:-1].strip()
    low = s.lower().replace(" ", "")
    if low in {x.replace(" ", "") for x in NOW_DEFAULTS}:
        return "current_timestamp"
    if low in ("true", "'t'", "'true'"):
        return "1"
    if low in ("false", "'f'", "'false'"):
        return "0"
    if low == "null":
        return None
    m = re.match(r"^'(-?\d+(\.\d+)?)'$", s)
    if m:
        return m.group(1)
    return s


def _balanced(s: str) -> bool:
    depth = 0
    in_str = False
    for ch in s:
        if ch == "'":
            in_str = not in_str
        elif not in_str:
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
                if depth < 0:
                    return False
    return depth == 0 and not in_str


def _lower_list(xs) -> tuple:
    return tuple(str(x).lower() for x in xs)


def _index_key(idx: dict) -> tuple:
    if any(c.startswith("(") for c in idx["columns"]):
        return ("expr", idx["name"].lower(), idx["unique"])
    return (_lower_list(idx["columns"]), idx["unique"])


def _col_diff(ca: dict, cb: dict, cross: bool) -> list[str]:
    changes = []
    if cross:
        if type_family(ca["type"]) != type_family(cb["type"]):
            changes.append("type")
    elif norm_type(ca["type"]) != norm_type(cb["type"]):
        changes.append("type")
    if ca["nullable"] != cb["nullable"]:
        changes.append("nullable")
    if norm_default(ca) != norm_default(cb):
        changes.append("default")
    return changes


def compare_schemas(sa: dict, sb: dict) -> dict:
    cross = sa["dialect"] != sb["dialect"]
    ta, tb = sa["tables"], sb["tables"]
    names: list[tuple[Optional[str], Optional[str]]] = []
    seen_b = set()
    for name in ta:
        other = _find(tb, name)
        names.append((name, other["name"] if other else None))
        if other:
            seen_b.add(other["name"])
    for name in tb:
        if name not in seen_b:
            names.append((None, name))
    out = []
    summary = {"same": 0, "changed": 0, "only_a": 0, "only_b": 0}
    for na, nb in sorted(names, key=lambda x: (x[0] or x[1]).lower()):
        a = ta.get(na) if na else None
        b = tb.get(nb) if nb else None
        item = {"name": na or nb, "name_a": na, "name_b": nb, "columns": [], "fks": [], "indexes": [],
                "pk": {"a": a["pk"] if a else None, "b": b["pk"] if b else None, "same": True}}
        if a and not b:
            item["status"] = "only_a"
        elif b and not a:
            item["status"] = "only_b"
        else:
            changed = False
            cols_b = {c["name"].lower(): c for c in b["columns"]}
            done = set()
            for ca in a["columns"]:
                cb = cols_b.get(ca["name"].lower())
                if cb is None:
                    item["columns"].append({"name": ca["name"], "status": "only_a", "a": ca, "b": None, "changes": []})
                    changed = True
                    continue
                done.add(cb["name"].lower())
                ch = _col_diff(ca, cb, cross)
                item["columns"].append({"name": ca["name"], "status": "changed" if ch else "same", "a": ca, "b": cb, "changes": ch})
                changed = changed or bool(ch)
            for cb in b["columns"]:
                if cb["name"].lower() not in done:
                    item["columns"].append({"name": cb["name"], "status": "only_b", "a": None, "b": cb, "changes": []})
                    changed = True
            if _lower_list(a["pk"]) != _lower_list(b["pk"]):
                item["pk"]["same"] = False
                changed = True
            fa = {(_lower_list(f["columns"]), f["ref_table"].lower()): f for f in a["fks"]}
            fb = {(_lower_list(f["columns"]), f["ref_table"].lower()): f for f in b["fks"]}
            for key in list(fa) + [k for k in fb if k not in fa]:
                x, y = fa.get(key), fb.get(key)
                if x and y:
                    same = (_lower_list(x["ref_columns"]) == _lower_list(y["ref_columns"])
                            and x["on_update"].upper() == y["on_update"].upper()
                            and x["on_delete"].upper() == y["on_delete"].upper())
                    status = "same" if same else "changed"
                else:
                    status = "only_a" if x else "only_b"
                changed = changed or status != "same"
                item["fks"].append({"status": status, "a": x, "b": y})
            ia = {_index_key(i): i for i in a["indexes"]}
            ib = {_index_key(i): i for i in b["indexes"]}
            for key in list(ia) + [k for k in ib if k not in ia]:
                x, y = ia.get(key), ib.get(key)
                status = "same" if x and y else ("only_a" if x else "only_b")
                changed = changed or status != "same"
                item["indexes"].append({"status": status, "a": x, "b": y})
            item["status"] = "changed" if changed else "same"
        summary[item["status"]] += 1
        out.append(item)
    return {"cross_dialect": cross, "tables": out, "summary": summary}


# ── Génération SQL ────────────────────────────────────────────────────────────

def map_type(col: dict, src: str, dst: str, sole_pk: bool = False) -> str:
    t = (col.get("type") or "").strip()
    if src == dst:
        return t or ("BLOB" if dst == "sqlite" else "text")
    n = norm_type(t)
    fam = type_family(t)
    if dst == "postgresql":
        if fam == "integer":
            if sole_pk and col.get("auto"):
                return "bigint" if "big" in n else "integer"
            return n.split("(")[0] if n.split("(")[0] in ("bigint", "smallint", "integer") else "integer"
        if fam == "text":
            m = re.match(r"^(varchar|char)(\(\d+\))$", n)
            return m.group(1) + m.group(2) if m else "text"
        if fam == "blob":
            return "bytea"
        if fam == "real":
            return "double precision"
        if fam == "boolean":
            return "boolean"
        if fam == "datetime":
            if n.startswith("timestamptz"):
                return "timestamptz"
            if n == "date":
                return "date"
            if n.startswith("time") and not n.startswith("timestamp"):
                return "time"
            return "timestamp"
        m = re.match(r"^numeric(\(\d+(,\d+)?\))?$", n)
        return "numeric" + (m.group(1) or "") if m else "numeric"
    if fam == "integer":
        return "INTEGER"
    if fam == "text":
        m = re.match(r"^(varchar|char)(\(\d+\))$", n)
        return m.group(1).upper() + m.group(2) if m else "TEXT"
    if fam == "blob":
        return "BLOB"
    if fam == "real":
        return "REAL"
    if fam == "boolean":
        return "BOOLEAN"
    if fam == "datetime":
        return "DATE" if n == "date" else "TIMESTAMP"
    m = re.match(r"^numeric(\(\d+(,\d+)?\))?$", n)
    return "NUMERIC" + (m.group(1) or "") if m else "NUMERIC"


def map_default(col: dict, src: str, dst: str) -> Optional[str]:
    d = col.get("default")
    if d is None or col.get("auto"):
        return None
    if src == dst:
        return str(d)
    n = norm_default(col)
    if n is None:
        return None
    if n == "current_timestamp":
        return "CURRENT_TIMESTAMP"
    fam = type_family(col.get("type", ""))
    if fam == "boolean" and n in ("0", "1"):
        return ("TRUE" if n == "1" else "FALSE") if dst == "postgresql" else n
    if re.match(r"^-?\d+(\.\d+)?$", n) or re.match(r"^'([^']|'')*'$", n):
        return n
    return None


class ScriptBuilder:
    def __init__(self, sa: dict, sb: dict, diff: dict, b_schema_name: str, include_drops: bool, tables: Optional[set]):
        self.sa, self.sb, self.diff = sa, sb, diff
        self.src, self.dst = sa["dialect"], sb["dialect"]
        self.ns = b_schema_name
        self.drops = include_drops
        self.only = {t.lower() for t in tables} if tables else None
        self.lines: list[str] = []
        self.warnings: list[str] = []
        self.post: list[str] = []

    def qt(self, name: str) -> str:
        return f"{_qi(self.ns)}.{_qi(name)}" if self.dst == "postgresql" else _qi(name)

    def warn(self, msg: str):
        self.warnings.append(msg)
        self.lines.append(f"-- ATTENTION : {msg}")

    def col_def(self, col: dict, table: dict, inline_pk: bool = False) -> str:
        sole = len(table["pk"]) == 1 and table["pk"][0].lower() == col["name"].lower()
        parts = [_qi(col["name"]), map_type(col, self.src, self.dst, sole)]
        if self.dst == "postgresql" and col.get("auto") and type_family(col["type"]) == "integer":
            parts.append("GENERATED ALWAYS AS IDENTITY" if col.get("identity") == "a" and self.src == "postgresql"
                         else "GENERATED BY DEFAULT AS IDENTITY")
        else:
            dflt = map_default(col, self.src, self.dst)
            if dflt is not None:
                parts.append(f"DEFAULT {dflt}")
            elif col.get("default") is not None and not col.get("auto"):
                self.warnings.append(f"Défaut de {table['name']}.{col['name']} non traduit : {col['default']}")
        if not col["nullable"] and not (self.dst == "sqlite" and inline_pk):
            parts.append("NOT NULL")
        return " ".join(parts)

    def fk_clause(self, fk: dict) -> str:
        ref = _find(self.sa["tables"], fk["ref_table"])
        ref_name = ref["name"] if ref else fk["ref_table"]
        s = (f"FOREIGN KEY ({', '.join(_qi(c) for c in fk['columns'])}) REFERENCES {self.qt(ref_name)} "
             f"({', '.join(_qi(c) for c in fk['ref_columns'])})")
        if (fk.get("on_update") or "NO ACTION").upper() != "NO ACTION":
            s += f" ON UPDATE {fk['on_update'].upper()}"
        if (fk.get("on_delete") or "NO ACTION").upper() != "NO ACTION":
            s += f" ON DELETE {fk['on_delete'].upper()}"
        return s

    def create_table(self, table: dict, name: Optional[str] = None, extra_cols: list[dict] = (), fks_inline: bool = True) -> str:
        name = name or table["name"]
        if self.src == self.dst == "sqlite" and table.get("sql") and not extra_cols and name == table["name"]:
            return table["sql"].rstrip(";") + ";"
        defs = [self.col_def(c, table) for c in table["columns"]]
        defs += [self.col_def(c, table) for c in extra_cols]
        if table["pk"]:
            defs.append(f"PRIMARY KEY ({', '.join(_qi(c) for c in table['pk'])})")
        if fks_inline:
            defs += [self.fk_clause(fk) for fk in table["fks"]]
        return f"CREATE TABLE {self.qt(name)} (\n    " + ",\n    ".join(defs) + "\n);"

    def index_name(self, table: dict, idx: dict) -> str:
        if idx["name"] and not idx["name"].startswith("sqlite_autoindex"):
            return idx["name"]
        cols = "_".join(re.sub(r"\W+", "", c) for c in idx["columns"])[:40]
        return f"{'ux' if idx['unique'] else 'ix'}_{table['name']}_{cols}"[:63]

    def create_index(self, table: dict, idx: dict, table_name: Optional[str] = None) -> str:
        if self.src == self.dst == "sqlite" and idx.get("sql") and table_name is None:
            return idx["sql"].rstrip(";") + ";"
        if self.dst == "postgresql" and idx.get("constraint"):
            return (f"ALTER TABLE {self.qt(table_name or table['name'])} ADD CONSTRAINT {_qi(self.index_name(table, idx))} "
                    f"UNIQUE ({', '.join(_qi(c) for c in idx['columns'])});")
        cols = ", ".join(c if c.startswith("(") else _qi(c) for c in idx["columns"])
        uq = "UNIQUE " if idx["unique"] else ""
        iname = _qi(self.index_name(table, idx))
        if idx.get("partial"):
            self.warnings.append(f"Index partiel {idx['name']} recréé sans sa condition WHERE")
        return f"CREATE {uq}INDEX {iname} ON {self.qt(table_name or table['name'])} ({cols});"

    def drop_index(self, idx: dict, table: dict) -> str:
        if self.dst == "postgresql":
            if idx.get("constraint"):
                return f"ALTER TABLE {self.qt(table['name'])} DROP CONSTRAINT {_qi(idx['name'])};"
            return f"DROP INDEX {_qi(self.ns)}.{_qi(idx['name'])};"
        if idx.get("constraint") or idx["name"].startswith("sqlite_autoindex"):
            return ""
        return f"DROP INDEX {_qi(idx['name'])};"

    def build(self) -> str:
        header = [f"-- Script de migration généré par LedroideDesktop (Comparateur de bases)",
                  f"-- Source (référence) : {self.src}   →   Cible : {self.dst}",
                  f"-- Généré le {dt.datetime.now().strftime('%d/%m/%Y %H:%M')}. Relisez-le et testez-le sur une copie avant de l'exécuter.",
                  ""]
        selected = [t for t in self.diff["tables"] if self.only is None or t["name"].lower() in self.only]
        needs_fk_off = self.dst == "sqlite" and any(t["status"] == "changed" and self._sqlite_rebuild(t) for t in selected)
        if needs_fk_off:
            self.lines.append("PRAGMA foreign_keys = OFF;")
        self.lines.append("BEGIN;")
        for t in selected:
            if t["status"] == "only_a":
                self._create(t)
        for t in selected:
            if t["status"] == "changed":
                self.lines.append("")
                self.lines.append(f"-- Table {t['name']} : modifications")
                if self.dst == "postgresql":
                    self._alter_pg(t)
                else:
                    self._alter_sqlite(t)
        for t in selected:
            if t["status"] == "only_b":
                self.lines.append("")
                stmt = f"DROP TABLE {self.qt(t['name_b'])};"
                self.lines.append(stmt if self.drops else f"-- (suppression désactivée) {stmt}")
        if self.post:
            self.lines.append("")
            self.lines.append("-- Clés étrangères")
            self.lines.extend(self.post)
        self.lines.append("")
        self.lines.append("COMMIT;")
        if needs_fk_off:
            self.lines.append("PRAGMA foreign_key_check;")
            self.lines.append("PRAGMA foreign_keys = ON;")
        body = [ln for ln in self.lines]
        if len(selected) == 0 or all(t["status"] == "same" for t in selected):
            header.append("-- Aucune différence de schéma : rien à migrer.")
        return "\n".join(header + body) + "\n"

    def _create(self, t: dict):
        table = self.sa["tables"][t["name_a"]]
        self.lines.append("")
        self.lines.append(f"-- Nouvelle table {table['name']}")
        inline = self.dst == "sqlite"
        self.lines.append(self.create_table(table, fks_inline=inline))
        if not inline:
            for fk in table["fks"]:
                self.post.append(f"ALTER TABLE {self.qt(table['name'])} ADD {self.fk_clause(fk)};")
        for idx in table["indexes"]:
            if self.src == self.dst == "sqlite" and idx.get("constraint"):
                continue
            self.lines.append(self.create_index(table, idx))

    def _alter_pg(self, t: dict):
        a = self.sa["tables"][t["name_a"]]
        b = self.sb["tables"][t["name_b"]]
        qt = self.qt(b["name"])
        for idx in t["indexes"]:
            if idx["status"] == "only_b":
                self.lines.append(self.drop_index(idx["b"], b))
        for fk in t["fks"]:
            if fk["status"] in ("only_b", "changed") and fk["b"] and fk["b"].get("name"):
                self.lines.append(f"ALTER TABLE {qt} DROP CONSTRAINT {_qi(fk['b']['name'])};")
        if not t["pk"]["same"] and b["pk"] and b["pk_name"]:
            self.lines.append(f"ALTER TABLE {qt} DROP CONSTRAINT {_qi(b['pk_name'])};")
        for c in t["columns"]:
            ca, cb = c["a"], c["b"]
            if c["status"] == "only_a":
                if not ca["nullable"] and map_default(ca, self.src, self.dst) is None and not ca.get("auto"):
                    self.warn(f"la colonne {a['name']}.{ca['name']} est NOT NULL sans défaut : l'ajout échouera si la table contient des lignes")
                self.lines.append(f"ALTER TABLE {qt} ADD COLUMN {self.col_def(ca, a)};")
            elif c["status"] == "only_b":
                stmt = f"ALTER TABLE {qt} DROP COLUMN {_qi(cb['name'])};"
                self.lines.append(stmt if self.drops else f"-- (suppression désactivée) {stmt}")
            elif c["status"] == "changed":
                col = _qi(cb["name"])
                if "type" in c["changes"]:
                    newt = map_type(ca, self.src, self.dst)
                    self.lines.append(f"ALTER TABLE {qt} ALTER COLUMN {col} TYPE {newt} USING {col}::{newt};")
                if "default" in c["changes"]:
                    if ca.get("auto") and not cb.get("auto"):
                        self.lines.append(f"ALTER TABLE {qt} ALTER COLUMN {col} ADD GENERATED BY DEFAULT AS IDENTITY;")
                    else:
                        if cb.get("identity"):
                            self.lines.append(f"ALTER TABLE {qt} ALTER COLUMN {col} DROP IDENTITY;")
                        dflt = map_default(ca, self.src, self.dst)
                        self.lines.append(f"ALTER TABLE {qt} ALTER COLUMN {col} SET DEFAULT {dflt};" if dflt is not None
                                          else f"ALTER TABLE {qt} ALTER COLUMN {col} DROP DEFAULT;")
                if "nullable" in c["changes"]:
                    self.lines.append(f"ALTER TABLE {qt} ALTER COLUMN {col} {'DROP' if ca['nullable'] else 'SET'} NOT NULL;")
        if not t["pk"]["same"] and a["pk"]:
            self.lines.append(f"ALTER TABLE {qt} ADD PRIMARY KEY ({', '.join(_qi(x) for x in a['pk'])});")
        for fk in t["fks"]:
            if fk["status"] in ("only_a", "changed"):
                self.post.append(f"ALTER TABLE {qt} ADD {self.fk_clause(fk['a'])};")
        for idx in t["indexes"]:
            if idx["status"] == "only_a":
                self.lines.append(self.create_index(a, idx["a"]))

    def _sqlite_rebuild(self, t: dict) -> bool:
        if not t["pk"]["same"]:
            return True
        if any(f["status"] != "same" for f in t["fks"]):
            return True
        for c in t["columns"]:
            if c["status"] == "changed":
                return True
            if c["status"] == "only_a":
                ca = c["a"]
                if ca.get("auto") or (not ca["nullable"] and map_default(ca, self.src, self.dst) is None):
                    return True
                if map_default(ca, self.src, self.dst) == "CURRENT_TIMESTAMP":
                    return True
            if c["status"] == "only_b" and self.drops:
                b = self.sb["tables"][t["name_b"]]
                name = c["b"]["name"].lower()
                used = any(name in _lower_list(i["columns"]) for i in b["indexes"]) or \
                    any(name in _lower_list(f["columns"]) for f in b["fks"]) or name in _lower_list(b["pk"])
                if used:
                    return True
        return False

    def _alter_sqlite(self, t: dict):
        a = self.sa["tables"][t["name_a"]]
        b = self.sb["tables"][t["name_b"]]
        qt = self.qt(b["name"])
        if not self._sqlite_rebuild(t):
            for idx in t["indexes"]:
                if idx["status"] == "only_b":
                    stmt = self.drop_index(idx["b"], b)
                    if stmt:
                        self.lines.append(stmt)
            for c in t["columns"]:
                if c["status"] == "only_a":
                    self.lines.append(f"ALTER TABLE {qt} ADD COLUMN {self.col_def(c['a'], a)};")
                elif c["status"] == "only_b":
                    stmt = f"ALTER TABLE {qt} DROP COLUMN {_qi(c['b']['name'])};"
                    self.lines.append(stmt if self.drops else f"-- (suppression désactivée) {stmt}")
            for idx in t["indexes"]:
                if idx["status"] == "only_a":
                    if idx["a"].get("constraint") and self.src == "sqlite":
                        self.warn(f"contrainte UNIQUE ({', '.join(idx['a']['columns'])}) ajoutée sous forme d'index unique")
                        self.lines.append(self.create_index(a, {**idx["a"], "sql": None, "name": ""}))
                    else:
                        self.lines.append(self.create_index(a, idx["a"]))
            return
        self.lines.append(f"-- SQLite ne sait pas modifier ces colonnes : la table est reconstruite")
        keep_b = [c["b"] for c in t["columns"] if c["status"] == "only_b"] if not self.drops else []
        tmp = f"_new_{b['name']}"
        if keep_b:
            self.lines.append(f"-- Colonnes conservées (suppressions désactivées) : {', '.join(c['name'] for c in keep_b)}")
        if self.src == "sqlite" and a.get("sql") and not keep_b:
            sql = re.sub(r'^\s*CREATE\s+TABLE\s+(IF\s+NOT\s+EXISTS\s+)?("(?:[^"]|"")+"|\[[^\]]+\]|`[^`]+`|[\w$]+)',
                         f"CREATE TABLE {_qi(tmp)}", a["sql"], count=1, flags=re.I)
            self.lines.append(sql.rstrip(";") + ";")
        else:
            self.lines.append(self.create_table(a, tmp, keep_b))
        common = []
        for c in t["columns"]:
            if c["status"] in ("same", "changed"):
                common.append((c["a"]["name"], c["b"]["name"]))
            elif c["status"] == "only_b" and not self.drops:
                common.append((c["b"]["name"], c["b"]["name"]))
            elif c["status"] == "only_a" and not c["a"]["nullable"] and map_default(c["a"], self.src, self.dst) is None and not c["a"].get("auto"):
                self.warn(f"la colonne {a['name']}.{c['a']['name']} est NOT NULL sans défaut : la copie échouera si la table contient des lignes")
        for c in t["columns"]:
            if c["status"] == "changed" and "nullable" in c["changes"] and not c["a"]["nullable"]:
                self.warn(f"{a['name']}.{c['a']['name']} devient NOT NULL : les lignes contenant NULL feront échouer la copie")
        self.lines.append(f"INSERT INTO {_qi(tmp)} ({', '.join(_qi(x) for x, _ in common)})\n"
                          f"    SELECT {', '.join(_qi(y) for _, y in common)} FROM {qt};")
        self.lines.append(f"DROP TABLE {qt};")
        self.lines.append(f"ALTER TABLE {_qi(tmp)} RENAME TO {_qi(a['name'])};")
        for idx in a["indexes"]:
            if idx.get("constraint") and self.src == "sqlite":
                continue
            self.lines.append(self.create_index(a, idx))


# ── Valeurs ───────────────────────────────────────────────────────────────────

def norm_val(v: Any) -> Any:
    if v is None:
        return None
    if isinstance(v, bool):
        return int(v)
    if isinstance(v, int):
        return v
    if isinstance(v, float):
        if math.isfinite(v) and v.is_integer():
            return int(v)
        return v
    if isinstance(v, decimal.Decimal):
        if v.is_finite() and v == v.to_integral_value():
            return int(v)
        return float(v)
    if isinstance(v, (bytes, bytearray, memoryview)):
        return bytes(v)
    if isinstance(v, dt.datetime):
        return v.isoformat(sep=" ")
    if isinstance(v, (dt.date, dt.time)):
        return v.isoformat()
    if isinstance(v, (dict, list)):
        return json.dumps(v, sort_keys=True, ensure_ascii=False)
    return str(v)


def ord_key(values: tuple) -> tuple:
    out = []
    for v in values:
        n = norm_val(v)
        if n is None:
            out.append((0, 0))
        elif isinstance(n, (int, float)):
            out.append((1, n))
        elif isinstance(n, str):
            out.append((2, n))
        else:
            out.append((3, n))
    return tuple(out)


def disp(v: Any) -> Any:
    if v is None or isinstance(v, (bool, int)):
        return v
    if isinstance(v, float):
        return v if math.isfinite(v) else str(v)
    if isinstance(v, decimal.Decimal):
        return str(v)
    if isinstance(v, (bytes, bytearray, memoryview)):
        b = bytes(v)
        return "0x" + b[:48].hex() + ("…" if len(b) > 48 else "") + f" ({len(b)} o)"
    if isinstance(v, dt.datetime):
        return v.isoformat(sep=" ")
    if isinstance(v, (dt.date, dt.time)):
        return v.isoformat()
    if isinstance(v, (dict, list)):
        s = json.dumps(v, ensure_ascii=False)
    else:
        s = str(v)
    return s if len(s) <= MAX_TEXT else s[:MAX_TEXT] + "…"


def sql_literal(v: Any, dialect: str, target_type: str = "") -> str:
    if v is None:
        return "NULL"
    fam = type_family(target_type) if target_type else ""
    if isinstance(v, bool):
        if dialect == "postgresql":
            return "TRUE" if v else "FALSE"
        return "1" if v else "0"
    if fam == "boolean" and dialect == "postgresql" and isinstance(v, (int, float, decimal.Decimal)):
        return "TRUE" if v else "FALSE"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        if not math.isfinite(v):
            return f"'{v}'" if dialect == "postgresql" else "NULL"
        return repr(v)
    if isinstance(v, decimal.Decimal):
        return str(v) if v.is_finite() else f"'{v}'"
    if isinstance(v, (bytes, bytearray, memoryview)):
        h = bytes(v).hex()
        return f"'\\x{h}'::bytea" if dialect == "postgresql" else f"X'{h}'"
    if isinstance(v, dt.datetime):
        s = v.isoformat(sep=" ")
    elif isinstance(v, (dt.date, dt.time)):
        s = v.isoformat()
    elif isinstance(v, (dict, list)):
        s = json.dumps(v, ensure_ascii=False)
    else:
        s = str(v)
    s = s.replace("\x00", "")
    return "'" + s.replace("'", "''") + "'"


# ── Handlers ──────────────────────────────────────────────────────────────────

@module.on("recents.list")
async def recents_list(payload: dict):
    db = await _db()
    rows = await db.fetch_all("SELECT id, kind, value, pg_schema FROM recents ORDER BY used_at DESC LIMIT 30")
    return {"recents": rows}


@module.on("recents.delete")
async def recents_delete(payload: dict):
    db = await _db()
    if payload.get("all"):
        await db.execute("DELETE FROM recents")
    else:
        await db.execute("DELETE FROM recents WHERE id=?", (int(payload.get("id") or 0),))
    return await recents_list({})


@module.on("conn.test")
async def conn_test(payload: dict):
    try:
        conn = _parse_conn(payload.get("conn"))
    except ValidationError as exc:
        return _err(_validation_message(exc))
    except ValueError as exc:
        return _err(str(exc))
    src = None
    t0 = time.perf_counter()
    try:
        src = await _open(conn)
        schema = await asyncio.to_thread(src.schema)
    except Exception as exc:
        log.info("conn.test failed (%s): %s", conn.kind, type(exc).__name__)
        return _err(_friendly(exc, conn), "CONNECTION_FAILED")
    finally:
        await _close(src)
    await _remember(conn)
    return {"ok": True, "dialect": src.dialect, "version": src.version, "label": conn.label(),
            "tables": len(schema["tables"]), "ms": round((time.perf_counter() - t0) * 1000)}


async def _load_two(raw_a: Any, raw_b: Any) -> tuple[ConnIn, ConnIn]:
    try:
        a = _parse_conn(raw_a)
    except ValidationError as exc:
        raise ValueError("Base A — " + _validation_message(exc))
    except ValueError as exc:
        raise ValueError(f"Base A — {exc}")
    try:
        b = _parse_conn(raw_b)
    except ValidationError as exc:
        raise ValueError("Base B — " + _validation_message(exc))
    except ValueError as exc:
        raise ValueError(f"Base B — {exc}")
    return a, b


async def _introspect(conn: ConnIn, side: str) -> tuple[dict, str]:
    src = None
    try:
        src = await _open(conn)
        schema = await asyncio.to_thread(src.schema)
        return schema, src.version
    except Exception as exc:
        log.info("introspection %s failed: %s", side, type(exc).__name__)
        raise RuntimeError(f"Base {side} — {_friendly(exc, conn)}")
    finally:
        await _close(src)


@module.on("schema.compare")
async def schema_compare(payload: dict):
    try:
        a, b = await _load_two(payload.get("a"), payload.get("b"))
    except ValueError as exc:
        return _err(str(exc))
    try:
        (sa, va), (sb, vb) = await asyncio.gather(_introspect(a, "A"), _introspect(b, "B"))
    except RuntimeError as exc:
        return _err(str(exc), "CONNECTION_FAILED")
    await _remember(a)
    await _remember(b)
    diff = compare_schemas(sa, sb)
    cid = _cache_put(_schema_cache, {"a": sa, "b": sb, "diff": diff, "b_ns": b.pg_schema if b.kind == "postgresql" else ""})
    common = []
    for t in diff["tables"]:
        if t["name_a"] and t["name_b"]:
            ta, tb = sa["tables"][t["name_a"]], sb["tables"][t["name_b"]]
            cols_b = {c["name"].lower() for c in tb["columns"]}
            common.append({"name": t["name"], "pk": ta["pk"] if ta["pk"] else tb["pk"],
                           "columns": [c["name"] for c in ta["columns"] if c["name"].lower() in cols_b]})
    return {"id": cid, "diff": diff, "common_tables": common,
            "a": {"dialect": sa["dialect"], "version": va, "label": a.label(), "tables": len(sa["tables"])},
            "b": {"dialect": sb["dialect"], "version": vb, "label": b.label(), "tables": len(sb["tables"])}}


@module.on("schema.script")
async def schema_script(payload: dict):
    entry = _schema_cache.get(str(payload.get("id") or ""))
    if not entry:
        return _err("Comparaison expirée : relancez la comparaison du schéma", "NOT_FOUND")
    tables = payload.get("tables")
    if tables is not None and not isinstance(tables, list):
        return _err("tables : liste attendue")
    builder = ScriptBuilder(entry["a"], entry["b"], entry["diff"], entry["b_ns"],
                            bool(payload.get("include_drops")), set(map(str, tables)) if tables else None)
    sql = builder.build()
    return {"sql": sql, "dialect": entry["b"]["dialect"], "warnings": list(dict.fromkeys(builder.warnings))}


class DataReq(BaseModel):
    table: str = Field(min_length=1, max_length=200)
    key: list[str] = Field(default_factory=list, max_length=16)
    columns: Optional[list[str]] = None
    batch_size: int = Field(2000, ge=50, le=50000)
    max_rows: int = Field(0, ge=0, le=100_000_000)
    max_diffs: int = Field(1000, ge=1, le=100_000)


class _Stream:
    def __init__(self, src: Source, table: str, keys: list[str], cols: list[str], text_keys: set, batch: int, max_rows: int):
        self.src, self.table, self.keys, self.cols = src, table, keys, cols
        self.text_keys, self.batch, self.max_rows = text_keys, batch, max_rows
        self.buf: list[tuple] = []
        self.pos = 0
        self.after: Optional[tuple] = None
        self.done = False
        self.read = 0
        self.prev: Optional[tuple] = None

    async def peek(self) -> Optional[tuple]:
        if self.pos >= len(self.buf):
            if self.done:
                return None
            limit = self.batch
            if self.max_rows:
                limit = min(limit, self.max_rows - self.read)
                if limit <= 0:
                    self.done = True
                    return None
            self.buf = await asyncio.to_thread(self.src.fetch, self.table, self.keys, self.cols, self.text_keys, self.after, limit)
            self.pos = 0
            if len(self.buf) < limit:
                self.done = True
            if not self.buf:
                return None
            self.after = self.buf[-1][: len(self.keys)]
        return self.buf[self.pos]

    def pop(self) -> tuple:
        row = self.buf[self.pos]
        self.pos += 1
        self.read += 1
        k = ord_key(row[: len(self.keys)])
        if self.prev is not None and k <= self.prev:
            raise ValueError("La clé choisie n'identifie pas les lignes de façon unique (doublons ou ordre incohérent)")
        self.prev = k
        return row


@module.job("data.compare")
async def data_compare(payload: dict, job):
    a, b = await _load_two(payload.get("a"), payload.get("b"))
    try:
        req = DataReq.model_validate(payload.get("options") or {})
    except ValidationError as exc:
        raise ValueError(_validation_message(exc))
    src_a = src_b = None
    try:
        await job.progress(0, "Connexion aux bases…")
        try:
            src_a = await _open(a)
        except Exception as exc:
            raise RuntimeError(f"Base A — {_friendly(exc, a)}")
        try:
            src_b = await _open(b)
        except Exception as exc:
            raise RuntimeError(f"Base B — {_friendly(exc, b)}")
        sa, sb = await asyncio.gather(asyncio.to_thread(src_a.schema), asyncio.to_thread(src_b.schema))
        ta, tb = _find(sa["tables"], req.table), _find(sb["tables"], req.table)
        if not ta or not tb:
            raise ValueError(f"La table « {req.table} » doit exister dans les deux bases")
        cols_a = {c["name"].lower(): c for c in ta["columns"]}
        cols_b = {c["name"].lower(): c for c in tb["columns"]}
        keys = req.key or ta["pk"] or tb["pk"]
        if not keys:
            raise ValueError("Cette table n'a pas de clé primaire : choisissez la ou les colonnes servant de clé")
        for k in keys:
            if k.lower() not in cols_a or k.lower() not in cols_b:
                raise ValueError(f"Colonne de clé « {k} » absente d'une des deux bases")
        common = [c["name"] for c in ta["columns"] if c["name"].lower() in cols_b]
        if req.columns is not None:
            wanted = {c.lower() for c in req.columns}
            common = [c for c in common if c.lower() in wanted]
        key_low = {k.lower() for k in keys}
        compared = [c for c in common if c.lower() not in key_low]
        keys_a = [cols_a[k.lower()]["name"] for k in keys]
        keys_b = [cols_b[k.lower()]["name"] for k in keys]
        cmp_a = [cols_a[c.lower()]["name"] for c in compared]
        cmp_b = [cols_b[c.lower()]["name"] for c in compared]
        text_a = {cols_a[k.lower()]["name"] for k in keys if type_family(cols_a[k.lower()]["type"]) == "text" and src_a.dialect == "postgresql"}
        text_b = {cols_b[k.lower()]["name"] for k in keys if type_family(cols_b[k.lower()]["type"]) == "text" and src_b.dialect == "postgresql"}
        await job.progress(1, "Comptage des lignes…")
        n_a, n_b = await asyncio.gather(asyncio.to_thread(src_a.count, ta["name"]), asyncio.to_thread(src_b.count, tb["name"]))
        if req.max_rows:
            n_a, n_b = min(n_a, req.max_rows), min(n_b, req.max_rows)
        total = max(1, n_a + n_b)
        sa_stream = _Stream(src_a, ta["name"], keys_a, cmp_a, text_a, req.batch_size, req.max_rows)
        sb_stream = _Stream(src_b, tb["name"], keys_b, cmp_b, text_b, req.batch_size, req.max_rows)
        nk = len(keys)
        added: list = []
        removed: list = []
        changed: list = []
        counts = {"added": 0, "removed": 0, "changed": 0, "same": 0}
        last_report = time.monotonic()

        def keep(lst: list, item) -> None:
            if len(added) + len(removed) + len(changed) < req.max_diffs:
                lst.append(item)

        while True:
            ra = await sa_stream.peek()
            rb = await sb_stream.peek()
            if ra is None and rb is None:
                break
            if rb is None or (ra is not None and ord_key(ra[:nk]) < ord_key(rb[:nk])):
                sa_stream.pop()
                counts["added"] += 1
                keep(added, ra)
            elif ra is None or ord_key(rb[:nk]) < ord_key(ra[:nk]):
                sb_stream.pop()
                counts["removed"] += 1
                keep(removed, rb)
            else:
                sa_stream.pop()
                sb_stream.pop()
                diff_cols = [i for i in range(len(compared)) if norm_val(ra[nk + i]) != norm_val(rb[nk + i])]
                if diff_cols:
                    counts["changed"] += 1
                    keep(changed, (ra, rb, diff_cols))
                else:
                    counts["same"] += 1
            if time.monotonic() - last_report > 0.25:
                last_report = time.monotonic()
                done = sa_stream.read + sb_stream.read
                await job.progress(min(99, round(done * 100 / total, 1)),
                                   f"{done:,} / {n_a + n_b:,} lignes lues".replace(",", " "), counts=dict(counts))
        truncated = sum(counts[k] for k in ("added", "removed", "changed")) > len(added) + len(removed) + len(changed)
        rid = _cache_put(_data_cache, {
            "dialect": src_b.dialect, "table": tb["name"], "ns": b.pg_schema if b.kind == "postgresql" else "",
            "keys": keys_b, "columns": cmp_b, "types": {c["name"]: c["type"] for c in tb["columns"]},
            "identity_always": any(c.get("identity") == "a" for c in tb["columns"] if c["name"] in keys_b + cmp_b),
            "added": added, "removed": removed, "changed": changed, "truncated": truncated,
        })
        nkk = nk
        return {
            "id": rid, "table": ta["name"], "key": keys_a, "columns": cmp_a, "counts": counts,
            "rows_a": sa_stream.read, "rows_b": sb_stream.read, "truncated": truncated,
            "limited": bool(req.max_rows) and (sa_stream.read >= req.max_rows or sb_stream.read >= req.max_rows),
            "added": [[disp(v) for v in r] for r in added],
            "removed": [[disp(v) for v in r] for r in removed],
            "changed": [{"key": [disp(v) for v in ra[:nkk]], "a": [disp(v) for v in ra[nkk:]],
                         "b": [disp(v) for v in rb[nkk:]], "diff": d} for ra, rb, d in changed],
        }
    finally:
        await _close(src_a)
        await _close(src_b)


@module.on("data.script")
async def data_script(payload: dict):
    entry = _data_cache.get(str(payload.get("id") or ""))
    if not entry:
        return _err("Résultat expiré : relancez la comparaison des données", "NOT_FOUND")
    inserts = payload.get("inserts", True)
    updates = payload.get("updates", True)
    deletes = bool(payload.get("deletes", False))
    d = entry["dialect"]
    keys, cols, types = entry["keys"], entry["columns"], entry["types"]
    qt = f"{_qi(entry['ns'])}.{_qi(entry['table'])}" if d == "postgresql" else _qi(entry["table"])
    nk = len(keys)

    def where(row: tuple) -> str:
        return " AND ".join(f"{_qi(k)} = {sql_literal(row[i], d, types.get(k, ''))}" for i, k in enumerate(keys))

    lines = ["-- Script de synchronisation des données généré par LedroideDesktop (Comparateur de bases)",
             f"-- Table {entry['table']} — cible {d}. Relisez-le et testez-le sur une copie avant de l'exécuter."]
    if entry["truncated"]:
        lines.append("-- ATTENTION : la comparaison a atteint la limite de différences détaillées ; ce script est partiel.")
    lines.append("BEGIN;")
    n = 0
    if deletes and entry["removed"]:
        lines.append("")
        lines.append(f"-- Lignes absentes de la base de référence ({len(entry['removed'])})")
        for row in entry["removed"]:
            lines.append(f"DELETE FROM {qt} WHERE {where(row)};")
            n += 1
    if updates and entry["changed"]:
        lines.append("")
        lines.append(f"-- Lignes modifiées ({len(entry['changed'])})")
        for ra, rb, diff_cols in entry["changed"]:
            sets = ", ".join(f"{_qi(cols[i])} = {sql_literal(ra[nk + i], d, types.get(cols[i], ''))}" for i in diff_cols)
            lines.append(f"UPDATE {qt} SET {sets} WHERE {where(rb)};")
            n += 1
    if inserts and entry["added"]:
        lines.append("")
        lines.append(f"-- Lignes manquantes ({len(entry['added'])})")
        names = ", ".join(_qi(c) for c in keys + cols)
        override = " OVERRIDING SYSTEM VALUE" if d == "postgresql" and entry["identity_always"] else ""
        for row in entry["added"]:
            vals = ", ".join(sql_literal(v, d, types.get((keys + cols)[i], "")) for i, v in enumerate(row))
            lines.append(f"INSERT INTO {qt} ({names}){override} VALUES ({vals});")
            n += 1
    lines.append("")
    lines.append("COMMIT;")
    if n == 0:
        lines.insert(2, "-- Aucune instruction : les données sont identiques (ou rien n'est sélectionné).")
    return {"sql": "\n".join(lines) + "\n", "dialect": d, "statements": n}


@module.on("psycopg.status")
async def psycopg_status(payload: dict):
    p = _psycopg()
    return {"installed": p is not None, "version": getattr(p, "__version__", None) if p else None}
