import asyncio
import hashlib
import json
import logging
import os
import secrets
import sqlite3
import threading
import time
import uuid
from datetime import datetime, timezone

from ledroide_sdk import Module

log = logging.getLogger("ledroide.encrypted-notes")

module = Module(__file__)
router = module.router

DB_NAME = "vault.db"
CANARY = b"ledroide-encrypted-notes:canary:v1"
CANARY_AAD = b"canary"
NONCE_LEN = 12
KEY_LEN = 32
MIN_PASSWORD = 8
SCRYPT = {"n": 2 ** 17, "r": 8, "p": 1}
MAX_TITLE = 300
MAX_CONTENT = 2_000_000
IDLE_CHECK_S = 10


class VaultError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _aesgcm():
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        from cryptography.exceptions import InvalidTag
    except ImportError:
        raise VaultError("DEPENDENCY_MISSING",
                         "La bibliothèque « cryptography » n'est pas installée. Réactivez le module "
                         "pour installer ses dépendances (menu Bibliothèques).")
    return AESGCM, InvalidTag


def _derive(password: str, kdf: dict, salt: bytes) -> bytearray:
    if kdf.get("name") == "scrypt":
        n, r, p = int(kdf["n"]), int(kdf["r"]), int(kdf["p"])
        key = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=n, r=r, p=p,
                             maxmem=128 * n * r * 2, dklen=KEY_LEN)
    elif kdf.get("name") == "pbkdf2-sha256":
        key = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, int(kdf["iterations"]), KEY_LEN)
    else:
        raise VaultError("BAD_VAULT", "Paramètres de dérivation inconnus")
    return bytearray(key)


class Store:
    """Own SQLite file so that a password change rewrites every note in one transaction."""

    def __init__(self):
        self.lock = threading.Lock()
        self.conn: sqlite3.Connection | None = None

    def _connect(self) -> sqlite3.Connection:
        if self.conn is None:
            self.conn = sqlite3.connect(str(module.data_path(DB_NAME)), check_same_thread=False, isolation_level=None)
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.execute("PRAGMA secure_delete=ON")
            self.conn.executescript(
                "CREATE TABLE IF NOT EXISTS vault (id INTEGER PRIMARY KEY CHECK (id = 1), kdf TEXT NOT NULL, "
                "salt BLOB NOT NULL, canary BLOB NOT NULL, created TEXT NOT NULL, updated TEXT NOT NULL);"
                "CREATE TABLE IF NOT EXISTS notes (id TEXT PRIMARY KEY, data BLOB NOT NULL, "
                "created TEXT NOT NULL, updated TEXT NOT NULL);")
        return self.conn

    async def run(self, fn, *args):
        def locked():
            with self.lock:
                return fn(self._connect(), *args)
        return await asyncio.to_thread(locked)

    def close(self):
        with self.lock:
            if self.conn is not None:
                self.conn.close()
                self.conn = None


store = Store()


class Vault:
    def __init__(self):
        self.key: bytearray | None = None
        self.aead = None
        self.titles: dict[str, str] = {}
        self.last_activity = 0.0
        self.watchdog: asyncio.Task | None = None
        self.op_lock = asyncio.Lock()

    @property
    def unlocked(self) -> bool:
        return self.aead is not None

    def touch(self):
        self.last_activity = time.monotonic()

    def require(self):
        if not self.unlocked:
            raise VaultError("LOCKED", "Le coffre est verrouillé")
        self.touch()

    def set_key(self, key: bytearray):
        AESGCM, _ = _aesgcm()
        self.wipe()
        self.key = key
        self.aead = AESGCM(bytes(key))
        self.touch()

    def wipe(self):
        if self.key is not None:
            for i in range(len(self.key)):
                self.key[i] = 0
        self.key = None
        self.aead = None
        self.titles.clear()

    def encrypt(self, plaintext: bytes, aad: bytes) -> bytes:
        nonce = secrets.token_bytes(NONCE_LEN)
        return nonce + self.aead.encrypt(nonce, plaintext, aad)

    def decrypt(self, blob: bytes, aad: bytes) -> bytes:
        return self.aead.decrypt(blob[:NONCE_LEN], blob[NONCE_LEN:], aad)


vault = Vault()


def _auto_lock_s() -> float:
    try:
        return max(0, int(module.config.get("auto_lock_minutes", 5))) * 60
    except (TypeError, ValueError):
        return 300


async def _lock(reason: str):
    was = vault.unlocked
    vault.wipe()
    task, vault.watchdog = vault.watchdog, None
    if task is not None and task is not asyncio.current_task():
        task.cancel()
    if was:
        log.info("vault locked (%s)", reason)
        await module.events.emit("locked", {"reason": reason})


async def _watchdog():
    while vault.unlocked:
        await asyncio.sleep(IDLE_CHECK_S)
        limit = _auto_lock_s()
        if limit and vault.unlocked and time.monotonic() - vault.last_activity >= limit:
            await _lock("auto")
            return


def _start_watchdog():
    if vault.watchdog is None or vault.watchdog.done():
        vault.watchdog = asyncio.create_task(_watchdog())


def _read_vault(c: sqlite3.Connection):
    row = c.execute("SELECT kdf, salt, canary FROM vault WHERE id = 1").fetchone()
    if row is None:
        return None
    return {"kdf": json.loads(row[0]), "salt": row[1], "canary": row[2]}


def _check_password(password: str, confirm: str | None = None):
    if len(password) < MIN_PASSWORD:
        raise VaultError("WEAK_PASSWORD", f"Le mot de passe doit contenir au moins {MIN_PASSWORD} caractères")
    if confirm is not None and password != confirm:
        raise VaultError("PASSWORD_MISMATCH", "Les deux mots de passe ne correspondent pas")


async def _verify(password: str) -> tuple[bytearray, dict]:
    """Derives the key and checks it against the canary; returns (key, vault row)."""
    _, InvalidTag = _aesgcm()
    v = await store.run(_read_vault)
    if v is None:
        raise VaultError("NOT_INITIALIZED", "Aucun coffre n'a encore été créé")
    key = await asyncio.to_thread(_derive, password, v["kdf"], v["salt"])
    AESGCM, _ = _aesgcm()
    try:
        plain = AESGCM(bytes(key)).decrypt(v["canary"][:NONCE_LEN], v["canary"][NONCE_LEN:], CANARY_AAD)
    except InvalidTag:
        plain = None
    if plain != CANARY:
        for i in range(len(key)):
            key[i] = 0
        raise VaultError("WRONG_PASSWORD", "Mot de passe incorrect")
    return key, v


def _new_kdf() -> dict:
    return {"name": "scrypt", **SCRYPT}


async def _guard(coro):
    try:
        return await coro
    except VaultError as exc:
        return {"error": exc.message, "code": exc.code}


def _load_titles(c: sqlite3.Connection) -> list[tuple]:
    return c.execute("SELECT id, data FROM notes").fetchall()


async def _refresh_titles():
    _, InvalidTag = _aesgcm()
    titles = {}
    for note_id, data in await store.run(_load_titles):
        try:
            titles[note_id] = json.loads(vault.decrypt(data, note_id.encode()))["title"]
        except (InvalidTag, ValueError, KeyError):
            titles[note_id] = "⚠ Note illisible"
    vault.titles = titles


# ── Vault lifecycle ──────────────────────────────────────────────────────────

@module.on("status")
async def status(payload: dict):
    try:
        v = await store.run(_read_vault)
    except sqlite3.Error as exc:
        return {"error": f"Base du coffre illisible : {exc}", "code": "STORAGE_ERROR"}
    try:
        _aesgcm()
        dep = True
    except VaultError:
        dep = False
    return {"initialized": v is not None, "unlocked": vault.unlocked, "dependency_ok": dep,
            "auto_lock_minutes": _auto_lock_s() / 60, "kdf": v["kdf"] if v else None,
            "min_password": MIN_PASSWORD}


@module.on("setup")
async def setup(payload: dict):
    async def run():
        async with vault.op_lock:
            AESGCM, _ = _aesgcm()
            password = str(payload.get("password") or "")
            _check_password(password, str(payload.get("confirm") or ""))
            if await store.run(_read_vault) is not None:
                raise VaultError("ALREADY_INITIALIZED", "Un coffre existe déjà")
            kdf, salt = _new_kdf(), secrets.token_bytes(16)
            key = await asyncio.to_thread(_derive, password, kdf, salt)
            nonce = secrets.token_bytes(NONCE_LEN)
            canary = nonce + AESGCM(bytes(key)).encrypt(nonce, CANARY, CANARY_AAD)
            now = _now()

            def write(c):
                c.execute("INSERT INTO vault(id, kdf, salt, canary, created, updated) VALUES (1, ?, ?, ?, ?, ?)",
                          (json.dumps(kdf), salt, canary, now, now))
            await store.run(write)
            vault.set_key(key)
            vault.titles = {}
            _start_watchdog()
            log.info("vault created")
            return {"ok": True, "unlocked": True}
    return await _guard(run())


@module.on("unlock")
async def unlock(payload: dict):
    async def run():
        async with vault.op_lock:
            key, _ = await _verify(str(payload.get("password") or ""))
            vault.set_key(key)
            await _refresh_titles()
            _start_watchdog()
            log.info("vault unlocked")
            return {"ok": True, "unlocked": True, "count": len(vault.titles)}
    result = await _guard(run())
    if result.get("code") == "WRONG_PASSWORD":
        log.info("unlock refused: wrong password")
    return result


@module.on("lock")
async def lock(payload: dict):
    await _lock("manual")
    return {"ok": True, "unlocked": False}


@module.on("ping")
async def ping(payload: dict):
    if vault.unlocked:
        vault.touch()
    return {"unlocked": vault.unlocked}


@module.on("change_password")
async def change_password(payload: dict):
    async def run():
        async with vault.op_lock:
            vault.require()
            AESGCM, InvalidTag = _aesgcm()
            new = str(payload.get("new") or "")
            _check_password(new, str(payload.get("confirm") or ""))
            old_key, _ = await _verify(str(payload.get("current") or ""))
            old = AESGCM(bytes(old_key))
            kdf, salt = _new_kdf(), secrets.token_bytes(16)
            new_key = await asyncio.to_thread(_derive, new, kdf, salt)
            new_aead = AESGCM(bytes(new_key))

            def seal(aead, plain, aad):
                n = secrets.token_bytes(NONCE_LEN)
                return n + aead.encrypt(n, plain, aad)

            def rekey(c):
                rows = c.execute("SELECT id, data FROM notes").fetchall()
                out = []
                for note_id, data in rows:
                    try:
                        plain = old.decrypt(data[:NONCE_LEN], data[NONCE_LEN:], note_id.encode())
                    except InvalidTag:
                        raise VaultError("CORRUPTED", "Une note est illisible : changement annulé, rien n'a été modifié")
                    out.append((seal(new_aead, plain, note_id.encode()), note_id))
                canary = seal(new_aead, CANARY, CANARY_AAD)
                c.execute("BEGIN IMMEDIATE")
                try:
                    c.executemany("UPDATE notes SET data = ? WHERE id = ?", out)
                    c.execute("UPDATE vault SET kdf = ?, salt = ?, canary = ?, updated = ? WHERE id = 1",
                              (json.dumps(kdf), salt, canary, _now()))
                    c.execute("COMMIT")
                except Exception:
                    c.execute("ROLLBACK")
                    raise
                return len(out)

            try:
                count = await store.run(rekey)
            finally:
                for i in range(len(old_key)):
                    old_key[i] = 0
            vault.set_key(new_key)
            await _refresh_titles()
            _start_watchdog()
            log.info("password changed, %d note(s) re-encrypted", count)
            return {"ok": True, "reencrypted": count}
    return await _guard(run())


@module.on("reset_vault")
async def reset_vault(payload: dict):
    if payload.get("confirm") != "SUPPRIMER":
        return {"error": "Confirmation manquante", "code": "CONFIRM_REQUIRED"}
    async with vault.op_lock:
        await _lock("reset")

        def wipe(c):
            c.execute("BEGIN IMMEDIATE")
            c.execute("DELETE FROM notes")
            c.execute("DELETE FROM vault")
            c.execute("COMMIT")
            c.execute("VACUUM")
        await store.run(wipe)
    log.info("vault reset")
    return {"ok": True}


# ── Notes ────────────────────────────────────────────────────────────────────

@module.on("list")
async def list_notes(payload: dict):
    async def run():
        vault.require()
        rows = await store.run(lambda c: c.execute("SELECT id, created, updated, length(data) FROM notes "
                                                    "ORDER BY updated DESC").fetchall())
        q = str(payload.get("q") or "").strip().lower()
        items = []
        for note_id, created, updated, size in rows:
            title = vault.titles.get(note_id, "")
            if q and not all(w in title.lower() for w in q.split()):
                continue
            items.append({"id": note_id, "title": title, "created": created, "updated": updated,
                          "size": max(0, size - NONCE_LEN - 16)})
        return {"items": items, "total": len(rows)}
    return await _guard(run())


@module.on("get")
async def get_note(payload: dict):
    async def run():
        vault.require()
        _, InvalidTag = _aesgcm()
        note_id = str(payload.get("id") or "")
        row = await store.run(lambda c: c.execute("SELECT data, created, updated FROM notes WHERE id = ?",
                                                  (note_id,)).fetchone())
        if row is None:
            raise VaultError("NOT_FOUND", "Note introuvable")
        try:
            doc = json.loads(vault.decrypt(row[0], note_id.encode()))
        except (InvalidTag, ValueError):
            raise VaultError("CORRUPTED", "Impossible de déchiffrer cette note (données altérées)")
        return {"id": note_id, "title": doc.get("title", ""), "content": doc.get("content", ""),
                "created": row[1], "updated": row[2]}
    return await _guard(run())


@module.on("save")
async def save_note(payload: dict):
    async def run():
        vault.require()
        title = str(payload.get("title") or "").strip()[:MAX_TITLE] or "Sans titre"
        content = str(payload.get("content") or "")
        if len(content) > MAX_CONTENT:
            raise VaultError("TOO_LARGE", "Note trop volumineuse (2 millions de caractères maximum)")
        note_id = str(payload.get("id") or "") or uuid.uuid4().hex
        blob = vault.encrypt(json.dumps({"title": title, "content": content}, ensure_ascii=False).encode("utf-8"),
                             note_id.encode())
        now = _now()

        def write(c):
            if payload.get("id"):
                cur = c.execute("UPDATE notes SET data = ?, updated = ? WHERE id = ?", (blob, now, note_id))
                if cur.rowcount == 0:
                    raise VaultError("NOT_FOUND", "Note introuvable (supprimée ?)")
                return c.execute("SELECT created FROM notes WHERE id = ?", (note_id,)).fetchone()[0]
            c.execute("INSERT INTO notes(id, data, created, updated) VALUES (?, ?, ?, ?)", (note_id, blob, now, now))
            return now
        created = await store.run(write)
        vault.titles[note_id] = title
        return {"id": note_id, "title": title, "created": created, "updated": now}
    return await _guard(run())


@module.on("delete")
async def delete_note(payload: dict):
    async def run():
        vault.require()
        note_id = str(payload.get("id") or "")
        n = await store.run(lambda c: c.execute("DELETE FROM notes WHERE id = ?", (note_id,)).rowcount)
        if not n:
            raise VaultError("NOT_FOUND", "Note introuvable")
        vault.titles.pop(note_id, None)
        return {"ok": True, "id": note_id}
    return await _guard(run())


@module.on_shutdown
async def shutdown():
    await _lock("shutdown")
    await asyncio.to_thread(store.close)
