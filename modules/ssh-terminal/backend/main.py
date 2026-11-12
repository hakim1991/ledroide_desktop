import asyncio
import base64
import codecs
import functools
import logging
import os
import posixpath
import re
import socket
import stat as statmod
import time
import uuid
from collections import deque
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, ValidationError, field_validator

from ledroide_sdk import Module

import sshterm_vault as vault

log = logging.getLogger("ledroide.ssh-terminal")

module = Module(__file__)
router = module.router

SCROLLBACK_CHARS = 400_000
FLUSH_DELAY = 0.012
MAX_EDIT_BYTES = 2 * 1024 * 1024
MAX_CHUNK_BYTES = 4 * 1024 * 1024
SECRET_KEYS = ("password", "key_data", "passphrase")


class SshError(Exception):
    def __init__(self, message: str, code: str, **extra):
        super().__init__(message)
        self.message, self.code, self.extra = message, code, extra


def err(message: str, code: str, **extra) -> dict:
    return {"error": message, "code": code, **extra}


def _ssh():
    try:
        import asyncssh
    except ImportError:
        raise SshError("La bibliothèque « asyncssh » n'est pas installée : réactivez le module pour l'installer.",
                       "DEPENDENCY_MISSING")
    return asyncssh


def friendly(exc: BaseException) -> tuple[str, str]:
    """Translate network / SSH / SFTP exceptions to a French message and an error code."""
    try:
        import asyncssh
    except ImportError:
        asyncssh = None
    if asyncssh is not None:
        if isinstance(exc, asyncssh.PermissionDenied):
            return "Authentification refusée : identifiant, mot de passe ou clé incorrect", "AUTH_FAILED"
        if isinstance(exc, asyncssh.KeyEncryptionError):
            return "Phrase de passe de la clé privée incorrecte", "BAD_PASSPHRASE"
        if isinstance(exc, asyncssh.KeyImportError):
            return f"Clé privée illisible : {exc}", "KEY_INVALID"
        if isinstance(exc, asyncssh.SFTPNoSuchFile):
            return f"Fichier ou dossier introuvable{_reason(exc)}", "NOT_FOUND"
        if isinstance(exc, asyncssh.SFTPPermissionDenied):
            return f"Permission refusée{_reason(exc)}", "PERMISSION_DENIED"
        if isinstance(exc, asyncssh.SFTPFileAlreadyExists):
            return "Un élément porte déjà ce nom", "EXISTS"
        if isinstance(exc, asyncssh.SFTPDirNotEmpty):
            return "Le dossier n'est pas vide", "DIR_NOT_EMPTY"
        if isinstance(exc, asyncssh.SFTPError):
            return f"Erreur SFTP : {exc.reason or exc}", "SFTP_ERROR"
        if isinstance(exc, asyncssh.ChannelOpenError):
            return f"Le serveur a refusé l'ouverture du canal : {exc.reason}", "CHANNEL_REFUSED"
        if isinstance(exc, asyncssh.DisconnectError):
            return f"Connexion fermée par le serveur : {exc.reason}", "DISCONNECTED"
        if isinstance(exc, asyncssh.ConnectionLost):
            return "Connexion perdue", "DISCONNECTED"
    if isinstance(exc, socket.gaierror):
        return "Nom d'hôte introuvable", "HOST_UNKNOWN"
    if isinstance(exc, ConnectionRefusedError):
        return "Connexion refusée : aucun serveur SSH sur ce port", "CONNECTION_REFUSED"
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
        return "Le serveur ne répond pas (délai dépassé)", "TIMEOUT"
    if isinstance(exc, OSError):
        return f"Erreur réseau : {exc.strerror or exc}", "NETWORK"
    return str(exc) or exc.__class__.__name__, "SSH_ERROR"


def _reason(exc) -> str:
    reason = getattr(exc, "reason", "") or ""
    return f" : {reason}" if reason and reason.lower() not in ("no such file", "permission denied") else ""


def guard(fn):
    @functools.wraps(fn)
    async def wrapper(payload: dict):
        try:
            return await fn(payload or {})
        except SshError as exc:
            return err(exc.message, exc.code, **exc.extra)
        except vault.VaultError as exc:
            return err(str(exc), "VAULT_ERROR")
        except ValidationError as exc:
            first = exc.errors()[0]
            field = ".".join(str(p) for p in first.get("loc", ())) or "valeur"
            return err(f"{field} : {first.get('msg')}", "INVALID_INPUT")
        except Exception as exc:
            message, code = friendly(exc)
            if code == "SSH_ERROR":
                log.exception("ssh-terminal: unexpected error")
            return err(message, code)
    return wrapper


def on(action: str):
    def decorator(fn):
        module.on(action)(guard(fn))
        return fn
    return decorator


# ── Saved hosts ──────────────────────────────────────────────────────────────

class HostModel(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    host: str = Field(min_length=1, max_length=255)
    port: int = Field(default=22, ge=1, le=65535)
    username: str = Field(min_length=1, max_length=64)
    auth: Literal["password", "key", "agent"] = "password"
    key_path: str = Field(default="", max_length=500)
    group: str = Field(default="", max_length=40)
    color: str = Field(default="", max_length=16)
    initial_dir: str = Field(default="", max_length=500)
    notes: str = Field(default="", max_length=2000)

    @field_validator("name", "host", "username", "group", "key_path", "initial_dir", mode="before")
    @classmethod
    def strip(cls, v):
        return v.strip() if isinstance(v, str) else v

    @field_validator("host")
    @classmethod
    def no_space(cls, v):
        if re.search(r"\s", v):
            raise ValueError("ne doit pas contenir d'espace")
        return v


def public_host(doc: dict) -> dict:
    secrets = doc.get("secrets") or {}
    out = {k: v for k, v in doc.items() if k != "secrets"}
    for k in SECRET_KEYS:
        out[f"has_{k}"] = bool(secrets.get(k))
    return out


async def get_host(host_id: str) -> dict:
    doc = await module.db.get("hosts", host_id or "")
    if not doc:
        raise SshError("Accès introuvable", "HOST_NOT_FOUND")
    return doc


@on("hosts_list")
async def hosts_list(payload):
    hosts = await module.db.query("hosts")
    hosts.sort(key=lambda h: ((h.get("group") or "").lower(), h.get("name", "").lower()))
    return {"hosts": [public_host(h) for h in hosts], "vault": vault.available()}


@on("host_save")
async def host_save(payload):
    raw = payload.get("host") or {}
    data = HostModel.model_validate(raw).model_dump()
    existing = await module.db.get("hosts", raw.get("_id")) if raw.get("_id") else None
    secrets = dict((existing or {}).get("secrets") or {})
    for key, value in (payload.get("secrets") or {}).items():
        if key not in SECRET_KEYS:
            continue
        if value is None or value == "":
            secrets.pop(key, None)
        else:
            secrets[key] = vault.protect(str(value))
    if data["auth"] == "key" and not data["key_path"] and not secrets.get("key_data"):
        raise SshError("Indiquez le chemin de la clé privée ou collez son contenu", "INVALID_INPUT")
    if data["key_path"]:
        p = Path(os.path.expandvars(os.path.expanduser(data["key_path"])))
        if data["auth"] == "key" and not p.is_file():
            raise SshError(f"Clé privée introuvable : {p}", "INVALID_INPUT")
    doc = {**(existing or {}), **data, "secrets": secrets}
    if existing:
        doc["_id"] = existing["_id"]
    doc.setdefault("created_at", time.time())
    doc["updated_at"] = time.time()
    saved = await module.db.save("hosts", doc)
    return {"host": public_host(saved)}


@on("host_duplicate")
async def host_duplicate(payload):
    src = await get_host(payload.get("id"))
    doc = {k: v for k, v in src.items() if k not in ("_id", "last_used")}
    doc["name"] = f"{src.get('name', '')} (copie)"[:80]
    doc["created_at"] = doc["updated_at"] = time.time()
    return {"host": public_host(await module.db.save("hosts", doc))}


@on("host_delete")
async def host_delete(payload):
    await get_host(payload.get("id"))
    await module.db.delete("hosts", payload["id"])
    return {"ok": True}


def _parse_ssh_config(text: str) -> list[dict]:
    entries, current = [], None
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        m = re.match(r"(\S+)\s*(?:=\s*|\s+)(.+)", line)
        if not m:
            continue
        key, value = m.group(1).lower(), m.group(2).strip().strip('"')
        if key == "match":
            current = None
        elif key == "host":
            names = [n for n in value.split() if not any(c in n for c in "*?!")]
            current = {"names": names, "opts": {}}
            if names:
                entries.append(current)
        elif current is not None:
            current["opts"].setdefault(key, value)
    out = []
    for e in entries:
        o = e["opts"]
        for name in e["names"]:
            try:
                port = int(o.get("port", 22))
            except ValueError:
                port = 22
            out.append({"name": name, "host": o.get("hostname", name), "port": port,
                        "username": o.get("user", ""), "key_path": o.get("identityfile", "")})
    return out


@on("import_ssh_config")
async def import_ssh_config(payload):
    path = Path.home() / ".ssh" / "config"
    if not path.is_file():
        raise SshError(f"Fichier {path} introuvable", "NOT_FOUND")
    candidates = _parse_ssh_config(path.read_text(encoding="utf-8", errors="replace"))
    existing = {(h.get("host"), h.get("port"), h.get("username")) for h in await module.db.query("hosts")}
    added, skipped = 0, 0
    default_user = os.environ.get("USERNAME", "root")
    for c in candidates:
        c["username"] = c["username"] or default_user
        if (c["host"], c["port"], c["username"]) in existing:
            skipped += 1
            continue
        c["auth"] = "key" if c["key_path"] else "agent"
        c["group"] = "~/.ssh/config"
        try:
            data = HostModel.model_validate(c).model_dump()
        except ValidationError:
            skipped += 1
            continue
        await module.db.save("hosts", {**data, "secrets": {}, "created_at": time.time(), "updated_at": time.time()})
        existing.add((c["host"], c["port"], c["username"]))
        added += 1
    return {"added": added, "skipped": skipped, "path": str(path)}


@on("known_hosts_list")
async def known_hosts_list(payload):
    return {"known": sorted(await module.db.query("known_hosts"), key=lambda k: k["_id"])}


@on("known_host_forget")
async def known_host_forget(payload):
    await module.db.delete("known_hosts", payload.get("id") or "")
    return {"ok": True}


# ── Sessions ─────────────────────────────────────────────────────────────────

class Session:
    def __init__(self, host: dict, conn, title: str):
        self.id = uuid.uuid4().hex[:12]
        self.host = host
        self.conn = conn
        self.title = title
        self.proc = None
        self.sftp = None
        self._sftp_lock = asyncio.Lock()
        self.reader: asyncio.Task | None = None
        self.scrollback: deque[str] = deque()
        self._scroll_len = 0
        self._pending: list[str] = []
        self._flush: asyncio.TimerHandle | None = None
        self.started = time.time()
        self.home = None
        self.uploads: dict[str, dict] = {}
        self.closed = False

    def info(self) -> dict:
        return {"sid": self.id, "host_id": self.host.get("_id"), "title": self.title, "started": self.started,
                "home": self.home, "color": self.host.get("color", ""),
                "target": f"{self.host['username']}@{self.host['host']}:{self.host.get('port', 22)}"}

    def push(self, text: str):
        self.scrollback.append(text)
        self._scroll_len += len(text)
        while self._scroll_len > SCROLLBACK_CHARS and len(self.scrollback) > 1:
            self._scroll_len -= len(self.scrollback.popleft())
        self._pending.append(text)
        if self._flush is None:
            self._flush = asyncio.get_running_loop().call_later(FLUSH_DELAY, self._schedule_emit)

    def _schedule_emit(self):
        self._flush = None
        asyncio.ensure_future(self.emit_pending())

    async def emit_pending(self):
        if self._pending:
            data, self._pending = "".join(self._pending), []
            await module.events.emit("term:data", {"sid": self.id, "data": data})

    async def get_sftp(self):
        async with self._sftp_lock:
            if self.sftp is None:
                self.sftp = await self.conn.start_sftp_client()
                if self.home is None:
                    self.home = await self.sftp.realpath(".")
            return self.sftp

    async def close(self, reason: str = ""):
        if self.closed:
            return
        self.closed = True
        for up in list(self.uploads.values()):
            try:
                await up["file"].close()
            except Exception:
                pass
        self.uploads.clear()
        if self.reader and self.reader is not asyncio.current_task():
            self.reader.cancel()
        try:
            if self.sftp:
                self.sftp.exit()
            self.conn.close()
            await asyncio.wait_for(self.conn.wait_closed(), 5)
        except Exception:
            pass
        sessions.pop(self.id, None)
        await self.emit_pending()
        await module.events.emit("term:exit", {"sid": self.id, "reason": reason})
        log.info("ssh session %s closed (%s)", self.id, reason or "fermée")


sessions: dict[str, Session] = {}


def get_session(payload) -> Session:
    s = sessions.get(payload.get("sid") or "")
    if not s or s.closed:
        raise SshError("Session fermée : reconnectez-vous", "SESSION_CLOSED")
    return s


async def _reader(s: Session):
    decoder = codecs.getincrementaldecoder("utf-8")("replace")
    reason = "Session terminée"
    try:
        while True:
            data = await s.proc.stdout.read(65536)
            if not data:
                break
            text = decoder.decode(data)
            if text:
                s.push(text)
        status = s.proc.exit_status
        if status not in (None, 0):
            reason = f"Session terminée (code {status})"
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        reason = friendly(exc)[0]
    tail = decoder.decode(b"", final=True)
    if tail:
        s.push(tail)
    await s.close(reason)


_PEM_BLOCK = re.compile(r"-----BEGIN ([A-Z0-9 ]+)-----(.*?)-----END \1-----", re.S)


def _fix_mislabeled_key(data: str) -> str:
    """OpenSSH-format keys are sometimes wrapped in a PEM header (BEGIN RSA PRIVATE KEY…);
    asyncssh then fails to parse them as DER, so re-label the block from its content."""
    m = _PEM_BLOCK.search(data)
    if not m or m.group(1) == "OPENSSH PRIVATE KEY":
        return data
    body = "".join(line.strip() for line in m.group(2).splitlines() if line.strip() and ":" not in line)
    try:
        raw = base64.b64decode(body, validate=True)
    except ValueError:
        return data
    if not raw.startswith(b"openssh-key-v1\0"):
        return data
    lines = "\n".join(body[i:i + 70] for i in range(0, len(body), 70))
    return f"-----BEGIN OPENSSH PRIVATE KEY-----\n{lines}\n-----END OPENSSH PRIVATE KEY-----\n"


def _key_from(host: dict, secrets: dict, passphrase: str | None):
    asyncssh = _ssh()
    key_data = secrets.get("key_data")
    try:
        if not key_data:
            path = Path(os.path.expandvars(os.path.expanduser(host.get("key_path") or "")))
            if not path.is_file():
                raise SshError(f"Clé privée introuvable : {path}", "KEY_NOT_FOUND")
            raw = path.read_bytes()
            try:
                text = raw.decode("ascii")
            except UnicodeDecodeError:
                return asyncssh.import_private_key(raw, passphrase or None)
            if not text.lstrip().startswith("-----"):
                return asyncssh.import_private_key(raw, passphrase or None)
            key_data = text
        return asyncssh.import_private_key(_fix_mislabeled_key(key_data), passphrase or None)
    except asyncssh.KeyImportError as exc:
        if "passphrase" in str(exc).lower() and not passphrase:
            raise SshError("Cette clé privée est protégée par une phrase de passe", "NEED_PASSPHRASE")
        raise
    except asyncssh.KeyEncryptionError:
        if not passphrase:
            raise SshError("Cette clé privée est protégée par une phrase de passe", "NEED_PASSPHRASE")
        raise SshError("Phrase de passe de la clé privée incorrecte", "BAD_PASSPHRASE")


def _client_class(known: set[str], trust: str | None, seen: dict):
    asyncssh = _ssh()

    class Client(asyncssh.SSHClient):
        def validate_host_public_key(self, host, addr, port, key):
            fp = key.get_fingerprint("sha256")
            seen.update(fingerprint=fp, key_type=key.get_algorithm())
            return fp in known or (bool(trust) and fp == trust)

    return Client


PROMPT_CODES = ("HOSTKEY_UNKNOWN", "HOSTKEY_CHANGED", "NEED_PASSWORD", "NEED_PASSPHRASE", "BAD_PASSPHRASE")


@on("connect")
async def connect(payload):
    """Questions for the user (host key, password, passphrase) come back as a normal
    result with `need: true`: the bridge only forwards `error` and `code` of an error."""
    try:
        return await _connect(payload)
    except SshError as exc:
        if exc.code not in PROMPT_CODES:
            raise
        return {"need": True, "code": exc.code, "message": exc.message, **exc.extra}


async def _connect(payload):
    asyncssh = _ssh()
    if payload.get("host_id"):
        host = await get_host(payload["host_id"])
    else:
        host = {**HostModel.model_validate(payload.get("host") or {}).model_dump(), "secrets": {}}
    secrets = {k: vault.unprotect(v) for k, v in (host.get("secrets") or {}).items() if k in SECRET_KEYS}
    password = payload.get("password") or secrets.get("password")
    passphrase = payload.get("passphrase") or secrets.get("passphrase")

    opts = dict(port=int(host.get("port") or 22), username=host["username"],
                connect_timeout=int(module.config.get("connect_timeout", 15) or 15),
                login_timeout=30, keepalive_interval=int(module.config.get("keepalive", 30) or 0) or None,
                keepalive_count_max=3)
    if host["auth"] == "password":
        if not password:
            raise SshError("Mot de passe requis", "NEED_PASSWORD")
        opts.update(password=password, client_keys=None, agent_path=None, preferred_auth="keyboard-interactive,password")
    elif host["auth"] == "key":
        opts.update(client_keys=[_key_from(host, secrets, passphrase)], agent_path=None,
                    preferred_auth="publickey,keyboard-interactive,password")
        if password:
            opts["password"] = password
    elif password:
        opts["password"] = password

    hid = f"{host['host'].lower()}:{opts['port']}"
    known_doc = await module.db.get("known_hosts", hid) or {}
    known = set(known_doc.get("fingerprints") or [])
    seen: dict = {}
    trust = payload.get("trust") or None
    try:
        conn = await asyncssh.connect(host["host"], known_hosts=([], [], []),
                                      client_factory=_client_class(known, trust, seen), **opts)
    except (asyncssh.HostKeyNotVerifiable, asyncssh.DisconnectError, OSError) as exc:
        fp = seen.get("fingerprint")
        if fp and fp not in known and fp != trust:
            extra = dict(fingerprint=fp, key_type=seen.get("key_type"), host=hid)
            if known:
                raise SshError(f"ATTENTION : la clé du serveur {hid} a changé. Il peut s'agir d'une réinstallation du "
                               "serveur ou d'une interception de la connexion.", "HOSTKEY_CHANGED",
                               expected=sorted(known), **extra)
            raise SshError(f"Première connexion à {hid} : vérifiez l'empreinte de la clé du serveur.",
                           "HOSTKEY_UNKNOWN", **extra)
        if isinstance(exc, asyncssh.PermissionDenied) and host["auth"] == "password" and not payload.get("password"):
            raise SshError("Mot de passe enregistré refusé : saisissez-le à nouveau", "NEED_PASSWORD", retry=True)
        raise

    if trust and trust == seen.get("fingerprint") and trust not in known:
        # Only reached after the user confirmed this fingerprint: it replaces any previous key.
        await module.db.save("known_hosts", {"_id": hid, "fingerprints": [trust],
                                             "key_type": seen.get("key_type"), "added_at": time.time()})

    title = host.get("name") or f"{host['username']}@{host['host']}"
    s = Session(host, conn, title)
    try:
        cols, rows = int(payload.get("cols") or 120), int(payload.get("rows") or 32)
        s.proc = await conn.create_process(term_type="xterm-256color", term_size=(cols, rows),
                                           encoding=None, stderr=asyncssh.STDOUT)
    except Exception:
        conn.close()
        raise
    sessions[s.id] = s
    s.reader = asyncio.create_task(_reader(s))

    if host.get("_id"):
        stored = await module.db.get("hosts", host["_id"])
        if stored:
            stored["last_used"] = time.time()
            if payload.get("save_password") and payload.get("password"):
                stored.setdefault("secrets", {})["password"] = vault.protect(payload["password"])
            if payload.get("save_passphrase") and payload.get("passphrase"):
                stored.setdefault("secrets", {})["passphrase"] = vault.protect(payload["passphrase"])
            await module.db.save("hosts", stored)
    try:
        await s.get_sftp()
    except Exception as exc:
        log.info("sftp unavailable for %s: %s", s.id, exc)
    log.info("ssh session %s opened to %s", s.id, hid)
    return s.info()


@on("sessions_list")
async def sessions_list(payload):
    return {"sessions": [s.info() for s in sessions.values() if not s.closed]}


@on("term_replay")
async def term_replay(payload):
    s = get_session(payload)
    return {"data": "".join(s.scrollback)}


@on("term_input")
async def term_input(payload):
    s = get_session(payload)
    s.proc.stdin.write(str(payload.get("data") or "").encode("utf-8"))
    return {"ok": True}


@on("term_resize")
async def term_resize(payload):
    s = get_session(payload)
    cols, rows = int(payload.get("cols") or 0), int(payload.get("rows") or 0)
    if 2 <= cols <= 1000 and 2 <= rows <= 500:
        s.proc.change_terminal_size(cols, rows)
    return {"ok": True}


@on("disconnect")
async def disconnect(payload):
    s = sessions.get(payload.get("sid") or "")
    if s:
        await s.close("Déconnecté")
    return {"ok": True}


@module.on_shutdown
async def _shutdown():
    for s in list(sessions.values()):
        await s.close("Module arrêté")


# ── SFTP ─────────────────────────────────────────────────────────────────────

def _rpath(path: str) -> str:
    path = str(path or "").strip()
    if not path.startswith("/"):
        raise SshError("Chemin distant invalide", "INVALID_PATH")
    return posixpath.normpath(path).replace("//", "/")


def _perm_string(mode: int) -> str:
    return statmod.filemode(mode)[1:] if mode is not None else ""


def _entry(name, attrs, longname: str = "") -> dict:
    mode = attrs.permissions
    kind = "file"
    if mode is not None:
        if statmod.S_ISDIR(mode):
            kind = "dir"
        elif statmod.S_ISLNK(mode):
            kind = "link"
    owner, group = attrs.owner, attrs.group
    if (owner is None or group is None) and longname:
        parts = longname.split(None, 8)
        if len(parts) >= 4:
            owner, group = owner or parts[2], group or parts[3]
    return {"name": name, "type": kind, "size": attrs.size or 0, "mtime": attrs.mtime,
            "perms": _perm_string(mode), "mode": f"{statmod.S_IMODE(mode):03o}" if mode is not None else "",
            "owner": owner if owner is not None else (str(attrs.uid) if attrs.uid is not None else ""),
            "group": group if group is not None else (str(attrs.gid) if attrs.gid is not None else "")}


@on("sftp_list")
async def sftp_list(payload):
    s = get_session(payload)
    sftp = await s.get_sftp()
    raw = payload.get("path") or s.home or "/"
    path = await sftp.realpath(_rpath(raw) if raw.startswith("/") else raw)
    entries = []
    for item in await sftp.readdir(path):
        name = item.filename if isinstance(item.filename, str) else item.filename.decode("utf-8", "replace")
        if name in (".", ".."):
            continue
        e = _entry(name, item.attrs, item.longname if isinstance(item.longname, str) else "")
        if e["type"] == "link":
            full = posixpath.join(path, name)
            try:
                target = await sftp.stat(full)
                e["link_dir"] = statmod.S_ISDIR(target.permissions or 0)
                e["size"] = target.size or 0
            except Exception:
                e["link_dir"], e["broken"] = False, True
            try:
                e["link_target"] = await sftp.readlink(full)
            except Exception:
                pass
        entries.append(e)
    parent = posixpath.dirname(path) if path != "/" else None
    return {"path": path, "parent": parent, "entries": entries, "home": s.home}


@on("sftp_mkdir")
async def sftp_mkdir(payload):
    sftp = await get_session(payload).get_sftp()
    await sftp.mkdir(_rpath(payload.get("path")))
    return {"ok": True}


@on("sftp_touch")
async def sftp_touch(payload):
    sftp = await get_session(payload).get_sftp()
    path = _rpath(payload.get("path"))
    if await sftp.exists(path):
        raise SshError("Un élément porte déjà ce nom", "EXISTS")
    async with sftp.open(path, "wb"):
        pass
    return {"ok": True}


@on("sftp_rename")
async def sftp_rename(payload):
    sftp = await get_session(payload).get_sftp()
    src, dst = _rpath(payload.get("src")), _rpath(payload.get("dst"))
    if src == dst:
        return {"ok": True}
    if await sftp.exists(dst):
        raise SshError(f"« {posixpath.basename(dst)} » existe déjà", "EXISTS")
    await sftp.rename(src, dst)
    return {"ok": True}


@on("sftp_delete")
async def sftp_delete(payload):
    sftp = await get_session(payload).get_sftp()
    deleted = 0
    for p in payload.get("paths") or []:
        path = _rpath(p)
        if path == "/":
            raise SshError("Impossible de supprimer la racine", "INVALID_PATH")
        attrs = await sftp.lstat(path)
        if attrs.permissions is not None and statmod.S_ISDIR(attrs.permissions):
            await sftp.rmtree(path)
        else:
            await sftp.remove(path)
        deleted += 1
    return {"deleted": deleted}


@on("sftp_chmod")
async def sftp_chmod(payload):
    sftp = await get_session(payload).get_sftp()
    mode = str(payload.get("mode") or "")
    if not re.fullmatch(r"[0-7]{3,4}", mode):
        raise SshError("Droits invalides (ex. 755)", "INVALID_INPUT")
    for p in payload.get("paths") or [payload.get("path")]:
        await sftp.chmod(_rpath(p), int(mode, 8))
    return {"ok": True}


@on("sftp_read")
async def sftp_read(payload):
    sftp = await get_session(payload).get_sftp()
    path = _rpath(payload.get("path"))
    attrs = await sftp.stat(path)
    if attrs.permissions is not None and statmod.S_ISDIR(attrs.permissions):
        raise SshError("C'est un dossier", "IS_DIR")
    if (attrs.size or 0) > MAX_EDIT_BYTES:
        raise SshError("Fichier trop volumineux pour l'éditeur (2 Mo max.) : téléchargez-le", "TOO_LARGE")
    async with sftp.open(path, "rb") as f:
        data = await f.read()
    if b"\x00" in data[:8192]:
        raise SshError("Fichier binaire : téléchargez-le pour l'ouvrir", "BINARY")
    try:
        text, encoding = data.decode("utf-8"), "utf-8"
    except UnicodeDecodeError:
        text, encoding = data.decode("latin-1"), "latin-1"
    return {"path": path, "content": text, "encoding": encoding, "size": len(data), "mtime": attrs.mtime,
            "crlf": b"\r\n" in data}


@on("sftp_write")
async def sftp_write(payload):
    sftp = await get_session(payload).get_sftp()
    path = _rpath(payload.get("path"))
    content = str(payload.get("content") or "")
    encoding = payload.get("encoding") if payload.get("encoding") in ("utf-8", "latin-1") else "utf-8"
    if payload.get("crlf"):
        content = content.replace("\r\n", "\n").replace("\n", "\r\n")
    data = content.encode(encoding, errors="replace")
    expected = payload.get("expected_mtime")
    if expected is not None and not payload.get("force") and await sftp.exists(path):
        current = (await sftp.stat(path)).mtime
        if current is not None and current != expected:
            raise SshError("Le fichier a été modifié sur le serveur depuis son ouverture", "CONFLICT")
    async with sftp.open(path, "wb") as f:
        await f.write(data)
    return {"ok": True, "size": len(data), "mtime": (await sftp.stat(path)).mtime}


@on("upload_begin")
async def upload_begin(payload):
    s = get_session(payload)
    sftp = await s.get_sftp()
    name = str(payload.get("name") or "")
    if not name or "/" in name or name in (".", ".."):
        raise SshError("Nom de fichier invalide", "INVALID_PATH")
    path = posixpath.join(_rpath(payload.get("dir")), name)
    if await sftp.exists(path) and not payload.get("overwrite"):
        raise SshError(f"« {name} » existe déjà", "EXISTS", path=path)
    tmp = posixpath.join(posixpath.dirname(path), f".{name}.ledroide-part")
    token = uuid.uuid4().hex
    s.uploads[token] = {"file": await sftp.open(tmp, "wb"), "tmp": tmp, "path": path, "written": 0}
    return {"token": token, "path": path}


def _upload(s: Session, payload) -> dict:
    up = s.uploads.get(payload.get("token") or "")
    if not up:
        raise SshError("Envoi inconnu ou expiré", "UPLOAD_UNKNOWN")
    return up


@on("upload_chunk")
async def upload_chunk(payload):
    s = get_session(payload)
    up = _upload(s, payload)
    data = base64.b64decode(payload.get("data") or "")
    if len(data) > MAX_CHUNK_BYTES:
        raise SshError("Bloc trop volumineux", "INVALID_INPUT")
    await up["file"].write(data, up["written"])
    up["written"] += len(data)
    return {"written": up["written"]}


@on("upload_end")
async def upload_end(payload):
    s = get_session(payload)
    up = _upload(s, payload)
    s.uploads.pop(payload["token"], None)
    sftp = await s.get_sftp()
    await up["file"].close()
    if await sftp.exists(up["path"]):
        try:
            await sftp.posix_rename(up["tmp"], up["path"])
        except Exception:
            await sftp.remove(up["path"])
            await sftp.rename(up["tmp"], up["path"])
    else:
        await sftp.rename(up["tmp"], up["path"])
    return {"path": up["path"], "size": up["written"]}


@on("upload_abort")
async def upload_abort(payload):
    s = sessions.get(payload.get("sid") or "")
    up = s.uploads.pop(payload.get("token") or "", None) if s else None
    if up:
        try:
            await up["file"].close()
            await (await s.get_sftp()).remove(up["tmp"])
        except Exception:
            pass
    return {"ok": True}


def download_root() -> Path:
    custom = (module.config.get("download_dir") or "").strip()
    d = Path(os.path.expandvars(os.path.expanduser(custom))) if custom else Path.home() / "Downloads" / "Ledroide SSH"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _unique_local(folder: Path, name: str) -> Path:
    p = folder / name
    stem, suffix, n = p.stem, p.suffix, 2
    while p.exists():
        p = folder / f"{stem} ({n}){suffix}"
        n += 1
    return p


def _safe_local_name(name: str) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).rstrip(" .")
    return cleaned or "fichier"


async def _plan(sftp, remote: str, local: Path, out: list):
    attrs = await sftp.lstat(remote)
    mode = attrs.permissions or 0
    if statmod.S_ISLNK(mode):
        try:
            attrs = await sftp.stat(remote)
            mode = attrs.permissions or 0
        except Exception:
            return
    if statmod.S_ISDIR(mode):
        out.append((None, local, 0))
        for item in await sftp.readdir(remote):
            name = item.filename if isinstance(item.filename, str) else item.filename.decode("utf-8", "replace")
            if name in (".", ".."):
                continue
            await _plan(sftp, posixpath.join(remote, name), local / _safe_local_name(name), out)
    elif statmod.S_ISREG(mode):
        out.append((remote, local, attrs.size or 0))


@module.job("download")
async def download(payload, job):
    try:
        s = get_session(payload)
        sftp = await s.get_sftp()
        root = download_root()
        plan: list = []
        for p in payload.get("paths") or []:
            remote = _rpath(p)
            await _plan(sftp, remote, _unique_local(root, _safe_local_name(posixpath.basename(remote) or "racine")), plan)
        total = sum(size for _, _, size in plan) or 1
        done, files = 0, 0
        await job.progress(0, f"{sum(1 for r, _, _ in plan if r)} fichier(s)")
        for remote, local, size in plan:
            if remote is None:
                local.mkdir(parents=True, exist_ok=True)
                continue
            local.parent.mkdir(parents=True, exist_ok=True)
            base = done
            last = [0.0]

            def progress(_src, _dst, copied, _total, base=base, name=posixpath.basename(remote)):
                now = time.monotonic()
                if now - last[0] > 0.25:
                    last[0] = now
                    asyncio.ensure_future(job.progress(min(99, (base + copied) * 100 / total), name))

            await sftp.get(remote, str(local), progress_handler=progress, block_size=256 * 1024)
            done += size
            files += 1
            await job.progress(min(99, done * 100 / total), posixpath.basename(remote))
        first = plan[0][1] if plan else root
        return {"folder": str(root), "first": str(first), "files": files, "bytes": done}
    except SshError as exc:
        raise RuntimeError(exc.message)
    except Exception as exc:
        raise RuntimeError(friendly(exc)[0])


@on("open_local")
async def open_local(payload):
    root = download_root().resolve()
    target = Path(payload.get("path") or root).resolve()
    if target != root and root not in target.parents:
        raise SshError("Chemin non autorisé", "INVALID_PATH")
    if not target.exists():
        raise SshError("Élément introuvable", "NOT_FOUND")
    if hasattr(os, "startfile"):
        if target.is_file() and payload.get("select"):
            import subprocess
            subprocess.Popen(["explorer", "/select,", str(target)])
        else:
            os.startfile(str(target))
    return {"ok": True}


@on("info")
async def info(payload):
    return {"download_dir": str(download_root()), "vault": vault.available(),
            "font_size": int(module.config.get("font_size", 14) or 14),
            "scrollback": int(module.config.get("scrollback", 5000) or 5000),
            "show_hidden": bool(module.config.get("show_hidden", False)),
            "asyncssh": _has_asyncssh()}


def _has_asyncssh() -> bool:
    try:
        import asyncssh  # noqa: F401
        return True
    except ImportError:
        return False
