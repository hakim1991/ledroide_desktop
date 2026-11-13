import asyncio
import base64
import csv
import hashlib
import hmac
import io
import json
import logging
import math
import re
import secrets
import sqlite3
import string
import struct
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qs, unquote, urlparse

from pydantic import BaseModel, Field, ValidationError, field_validator

from ledroide_sdk import Module

log = logging.getLogger("ledroide.password-vault")

module = Module(__file__)
router = module.router

DB_NAME = "vault.db"
EXPORT_FORMAT = "ledroide-password-vault-export"
CANARY = b"ledroide-password-vault:canary:v1"
CANARY_AAD = b"canary"
SETTINGS_AAD = b"settings"
EXPORT_AAD = b"ledroide-password-vault:export:v1"
NONCE_LEN = 12
KEY_LEN = 32
MIN_PASSWORD = 8
SCRYPT = {"n": 2 ** 17, "r": 8, "p": 1}
IDLE_CHECK_S = 5
HISTORY_LEN = 5
MAX_IMPORT_ROWS = 20000
DEFAULT_SETTINGS = {"auto_lock_minutes": 5, "clipboard_seconds": 20, "lock_on_hide": True}

WORDS = sorted(set((Path(__file__).with_name("words_fr.txt")).read_text(encoding="utf-8").split()))
SYMBOLS = "!#$%&*+-=?@^_~.,;:()[]{}"
AMBIGUOUS = set("Il1O0o|`'\"")
COMMON = {
    "password", "motdepasse", "azerty", "qwerty", "123456", "12345678", "123456789", "1234567890", "111111",
    "000000", "abc123", "iloveyou", "admin", "welcome", "letmein", "monkey", "dragon", "football", "soleil",
    "bonjour", "doudou", "loulou", "marseille", "chouchou", "nicolas", "jetaime", "azertyuiop", "qwertyuiop",
    "password1", "passw0rd", "secret", "master", "shadow", "sunshine", "princess", "superman", "batman",
    "trustno1", "changeme", "default", "root", "toor", "test", "guest", "user", "login",
}
KEYBOARD_ROWS = ["azertyuiop", "qsdfghjklm", "wxcvbn", "qwertyuiop", "asdfghjkl", "zxcvbnm", "1234567890"]


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
    if kdf.get("name") != "scrypt":
        raise VaultError("BAD_VAULT", "Paramètres de dérivation inconnus")
    n, r, p = int(kdf["n"]), int(kdf["r"]), int(kdf["p"])
    if not (2 ** 14 <= n <= 2 ** 20 and 1 <= r <= 16 and 1 <= p <= 4):
        raise VaultError("BAD_VAULT", "Paramètres de dérivation hors limites")
    return bytearray(hashlib.scrypt(password.encode("utf-8"), salt=salt, n=n, r=r, p=p,
                                    maxmem=128 * n * r * 2, dklen=KEY_LEN))


def _zero(buf: Optional[bytearray]):
    if buf is not None:
        for i in range(len(buf)):
            buf[i] = 0


def _seal(aead, plain: bytes, aad: bytes) -> bytes:
    nonce = secrets.token_bytes(NONCE_LEN)
    return nonce + aead.encrypt(nonce, plain, aad)


def _open(aead, blob: bytes, aad: bytes) -> bytes:
    return aead.decrypt(blob[:NONCE_LEN], blob[NONCE_LEN:], aad)


def _err(message: str, code: str = "BAD_REQUEST") -> dict:
    return {"error": message, "code": code}


def _validation_message(exc: ValidationError) -> str:
    first = exc.errors()[0]
    loc = ".".join(str(p) for p in first.get("loc", ()))
    msg = str(first.get("msg", "Données invalides")).removeprefix("Value error, ")
    return f"{loc} : {msg}" if loc else msg


# ── Storage ───────────────────────────────────────────────────────────────────

class Store:
    """Own SQLite file: a master password change rewrites every entry in one transaction."""

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
                "salt BLOB NOT NULL, canary BLOB NOT NULL, settings TEXT NOT NULL DEFAULT '{}');"
                "CREATE TABLE IF NOT EXISTS entries (id TEXT PRIMARY KEY, data BLOB NOT NULL);")
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
        self.entries: dict[str, dict] = {}
        self.unreadable: set[str] = set()
        self.last_activity = 0.0
        self.watchdog: asyncio.Task | None = None
        self.op_lock = asyncio.Lock()
        self.failures = 0

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
        _zero(self.key)
        self.key = None
        self.aead = None
        self.entries = {}
        self.unreadable = set()


vault = Vault()


def _read_vault(c: sqlite3.Connection):
    row = c.execute("SELECT kdf, salt, canary, settings FROM vault WHERE id = 1").fetchone()
    if row is None:
        return None
    try:
        settings = {**DEFAULT_SETTINGS, **json.loads(row[3] or "{}")}
    except ValueError:
        settings = dict(DEFAULT_SETTINGS)
    return {"kdf": json.loads(row[0]), "salt": row[1], "canary": row[2], "settings": settings}


async def _settings() -> dict:
    v = await store.run(_read_vault)
    return v["settings"] if v else dict(DEFAULT_SETTINGS)


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
        try:
            limit = int((await _settings())["auto_lock_minutes"]) * 60
        except (sqlite3.Error, TypeError, ValueError, KeyError):
            limit = DEFAULT_SETTINGS["auto_lock_minutes"] * 60
        if limit and vault.unlocked and time.monotonic() - vault.last_activity >= limit:
            await _lock("auto")
            return


def _start_watchdog():
    if vault.watchdog is None or vault.watchdog.done():
        vault.watchdog = asyncio.create_task(_watchdog())


def _check_password(password: str, confirm: str | None = None):
    if len(password) < MIN_PASSWORD:
        raise VaultError("WEAK_PASSWORD", f"Le mot de passe maître doit contenir au moins {MIN_PASSWORD} caractères")
    if len(password) > 1024:
        raise VaultError("BAD_REQUEST", "Mot de passe trop long")
    if confirm is not None and password != confirm:
        raise VaultError("PASSWORD_MISMATCH", "Les deux mots de passe ne correspondent pas")


async def _verify(password: str) -> tuple[bytearray, dict]:
    AESGCM, InvalidTag = _aesgcm()
    v = await store.run(_read_vault)
    if v is None:
        raise VaultError("NOT_INITIALIZED", "Aucun coffre n'a encore été créé")
    key = await asyncio.to_thread(_derive, password, v["kdf"], v["salt"])
    try:
        plain = _open(AESGCM(bytes(key)), v["canary"], CANARY_AAD)
    except InvalidTag:
        plain = None
    if plain != CANARY:
        _zero(key)
        vault.failures += 1
        await asyncio.sleep(min(vault.failures, 6) * 0.4)
        raise VaultError("WRONG_PASSWORD", "Mot de passe incorrect")
    vault.failures = 0
    return key, v


async def _load_entries():
    _, InvalidTag = _aesgcm()
    rows = await store.run(lambda c: c.execute("SELECT id, data FROM entries").fetchall())
    entries, bad = {}, set()
    for entry_id, data in rows:
        try:
            entries[entry_id] = json.loads(_open(vault.aead, data, entry_id.encode()))
        except (InvalidTag, ValueError):
            bad.add(entry_id)
    vault.entries, vault.unreadable = entries, bad


async def _guard(coro):
    try:
        return await coro
    except VaultError as exc:
        return _err(exc.message, exc.code)


# ── Strength ──────────────────────────────────────────────────────────────────

def _pool(pw: str) -> int:
    pool = 0
    if any(c in string.ascii_lowercase for c in pw):
        pool += 26
    if any(c in string.ascii_uppercase for c in pw):
        pool += 26
    if any(c in string.digits for c in pw):
        pool += 10
    if any(c in string.punctuation or c == " " for c in pw):
        pool += 33
    if any(ord(c) > 127 for c in pw):
        pool += 100
    return pool or 1


def _is_sequence(chunk: str) -> bool:
    low = chunk.lower()
    if len(low) < 3:
        return False
    diffs = {ord(b) - ord(a) for a, b in zip(low, low[1:])}
    if len(diffs) == 1 and diffs.pop() in (-1, 0, 1):
        return True
    return any(low in row or low in row[::-1] for row in KEYBOARD_ROWS)


def strength(pw: str) -> dict:
    if not pw:
        return {"score": 0, "entropy": 0, "label": "Vide", "warnings": []}
    warnings = []
    low = pw.lower()
    leet = low.translate(str.maketrans("4@3110$5!7+", "aaeiiossitt"))
    if low in COMMON or leet in COMMON or low.rstrip("0123456789!") in COMMON:
        return {"score": 0, "entropy": 5, "label": "Très faible", "warnings": ["Mot de passe très courant"]}
    bits_per_char = math.log2(_pool(pw))
    entropy = 0.0
    i = 0
    seq_found = word_found = False
    while i < len(pw):
        matched = 0
        for size in range(min(12, len(pw) - i), 3, -1):
            chunk = leet[i:i + size]
            if chunk in WORDS_SET or chunk in COMMON:
                matched, word_found = size, True
                entropy += math.log2(len(WORDS)) + (1 if pw[i:i + size] != low[i:i + size] else 0)
                break
        if not matched:
            size = 3
            while i + size <= len(pw) and _is_sequence(pw[i:i + size]):
                size += 1
            if size > 3:
                matched, seq_found = size - 1, True
                entropy += bits_per_char + math.log2(matched)
        if not matched:
            entropy += bits_per_char
            matched = 1
        i += matched
    if seq_found:
        warnings.append("Contient une suite ou une répétition (abc, 111, azerty…)")
    if word_found:
        warnings.append("Contient des mots du dictionnaire")
    if len(pw) < 12:
        warnings.append("Moins de 12 caractères")
    if re.fullmatch(r"(19|20)\d{2}|\d{1,2}[/.-]?\d{1,2}[/.-]?\d{2,4}", pw):
        entropy = min(entropy, 15)
        warnings.append("Ressemble à une date")
    entropy = round(entropy, 1)
    score = 0 if entropy < 28 else 1 if entropy < 40 else 2 if entropy < 60 else 3 if entropy < 80 else 4
    return {"score": score, "entropy": entropy,
            "label": ["Très faible", "Faible", "Moyen", "Fort", "Très fort"][score], "warnings": warnings}


WORDS_SET = set(WORDS)


# ── Generator ─────────────────────────────────────────────────────────────────

def _generate(opts: dict) -> dict:
    mode = opts.get("mode") or "password"
    if mode == "passphrase":
        count = max(3, min(12, int(opts.get("words") or 5)))
        sep = str(opts.get("separator") if opts.get("separator") is not None else "-")[:3]
        words = [secrets.choice(WORDS) for _ in range(count)]
        entropy = count * math.log2(len(WORDS))
        if opts.get("capitalize", True):
            words = [w.capitalize() for w in words]
        if opts.get("number", True):
            idx = secrets.randbelow(count)
            words[idx] += str(secrets.randbelow(100))
            entropy += math.log2(100) + math.log2(count)
        value = sep.join(words)
    else:
        length = max(4, min(128, int(opts.get("length") or 20)))
        classes = []
        if opts.get("lower", True):
            classes.append(string.ascii_lowercase)
        if opts.get("upper", True):
            classes.append(string.ascii_uppercase)
        if opts.get("digits", True):
            classes.append(string.digits)
        if opts.get("symbols", True):
            classes.append(SYMBOLS)
        if not classes:
            raise VaultError("BAD_REQUEST", "Choisissez au moins un type de caractères")
        if opts.get("exclude_ambiguous"):
            classes = ["".join(c for c in cls if c not in AMBIGUOUS) for cls in classes]
        if length < len(classes):
            raise VaultError("BAD_REQUEST", "Longueur trop courte pour les types choisis")
        alphabet = "".join(classes)
        while True:
            chars = [secrets.choice(alphabet) for _ in range(length)]
            if all(any(c in cls for c in chars) for cls in classes):
                break
        value = "".join(chars)
        entropy = length * math.log2(len(alphabet))
    entropy = round(entropy, 1)
    score = 0 if entropy < 28 else 1 if entropy < 40 else 2 if entropy < 60 else 3 if entropy < 80 else 4
    return {"password": value, "entropy": entropy, "score": score,
            "label": ["Très faible", "Faible", "Moyen", "Fort", "Très fort"][score]}


# ── TOTP (RFC 6238) ───────────────────────────────────────────────────────────

ALGOS = {"SHA1": hashlib.sha1, "SHA256": hashlib.sha256, "SHA512": hashlib.sha512}


def parse_totp(raw: str) -> dict:
    raw = (raw or "").strip()
    cfg = {"digits": 6, "period": 30, "algorithm": "SHA1", "issuer": "", "account": ""}
    secret = raw
    if raw.lower().startswith("otpauth://"):
        u = urlparse(raw)
        if u.netloc.lower() != "totp":
            raise ValueError("Seuls les codes TOTP (otpauth://totp/…) sont pris en charge")
        q = {k.lower(): v[0] for k, v in parse_qs(u.query).items()}
        secret = q.get("secret", "")
        label = unquote(u.path.lstrip("/"))
        issuer, _, account = label.rpartition(":")
        cfg["account"] = account.strip()
        cfg["issuer"] = q.get("issuer", issuer).strip()
        algo = q.get("algorithm", "SHA1").upper()
        if algo not in ALGOS:
            raise ValueError("Algorithme TOTP inconnu (SHA1, SHA256 ou SHA512)")
        cfg["algorithm"] = algo
        try:
            cfg["digits"] = int(q.get("digits", 6))
            cfg["period"] = int(q.get("period", 30))
        except ValueError:
            raise ValueError("Paramètres TOTP invalides")
        if not 6 <= cfg["digits"] <= 8 or not 10 <= cfg["period"] <= 300:
            raise ValueError("Paramètres TOTP hors limites")
    secret = re.sub(r"[\s-]", "", secret).upper().rstrip("=")
    if not secret or not re.fullmatch(r"[A-Z2-7]+", secret):
        raise ValueError("Secret TOTP invalide (base32 attendu : lettres A-Z et chiffres 2-7)")
    try:
        key = base64.b32decode(secret + "=" * (-len(secret) % 8))
    except ValueError:
        raise ValueError("Secret TOTP invalide (base32)")
    if len(key) < 5:
        raise ValueError("Secret TOTP trop court")
    cfg["key"] = key
    return cfg


def totp_code(cfg: dict, at: float | None = None) -> dict:
    now = time.time() if at is None else at
    counter = int(now // cfg["period"])
    digest = hmac.new(cfg["key"], struct.pack(">Q", counter), ALGOS[cfg["algorithm"]]).digest()
    offset = digest[-1] & 0x0F
    value = (struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF) % (10 ** cfg["digits"])
    return {"code": str(value).zfill(cfg["digits"]), "period": cfg["period"],
            "remaining": round(cfg["period"] - (now % cfg["period"]), 1)}


# ── Entries ───────────────────────────────────────────────────────────────────

class EntryIn(BaseModel):
    title: str = Field("", max_length=200)
    username: str = Field("", max_length=500)
    password: str = Field("", max_length=2000)
    url: str = Field("", max_length=2000)
    notes: str = Field("", max_length=50000)
    folder: str = Field("", max_length=100)
    tags: list[str] = Field(default_factory=list, max_length=30)
    favorite: bool = False
    totp: str = Field("", max_length=2000)

    @field_validator("title", "username", "url", "folder", "totp")
    @classmethod
    def _strip(cls, v: str) -> str:
        return v.strip()

    @field_validator("folder")
    @classmethod
    def _folder(cls, v: str) -> str:
        return "/".join(p.strip() for p in v.split("/") if p.strip())

    @field_validator("tags")
    @classmethod
    def _tags(cls, v: list[str]) -> list[str]:
        out = []
        for t in v:
            t = str(t).strip().lower()[:40]
            if t and t not in out:
                out.append(t)
        return out

    @field_validator("totp")
    @classmethod
    def _totp(cls, v: str) -> str:
        if v:
            parse_totp(v)
        return v


def _host(url: str) -> str:
    if not url:
        return ""
    u = urlparse(url if "://" in url else "https://" + url)
    return (u.hostname or "").removeprefix("www.")


def _normalize(e: EntryIn) -> dict:
    data = e.model_dump()
    if not data["title"]:
        data["title"] = _host(data["url"]) or data["username"] or "Sans titre"
    return data


def _summary(entry_id: str, e: dict, reuse: dict[str, int]) -> dict:
    pw = e.get("password", "")
    return {"id": entry_id, "title": e.get("title", ""), "username": e.get("username", ""),
            "url": e.get("url", ""), "host": _host(e.get("url", "")), "folder": e.get("folder", ""),
            "tags": e.get("tags", []), "favorite": bool(e.get("favorite")), "has_totp": bool(e.get("totp")),
            "has_password": bool(pw), "created": e.get("created", ""), "updated": e.get("updated", ""),
            "score": strength(pw)["score"] if pw else None, "reused": max(0, reuse.get(pw, 0) - 1) if pw else 0}


def _matches(e: dict, q: str) -> bool:
    hay = " ".join([e.get("title", ""), e.get("username", ""), e.get("url", ""), e.get("notes", ""),
                    e.get("folder", ""), " ".join(e.get("tags", []))]).lower()
    return all(w in hay for w in q.split())


async def _write_entry(entry_id: str, doc: dict, insert: bool):
    blob = _seal(vault.aead, json.dumps(doc, ensure_ascii=False).encode("utf-8"), entry_id.encode())

    def write(c):
        if insert:
            c.execute("INSERT INTO entries(id, data) VALUES (?, ?)", (entry_id, blob))
        elif not c.execute("UPDATE entries SET data = ? WHERE id = ?", (blob, entry_id)).rowcount:
            raise VaultError("NOT_FOUND", "Entrée introuvable")
    await store.run(write)
    vault.entries[entry_id] = doc


# ── Lifecycle handlers ────────────────────────────────────────────────────────

@module.on("status")
async def status(payload: dict):
    try:
        v = await store.run(_read_vault)
    except sqlite3.Error as exc:
        return _err(f"Base du coffre illisible : {exc}", "STORAGE_ERROR")
    try:
        _aesgcm()
        dep = True
    except VaultError:
        dep = False
    return {"initialized": v is not None, "unlocked": vault.unlocked, "dependency_ok": dep,
            "settings": v["settings"] if v else dict(DEFAULT_SETTINGS), "min_password": MIN_PASSWORD,
            "count": len(vault.entries) if vault.unlocked else None}


@module.on("setup")
async def setup(payload: dict):
    async def run():
        async with vault.op_lock:
            AESGCM, _ = _aesgcm()
            password = str(payload.get("password") or "")
            _check_password(password, str(payload.get("confirm") or ""))
            if await store.run(_read_vault) is not None:
                raise VaultError("ALREADY_INITIALIZED", "Un coffre existe déjà")
            kdf, salt = {"name": "scrypt", **SCRYPT}, secrets.token_bytes(16)
            key = await asyncio.to_thread(_derive, password, kdf, salt)
            canary = _seal(AESGCM(bytes(key)), CANARY, CANARY_AAD)
            await store.run(lambda c: c.execute(
                "INSERT INTO vault(id, kdf, salt, canary, settings) VALUES (1, ?, ?, ?, ?)",
                (json.dumps(kdf), salt, canary, json.dumps(DEFAULT_SETTINGS))))
            vault.set_key(key)
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
            await _load_entries()
            _start_watchdog()
            log.info("vault unlocked")
            return {"ok": True, "unlocked": True, "count": len(vault.entries), "unreadable": len(vault.unreadable)}
    result = await _guard(run())
    if result.get("code") == "WRONG_PASSWORD":
        log.info("unlock refused: wrong password")
    return result


@module.on("lock")
async def lock(payload: dict):
    await _lock(str(payload.get("reason") or "manual")[:20])
    return {"ok": True, "unlocked": False}


@module.on("ping")
async def ping(payload: dict):
    if vault.unlocked:
        vault.touch()
    return {"unlocked": vault.unlocked}


@module.on("settings.save")
async def settings_save(payload: dict):
    async def run():
        vault.require()
        cur = await _settings()
        try:
            new = {"auto_lock_minutes": max(0, min(240, int(payload.get("auto_lock_minutes", cur["auto_lock_minutes"])))),
                   "clipboard_seconds": max(0, min(300, int(payload.get("clipboard_seconds", cur["clipboard_seconds"])))),
                   "lock_on_hide": bool(payload.get("lock_on_hide", cur["lock_on_hide"]))}
        except (TypeError, ValueError):
            raise VaultError("BAD_REQUEST", "Réglages invalides")
        await store.run(lambda c: c.execute("UPDATE vault SET settings = ? WHERE id = 1", (json.dumps(new),)))
        return {"settings": new}
    return await _guard(run())


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
            kdf, salt = {"name": "scrypt", **SCRYPT}, secrets.token_bytes(16)
            new_key = await asyncio.to_thread(_derive, new, kdf, salt)
            new_aead = AESGCM(bytes(new_key))

            def rekey(c):
                out = []
                for entry_id, data in c.execute("SELECT id, data FROM entries").fetchall():
                    try:
                        plain = _open(old, data, entry_id.encode())
                    except InvalidTag:
                        raise VaultError("CORRUPTED", "Une entrée est illisible : changement annulé, rien n'a été modifié")
                    out.append((_seal(new_aead, plain, entry_id.encode()), entry_id))
                c.execute("BEGIN IMMEDIATE")
                try:
                    c.executemany("UPDATE entries SET data = ? WHERE id = ?", out)
                    c.execute("UPDATE vault SET kdf = ?, salt = ?, canary = ? WHERE id = 1",
                              (json.dumps(kdf), salt, _seal(new_aead, CANARY, CANARY_AAD)))
                    c.execute("COMMIT")
                except Exception:
                    c.execute("ROLLBACK")
                    raise
                return len(out)

            try:
                count = await store.run(rekey)
            except VaultError:
                _zero(new_key)
                raise
            finally:
                _zero(old_key)
            vault.set_key(new_key)
            await _load_entries()
            _start_watchdog()
            log.info("master password changed, %d entr(y/ies) re-encrypted", count)
            return {"ok": True, "reencrypted": count}
    return await _guard(run())


@module.on("reset_vault")
async def reset_vault(payload: dict):
    if payload.get("confirm") != "SUPPRIMER":
        return _err("Confirmation manquante", "CONFIRM_REQUIRED")
    async with vault.op_lock:
        await _lock("reset")

        def wipe(c):
            c.execute("BEGIN IMMEDIATE")
            c.execute("DELETE FROM entries")
            c.execute("DELETE FROM vault")
            c.execute("COMMIT")
            c.execute("VACUUM")
        await store.run(wipe)
    log.info("vault reset")
    return {"ok": True}


# ── Entry handlers ────────────────────────────────────────────────────────────

@module.on("entries.list")
async def entries_list(payload: dict):
    async def run():
        vault.require()
        q = str(payload.get("q") or "").strip().lower()
        folder = payload.get("folder")
        tag = str(payload.get("tag") or "")
        view = str(payload.get("view") or "all")
        reuse: dict[str, int] = {}
        for e in vault.entries.values():
            if e.get("password"):
                reuse[e["password"]] = reuse.get(e["password"], 0) + 1
        items, folders, tags = [], {}, {}
        weak = reused = totp = favs = 0
        for entry_id, e in vault.entries.items():
            s = _summary(entry_id, e, reuse)
            folders[s["folder"]] = folders.get(s["folder"], 0) + 1
            for t in s["tags"]:
                tags[t] = tags.get(t, 0) + 1
            is_weak = s["score"] is not None and s["score"] <= 1
            weak += is_weak
            reused += s["reused"] > 0
            totp += s["has_totp"]
            favs += s["favorite"]
            if folder is not None and folder != "*" and s["folder"] != folder and not s["folder"].startswith(folder + "/"):
                continue
            if tag and tag not in s["tags"]:
                continue
            if view == "favorites" and not s["favorite"]:
                continue
            if view == "weak" and not is_weak:
                continue
            if view == "reused" and not s["reused"]:
                continue
            if view == "totp" and not s["has_totp"]:
                continue
            if q and not _matches(e, q):
                continue
            items.append(s)
        items.sort(key=lambda s: (not s["favorite"], s["title"].lower()))
        return {"items": items, "total": len(vault.entries), "unreadable": len(vault.unreadable),
                "folders": dict(sorted(folders.items())), "tags": dict(sorted(tags.items())),
                "counts": {"weak": weak, "reused": reused, "totp": totp, "favorites": favs}}
    return await _guard(run())


@module.on("entry.get")
async def entry_get(payload: dict):
    async def run():
        vault.require()
        entry_id = str(payload.get("id") or "")
        e = vault.entries.get(entry_id)
        if e is None:
            raise VaultError("NOT_FOUND", "Entrée introuvable")
        out = {"id": entry_id, **e, "strength": strength(e.get("password", ""))}
        if e.get("totp"):
            try:
                cfg = parse_totp(e["totp"])
                out["totp_info"] = {"issuer": cfg["issuer"], "account": cfg["account"], "digits": cfg["digits"],
                                    "period": cfg["period"], "algorithm": cfg["algorithm"]}
            except ValueError as exc:
                out["totp_info"] = {"error": str(exc)}
        return out
    return await _guard(run())


@module.on("entry.save")
async def entry_save(payload: dict):
    async def run():
        vault.require()
        try:
            data = _normalize(EntryIn.model_validate(payload.get("entry") or {}))
        except ValidationError as exc:
            raise VaultError("BAD_REQUEST", _validation_message(exc))
        entry_id = str(payload.get("id") or "")
        now = _now()
        if entry_id:
            old = vault.entries.get(entry_id)
            if old is None:
                raise VaultError("NOT_FOUND", "Entrée introuvable")
            history = list(old.get("history", []))
            changed = old.get("password_changed", old.get("created", now))
            if old.get("password") and old["password"] != data["password"]:
                history = [{"password": old["password"], "until": now}] + history
                changed = now
            doc = {**data, "created": old.get("created", now), "updated": now,
                   "password_changed": changed, "history": history[:HISTORY_LEN]}
            await _write_entry(entry_id, doc, insert=False)
        else:
            entry_id = uuid.uuid4().hex
            doc = {**data, "created": now, "updated": now, "password_changed": now, "history": []}
            await _write_entry(entry_id, doc, insert=True)
        return {"id": entry_id, "title": doc["title"]}
    return await _guard(run())


@module.on("entry.favorite")
async def entry_favorite(payload: dict):
    async def run():
        vault.require()
        entry_id = str(payload.get("id") or "")
        old = vault.entries.get(entry_id)
        if old is None:
            raise VaultError("NOT_FOUND", "Entrée introuvable")
        await _write_entry(entry_id, {**old, "favorite": bool(payload.get("favorite"))}, insert=False)
        return {"ok": True}
    return await _guard(run())


@module.on("entry.delete")
async def entry_delete(payload: dict):
    async def run():
        vault.require()
        entry_id = str(payload.get("id") or "")
        n = await store.run(lambda c: c.execute("DELETE FROM entries WHERE id = ?", (entry_id,)).rowcount)
        if not n:
            raise VaultError("NOT_FOUND", "Entrée introuvable")
        vault.entries.pop(entry_id, None)
        vault.unreadable.discard(entry_id)
        return {"ok": True}
    return await _guard(run())


@module.on("entry.clear_history")
async def entry_clear_history(payload: dict):
    async def run():
        vault.require()
        entry_id = str(payload.get("id") or "")
        old = vault.entries.get(entry_id)
        if old is None:
            raise VaultError("NOT_FOUND", "Entrée introuvable")
        await _write_entry(entry_id, {**old, "history": []}, insert=False)
        return {"ok": True}
    return await _guard(run())


@module.on("folder.rename")
async def folder_rename(payload: dict):
    async def run():
        vault.require()
        old = "/".join(p.strip() for p in str(payload.get("old") or "").split("/") if p.strip())
        new = "/".join(p.strip() for p in str(payload.get("new") or "").split("/") if p.strip())[:100]
        if not old:
            raise VaultError("BAD_REQUEST", "Dossier invalide")
        n = 0
        for entry_id, e in list(vault.entries.items()):
            f = e.get("folder", "")
            if f == old or f.startswith(old + "/"):
                await _write_entry(entry_id, {**e, "folder": (new + f[len(old):]).strip("/")}, insert=False)
                n += 1
        return {"ok": True, "moved": n}
    return await _guard(run())


@module.on("totp.codes")
async def totp_codes(payload: dict):
    async def run():
        vault.require()
        out = {}
        for entry_id in [str(i) for i in (payload.get("ids") or [])][:200]:
            e = vault.entries.get(entry_id)
            if not e or not e.get("totp"):
                continue
            try:
                out[entry_id] = totp_code(parse_totp(e["totp"]))
            except ValueError as exc:
                out[entry_id] = {"error": str(exc)}
        return {"codes": out}
    return await _guard(run())


@module.on("totp.preview")
async def totp_preview(payload: dict):
    async def run():
        vault.require()
        try:
            cfg = parse_totp(str(payload.get("totp") or ""))
        except ValueError as exc:
            raise VaultError("BAD_TOTP", str(exc))
        return {**totp_code(cfg), "issuer": cfg["issuer"], "account": cfg["account"],
                "digits": cfg["digits"], "algorithm": cfg["algorithm"]}
    return await _guard(run())


@module.on("generate")
async def generate(payload: dict):
    try:
        return _generate(payload or {})
    except VaultError as exc:
        return _err(exc.message, exc.code)
    except (TypeError, ValueError):
        return _err("Options du générateur invalides")


@module.on("strength")
async def strength_handler(payload: dict):
    return strength(str(payload.get("password") or "")[:2000])


# ── Import / export ───────────────────────────────────────────────────────────

def _pick(row: dict, *names: str) -> str:
    for n in names:
        v = row.get(n)
        if v:
            return str(v).strip()
    return ""


def parse_csv(text: str) -> tuple[str, list[dict]]:
    text = text.lstrip("﻿")
    sample = text[:4096]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    reader = csv.DictReader(io.StringIO(text), dialect=dialect)
    if not reader.fieldnames:
        raise VaultError("BAD_CSV", "Fichier CSV vide ou sans en-tête")
    reader.fieldnames = [f.strip().lower() for f in reader.fieldnames]
    cols = set(reader.fieldnames)
    if "login_password" in cols or "login_uri" in cols:
        fmt = "bitwarden"
    elif {"name", "url", "username", "password"} <= cols:
        fmt = "chrome"
    elif {"url", "username", "password"} <= cols and ("httprealm" in cols or "guid" in cols or "formactionorigin" in cols):
        fmt = "firefox"
    elif "password" in cols or "mot de passe" in cols:
        fmt = "générique"
    else:
        raise VaultError("BAD_CSV", "Format CSV non reconnu (colonnes Chrome, Firefox ou Bitwarden attendues)")
    out = []
    for i, row in enumerate(reader):
        if i >= MAX_IMPORT_ROWS:
            break
        row = {(k or "").strip().lower(): (v if isinstance(v, str) else "") for k, v in row.items()}
        if fmt == "bitwarden":
            if row.get("type") and row["type"] not in ("login", "note"):
                continue
            e = {"title": _pick(row, "name"), "username": _pick(row, "login_username"),
                 "password": row.get("login_password", ""), "url": _pick(row, "login_uri").split(",")[0],
                 "notes": row.get("notes", ""), "folder": _pick(row, "folder"),
                 "favorite": row.get("favorite", "") in ("1", "true"), "totp": _pick(row, "login_totp")}
            if row.get("fields"):
                e["notes"] = (e["notes"] + "\n\n" + row["fields"]).strip()
        elif fmt == "firefox":
            e = {"title": "", "username": _pick(row, "username"), "password": row.get("password", ""),
                 "url": _pick(row, "url"), "notes": ""}
        else:
            e = {"title": _pick(row, "name", "title", "titre", "nom"),
                 "username": _pick(row, "username", "login", "user", "identifiant", "email"),
                 "password": row.get("password") or row.get("mot de passe") or "",
                 "url": _pick(row, "url", "uri", "website", "site"),
                 "notes": row.get("note") or row.get("notes") or row.get("extra") or "",
                 "folder": _pick(row, "folder", "group", "grouping", "dossier"),
                 "totp": _pick(row, "totp", "otp", "otpauth")}
        if not (e.get("title") or e.get("url") or e.get("username") or e.get("password") or e.get("notes")):
            continue
        out.append(e)
    return fmt, out


@module.on("import.csv")
async def import_csv(payload: dict):
    async def run():
        vault.require()
        text = str(payload.get("text") or "")
        if len(text) > 30_000_000:
            raise VaultError("TOO_LARGE", "Fichier trop volumineux")
        fmt, rows = parse_csv(text)
        skip_dupes = payload.get("skip_duplicates", True)
        existing = {(e.get("url", "").lower(), e.get("username", ""), e.get("password", "")) for e in vault.entries.values()}
        imported = skipped = 0
        errors = []
        now = _now()
        for n, raw in enumerate(rows, start=2):
            totp = raw.get("totp", "")
            if totp:
                try:
                    parse_totp(totp)
                except ValueError:
                    raw["notes"] = (raw.get("notes", "") + f"\n\nTOTP (non reconnu) : {totp}").strip()
                    raw["totp"] = ""
            try:
                data = _normalize(EntryIn.model_validate(raw))
            except ValidationError as exc:
                errors.append(f"Ligne {n} : {_validation_message(exc)}")
                continue
            sig = (data["url"].lower(), data["username"], data["password"])
            if skip_dupes and sig in existing:
                skipped += 1
                continue
            existing.add(sig)
            entry_id = uuid.uuid4().hex
            await _write_entry(entry_id, {**data, "created": now, "updated": now, "password_changed": now,
                                          "history": []}, insert=True)
            imported += 1
        log.info("csv import (%s): %d imported, %d skipped, %d errors", fmt, imported, skipped, len(errors))
        return {"format": fmt, "imported": imported, "skipped": skipped, "errors": errors[:50]}
    return await _guard(run())


@module.on("export.encrypted")
async def export_encrypted(payload: dict):
    async def run():
        async with vault.op_lock:
            vault.require()
            AESGCM, _ = _aesgcm()
            key, _ = await _verify(str(payload.get("master") or ""))
            _zero(key)
            password = str(payload.get("password") or "")
            _check_password(password, str(payload.get("confirm") or ""))
            kdf, salt = {"name": "scrypt", **SCRYPT}, secrets.token_bytes(16)
            ekey = await asyncio.to_thread(_derive, password, kdf, salt)
            entries = [{k: v for k, v in e.items() if k != "history"} for e in vault.entries.values()]
            plain = json.dumps({"entries": entries, "exported": _now()}, ensure_ascii=False).encode("utf-8")
            blob = _seal(AESGCM(bytes(ekey)), plain, EXPORT_AAD)
            _zero(ekey)
            log.info("encrypted export of %d entr(y/ies)", len(entries))
            return {"format": EXPORT_FORMAT, "version": 1, "kdf": kdf, "cipher": "AES-256-GCM",
                    "salt": base64.b64encode(salt).decode(), "data": base64.b64encode(blob).decode(),
                    "count": len(entries)}
    return await _guard(run())


@module.on("import.encrypted")
async def import_encrypted(payload: dict):
    async def run():
        async with vault.op_lock:
            vault.require()
            AESGCM, InvalidTag = _aesgcm()
            data = payload.get("data") or {}
            if not isinstance(data, dict) or data.get("format") != EXPORT_FORMAT:
                raise VaultError("BAD_FILE", "Fichier non reconnu (export chiffré du Coffre attendu)")
            try:
                salt = base64.b64decode(data["salt"])
                blob = base64.b64decode(data["data"])
                ekey = await asyncio.to_thread(_derive, str(payload.get("password") or ""), data["kdf"], salt)
            except (KeyError, ValueError, TypeError):
                raise VaultError("BAD_FILE", "Fichier d'export invalide")
            try:
                plain = json.loads(_open(AESGCM(bytes(ekey)), blob, EXPORT_AAD))
            except InvalidTag:
                raise VaultError("WRONG_PASSWORD", "Mot de passe de l'export incorrect")
            except ValueError:
                raise VaultError("BAD_FILE", "Contenu de l'export illisible")
            finally:
                _zero(ekey)
            existing = {(e.get("url", "").lower(), e.get("username", ""), e.get("password", ""))
                        for e in vault.entries.values()}
            imported = skipped = 0
            now = _now()
            for raw in plain.get("entries") or []:
                if not isinstance(raw, dict):
                    continue
                try:
                    data_in = _normalize(EntryIn.model_validate({k: raw.get(k) for k in EntryIn.model_fields if k in raw}))
                except ValidationError:
                    skipped += 1
                    continue
                sig = (data_in["url"].lower(), data_in["username"], data_in["password"])
                if sig in existing:
                    skipped += 1
                    continue
                existing.add(sig)
                created = str(raw.get("created") or now)[:40]
                await _write_entry(uuid.uuid4().hex, {**data_in, "created": created, "updated": now,
                                                      "password_changed": str(raw.get("password_changed") or created)[:40],
                                                      "history": []}, insert=True)
                imported += 1
            return {"imported": imported, "skipped": skipped}
    return await _guard(run())


@module.on_shutdown
async def shutdown():
    await _lock("shutdown")
    await asyncio.to_thread(store.close)
