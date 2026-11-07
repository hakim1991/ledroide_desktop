"""Éditeur du fichier hosts — lecture, validation, sauvegardes, écriture (élevée si besoin)."""
import asyncio
import hashlib
import importlib.util
import ipaddress
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Optional

from ledroide_sdk.http import JSONResponse
from pydantic import BaseModel
from ledroide_sdk import Module

module = Module(__file__)
router = module.router

BACKUP_DIR = Path(__file__).parent / "backups"
KEEP_BACKUPS = 10
BACKUP_RE = re.compile(r"^hosts_\d{8}_\d{6}(_\d+)?\.bak$")
HOSTNAME_RE = re.compile(r"^(?=.{1,253}$)(?!-)[A-Za-z0-9_](?:[A-Za-z0-9_-]{0,61}[A-Za-z0-9_])?"
                         r"(?:\.[A-Za-z0-9_](?:[A-Za-z0-9_-]{0,61}[A-Za-z0-9_])?)*\.?$")
MAX_SIZE = 5 * 1024 * 1024


def _load_elevate():
    name = "hosts_editor_elevate"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, Path(__file__).parent / "elevate.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


elevate = _load_elevate()


def hosts_path() -> Path:
    override = os.environ.get("LEDROIDE_HOSTS_PATH")
    if override:
        return Path(override)
    root = os.environ.get("SystemRoot", r"C:\Windows")
    return Path(root) / "System32" / "drivers" / "etc" / "hosts"


def _err(msg: str, status: int = 400, code: Optional[str] = None, **extra):
    c = {"error": msg}
    if code:
        c["code"] = code
    c.update(extra)
    return JSONResponse(status_code=status, content=c)


# ---------------------------------------------------------------- Lecture / analyse

def _decode(raw: bytes):
    if raw.startswith(b"\xef\xbb\xbf"):
        return raw[3:].decode("utf-8", errors="replace"), "utf-8-sig"
    try:
        return raw.decode("utf-8"), "utf-8"
    except UnicodeDecodeError:
        return raw.decode("cp1252", errors="replace"), "cp1252"


def _read_file() -> dict:
    p = hosts_path()
    raw = p.read_bytes()
    text, enc = _decode(raw)
    eol = "\r\n" if "\r\n" in text or "\n" not in text else "\n"
    norm = text.replace("\r\n", "\n").replace("\r", "\n")
    st = p.stat()
    return {"text": norm, "encoding": enc, "eol": eol, "hash": hashlib.sha256(raw).hexdigest(),
            "size": st.st_size, "mtime": st.st_mtime, "path": str(p)}


def valid_ip(s: str) -> bool:
    try:
        ipaddress.ip_address(s.split("%", 1)[0] if ":" in s else s)
        return True
    except ValueError:
        return False


def valid_host(s: str) -> bool:
    return bool(HOSTNAME_RE.match(s)) and not valid_ip(s)


def parse_line(line: str) -> dict:
    """Analyse une ligne. type = entry | comment | blank | invalid."""
    stripped = line.strip()
    if not stripped:
        return {"type": "blank"}
    enabled = True
    body = stripped
    if stripped.startswith("#"):
        body = stripped.lstrip("#").strip()
        enabled = False
    comment = ""
    if "#" in body:
        body, comment = body.split("#", 1)
        comment = comment.strip()
    parts = body.split()
    if parts and valid_ip(parts[0]) and len(parts) >= 2 and all(valid_host(h) for h in parts[1:]):
        return {"type": "entry", "enabled": enabled, "ip": parts[0], "hosts": parts[1:], "comment": comment}
    if not enabled:
        return {"type": "comment"}
    return {"type": "invalid", "reason": _invalid_reason(parts)}


def _invalid_reason(parts) -> str:
    if not parts:
        return "Ligne vide après suppression du commentaire."
    if not valid_ip(parts[0]):
        return f"Adresse IP invalide : « {parts[0][:60]} »."
    if len(parts) < 2:
        return "Aucun nom d'hôte après l'adresse IP."
    bad = [h for h in parts[1:] if not valid_host(h)]
    return f"Nom d'hôte invalide : « {bad[0][:80]} »." if bad else "Ligne invalide."


def parse_text(text: str) -> dict:
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines = lines[:-1]
    out, errors = [], []
    for i, l in enumerate(lines):
        d = parse_line(l)
        d["raw"] = l
        d["n"] = i + 1
        out.append(d)
        if d["type"] == "invalid":
            errors.append({"line": i + 1, "error": d["reason"], "raw": l[:200]})
    return {"lines": out, "errors": errors,
            "stats": {"entries": sum(d["type"] == "entry" for d in out),
                      "enabled": sum(d["type"] == "entry" and d["enabled"] for d in out),
                      "disabled": sum(d["type"] == "entry" and not d["enabled"] for d in out)}}


@router.get("/status")
async def status():
    p = hosts_path()
    return {"admin": elevate.is_admin(), "path": str(p), "exists": p.exists(),
            "override": bool(os.environ.get("LEDROIDE_HOSTS_PATH"))}


@router.get("/hosts")
async def get_hosts():
    try:
        d = await asyncio.to_thread(_read_file)
    except FileNotFoundError:
        return _err(f"Fichier introuvable : {hosts_path()}", 404)
    except PermissionError:
        return _err("Lecture du fichier hosts refusée.", 403)
    d.update(parse_text(d["text"]))
    d["admin"] = elevate.is_admin()
    return d


class TextIn(BaseModel):
    text: str


@router.post("/parse")
async def parse(payload: TextIn):
    if len(payload.text) > MAX_SIZE:
        return _err("Texte trop volumineux.")
    return parse_text(payload.text.replace("\r\n", "\n").replace("\r", "\n"))


class EntryIn(BaseModel):
    ip: str
    hosts: str
    comment: str = ""


@router.post("/validate-entry")
async def validate_entry(payload: EntryIn):
    errors = []
    ip = payload.ip.strip()
    hosts = payload.hosts.replace(",", " ").split()
    if not valid_ip(ip):
        errors.append("Adresse IP invalide (IPv4 ou IPv6 attendue).")
    if not hosts:
        errors.append("Indiquez au moins un nom d'hôte.")
    bad = [h for h in hosts if not valid_host(h)]
    if bad:
        errors.append("Nom(s) d'hôte invalide(s) : " + ", ".join(bad[:5]))
    if "\n" in payload.comment or "\r" in payload.comment:
        errors.append("Le commentaire doit tenir sur une ligne.")
    return {"ok": not errors, "errors": errors, "ip": ip, "hosts": hosts}


# ---------------------------------------------------------------- Sauvegardes

def _make_backup(raw: bytes) -> str:
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    base = time.strftime("hosts_%Y%m%d_%H%M%S")
    same = [_backup_key(p)[1] for p in BACKUP_DIR.glob(base + "*.bak") if BACKUP_RE.match(p.name)]
    # suffixe toujours croissant dans la même seconde (l'ordre reste chronologique même après rotation)
    name = f"{base}_{max(same) + 1}.bak" if same else base + ".bak"
    (BACKUP_DIR / name).write_bytes(raw)
    _prune()
    return name


def _backup_key(p: Path):
    m = re.match(r"^hosts_(\d{8}_\d{6})(?:_(\d+))?\.bak$", p.name)
    return (m.group(1), int(m.group(2) or 0)) if m else ("", 0)


def _backups() -> list:
    if not BACKUP_DIR.is_dir():
        return []
    items = [p for p in BACKUP_DIR.iterdir() if p.is_file() and BACKUP_RE.match(p.name)]
    items.sort(key=_backup_key, reverse=True)
    return items


def _prune():
    for p in _backups()[KEEP_BACKUPS:]:
        try:
            p.unlink()
        except OSError:
            pass


@router.get("/backups")
async def backups():
    out = []
    for p in _backups():
        st = p.stat()
        out.append({"name": p.name, "size": st.st_size, "mtime": st.st_mtime})
    return {"backups": out, "keep": KEEP_BACKUPS}


def _backup_path(name: str) -> Optional[Path]:
    if "/" in name or "\\" in name or ".." in name or not BACKUP_RE.match(name):
        return None
    p = BACKUP_DIR / name
    return p if p.is_file() else None


@router.get("/backups/{name}")
async def backup_content(name: str):
    p = _backup_path(name)
    if not p:
        return _err("Sauvegarde introuvable.", 404)
    text, _ = _decode(p.read_bytes())
    return {"name": name, "text": text.replace("\r\n", "\n")}


# ---------------------------------------------------------------- Écriture

def _flush_dns() -> dict:
    """ipconfig /flushdns (ne nécessite pas de droits admin). Moqué dans les tests."""
    try:
        r = subprocess.run(["ipconfig", "/flushdns"], capture_output=True, timeout=30,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return {"ok": r.returncode == 0, "output": r.stdout.decode("cp850", errors="replace").strip()}
    except Exception as e:
        return {"ok": False, "output": str(e)}


def _elevated_copy(src: Path, dst: Path) -> dict:
    script = (f"Copy-Item -LiteralPath {elevate.ps_quote(str(src))} -Destination {elevate.ps_quote(str(dst))} "
              "-Force -ErrorAction Stop; 'OK'")
    return elevate.run_powershell(script, elevate=True, timeout=120)


def _write_sync(text: str, base_hash: Optional[str]) -> dict:
    """Renvoie {"ok":..} ou {"_error": (msg, status, code)}."""
    cur = _read_file()
    if base_hash and base_hash != cur["hash"]:
        return {"_error": ("Le fichier hosts a été modifié par un autre programme depuis son chargement. "
                           "Rechargez-le avant d'enregistrer.", 409, "CONFLICT")}
    norm = text.replace("\r\n", "\n").replace("\r", "\n")
    if norm and not norm.endswith("\n"):
        norm += "\n"
    data = norm.replace("\n", cur["eol"]).encode("utf-8-sig" if cur["encoding"] == "utf-8-sig" else cur["encoding"],
                                                 errors="replace")
    path = hosts_path()
    raw_cur = path.read_bytes()
    if raw_cur == data:
        return {"ok": True, "unchanged": True, "backup": None, "elevated": False}
    backup = _make_backup(raw_cur)
    elevated = False
    try:
        with open(path, "r+b") as f:     # ouverture en écriture (échoue sans droits admin sur le vrai fichier)
            f.seek(0)
            f.write(data)
            f.truncate()
    except PermissionError:
        tmpdir = Path(tempfile.gettempdir()) / "ledroide_hosts_editor"
        tmpdir.mkdir(parents=True, exist_ok=True)
        tmp = tmpdir / f"hosts_{os.getpid()}_{int(time.time() * 1000)}.tmp"
        tmp.write_bytes(data)
        try:
            r = _elevated_copy(tmp, path)
        finally:
            try:
                tmp.unlink()
            except OSError:
                pass
        elevated = True
        if r.get("cancelled"):
            return {"_error": ("Élévation refusée : le fichier hosts n'a pas été modifié.", 403, "UAC_CANCELLED")}
        if not r.get("ok"):
            return {"_error": ("Échec de l'écriture élevée : " + (r.get("output") or "erreur inconnue"), 500, "WRITE_FAILED")}
    if path.read_bytes() != data:
        return {"_error": ("Le fichier n'a pas été écrit correctement (antivirus ? fichier verrouillé ?). "
                           f"Une sauvegarde est disponible : {backup}", 500, "WRITE_FAILED")}
    return {"ok": True, "unchanged": False, "backup": backup, "elevated": elevated}


class SaveIn(BaseModel):
    text: str
    base_hash: Optional[str] = None
    flush_dns: bool = False
    force: bool = False           # enregistrer malgré des lignes invalides


async def _save(text: str, base_hash: Optional[str], flush: bool, force: bool):
    if len(text) > MAX_SIZE:
        return _err("Texte trop volumineux.")
    if "\x00" in text:
        return _err("Caractère nul interdit.")
    norm = text.replace("\r\n", "\n").replace("\r", "\n")
    try:
        cur = await asyncio.to_thread(_read_file)
    except FileNotFoundError:
        return _err(f"Fichier introuvable : {hosts_path()}", 404)
    if norm.rstrip("\n") == cur["text"].rstrip("\n") and (not base_hash or base_hash == cur["hash"]):
        return {"ok": True, "unchanged": True, "backup": None, "elevated": False, "hash": cur["hash"]}
    parsed = parse_text(norm)
    if parsed["errors"] and not force:
        return _err(f"{len(parsed['errors'])} ligne(s) invalide(s). Corrigez-les ou commentez-les.", 400,
                    "INVALID_LINES", lines=parsed["errors"][:50])
    try:
        res = await asyncio.to_thread(_write_sync, text, base_hash)
    except FileNotFoundError:
        return _err(f"Fichier introuvable : {hosts_path()}", 404)
    if "_error" in res:
        msg, st, code = res["_error"]
        return _err(msg, st, code)
    if flush and not res.get("unchanged"):
        res["flush"] = await asyncio.to_thread(_flush_dns)
    d = await asyncio.to_thread(_read_file)
    res["hash"] = d["hash"]
    return res


@router.post("/save")
async def save(payload: SaveIn):
    return await _save(payload.text, payload.base_hash, payload.flush_dns, payload.force)


class RestoreIn(BaseModel):
    base_hash: Optional[str] = None
    flush_dns: bool = False


@router.post("/backups/{name}/restore")
async def restore(name: str, payload: RestoreIn = RestoreIn()):
    p = _backup_path(name)
    if not p:
        return _err("Sauvegarde introuvable.", 404)
    text, _ = _decode(p.read_bytes())
    res = await _save(text, payload.base_hash, payload.flush_dns, True)
    if isinstance(res, dict):
        res["restored"] = name
    return res


@router.post("/flush-dns")
async def flush_dns():
    r = await asyncio.to_thread(_flush_dns)
    if not r["ok"]:
        return _err("Échec du vidage du cache DNS : " + r["output"], 500)
    return {"ok": True, "output": r["output"]}


@router.get("/health")
async def health():
    return {"ok": True}
