import asyncio
import hashlib
import json
import logging
import os
import re
import secrets
import string
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from ledroide_sdk import Module

log = logging.getLogger("ledroide.env-editor")

module = Module(__file__)
router = module.router

MAX_FILE = 2_000_000
MAX_DEPTH = 4
MAX_FILES = 500
KEEP_BACKUPS = 30
SKIP_DIRS = {"node_modules", ".git", ".hg", ".svn", "venv", ".venv", "env", "__pycache__", "dist", "build", ".next",
             ".nuxt", "target", "vendor", ".idea", ".vscode", ".cache", ".tox", ".mypy_cache", ".pytest_cache", "coverage"}
KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.\-]*$")
ENTRY_RE = re.compile(r"^(\s*)(export\s+)?([^=\s#][^=\s]*)(\s*)=(\s*)(.*)$")
SECRET_RE = re.compile(r"(SECRET|PASSW|PWD|PASSPHRASE|TOKEN|API_?KEY|APIKEY|PRIVATE|CREDENTIAL|AUTH|SALT|SIGNING|"
                       r"ACCESS_?KEY|CLIENT_?KEY|_KEY$|^KEY$|DSN|CONN(ECTION)?_?STR|COOKIE|SESSION|WEBHOOK|ENCRYPT)", re.I)
URL_CRED_RE = re.compile(r"^[a-z][a-z0-9+.-]*://[^/\s:@]+:[^/\s@]+@", re.I)

_ready = False
_lock = asyncio.Lock()
_io_lock = asyncio.Lock()


class EnvError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _err(message: str, code: str = "BAD_REQUEST") -> dict:
    return {"error": message, "code": code}


async def _guard(coro):
    try:
        return await coro
    except EnvError as exc:
        return _err(exc.message, exc.code)


async def _db():
    global _ready
    if not _ready:
        async with _lock:
            if not _ready:
                await module.db.executescript(
                    "CREATE TABLE IF NOT EXISTS folders (id INTEGER PRIMARY KEY AUTOINCREMENT, path TEXT NOT NULL UNIQUE,"
                    " name TEXT NOT NULL, added_at TEXT NOT NULL);")
                _ready = True
    return module.db


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── Parsing ───────────────────────────────────────────────────────────────────

def is_secret(key: str, value: str) -> bool:
    return bool(key and SECRET_RE.search(key)) or bool(value and URL_CRED_RE.match(value))


def _find_close(s: str, q: str, start: int) -> int:
    i = start
    while i < len(s):
        if s[i] == "\\" and q == '"':
            i += 2
            continue
        if s[i] == q:
            return i
        i += 1
    return -1


def _unescape_dq(s: str) -> str:
    return re.sub(r'\\(.)', lambda m: {"n": "\n", "r": "\r", "t": "\t"}.get(m.group(1), m.group(1)), s, flags=re.S)


def parse(text: str) -> list[dict]:
    lines = text.split("\n")
    items = []
    i = 0
    while i < len(lines):
        raw = lines[i]
        n = i + 1
        i += 1
        stripped = raw.strip()
        if not stripped:
            items.append({"kind": "blank", "n": n, "raw": raw})
            continue
        if stripped.startswith("#"):
            items.append({"kind": "comment", "n": n, "raw": raw, "text": stripped[1:].lstrip() if stripped.startswith("# ") else stripped[1:]})
            continue
        m = ENTRY_RE.match(raw)
        if not m:
            items.append({"kind": "invalid", "n": n, "raw": raw, "error": "Ligne sans « = » (ni commentaire ni affectation)"})
            continue
        _, export, key, sp1, sp2, rest = m.groups()
        item = {"kind": "entry", "n": n, "raw": raw, "key": key, "export": bool(export), "quote": "", "value": "",
                "comment": "", "lines": 1, "warnings": []}
        if not KEY_RE.match(key):
            items.append({"kind": "invalid", "n": n, "raw": raw, "key": key,
                          "error": f"Nom de clé invalide « {key} » (lettres, chiffres, _ ; ne commence pas par un chiffre)"})
            continue
        if sp1 or sp2:
            item["warnings"].append("Espaces autour de « = » (mal géré par certains outils)")
        if rest[:1] in ('"', "'", "`"):
            q = rest[0]
            body = rest
            close = _find_close(body, q, 1)
            extra = 0
            while close == -1 and q == '"' and i + extra < len(lines):
                nxt = ENTRY_RE.match(lines[i + extra])
                if nxt and KEY_RE.match(nxt.group(3)) and '"' not in lines[i + extra].split("=", 1)[0]:
                    break
                body += "\n" + lines[i + extra]
                extra += 1
                close = _find_close(body, q, 1)
            if close == -1:
                items.append({"kind": "invalid", "n": n, "raw": raw, "key": key, "error": "Guillemet non fermé"})
                continue
            i += extra
            item["lines"] = 1 + extra
            if extra:
                item["raw"] = "\n".join(lines[n - 1:n + extra])
            inner = body[1:close]
            item["quote"] = q
            item["value"] = _unescape_dq(inner) if q == '"' else inner
            after = body[close + 1:].strip()
            if after.startswith("#"):
                item["comment"] = after[1:].strip()
            elif after:
                item["warnings"].append("Texte après la valeur entre guillemets (ignoré par la plupart des outils)")
        else:
            cm = re.search(r"\s+#", rest)
            if cm:
                item["comment"] = rest[cm.end():].strip()
                rest = rest[:cm.start()]
            elif rest.startswith("#"):
                item["comment"] = rest[1:].strip()
                rest = ""
            item["value"] = rest.strip()
            if re.search(r"\s", item["value"]):
                item["warnings"].append("Valeur contenant des espaces sans guillemets")
        item["secret"] = is_secret(key, item["value"])
        items.append(item)
    return items


def analyze(items: list[dict]) -> list[dict]:
    issues, seen = [], {}
    for it in items:
        if it["kind"] == "invalid":
            issues.append({"n": it["n"], "level": "error", "message": it["error"], "key": it.get("key", "")})
        elif it["kind"] == "entry":
            if it["key"] in seen:
                issues.append({"n": it["n"], "level": "warning", "key": it["key"],
                               "message": f"Clé « {it['key']} » en double (déjà définie ligne {seen[it['key']]})"})
            else:
                seen[it["key"]] = it["n"]
            for w in it.get("warnings", []):
                issues.append({"n": it["n"], "level": "info", "key": it["key"], "message": w})
    return issues


def format_value(value: str, quote: str = "") -> str:
    if value == "":
        return '""' if quote in ('"', "'") else ""
    if quote == "'" and "'" not in value and "\n" not in value:
        return f"'{value}'"
    if quote == "`" and "`" not in value:
        return f"`{value}`"
    if quote == '"' or re.search(r"[\s#\"'`\\]", value):
        return '"' + value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n").replace("\r", "") + '"'
    return value


def serialize(items: list[dict]) -> str:
    out = []
    for it in items:
        kind = it.get("kind")
        if not it.get("dirty") and isinstance(it.get("raw"), str) and kind != "entry_new":
            out.append(it["raw"])
            continue
        if kind == "blank":
            out.append("")
        elif kind == "comment":
            text = str(it.get("text", "")).replace("\r", "")
            out.extend("# " + t if t else "#" for t in text.split("\n"))
        elif kind in ("entry", "entry_new"):
            key = str(it.get("key", "")).strip()
            if not KEY_RE.match(key):
                raise EnvError("BAD_KEY", f"Nom de clé invalide : « {key or '(vide)'} »")
            line = ("export " if it.get("export") else "") + key + "=" + format_value(str(it.get("value", "")), str(it.get("quote") or ""))
            comment = str(it.get("comment") or "").replace("\n", " ").strip()
            if comment:
                line += " # " + comment
            out.append(line)
        elif kind == "invalid":
            out.append(str(it.get("raw", "")).replace("\n", " "))
    return "\n".join(out)


# ── Files & paths ─────────────────────────────────────────────────────────────

async def _roots() -> list[dict]:
    return await (await _db()).fetch_all("SELECT * FROM folders ORDER BY name COLLATE NOCASE, id")


def _within(path: str, root: str) -> bool:
    try:
        return os.path.commonpath([os.path.normcase(path), os.path.normcase(root)]) == os.path.normcase(root)
    except ValueError:
        return False


async def _check_file(path, must_exist: bool = True) -> tuple[str, dict]:
    if not isinstance(path, str) or not path or "\x00" in path:
        raise EnvError("BAD_PATH", "Chemin invalide")
    if not os.path.isabs(path):
        raise EnvError("BAD_PATH", "Chemin absolu attendu")
    name = os.path.basename(path)
    if not name.startswith(".env") or name in (".env.", ".env..") or "/" in name or "\\" in name:
        raise EnvError("FORBIDDEN", "Seuls les fichiers .env* peuvent être lus ou modifiés")
    if os.path.lexists(path) and os.path.islink(path):
        raise EnvError("FORBIDDEN", "Les liens symboliques ne sont pas pris en charge")
    real = os.path.realpath(path)
    if os.path.basename(real) != name:
        raise EnvError("FORBIDDEN", "Chemin refusé")
    for root in await _roots():
        rr = os.path.realpath(root["path"])
        if _within(real, rr) and real != rr:
            if must_exist and not os.path.isfile(real):
                raise EnvError("NOT_FOUND", "Fichier introuvable")
            return real, root
    raise EnvError("FORBIDDEN", "Ce fichier n'est pas dans un dossier de projet ajouté")


def _read_text(path: str) -> dict:
    st = os.stat(path)
    if st.st_size > MAX_FILE:
        raise EnvError("TOO_LARGE", "Fichier trop volumineux (2 Mo maximum)")
    with open(path, "rb") as f:
        data = f.read()
    bom = data.startswith(b"\xef\xbb\xbf")
    try:
        text = data[3 if bom else 0:].decode("utf-8")
    except UnicodeDecodeError:
        raise EnvError("BAD_ENCODING", "Fichier non encodé en UTF-8")
    crlf = "\r\n" in text
    text = text.replace("\r\n", "\n")
    final_nl = text.endswith("\n")
    if final_nl:
        text = text[:-1]
    return {"text": text, "bom": bom, "crlf": crlf, "final_newline": final_nl or not text,
            "hash": hashlib.sha256(data).hexdigest(), "mtime": st.st_mtime, "size": st.st_size}


def _backup_dir(path: str) -> Path:
    d = module.data_path("backups") / hashlib.sha256(os.path.normcase(path).encode()).hexdigest()[:20]
    d.mkdir(parents=True, exist_ok=True)
    meta = d / "source.json"
    if not meta.exists():
        meta.write_text(json.dumps({"path": path}), encoding="utf-8")
    return d


def _backup(path: str) -> Optional[str]:
    if not os.path.isfile(path):
        return None
    d = _backup_dir(path)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    target = d / f"{stamp}.bak"
    with open(path, "rb") as src, open(target, "wb") as dst:
        dst.write(src.read())
    olds = sorted(d.glob("*.bak"))
    for old in olds[:-KEEP_BACKUPS]:
        old.unlink(missing_ok=True)
    return target.name


def _write(path: str, text: str, fmt: dict):
    nl = "\r\n" if fmt.get("crlf") else "\n"
    body = text.replace("\r\n", "\n")
    if fmt.get("final_newline", True) and body and not body.endswith("\n"):
        body += "\n"
    data = (b"\xef\xbb\xbf" if fmt.get("bom") else b"") + body.replace("\n", nl).encode("utf-8")
    if len(data) > MAX_FILE:
        raise EnvError("TOO_LARGE", "Contenu trop volumineux (2 Mo maximum)")
    tmp = os.path.join(os.path.dirname(path), f".ledroide-tmp-{secrets.token_hex(6)}")
    try:
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def _scan(root: str) -> tuple[list[dict], bool]:
    out, truncated = [], False
    root_depth = root.rstrip("\\/").count(os.sep)
    for dirpath, dirnames, filenames in os.walk(root):
        depth = dirpath.rstrip("\\/").count(os.sep) - root_depth
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS and not os.path.islink(os.path.join(dirpath, d))) \
            if depth < MAX_DEPTH else []
        for fn in sorted(filenames):
            if not fn.startswith(".env") or fn in (".env.", ".env.."):
                continue
            full = os.path.join(dirpath, fn)
            if os.path.islink(full):
                continue
            try:
                st = os.stat(full)
            except OSError:
                continue
            out.append({"path": full, "rel": os.path.relpath(full, root).replace("\\", "/"), "name": fn,
                        "size": st.st_size, "mtime": st.st_mtime})
            if len(out) >= MAX_FILES:
                return out, True
    return out, truncated


def _file_view(path: str, root: dict) -> dict:
    r = _read_text(path)
    items = parse(r["text"])
    return {"path": path, "rel": os.path.relpath(path, os.path.realpath(root["path"])).replace("\\", "/"),
            "folder_id": root["id"], "folder": root["name"], "items": items, "issues": analyze(items),
            "hash": r["hash"], "mtime": r["mtime"], "size": r["size"], "crlf": r["crlf"], "bom": r["bom"],
            "entries": sum(1 for i in items if i["kind"] == "entry")}


# ── Folder handlers ───────────────────────────────────────────────────────────

@module.on("folders.list")
async def folders_list(payload: dict):
    out = []
    for f in await _roots():
        exists = os.path.isdir(f["path"])
        files, truncated = await asyncio.to_thread(_scan, os.path.realpath(f["path"])) if exists else ([], False)
        out.append({**f, "exists": exists, "files": files, "truncated": truncated})
    return {"folders": out}


@module.on("folder.add")
async def folder_add(payload: dict):
    path = str(payload.get("path") or "").strip().strip('"')
    if not path or not os.path.isabs(path):
        return _err("Indiquez un chemin de dossier absolu", "BAD_PATH")
    real = os.path.realpath(path)
    if not os.path.isdir(real):
        return _err("Dossier introuvable", "NOT_FOUND")
    if os.path.dirname(real.rstrip("\\/")) == real.rstrip("\\/") or real.rstrip("\\/") == os.path.splitdrive(real)[0]:
        return _err("Choisissez un dossier de projet, pas la racine d'un disque", "BAD_PATH")
    db = await _db()
    for f in await _roots():
        if os.path.normcase(os.path.realpath(f["path"])) == os.path.normcase(real):
            return _err("Ce dossier est déjà dans la liste", "CONFLICT")
    name = os.path.basename(real.rstrip("\\/")) or real
    row = await db.fetch_one("INSERT INTO folders (path, name, added_at) VALUES (?,?,?) RETURNING id", (real, name, _now()))
    log.info("folder added (%d)", row["id"])
    return {"id": row["id"], "path": real, "name": name}


@module.on("folder.remove")
async def folder_remove(payload: dict):
    n = await (await _db()).execute("DELETE FROM folders WHERE id=?", (int(payload.get("id") or 0),))
    return {"ok": True} if n else _err("Dossier introuvable", "NOT_FOUND")


@module.on("fs.browse")
async def fs_browse(payload: dict):
    path = str(payload.get("path") or "").strip()

    def run():
        if not path:
            if os.name == "nt":
                drives = [f"{d}:\\" for d in string.ascii_uppercase if os.path.exists(f"{d}:\\")]
            else:
                drives = ["/"]
            return {"path": "", "parent": None, "dirs": [{"name": d, "path": d} for d in drives], "home": str(Path.home())}
        if not os.path.isabs(path) or not os.path.isdir(path):
            raise EnvError("NOT_FOUND", "Dossier introuvable")
        real = os.path.realpath(path)
        dirs = []
        try:
            with os.scandir(real) as it:
                for e in it:
                    try:
                        if e.is_dir(follow_symlinks=False) and not e.name.startswith("$"):
                            dirs.append({"name": e.name, "path": os.path.join(real, e.name)})
                    except OSError:
                        continue
        except PermissionError:
            raise EnvError("FORBIDDEN", "Accès refusé à ce dossier")
        dirs.sort(key=lambda d: d["name"].lower())
        parent = os.path.dirname(real.rstrip("\\/"))
        has_env = any(n.startswith(".env") for n in os.listdir(real)) if os.access(real, os.R_OK) else False
        return {"path": real, "parent": parent if parent and parent != real else "", "dirs": dirs[:2000],
                "has_env": has_env, "home": str(Path.home())}
    return await _guard(asyncio.to_thread(run))


# ── File handlers ─────────────────────────────────────────────────────────────

@module.on("file.read")
async def file_read(payload: dict):
    async def run():
        real, root = await _check_file(payload.get("path"))
        return await asyncio.to_thread(_file_view, real, root)
    return await _guard(run())


@module.on("parse")
async def parse_handler(payload: dict):
    text = str(payload.get("text") or "").replace("\r\n", "\n")
    if len(text) > MAX_FILE:
        return _err("Contenu trop volumineux", "TOO_LARGE")
    items = parse(text)
    return {"items": items, "issues": analyze(items)}


@module.on("serialize")
async def serialize_handler(payload: dict):
    async def run():
        items = payload.get("items")
        if not isinstance(items, list):
            raise EnvError("BAD_REQUEST", "Contenu invalide")
        text = serialize(items)
        parsed = parse(text)
        return {"text": text, "items": parsed, "issues": analyze(parsed)}
    return await _guard(run())


@module.on("file.save")
async def file_save(payload: dict):
    async def run():
        async with _io_lock:
            real, root = await _check_file(payload.get("path"))
            cur = await asyncio.to_thread(_read_text, real)
            if not payload.get("force") and payload.get("hash") and payload["hash"] != cur["hash"]:
                raise EnvError("CONFLICT", "Le fichier a été modifié sur le disque depuis son ouverture")
            if isinstance(payload.get("items"), list):
                text = serialize(payload["items"])
            elif isinstance(payload.get("text"), str):
                text = payload["text"]
            else:
                raise EnvError("BAD_REQUEST", "Contenu manquant")
            if text == cur["text"]:
                view = await asyncio.to_thread(_file_view, real, root)
                return {**view, "unchanged": True}
            backup = await asyncio.to_thread(_backup, real)
            await asyncio.to_thread(_write, real, text, cur)
            log.info("saved %s (backup %s)", os.path.basename(real), backup)
            view = await asyncio.to_thread(_file_view, real, root)
            return {**view, "backup": backup}
    return await _guard(run())


@module.on("file.create")
async def file_create(payload: dict):
    async def run():
        async with _io_lock:
            db = await _db()
            folder = await db.fetch_one("SELECT * FROM folders WHERE id=?", (int(payload.get("folder_id") or 0),))
            if not folder:
                raise EnvError("NOT_FOUND", "Dossier introuvable")
            name = str(payload.get("name") or "").strip()
            sub = str(payload.get("subdir") or "").strip().strip("/\\")
            if not re.fullmatch(r"\.env[A-Za-z0-9._\-]*", name) or name in (".env.", ".env.."):
                raise EnvError("BAD_NAME", "Le nom doit commencer par « .env » (ex. .env.local)")
            base = os.path.realpath(folder["path"])
            target_dir = os.path.realpath(os.path.join(base, sub)) if sub else base
            if not _within(target_dir, base) or not os.path.isdir(target_dir):
                raise EnvError("BAD_PATH", "Sous-dossier invalide")
            path = os.path.join(target_dir, name)
            if os.path.lexists(path):
                raise EnvError("CONFLICT", "Ce fichier existe déjà")
            text = ""
            src = payload.get("copy_from")
            if src:
                src_real, _ = await _check_file(src)
                cur = await asyncio.to_thread(_read_text, src_real)
                text = cur["text"]
                if payload.get("blank_values"):
                    items = parse(text)
                    for it in items:
                        if it["kind"] == "entry" and it.get("secret"):
                            it["value"], it["dirty"] = "", True
                    text = serialize(items)
            await _check_file(path, must_exist=False)
            await asyncio.to_thread(_write, path, text, {"final_newline": True})
            log.info("created %s", name)
            return await asyncio.to_thread(_file_view, path, folder)
    return await _guard(run())


@module.on("compare")
async def compare(payload: dict):
    async def run():
        paths = payload.get("paths") or []
        if not isinstance(paths, list) or not 2 <= len(paths) <= 8:
            raise EnvError("BAD_REQUEST", "Choisissez entre 2 et 8 fichiers à comparer")
        files = []
        for p in paths:
            real, root = await _check_file(p)
            files.append(await asyncio.to_thread(_file_view, real, root))
        ref = max(0, min(len(files) - 1, int(payload.get("reference") or 0)))
        order: list[str] = []
        maps = []
        for f in files:
            mp = {}
            for it in f["items"]:
                if it["kind"] == "entry":
                    mp[it["key"]] = it["value"]
                    if it["key"] not in order:
                        order.append(it["key"])
            maps.append(mp)
        rows = []
        for key in order:
            vals = [m.get(key) for m in maps]
            present = [v is not None for v in vals]
            if not all(present):
                status = "missing"
            elif len({v for v in vals}) > 1:
                status = "different"
            elif all(v == "" for v in vals):
                status = "empty"
            else:
                status = "same"
            rows.append({"key": key, "values": vals, "status": status,
                         "secret": any(is_secret(key, v or "") for v in vals)})
        summary = []
        for i, mp in enumerate(maps):
            summary.append({"missing": [k for k in maps[ref] if k not in mp] if i != ref else [],
                            "extra": [k for k in mp if k not in maps[ref]] if i != ref else [],
                            "different": [k for k in mp if k in maps[ref] and mp[k] != maps[ref][k]] if i != ref else [],
                            "count": len(mp)})
        return {"files": [{"path": f["path"], "rel": f["rel"], "folder": f["folder"], "hash": f["hash"],
                           "issues": len([x for x in f["issues"] if x["level"] != "info"])} for f in files],
                "reference": ref, "rows": rows, "summary": summary}
    return await _guard(run())


@module.on("keys.add")
async def keys_add(payload: dict):
    async def run():
        async with _io_lock:
            real, root = await _check_file(payload.get("path"))
            cur = await asyncio.to_thread(_read_text, real)
            if payload.get("hash") and payload["hash"] != cur["hash"]:
                raise EnvError("CONFLICT", "Le fichier a été modifié sur le disque depuis la comparaison")
            existing = {i["key"] for i in parse(cur["text"]) if i["kind"] == "entry"}
            lines = []
            added = 0
            for k in payload.get("keys") or []:
                key = str((k or {}).get("key") or "").strip()
                if not KEY_RE.match(key):
                    raise EnvError("BAD_KEY", f"Nom de clé invalide : « {key} »")
                if key in existing:
                    continue
                existing.add(key)
                lines.append(f"{key}={format_value(str(k.get('value') or ''))}")
                added += 1
            if not added:
                return {"added": 0}
            header = str(payload.get("comment") or "").replace("\n", " ").strip()
            text = cur["text"]
            block = ([f"# {header}"] if header else []) + lines
            text = (text + ("\n\n" if text.strip() else "") if text else "") + "\n".join(block)
            backup = await asyncio.to_thread(_backup, real)
            await asyncio.to_thread(_write, real, text, cur)
            log.info("added %d key(s) to %s", added, os.path.basename(real))
            return {"added": added, "backup": backup}
    return await _guard(run())


@module.on("backups.list")
async def backups_list(payload: dict):
    async def run():
        real, _ = await _check_file(payload.get("path"), must_exist=False)
        d = _backup_dir(real)
        out = []
        for b in sorted(d.glob("*.bak"), reverse=True):
            st = b.stat()
            out.append({"id": b.name, "size": st.st_size, "date": datetime.fromtimestamp(st.st_mtime).isoformat(timespec="seconds")})
        return {"backups": out}
    return await _guard(run())


def _backup_file(real: str, backup_id: str) -> Path:
    if not re.fullmatch(r"[0-9\-]+\.bak", backup_id or ""):
        raise EnvError("BAD_REQUEST", "Copie de sécurité invalide")
    f = _backup_dir(real) / backup_id
    if not f.is_file():
        raise EnvError("NOT_FOUND", "Copie de sécurité introuvable")
    return f


@module.on("backup.read")
async def backup_read(payload: dict):
    async def run():
        real, _ = await _check_file(payload.get("path"), must_exist=False)
        f = _backup_file(real, str(payload.get("id") or ""))
        r = await asyncio.to_thread(_read_text, str(f))
        items = parse(r["text"])
        return {"id": f.name, "items": items, "issues": analyze(items)}
    return await _guard(run())


@module.on("backup.restore")
async def backup_restore(payload: dict):
    async def run():
        async with _io_lock:
            real, root = await _check_file(payload.get("path"), must_exist=False)
            f = _backup_file(real, str(payload.get("id") or ""))
            data = await asyncio.to_thread(f.read_bytes)
            backup = await asyncio.to_thread(_backup, real)

            def write():
                tmp = os.path.join(os.path.dirname(real), f".ledroide-tmp-{secrets.token_hex(6)}")
                try:
                    with open(tmp, "wb") as fh:
                        fh.write(data)
                    os.replace(tmp, real)
                finally:
                    if os.path.exists(tmp):
                        os.unlink(tmp)
            await asyncio.to_thread(write)
            log.info("restored %s from backup", os.path.basename(real))
            return {**await asyncio.to_thread(_file_view, real, root), "backup": backup}
    return await _guard(run())
