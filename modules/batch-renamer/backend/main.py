import asyncio
import json
import os
import re
import threading
import time
import uuid
from pathlib import Path
from typing import List, Optional

from ledroide_sdk.http import JSONResponse
from ledroide_sdk import Module, data_path
from pydantic import BaseModel

module = Module(__file__)
router = module.router

LOG_FILE = data_path(__file__, "rename_log.json")
MAX_FILES = 20000
INVALID_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}
PATTERN_TOKEN = re.compile(r"\{(n|name)(?::(\d+))?\}")
_lock = threading.Lock()


class Rules(BaseModel):
    find: str = ""
    replace: str = ""
    regex: bool = False
    case_sensitive: bool = True
    case: str = "none"            # none | lower | upper | title | sentence
    prefix: str = ""
    suffix: str = ""
    numbering: bool = False
    pattern: str = "{name}_{n:03}"
    start: int = 1
    step: int = 1
    keep_ext: bool = True
    ext_case: str = "keep"        # keep | lower | upper
    sort: str = "name"            # name | mtime


class Req(BaseModel):
    folder: str
    extensions: str = ""
    recursive: bool = False
    rules: Rules = Rules()


class UserError(Exception):
    def __init__(self, msg, status=400):
        super().__init__(msg)
        self.status = status


def _natural_key(s: str):
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", s)]


def _check_folder(folder: str) -> Path:
    folder = (folder or "").strip().strip('"')
    if not folder:
        raise UserError("Indiquez un dossier.")
    p = Path(folder)
    if not p.is_absolute():
        raise UserError("Le chemin doit être absolu (ex. C:\\Users\\Moi\\Images).")
    if not p.is_dir():
        raise UserError("Dossier introuvable.")
    return p


def _scan(folder: Path, extensions: str, recursive: bool, sort: str):
    exts = {("." + e.strip().lstrip(".").lower()) for e in re.split(r"[,;\s]+", extensions or "") if e.strip().lstrip(".")}
    files = []

    def take(path: Path):
        if exts and path.suffix.lower() not in exts:
            return
        files.append(path)
        if len(files) > MAX_FILES:
            raise UserError(f"Trop de fichiers (plus de {MAX_FILES}). Affinez le dossier ou le filtre.")

    if recursive:
        for root, dirs, names in os.walk(folder):
            dirs[:] = [d for d in dirs if not d.startswith("$")]
            for n in names:
                take(Path(root) / n)
    else:
        with os.scandir(folder) as it:
            for e in it:
                try:
                    if e.is_file():
                        take(Path(e.path))
                except OSError:
                    pass

    if sort == "mtime":
        def mt(p):
            try:
                return p.stat().st_mtime
            except OSError:
                return 0
        files.sort(key=lambda p: (mt(p), _natural_key(str(p.relative_to(folder)))))
    else:
        files.sort(key=lambda p: _natural_key(str(p.relative_to(folder))))
    return files


def _apply_case(s: str, mode: str) -> str:
    if mode == "lower":
        return s.lower()
    if mode == "upper":
        return s.upper()
    if mode == "title":
        return re.sub(r"[^\W_]+", lambda m: m.group(0)[:1].upper() + m.group(0)[1:].lower(), s)
    if mode == "sentence":
        s = s.lower()
        m = re.search(r"[^\W\d_]", s)
        return s[:m.start()] + s[m.start()].upper() + s[m.start() + 1:] if m else s
    return s


def _compile_find(r: Rules):
    if not r.find:
        return None
    flags = 0 if r.case_sensitive else re.IGNORECASE
    try:
        return re.compile(r.find if r.regex else re.escape(r.find), flags)
    except re.error as e:
        raise UserError(f"Expression régulière invalide : {e}")


def new_name(old: str, r: Rules, finder, n: int) -> str:
    if r.keep_ext:
        stem, ext = os.path.splitext(old)
        if not stem:  # fichier « .gitignore »
            stem, ext = old, ""
    else:
        stem, ext = old, ""
    if finder is not None:
        repl = r.replace if r.regex else r.replace.replace("\\", "\\\\")
        try:
            stem = finder.sub(repl, stem)
        except (re.error, IndexError) as e:
            raise UserError(f"Remplacement invalide : {e}")
    stem = _apply_case(stem, r.case)
    if r.numbering:
        def tok(m):
            if m.group(1) == "name":
                return stem
            width = int(m.group(2)) if m.group(2) else 0
            return str(n).zfill(width) if n >= 0 else "-" + str(-n).zfill(width)
        stem = PATTERN_TOKEN.sub(tok, r.pattern)
    stem = f"{r.prefix}{stem}{r.suffix}"
    if r.ext_case == "lower":
        ext = ext.lower()
    elif r.ext_case == "upper":
        ext = ext.upper()
    return stem + ext


def _invalid_reason(name: str) -> Optional[str]:
    if not name or not name.strip():
        return "nom vide"
    if INVALID_CHARS.search(name):
        return "caractère interdit"
    if name.endswith((".", " ")):
        return "finit par un point ou une espace"
    if name.split(".")[0].upper() in RESERVED:
        return "nom réservé par Windows"
    if len(name) > 255:
        return "nom trop long"
    return None


def build_plan(req: Req):
    folder = _check_folder(req.folder)
    r = req.rules
    if r.numbering and not re.search(r"\{n(?::\d+)?\}", r.pattern):
        raise UserError("Le modèle de numérotation doit contenir {n} (ex. photo_{n:03}).")
    if r.case not in ("none", "lower", "upper", "title", "sentence"):
        raise UserError("Mode de casse invalide.")
    finder = _compile_find(r)
    files = _scan(folder, req.extensions, req.recursive, r.sort)

    items = []
    n = r.start
    for p in files:
        nn = new_name(p.name, r, finder, n)
        n += r.step
        items.append({
            "path": str(p), "dir": str(p.parent),
            "rel_dir": "" if p.parent == folder else str(p.parent.relative_to(folder)),
            "old": p.name, "new": nn,
        })

    # sources qui vont réellement changer de nom (chemins normalisés, insensibles à la casse)
    moving = {os.path.normcase(it["path"]) for it in items if it["new"] != it["old"]}
    targets = {}
    for it in items:
        if it["new"] == it["old"]:
            it["status"] = "same"
            continue
        reason = _invalid_reason(it["new"])
        if reason:
            it["status"], it["reason"] = "invalid", reason
            continue
        key = os.path.normcase(os.path.join(it["dir"], it["new"]))
        targets.setdefault(key, []).append(it)
        it["status"] = "ok"
    for key, group in targets.items():
        if len(group) > 1:
            for it in group:
                it["status"], it["reason"] = "conflict", f"nom en double ({len(group)} fichiers)"
            continue
        it = group[0]
        case_only = it["old"].lower() == it["new"].lower()
        if not case_only and os.path.lexists(key) and key not in moving:
            it["status"], it["reason"] = "conflict", "un fichier ou dossier de ce nom existe déjà"

    counts = {s: sum(1 for i in items if i["status"] == s) for s in ("ok", "same", "conflict", "invalid")}
    return folder, items, counts


def _two_phase(pairs):
    """pairs: [(src, dst)]. Renomme via des noms temporaires pour gérer les permutations.
    Retourne la liste effectuée ; annule tout en cas d'erreur."""
    temps = []
    try:
        for src, dst in pairs:
            tmp = os.path.join(os.path.dirname(src), f".~lrtmp_{uuid.uuid4().hex[:10]}")
            os.rename(src, tmp)
            temps.append((src, tmp, dst))
    except OSError as e:
        for src, tmp, _ in reversed(temps):
            try:
                os.rename(tmp, src)
            except OSError:
                pass
        raise UserError(f"Échec du renommage ({e.filename}) : {e.strerror}. Aucune modification conservée.", 409)
    done = []
    try:
        for src, tmp, dst in temps:
            if os.path.lexists(dst):
                raise OSError(17, "la destination existe déjà", dst)
            os.rename(tmp, dst)
            done.append((src, tmp, dst))
    except OSError as e:
        for src, tmp, dst in reversed(done):
            try:
                os.rename(dst, tmp)
            except OSError:
                pass
        for src, tmp, _ in temps:
            try:
                os.rename(tmp, src)
            except OSError:
                pass
        raise UserError(f"Échec du renommage ({e.filename}) : {e.strerror}. Opération annulée.", 409)
    return [(s, d) for s, _, d in done]


def _read_log():
    try:
        return json.loads(LOG_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _log_summary(log):
    if not log:
        return {"available": False}
    return {"available": True, "count": len(log.get("ops", [])), "folder": log.get("folder"),
            "timestamp": log.get("timestamp"), "sample": log.get("ops", [])[:5]}


def _run(fn):
    async def inner():
        try:
            return await asyncio.to_thread(fn)
        except UserError as e:
            return JSONResponse(status_code=e.status, content={"error": str(e)})
        except PermissionError:
            return JSONResponse(status_code=403, content={"error": "Accès refusé à ce dossier."})
        except OSError as e:
            return JSONResponse(status_code=400, content={"error": f"Erreur système : {e.strerror or e}"})
    return inner()


# ───────────────────────── routes ─────────────────────────

@router.post("/preview")
async def preview(req: Req):
    def fn():
        folder, items, counts = build_plan(req)
        return {"folder": str(folder), "total": len(items), "counts": counts,
                "items": [{k: v for k, v in i.items() if k != "path"} for i in items]}
    return await _run(fn)


@router.post("/apply")
async def apply(req: Req):
    def fn():
        with _lock:
            folder, items, counts = build_plan(req)
            if counts["conflict"] or counts["invalid"]:
                raise UserError("Des conflits ou noms invalides subsistent : corrigez-les avant d'appliquer.", 409)
            pairs = [(i["path"], os.path.join(i["dir"], i["new"])) for i in items if i["status"] == "ok"]
            if not pairs:
                raise UserError("Aucun fichier à renommer.")
            done = _two_phase(pairs)
            log = {"timestamp": time.strftime("%Y-%m-%d %H:%M:%S"), "folder": str(folder),
                   "ops": [{"old": s, "new": d} for s, d in done]}
            try:
                LOG_FILE.write_text(json.dumps(log, ensure_ascii=False, indent=1), encoding="utf-8")
                logged = True
            except OSError:
                logged = False
            return {"renamed": len(done), "undo": _log_summary(log) if logged else {"available": False}}
    return await _run(fn)


@router.get("/undo")
async def undo_info():
    return _log_summary(await asyncio.to_thread(_read_log))


@router.post("/undo")
async def undo():
    def fn():
        with _lock:
            log = _read_log()
            if not log or not log.get("ops"):
                raise UserError("Aucune opération à annuler.", 404)
            ops = log["ops"]
            news = {os.path.normcase(o["new"]) for o in ops}
            pairs, skipped = [], []
            for o in ops:
                if not os.path.lexists(o["new"]):
                    skipped.append({"file": o["new"], "reason": "fichier introuvable"})
                elif os.path.lexists(o["old"]) and os.path.normcase(o["old"]) not in news \
                        and o["old"].lower() != o["new"].lower():
                    skipped.append({"file": o["old"], "reason": "un fichier occupe déjà l'ancien nom"})
                else:
                    pairs.append((o["new"], o["old"]))
            done = _two_phase(pairs) if pairs else []
            try:
                LOG_FILE.unlink()
            except OSError:
                pass
            return {"restored": len(done), "skipped": skipped}
    return await _run(fn)
