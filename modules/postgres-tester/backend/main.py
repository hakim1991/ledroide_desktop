"""PostgreSQL tester: service/port/auth checks, databases, SQL console, roles & passwords."""
import asyncio
import base64
import datetime as dt
import decimal
import hashlib
import hmac
import ipaddress
import json
import os
import re
import secrets
import socket
import sqlite3
import subprocess
import sys
import time
import unicodedata
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Optional

from ledroide_sdk.http import JSONResponse
from pydantic import BaseModel, Field
from ledroide_sdk import Module, data_path

module = Module(__file__)
router = module.router

DB_PATH = Path(os.environ.get("LEDROIDE_PG_TESTER_DB") or data_path(__file__, "postgres-tester.db"))
MAX_ROWS_HARD = 10000
SSL_MODES = {"disable", "allow", "prefer", "require", "verify-ca", "verify-full"}
HOST_RE = re.compile(r"^[A-Za-z0-9._\-:%\[\]]{1,253}$")
NAME_RE = re.compile(r"^[^\x00]{1,63}$")


# ───────────────────────── local storage ─────────────────────────
@contextmanager
def _db():
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def _init():
    with _db() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS profiles (
            id INTEGER PRIMARY KEY AUTOINCREMENT, label TEXT NOT NULL, host TEXT NOT NULL, port INTEGER NOT NULL,
            user TEXT NOT NULL, dbname TEXT NOT NULL, sslmode TEXT NOT NULL DEFAULT 'prefer',
            secret BLOB, created_at TEXT NOT NULL)""")
        c.execute("""CREATE TABLE IF NOT EXISTS history (
            id INTEGER PRIMARY KEY AUTOINCREMENT, target TEXT NOT NULL, sql TEXT NOT NULL,
            ok INTEGER NOT NULL, duration_ms REAL, at TEXT NOT NULL)""")


_init()


# ───────────────────────── Windows DPAPI (saved passwords) ─────────────────────────
def _dpapi(data: bytes, protect: bool) -> bytes:
    import ctypes
    from ctypes import wintypes as wt

    class BLOB(ctypes.Structure):
        _fields_ = [("cbData", wt.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    buf = ctypes.create_string_buffer(data, len(data))
    inp = BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    out = BLOB()
    fn = ctypes.windll.crypt32.CryptProtectData if protect else ctypes.windll.crypt32.CryptUnprotectData
    if not fn(ctypes.byref(inp), None, None, None, None, 0x1, ctypes.byref(out)):
        raise OSError("DPAPI a échoué")
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(ctypes.cast(out.pbData, ctypes.c_void_p))


def _encrypt(pw: str) -> bytes:
    if sys.platform != "win32":
        raise OSError("Mémorisation du mot de passe disponible uniquement sous Windows")
    return _dpapi(pw.encode("utf-8"), True)


def _decrypt(blob: bytes) -> str:
    return _dpapi(blob, False).decode("utf-8")


# ───────────────────────── helpers ─────────────────────────
def _err(msg: str, code: str = "ERROR", status: int = 400, **extra):
    return JSONResponse(status_code=status, content={"error": msg, "code": code, **extra})


def _psycopg():
    try:
        import psycopg  # noqa
        return psycopg
    except ImportError:
        return None


MISSING = {"error": "Le pilote psycopg n'est pas installé. Installez les dépendances du module.", "code": "MISSING_DEP"}


class Conn(BaseModel):
    host: str = "localhost"
    port: int = Field(5432, ge=1, le=65535)
    user: str = "postgres"
    password: Optional[str] = None
    dbname: str = "postgres"
    sslmode: str = "prefer"
    profile_id: Optional[int] = None
    timeout: int = Field(5, ge=1, le=60)


def _resolve(c: Conn) -> dict:
    if not HOST_RE.match(c.host or ""):
        raise ValueError("Hôte invalide")
    if c.sslmode not in SSL_MODES:
        raise ValueError("Mode SSL invalide")
    if not NAME_RE.match(c.user or "") or not NAME_RE.match(c.dbname or ""):
        raise ValueError("Utilisateur ou base invalide")
    pw = c.password
    if pw is None and c.profile_id:
        with _db() as db:
            r = db.execute("SELECT secret FROM profiles WHERE id=?", (c.profile_id,)).fetchone()
        if r and r["secret"]:
            pw = _decrypt(r["secret"])
    return {"host": c.host, "port": c.port, "user": c.user, "password": pw or "", "dbname": c.dbname,
            "sslmode": c.sslmode, "connect_timeout": c.timeout, "application_name": "LedroideDesktop"}


def _connect(params: dict, **kw):
    psycopg = _psycopg()
    return psycopg.connect(**params, **kw)


# French explanations of common errors
_HINTS = [
    (r"password authentication failed", "Mot de passe incorrect (ou utilisateur inexistant).", "AUTH_FAILED"),
    (r"no password supplied|fe_sendauth", "Le serveur exige un mot de passe.", "AUTH_REQUIRED"),
    (r'database ".*" does not exist', "La base de données n'existe pas.", "DB_NOT_FOUND"),
    (r'role ".*" does not exist', "L'utilisateur (rôle) n'existe pas.", "ROLE_NOT_FOUND"),
    (r"no pg_hba\.conf entry", "Connexion refusée par pg_hba.conf (adresse, utilisateur ou SSL non autorisé).", "HBA_REJECT"),
    (r"Connection refused|actively refused|refusé", "Connexion refusée : rien n'écoute sur ce port (serveur arrêté ?).", "REFUSED"),
    (r"timeout expired|timed out", "Délai dépassé : hôte injoignable ou pare-feu.", "TIMEOUT"),
    (r"could not translate host name|getaddrinfo|Name or service not known|Unknown host", "Nom d'hôte introuvable (DNS).", "DNS"),
    (r"SSL", "Problème SSL/TLS (essayez un autre mode SSL).", "SSL"),
    (r"too many connections|remaining connection slots", "Trop de connexions sur le serveur.", "TOO_MANY"),
    (r"permission denied|must be superuser|must be owner", "Droits insuffisants pour cette opération.", "PERMISSION"),
    (r"canceling statement due to statement timeout", "Requête interrompue : délai maximal dépassé.", "STATEMENT_TIMEOUT"),
]


def _explain(exc: Exception) -> dict:
    raw = str(exc).strip()
    for pat, msg, code in _HINTS:
        if re.search(pat, raw, re.I):
            return {"error": msg, "code": code, "detail": raw}
    return {"error": raw.splitlines()[0] if raw else type(exc).__name__, "code": "PG_ERROR", "detail": raw}


def _jsonable(v: Any):
    if v is None or isinstance(v, (bool, int, str)):
        return v
    if isinstance(v, float):
        return v if v == v and v not in (float("inf"), float("-inf")) else str(v)
    if isinstance(v, decimal.Decimal):
        return str(v)
    if isinstance(v, (dt.datetime, dt.date, dt.time)):
        return v.isoformat()
    if isinstance(v, dt.timedelta):
        return str(v)
    if isinstance(v, (bytes, bytearray, memoryview)):
        b = bytes(v)
        return "\\x" + b[:64].hex() + ("…" if len(b) > 64 else "")
    if isinstance(v, (dict, list)):
        return json.loads(json.dumps(v, default=str))
    if isinstance(v, (uuid.UUID, ipaddress.IPv4Address, ipaddress.IPv6Address, ipaddress.IPv4Network, ipaddress.IPv6Network)):
        return str(v)
    return str(v)


def _target(p: dict) -> str:
    return f"{p['user']}@{p['host']}:{p['port']}/{p['dbname']}"


def _redact(sql: str) -> str:
    return re.sub(r"(PASSWORD\s+)('(?:[^']|'')*'|\$\w*\$.*?\$\w*\$)", r"\1'••••••'", sql, flags=re.I | re.S)


# ───────────────────────── status / local service ─────────────────────────
def _local_services() -> list:
    if sys.platform != "win32":
        return []
    script = ("[Console]::OutputEncoding=[Text.Encoding]::UTF8;"
              "Get-CimInstance Win32_Service -Filter \"Name LIKE 'postgresql%'\" | "
              "Select-Object Name,DisplayName,State,StartMode,ProcessId,PathName | ConvertTo-Json -Compress")
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script], capture_output=True,
                           timeout=20, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        out = r.stdout.decode("utf-8", "replace").strip()
        if not out:
            return []
        data = json.loads(out)
        data = data if isinstance(data, list) else [data]
        res = []
        for s in data:
            path = s.get("PathName") or ""
            m = re.search(r'-D\s+"?([^"]+?)"?(\s+-|$)', path)
            ver = re.search(r"PostgreSQL[\\/](\d+)", path)
            res.append({"name": s.get("Name"), "display": s.get("DisplayName"), "state": s.get("State"),
                        "start": s.get("StartMode"), "pid": s.get("ProcessId"),
                        "data_dir": m.group(1) if m else None, "version": ver.group(1) if ver else None})
        return res
    except Exception:
        return []


@router.get("/status")
async def status():
    pg = _psycopg()
    return {"driver": pg.__version__ if pg else None, "libpq": (pg.pq.version() if pg else None)}


@router.get("/services")
async def services():
    return {"services": await asyncio.to_thread(_local_services)}


# ───────────────────────── profiles ─────────────────────────
class ProfileIn(BaseModel):
    label: str
    host: str = "localhost"
    port: int = Field(5432, ge=1, le=65535)
    user: str = "postgres"
    dbname: str = "postgres"
    sslmode: str = "prefer"
    password: Optional[str] = None
    remember: bool = False


def _profile_row(r) -> dict:
    return {"id": r["id"], "label": r["label"], "host": r["host"], "port": r["port"], "user": r["user"],
            "dbname": r["dbname"], "sslmode": r["sslmode"], "has_password": bool(r["secret"])}


@router.get("/profiles")
async def list_profiles():
    with _db() as db:
        return [_profile_row(r) for r in db.execute("SELECT * FROM profiles ORDER BY label COLLATE NOCASE")]


@router.post("/profiles")
async def save_profile(p: ProfileIn, profile_id: Optional[int] = None):
    if not p.label.strip():
        return _err("Nom du profil requis")
    try:
        _resolve(Conn(host=p.host, port=p.port, user=p.user, dbname=p.dbname, sslmode=p.sslmode))
    except ValueError as e:
        return _err(str(e))
    secret = None
    if p.remember and p.password:
        try:
            secret = _encrypt(p.password)
        except OSError as e:
            return _err(str(e))
    with _db() as db:
        if profile_id:
            old = db.execute("SELECT secret FROM profiles WHERE id=?", (profile_id,)).fetchone()
            if not old:
                return _err("Profil introuvable", status=404)
            if p.remember and not p.password:
                secret = old["secret"]  # keep the stored password
            db.execute("UPDATE profiles SET label=?, host=?, port=?, user=?, dbname=?, sslmode=?, secret=? WHERE id=?",
                       (p.label.strip(), p.host, p.port, p.user, p.dbname, p.sslmode, secret, profile_id))
            pid = profile_id
        else:
            cur = db.execute("INSERT INTO profiles (label, host, port, user, dbname, sslmode, secret, created_at) VALUES (?,?,?,?,?,?,?,?)",
                             (p.label.strip(), p.host, p.port, p.user, p.dbname, p.sslmode, secret, dt.datetime.now().isoformat()))
            pid = cur.lastrowid
        return _profile_row(db.execute("SELECT * FROM profiles WHERE id=?", (pid,)).fetchone())


@router.delete("/profiles/{profile_id}")
async def delete_profile(profile_id: int):
    with _db() as db:
        n = db.execute("DELETE FROM profiles WHERE id=?", (profile_id,)).rowcount
    return {"ok": True} if n else _err("Profil introuvable", status=404)


# ───────────────────────── connection test ─────────────────────────
class TestIn(BaseModel):
    conn: Conn
    check_db: Optional[str] = None


def _run_test(c: Conn, check_db: Optional[str]) -> dict:
    steps = []

    def step(key, label, ok, msg, ms=None, **extra):
        steps.append({"key": key, "label": label, "ok": ok, "message": msg, "ms": ms, **extra})

    try:
        params = _resolve(c)
    except ValueError as e:
        step("input", "Paramètres", False, str(e))
        return {"ok": False, "steps": steps}

    # 1. DNS
    t = time.perf_counter()
    try:
        infos = socket.getaddrinfo(c.host, c.port, type=socket.SOCK_STREAM)
        addrs = sorted({i[4][0] for i in infos})
        step("dns", "Résolution du nom", True, ", ".join(addrs[:4]), round((time.perf_counter() - t) * 1000, 1))
    except OSError as e:
        step("dns", "Résolution du nom", False, f"Nom d'hôte introuvable ({e})")
        return {"ok": False, "steps": steps}

    # 2. TCP port
    t = time.perf_counter()
    try:
        with socket.create_connection((c.host, c.port), timeout=c.timeout):
            pass
        step("tcp", "Port TCP", True, f"Le port {c.port} est ouvert : un serveur écoute", round((time.perf_counter() - t) * 1000, 1))
    except OSError as e:
        msg = "Délai dépassé (pare-feu ou hôte injoignable)" if isinstance(e, socket.timeout) else "Connexion refusée : aucun serveur n'écoute sur ce port"
        step("tcp", "Port TCP", False, msg, detail=str(e))
        return {"ok": False, "steps": steps}

    if not _psycopg():
        step("auth", "Connexion PostgreSQL", False, MISSING["error"], code="MISSING_DEP")
        return {"ok": False, "steps": steps}

    # 3. auth + server info
    t = time.perf_counter()
    try:
        with _connect(params, autocommit=True) as conn:
            ms = round((time.perf_counter() - t) * 1000, 1)
            cur = conn.execute("""SELECT version(), current_setting('server_version'), now() - pg_postmaster_start_time(),
                current_user, current_database(), inet_server_addr()::text, current_setting('server_encoding'),
                current_setting('max_connections')::int, (SELECT count(*) FROM pg_stat_activity),
                r.rolsuper, r.rolcreatedb, r.rolcreaterole, pg_is_in_recovery(),
                (SELECT setting FROM pg_settings WHERE name='password_encryption'),
                (SELECT ssl FROM pg_stat_ssl WHERE pid = pg_backend_pid())
                FROM pg_roles r WHERE r.rolname = current_user""")
            (ver, sver, uptime, cuser, cdb, saddr, enc, maxc, nconn, su, cdbr, crr, recovery, pwenc, ssl) = cur.fetchone()
            step("auth", "Authentification", True, f"Connecté en tant que « {cuser} » sur « {cdb} »", ms)
            info = {"version": ver, "server_version": sver, "uptime": str(uptime).split(".")[0], "user": cuser,
                    "database": cdb, "server_addr": saddr, "encoding": enc, "max_connections": maxc,
                    "connections": nconn, "superuser": su, "createdb": cdbr, "createrole": crr,
                    "replica": recovery, "password_encryption": pwenc, "ssl": bool(ssl)}
            step("server", "Serveur", True, f"PostgreSQL {sver} — en marche depuis {info['uptime']}")
            if check_db:
                r = conn.execute("SELECT pg_get_userbyid(datdba), pg_encoding_to_char(encoding), datallowconn, "
                                 "pg_size_pretty(pg_database_size(oid)) FROM pg_database WHERE datname = %s", (check_db,)).fetchone()
                if r:
                    step("db", f"Base « {check_db} »", True, f"Existe — propriétaire {r[0]}, {r[1]}, {r[3]}"
                         + ("" if r[2] else " (connexions interdites)"))
                else:
                    step("db", f"Base « {check_db} »", False, "Cette base n'existe pas")
            return {"ok": all(s["ok"] for s in steps), "steps": steps, "info": info}
    except Exception as e:
        x = _explain(e)
        step("auth", "Authentification", False, x["error"], code=x["code"], detail=x["detail"])
        return {"ok": False, "steps": steps}


@router.post("/test")
async def test(body: TestIn):
    return await asyncio.to_thread(_run_test, body.conn, (body.check_db or "").strip() or None)


# ───────────────────────── generic runner ─────────────────────────
def _with_conn(c: Conn, fn, **kw):
    if not _psycopg():
        return JSONResponse(status_code=400, content=MISSING)
    try:
        params = _resolve(c)
    except ValueError as e:
        return _err(str(e))
    try:
        with _connect(params, **kw) as conn:
            return fn(conn, params)
    except Exception as e:
        return JSONResponse(status_code=400, content=_explain(e))


class ConnOnly(BaseModel):
    conn: Conn


# ───────────────────────── databases ─────────────────────────
@router.post("/databases")
async def databases(body: ConnOnly):
    def fn(conn, _):
        rows = conn.execute("""SELECT d.datname, pg_get_userbyid(d.datdba), pg_encoding_to_char(d.encoding), d.datcollate,
              d.datallowconn, d.datistemplate,
              CASE WHEN has_database_privilege(d.datname, 'CONNECT') THEN pg_database_size(d.oid) END,
              (SELECT count(*) FROM pg_stat_activity a WHERE a.datname = d.datname)
              FROM pg_database d ORDER BY d.datistemplate, d.datname""").fetchall()
        return {"databases": [{"name": r[0], "owner": r[1], "encoding": r[2], "collate": r[3], "allow_conn": r[4],
                               "template": r[5], "size": r[6], "connections": r[7]} for r in rows]}
    return await asyncio.to_thread(_with_conn, body.conn, fn, autocommit=True)


class DbCreate(BaseModel):
    conn: Conn
    name: str
    owner: Optional[str] = None
    encoding: Optional[str] = "UTF8"
    template: Optional[str] = None


@router.post("/databases/create")
async def create_database(body: DbCreate):
    from_name = body.name.strip()
    if not NAME_RE.match(from_name):
        return _err("Nom de base invalide (1 à 63 caractères)")

    def fn(conn, _):
        from psycopg import sql
        q = sql.SQL("CREATE DATABASE {}").format(sql.Identifier(from_name))
        if body.owner:
            q += sql.SQL(" OWNER {}").format(sql.Identifier(body.owner))
        if body.encoding:
            if not re.match(r"^[A-Za-z0-9_\-]{2,20}$", body.encoding):
                return _err("Encodage invalide")
            q += sql.SQL(" ENCODING {}").format(sql.Literal(body.encoding))
        if body.template:
            q += sql.SQL(" TEMPLATE {}").format(sql.Identifier(body.template))
        conn.execute(q)
        return {"ok": True, "name": from_name}
    return await asyncio.to_thread(_with_conn, body.conn, fn, autocommit=True)


# ───────────────────────── SQL console ─────────────────────────
class QueryIn(BaseModel):
    conn: Conn
    sql: str
    read_only: bool = False
    dry_run: bool = False          # run in a transaction, then ROLLBACK
    max_rows: int = Field(500, ge=1, le=MAX_ROWS_HARD)
    timeout_s: int = Field(30, ge=1, le=3600)


def _run_query(q: QueryIn):
    if not q.sql.strip():
        return _err("Requête vide")
    if not _psycopg():
        return JSONResponse(status_code=400, content=MISSING)
    try:
        params = _resolve(q.conn)
    except ValueError as e:
        return _err(str(e))
    params["options"] = f"-c statement_timeout={q.timeout_s * 1000}"
    notices, results = [], []
    t0 = time.perf_counter()
    target = _target(params)
    try:
        conn = _connect(params, autocommit=not q.dry_run)
    except Exception as e:
        return JSONResponse(status_code=400, content=_explain(e))
    ok, error = True, None
    try:
        conn.add_notice_handler(lambda d: notices.append(f"{d.severity}: {d.message_primary}"))
        if q.read_only:
            conn.execute("SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY")
            if q.dry_run:
                conn.execute("SET TRANSACTION READ ONLY")
        cur = conn.cursor()
        cur.execute(q.sql.encode("utf-8"), prepare=False)
        while True:
            res = {"status": cur.statusmessage, "rowcount": cur.rowcount}
            if cur.description:
                cols = [d.name for d in cur.description]
                rows = cur.fetchmany(q.max_rows + 1)
                res.update(columns=cols, rows=[[_jsonable(v) for v in r] for r in rows[:q.max_rows]],
                           truncated=len(rows) > q.max_rows)
            results.append(res)
            if not cur.nextset():
                break
    except Exception as e:
        ok, error = False, _explain(e)
    finally:
        try:
            if q.dry_run:
                conn.rollback()
        except Exception:
            pass
        conn.close()
    duration = round((time.perf_counter() - t0) * 1000, 1)
    try:
        with _db() as db:
            db.execute("INSERT INTO history (target, sql, ok, duration_ms, at) VALUES (?,?,?,?,?)",
                       (target, _redact(q.sql.strip())[:20000], int(ok), duration, dt.datetime.now().isoformat(timespec="seconds")))
            db.execute("DELETE FROM history WHERE id NOT IN (SELECT id FROM history ORDER BY id DESC LIMIT 300)")
    except Exception:
        pass
    body = {"ok": ok, "results": results, "notices": notices, "duration_ms": duration, "dry_run": q.dry_run}
    if not ok:
        body.update(error)
        return JSONResponse(status_code=400, content=body)
    return body


@router.post("/query")
async def query(body: QueryIn):
    return await asyncio.to_thread(_run_query, body)


@router.get("/history")
async def history(limit: int = 100):
    with _db() as db:
        rows = db.execute("SELECT * FROM history ORDER BY id DESC LIMIT ?", (max(1, min(limit, 300)),)).fetchall()
    return [dict(r) for r in rows]


@router.delete("/history")
async def clear_history():
    with _db() as db:
        db.execute("DELETE FROM history")
    return {"ok": True}


# ───────────────────────── roles & passwords ─────────────────────────
def scram_verifier(password: str, iterations: int = 4096) -> str:
    """Build a SCRAM-SHA-256 verifier, so the clear password never reaches the server (nor its logs)."""
    pw = unicodedata.normalize("NFKC", password).encode("utf-8")
    salt = secrets.token_bytes(16)
    salted = hashlib.pbkdf2_hmac("sha256", pw, salt, iterations)
    client_key = hmac.new(salted, b"Client Key", hashlib.sha256).digest()
    stored_key = hashlib.sha256(client_key).digest()
    server_key = hmac.new(salted, b"Server Key", hashlib.sha256).digest()
    b64 = lambda b: base64.b64encode(b).decode()
    return f"SCRAM-SHA-256${iterations}:{b64(salt)}${b64(stored_key)}:{b64(server_key)}"


@router.post("/roles")
async def roles(body: ConnOnly):
    def fn(conn, _):
        rows = conn.execute("""SELECT r.rolname, r.rolsuper, r.rolcanlogin, r.rolcreatedb, r.rolcreaterole, r.rolreplication,
              r.rolconnlimit, r.rolvaliduntil,
              ARRAY(SELECT b.rolname FROM pg_auth_members m JOIN pg_roles b ON b.oid = m.roleid WHERE m.member = r.oid ORDER BY 1),
              (SELECT count(*) FROM pg_stat_activity a WHERE a.usename = r.rolname)
              FROM pg_roles r WHERE r.rolname !~ '^pg_' ORDER BY r.rolcanlogin DESC, r.rolname""").fetchall()
        me = conn.execute("SELECT current_user, rolsuper, rolcreaterole FROM pg_roles WHERE rolname = current_user").fetchone()
        return {"roles": [{"name": r[0], "superuser": r[1], "login": r[2], "createdb": r[3], "createrole": r[4],
                           "replication": r[5], "conn_limit": r[6], "valid_until": _jsonable(r[7]), "member_of": r[8],
                           "sessions": r[9]} for r in rows],
                "me": {"name": me[0], "superuser": me[1], "createrole": me[2]}}
    return await asyncio.to_thread(_with_conn, body.conn, fn, autocommit=True)


class PasswordIn(BaseModel):
    conn: Conn
    role: str
    new_password: str = Field(..., min_length=1, max_length=1000)
    method: str = "scram"            # scram (hashed client-side) | plain
    valid_until: Optional[str] = None  # ISO date, "infinity" or None (unchanged)
    update_profile: bool = False     # also update the saved profile password (when changing our own role)


@router.post("/roles/password")
async def change_password(body: PasswordIn):
    if body.method not in ("scram", "plain"):
        return _err("Méthode inconnue")
    if body.valid_until and body.valid_until != "infinity":
        try:
            dt.datetime.fromisoformat(body.valid_until)
        except ValueError:
            return _err("Date d'expiration invalide")

    def fn(conn, params):
        from psycopg import sql
        exists = conn.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (body.role,)).fetchone()
        if not exists:
            return _err(f"Le rôle « {body.role} » n'existe pas", "ROLE_NOT_FOUND", 404)
        secret = scram_verifier(body.new_password) if body.method == "scram" else body.new_password
        q = sql.SQL("ALTER ROLE {} WITH PASSWORD {}").format(sql.Identifier(body.role), sql.Literal(secret))
        if body.valid_until:
            q += sql.SQL(" VALID UNTIL {}").format(sql.Literal(body.valid_until))
        conn.execute(q)
        res = {"ok": True, "role": body.role, "method": body.method, "self": body.role == params["user"]}
        if body.update_profile and body.conn.profile_id and res["self"]:
            try:
                with _db() as db:
                    db.execute("UPDATE profiles SET secret=? WHERE id=? AND secret IS NOT NULL",
                               (_encrypt(body.new_password), body.conn.profile_id))
                res["profile_updated"] = True
            except Exception:
                res["profile_updated"] = False
        return res
    return await asyncio.to_thread(_with_conn, body.conn, fn, autocommit=True)


class RoleCreate(BaseModel):
    conn: Conn
    role: str
    password: Optional[str] = None
    login: bool = True
    createdb: bool = False
    createrole: bool = False
    superuser: bool = False
    valid_until: Optional[str] = None


@router.post("/roles/create")
async def create_role(body: RoleCreate):
    name = body.role.strip()
    if not NAME_RE.match(name) or name.lower().startswith("pg_"):
        return _err("Nom de rôle invalide")

    def fn(conn, _):
        from psycopg import sql
        opts = [("LOGIN" if body.login else "NOLOGIN"), ("CREATEDB" if body.createdb else "NOCREATEDB"),
                ("CREATEROLE" if body.createrole else "NOCREATEROLE"), ("SUPERUSER" if body.superuser else "NOSUPERUSER")]
        q = sql.SQL("CREATE ROLE {} WITH " + " ".join(opts)).format(sql.Identifier(name))
        if body.password:
            q += sql.SQL(" PASSWORD {}").format(sql.Literal(scram_verifier(body.password)))
        if body.valid_until:
            q += sql.SQL(" VALID UNTIL {}").format(sql.Literal(body.valid_until))
        conn.execute(q)
        return {"ok": True, "role": name}
    return await asyncio.to_thread(_with_conn, body.conn, fn, autocommit=True)


class LoginTest(BaseModel):
    conn: Conn          # host / port / dbname / sslmode are used
    role: str
    password: str


@router.post("/roles/test-login")
async def test_login(body: LoginTest):
    c = body.conn.model_copy(update={"user": body.role, "password": body.password, "profile_id": None})

    def fn(conn, _):
        return {"ok": True, "user": conn.execute("SELECT current_user").fetchone()[0]}
    return await asyncio.to_thread(_with_conn, c, fn, autocommit=True)
