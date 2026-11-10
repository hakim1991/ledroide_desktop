import asyncio
import fnmatch
import hashlib
import logging
import os
import shutil
import threading
import time
import uuid
from datetime import datetime, timezone

from ledroide_sdk import Module

log = logging.getLogger("ledroide.folder-sync")

module = Module(__file__)
router = module.router

MODES = ("copy", "mirror", "bidirectional")
DEFAULT_EXCLUDES = ["node_modules", ".git", "__pycache__", "*.tmp", "~$*", "Thumbs.db", "desktop.ini"]
LIST_LIMIT = 5000
PLAN_TTL_S = 3600
_plans: dict[str, dict] = {}


class SyncError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class Cancelled(Exception):
    pass


class Ctl:
    """Shared state between a worker thread and the job coroutine."""

    def __init__(self):
        self.cancel = threading.Event()
        self.pct = 0.0
        self.msg = ""

    def progress(self, pct: float, msg: str):
        self.pct, self.msg = pct, msg

    def check(self):
        if self.cancel.is_set():
            raise Cancelled()


async def _run_threaded(job, fn, *args):
    """Runs fn(ctl, *args) in a thread, relays progress, and on job cancellation
    asks the thread to stop and waits for it (never leaves a copy half done)."""
    ctl = Ctl()
    fut = asyncio.ensure_future(asyncio.to_thread(fn, ctl, *args))
    last = None
    try:
        while not fut.done():
            await asyncio.wait({fut}, timeout=0.25)
            cur = (round(ctl.pct, 1), ctl.msg)
            if cur != last:
                last = cur
                await job.progress(cur[0], cur[1])
        return fut.result()
    except asyncio.CancelledError:
        ctl.cancel.set()
        await asyncio.wait({fut})
        if fut.exception() is None or isinstance(fut.exception(), Cancelled):
            return fut.result() if fut.exception() is None else None
        raise


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _norm(p: str) -> str:
    return os.path.normcase(os.path.realpath(p))


def _validate_dirs(source: str, target: str, create_target: bool = False) -> tuple[str, str]:
    source, target = (source or "").strip().strip('"'), (target or "").strip().strip('"')
    if not source or not target:
        raise SyncError("PATH_REQUIRED", "Indiquez un dossier source et un dossier cible")
    if not os.path.isabs(source) or not os.path.isabs(target):
        raise SyncError("BAD_PATH", "Les chemins doivent être absolus (ex. C:\\Documents)")
    if not os.path.isdir(source):
        raise SyncError("SOURCE_NOT_FOUND", f"Dossier source introuvable : {source}")
    if os.path.exists(target) and not os.path.isdir(target):
        raise SyncError("TARGET_NOT_DIR", f"La cible n'est pas un dossier : {target}")
    if not os.path.exists(target) and not create_target:
        raise SyncError("TARGET_NOT_FOUND", f"Dossier cible introuvable : {target}")
    ns, nt = _norm(source), _norm(target)
    if ns == nt:
        raise SyncError("SAME_DIR", "La source et la cible sont le même dossier")
    if nt.startswith(ns.rstrip(os.sep) + os.sep):
        raise SyncError("TARGET_INSIDE_SOURCE", "La cible ne peut pas être à l'intérieur de la source")
    if ns.startswith(nt.rstrip(os.sep) + os.sep):
        raise SyncError("SOURCE_INSIDE_TARGET", "La source ne peut pas être à l'intérieur de la cible")
    return os.path.abspath(source), os.path.abspath(target)


def _clean_excludes(raw) -> list[str]:
    if isinstance(raw, str):
        raw = raw.replace(";", "\n").splitlines()
    out = []
    for p in raw or []:
        p = str(p).strip().replace("\\", "/").strip("/")
        if p and p not in out:
            out.append(p[:200])
    return out[:100]


def _excluded(rel: str, name: str, patterns: list[str]) -> bool:
    low_rel, low_name = rel.lower(), name.lower()
    for p in patterns:
        lp = p.lower()
        if "/" in lp:
            if fnmatch.fnmatchcase(low_rel, lp) or fnmatch.fnmatchcase(low_rel, lp + "/*"):
                return True
        elif fnmatch.fnmatchcase(low_name, lp):
            return True
    return False


def _scan(ctl: Ctl, root: str, patterns: list[str], label: str) -> tuple[dict, dict, int]:
    files: dict[str, dict] = {}
    dirs: dict[str, str] = {}
    errors = 0
    stack = [""]
    while stack:
        ctl.check()
        rel_dir = stack.pop()
        try:
            with os.scandir(os.path.join(root, rel_dir)) as it:
                entries = list(it)
        except OSError:
            errors += 1
            continue
        for e in entries:
            rel = f"{rel_dir}/{e.name}" if rel_dir else e.name
            if _excluded(rel, e.name, patterns):
                continue
            try:
                if e.is_symlink():
                    continue
                if e.is_dir():
                    dirs[rel.lower()] = rel
                    stack.append(rel)
                elif e.is_file():
                    st = e.stat()
                    files[rel.lower()] = {"rel": rel, "size": st.st_size, "mtime": st.st_mtime}
            except OSError:
                errors += 1
        if len(files) % 500 == 0:
            ctl.progress(ctl.pct, f"Analyse de la {label} : {len(files)} fichiers")
    return files, dirs, errors


def _sha256(ctl: Ctl, path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(1024 * 1024):
            ctl.check()
            h.update(chunk)
    return h.hexdigest()


def _compare(ctl: Ctl, source: str, target: str, patterns: list[str], use_hash: bool, mode: str, tol: float) -> dict:
    ctl.progress(2, "Analyse de la source…")
    src, src_dirs, err1 = _scan(ctl, source, patterns, "source")
    ctl.progress(20, "Analyse de la cible…")
    tgt, tgt_dirs, err2 = (_scan(ctl, target, patterns, "cible") if os.path.isdir(target) else ({}, {}, 0))

    new, modified, identical, target_only = [], [], [], []
    common = [k for k in src if k in tgt]
    total = max(1, len(common))
    for i, key in enumerate(common):
        ctl.check()
        s, t = src[key], tgt[key]
        same_size = s["size"] == t["size"]
        same_time = abs(s["mtime"] - t["mtime"]) <= tol
        if use_hash and same_size:
            if i % 20 == 0:
                ctl.progress(40 + 55 * i / total, f"Empreintes SHA-256 : {i}/{len(common)}")
            try:
                same = _sha256(ctl, os.path.join(source, s["rel"])) == _sha256(ctl, os.path.join(target, t["rel"]))
            except OSError:
                same = False
        else:
            same = same_size and same_time
        entry = {"rel": s["rel"], "size": s["size"], "src_mtime": s["mtime"],
                 "tgt_size": t["size"], "tgt_mtime": t["mtime"]}
        (identical if same else modified).append(entry)
    for key, s in src.items():
        if key not in tgt:
            new.append({"rel": s["rel"], "size": s["size"], "src_mtime": s["mtime"]})
    for key, t in tgt.items():
        if key not in src:
            target_only.append({"rel": t["rel"], "size": t["size"], "tgt_mtime": t["mtime"]})

    ops, conflicts = [], []
    for e in new:
        ops.append({"op": "copy", "rel": e["rel"], "size": e["size"]})
    for e in modified:
        if mode == "bidirectional":
            if abs(e["src_mtime"] - e["tgt_mtime"]) <= tol:
                conflicts.append({**e, "reason": "Contenus différents mais dates identiques"})
            elif e["src_mtime"] > e["tgt_mtime"]:
                ops.append({"op": "update", "rel": e["rel"], "size": e["size"]})
            else:
                ops.append({"op": "update_back", "rel": e["rel"], "size": e["tgt_size"]})
        else:
            if e["tgt_mtime"] > e["src_mtime"] + tol:
                e["warning"] = "La version de la cible est plus récente"
            ops.append({"op": "update", "rel": e["rel"], "size": e["size"], "warning": e.get("warning")})
    for e in target_only:
        if mode == "mirror":
            ops.append({"op": "delete", "rel": e["rel"], "size": e["size"]})
        elif mode == "bidirectional":
            ops.append({"op": "copy_back", "rel": e["rel"], "size": e["size"]})

    extra_dirs = []
    if mode == "mirror":
        extra_dirs = sorted((v for k, v in tgt_dirs.items() if k not in src_dirs), key=len, reverse=True)
    for lst in (new, modified, target_only, ops):
        lst.sort(key=lambda x: x["rel"].lower())
    ctl.progress(100, "Comparaison terminée")
    return {
        "counts": {"new": len(new), "modified": len(modified), "identical": len(identical),
                   "target_only": len(target_only), "conflicts": len(conflicts), "ops": len(ops),
                   "scan_errors": err1 + err2},
        "bytes": sum(o["size"] for o in ops if o["op"] != "delete"),
        "new": new, "modified": modified, "identical": identical, "target_only": target_only,
        "conflicts": conflicts, "ops": ops, "extra_dirs": extra_dirs,
    }


def _copy(src_path: str, dst_path: str):
    os.makedirs(os.path.dirname(dst_path), exist_ok=True)
    part = dst_path + ".fsync-part"
    try:
        shutil.copy2(src_path, part)
        os.replace(part, dst_path)
    finally:
        if os.path.exists(part):
            os.remove(part)


def _execute(ctl: Ctl, plan: dict, skip: set[str], journal: list[dict]) -> dict:
    source, target = plan["source"], plan["target"]
    ops = [o for o in plan["ops"] if o["rel"] not in skip]
    need_trash = any(o["op"] == "delete" for o in ops)
    send2trash = None
    if need_trash:
        from send2trash import send2trash
    total_bytes = max(1, sum(o["size"] for o in ops if o["op"] != "delete"))
    done_bytes, done, failed = 0, 0, 0
    labels = {"copy": "Copie", "update": "Mise à jour", "delete": "Corbeille",
              "copy_back": "Copie vers la source", "update_back": "Mise à jour de la source"}

    def log_line(level: str, op: str, rel: str, msg: str = ""):
        journal.append({"t": time.time(), "level": level, "op": op, "rel": rel, "msg": msg})

    try:
        for i, o in enumerate(ops):
            ctl.check()
            ctl.progress(100 * (done_bytes / total_bytes) if total_bytes > 1 else 100 * i / max(1, len(ops)),
                         f"{labels[o['op']]} : {o['rel']}")
            rel_os = o["rel"].replace("/", os.sep)
            s_path, t_path = os.path.join(source, rel_os), os.path.join(target, rel_os)
            try:
                if o["op"] in ("copy", "update"):
                    _copy(s_path, t_path)
                elif o["op"] in ("copy_back", "update_back"):
                    _copy(t_path, s_path)
                elif o["op"] == "delete":
                    if os.path.isfile(t_path):
                        send2trash(t_path)
                done += 1
                log_line("ok", o["op"], o["rel"])
            except OSError as exc:
                failed += 1
                log_line("error", o["op"], o["rel"], exc.strerror or str(exc))
            if o["op"] != "delete":
                done_bytes += o["size"]
        if plan["mode"] == "mirror":
            for d in plan.get("extra_dirs", []):
                path = os.path.join(target, d.replace("/", os.sep))
                try:
                    if os.path.isdir(path) and not os.listdir(path):
                        os.rmdir(path)
                except OSError:
                    pass
        cancelled = False
    except Cancelled:
        cancelled = True
        log_line("warn", "cancel", "", "Synchronisation annulée par l'utilisateur")
    ctl.progress(100, "Terminé")
    return {"done": done, "failed": failed, "skipped": len(plan["ops"]) - len(ops),
            "remaining": len(ops) - done - failed, "cancelled": cancelled, "bytes": done_bytes}


def _purge_plans():
    now = time.time()
    for pid, p in list(_plans.items()):
        if now - p["created"] > PLAN_TTL_S:
            _plans.pop(pid, None)


def _settings(payload: dict) -> dict:
    mode = payload.get("mode") or "copy"
    if mode not in MODES:
        raise SyncError("BAD_MODE", f"Mode inconnu : {mode}")
    excludes = _clean_excludes(payload.get("excludes", DEFAULT_EXCLUDES))
    return {"mode": mode, "excludes": excludes, "use_hash": bool(payload.get("use_hash"))}


@module.on("defaults")
async def defaults(payload: dict):
    try:
        import send2trash  # noqa: F401
        trash = True
    except ImportError:
        trash = False
    return {"excludes": DEFAULT_EXCLUDES, "send2trash": trash}


@module.job("compare")
async def compare(payload: dict, job):
    try:
        opts = _settings(payload)
        source, target = _validate_dirs(payload.get("source"), payload.get("target"),
                                        create_target=opts["mode"] != "bidirectional")
    except SyncError as exc:
        raise ValueError(exc.message)
    tol = float(module.config.get("mtime_tolerance_s", 2))
    result = await _run_threaded(job, _compare, source, target, opts["excludes"], opts["use_hash"], opts["mode"], tol)
    if result is None:
        raise asyncio.CancelledError()
    _purge_plans()
    plan_id = uuid.uuid4().hex
    _plans[plan_id] = {"source": source, "target": target, "mode": opts["mode"], "ops": result["ops"],
                       "extra_dirs": result["extra_dirs"], "created": time.time()}
    log.info("compare %s: %s", opts["mode"], result["counts"])
    trash_ok = True
    if result["counts"]["ops"] and any(o["op"] == "delete" for o in result["ops"]):
        try:
            import send2trash  # noqa: F401
        except ImportError:
            trash_ok = False
    out = {"plan_id": plan_id, "source": source, "target": target, **opts, "send2trash": trash_ok,
           "counts": result["counts"], "bytes": result["bytes"], "truncated": False}
    for key in ("new", "modified", "target_only", "conflicts", "ops"):
        out[key] = result[key][:LIST_LIMIT]
        out["truncated"] |= len(result[key]) > LIST_LIMIT
    out["identical"] = result["identical"][:500]
    return out


@module.job("execute")
async def execute(payload: dict, job):
    plan = _plans.get(str(payload.get("plan_id") or ""))
    if plan is None:
        raise ValueError("Aperçu introuvable ou expiré : relancez la comparaison avant d'exécuter")
    try:
        _validate_dirs(plan["source"], plan["target"], create_target=True)
    except SyncError as exc:
        raise ValueError(exc.message)
    if any(o["op"] == "delete" for o in plan["ops"]):
        try:
            import send2trash  # noqa: F401
        except ImportError:
            raise ValueError("Le mode miroir nécessite la bibliothèque send2trash (suppression vers la corbeille). "
                             "Installez les dépendances du module ; aucune suppression définitive n'est effectuée.")
    os.makedirs(plan["target"], exist_ok=True)
    skip = {str(s) for s in payload.get("skip") or []}
    journal: list[dict] = []
    started = time.time()
    stats = await _run_threaded(job, _execute, plan, skip, journal)
    if stats is None:
        stats = {"cancelled": True, "done": sum(1 for j in journal if j["level"] == "ok"),
                 "failed": sum(1 for j in journal if j["level"] == "error")}
    _plans.pop(str(payload.get("plan_id")), None)
    run = {"at": _now(), "source": plan["source"], "target": plan["target"], "mode": plan["mode"],
           "duration_s": round(time.time() - started, 2), **stats, "journal": journal[-2000:],
           "profile": str(payload.get("profile") or "")[:120]}
    saved = await module.db.save("runs", run)
    keep = int(module.config.get("journal_size", 50))
    for old in (await module.db.query("runs"))[keep:]:
        await module.db.delete("runs", old["_id"])
    log.info("sync %s: done=%s failed=%s cancelled=%s", plan["mode"], stats.get("done"), stats.get("failed"),
             stats.get("cancelled"))
    return saved


@module.on("runs_list")
async def runs_list(payload: dict):
    items = await module.db.query("runs")
    return {"items": [{k: v for k, v in r.items() if k != "journal"} | {"entries": len(r.get("journal", []))}
                      for r in items]}


@module.on("run_get")
async def run_get(payload: dict):
    doc = await module.db.get("runs", str(payload.get("id", "")))
    return doc or {"error": "Exécution introuvable", "code": "NOT_FOUND"}


@module.on("runs_clear")
async def runs_clear(payload: dict):
    for r in await module.db.query("runs"):
        await module.db.delete("runs", r["_id"])
    return {"ok": True}


@module.on("profiles_list")
async def profiles_list(payload: dict):
    items = await module.db.query("profiles")
    items.sort(key=lambda p: (p.get("name") or "").lower())
    return {"items": items}


@module.on("profile_save")
async def profile_save(payload: dict):
    name = str(payload.get("name") or "").strip()
    if not name:
        return {"error": "Le nom du profil est obligatoire", "code": "NAME_REQUIRED"}
    try:
        opts = _settings(payload)
    except SyncError as exc:
        return {"error": exc.message, "code": exc.code}
    doc = {"name": name[:80], "source": str(payload.get("source") or "").strip(),
           "target": str(payload.get("target") or "").strip(), **opts, "updated": _now()}
    if payload.get("id"):
        doc["_id"] = str(payload["id"])
    return await module.db.save("profiles", doc)


@module.on("profile_delete")
async def profile_delete(payload: dict):
    return await module.db.delete("profiles", str(payload.get("id", "")))


@module.on("validate")
async def validate(payload: dict):
    try:
        source, target = _validate_dirs(payload.get("source"), payload.get("target"), create_target=True)
    except SyncError as exc:
        return {"error": exc.message, "code": exc.code}
    return {"ok": True, "source": source, "target": target, "target_exists": os.path.isdir(target)}


@module.on_shutdown
async def cleanup():
    _plans.clear()
