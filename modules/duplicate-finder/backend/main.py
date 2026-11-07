import asyncio
import hashlib
import os
import threading
import time
import uuid
from collections import defaultdict
from pathlib import Path
from typing import List

from ledroide_sdk.http import JSONResponse
from ledroide_sdk import Module
from pydantic import BaseModel

module = Module(__file__)
router = module.router

CHUNK = 1024 * 1024
PARTIAL = 64 * 1024
MAX_JOBS = 5

_jobs: dict = {}
_jobs_lock = threading.Lock()


class ScanReq(BaseModel):
    folder: str
    recursive: bool = True
    min_size: int = 1  # octets


class TrashReq(BaseModel):
    job_id: str
    paths: List[str]


def _err(msg, status=400, code=None):
    c = {"error": msg}
    if code:
        c["code"] = code
    return JSONResponse(status_code=status, content=c)


class Cancelled(Exception):
    pass


class Job:
    def __init__(self, folder: Path, recursive: bool, min_size: int):
        self.id = uuid.uuid4().hex[:12]
        self.folder, self.recursive, self.min_size = folder, recursive, min_size
        self.state = "running"      # running | done | error | cancelled
        self.phase = "listing"      # listing | prefilter | hashing | done
        self.files_seen = 0
        self.candidates = 0
        self.bytes_total = 0
        self.bytes_done = 0
        self.errors = 0
        self.error = None
        self.groups = []
        self.started = time.time()
        self.finished = None
        self.cancel = threading.Event()
        self.lock = threading.Lock()

    def check(self):
        if self.cancel.is_set():
            raise Cancelled()

    # ── étapes ──
    def _walk(self):
        by_size = defaultdict(list)
        seen_inodes = set()
        stack = [self.folder]
        while stack:
            d = stack.pop()
            try:
                it = os.scandir(d)
            except OSError:
                self.errors += 1
                continue
            with it:
                for e in it:
                    self.check()
                    try:
                        if e.is_symlink():
                            continue
                        if e.is_dir(follow_symlinks=False):
                            if self.recursive and not e.name.startswith("$"):
                                stack.append(e.path)
                            continue
                        if not e.is_file(follow_symlinks=False):
                            continue
                        st = e.stat(follow_symlinks=False)
                    except OSError:
                        self.errors += 1
                        continue
                    self.files_seen += 1
                    if st.st_size < self.min_size:
                        continue
                    ino = (st.st_dev, st.st_ino)
                    if st.st_ino and ino in seen_inodes:
                        continue  # lien physique vers un fichier déjà vu
                    seen_inodes.add(ino)
                    by_size[st.st_size].append((e.path, st.st_mtime))
        return {s: f for s, f in by_size.items() if len(f) > 1}

    def _hash(self, path, limit=None):
        h = hashlib.sha256()
        remaining = limit
        with open(path, "rb") as f:
            while True:
                self.check()
                n = CHUNK if remaining is None else min(CHUNK, remaining)
                if n <= 0:
                    break
                b = f.read(n)
                if not b:
                    break
                h.update(b)
                if remaining is not None:
                    remaining -= len(b)
                else:
                    self.bytes_done += len(b)
        return h.hexdigest()

    def _group(self, files, key_fn):
        out = defaultdict(list)
        for f in files:
            try:
                out[key_fn(f[0])].append(f)
            except OSError:
                self.errors += 1
        return [g for g in out.values() if len(g) > 1]

    def run(self):
        try:
            by_size = self._walk()
            self.candidates = sum(len(v) for v in by_size.values())
            # pré-filtre : hash des 64 premiers Kio pour les gros fichiers
            self.phase = "prefilter"
            to_hash = []  # (size, files)
            for size, files in by_size.items():
                if size > PARTIAL:
                    for g in self._group(files, lambda p: self._hash(p, PARTIAL)):
                        to_hash.append((size, g))
                else:
                    to_hash.append((size, files))
            self.bytes_total = sum(size * len(fs) for size, fs in to_hash)
            self.phase = "hashing"
            groups = []
            for size, files in to_hash:
                res = defaultdict(list)
                for path, mtime in files:
                    try:
                        res[self._hash(path)].append({"path": path, "mtime": mtime})
                    except OSError:
                        self.errors += 1
                        self.bytes_done += size
                for digest, fl in res.items():
                    if len(fl) > 1:
                        fl.sort(key=lambda x: x["path"].lower())
                        groups.append({"id": digest[:16], "hash": digest, "size": size, "files": fl})
            groups.sort(key=lambda g: g["size"] * (len(g["files"]) - 1), reverse=True)
            with self.lock:
                self.groups = groups
            self.phase = "done"
            self.state = "done"
        except Cancelled:
            self.state = "cancelled"
        except Exception as e:  # pragma: no cover
            self.state, self.error = "error", str(e)
        finally:
            self.finished = time.time()

    def to_dict(self, with_groups=True):
        d = {
            "job_id": self.id, "state": self.state, "phase": self.phase, "folder": str(self.folder),
            "files_seen": self.files_seen, "candidates": self.candidates,
            "bytes_total": self.bytes_total, "bytes_done": self.bytes_done, "errors": self.errors,
            "elapsed": round((self.finished or time.time()) - self.started, 1),
        }
        if self.error:
            d["error"] = self.error
        if self.state == "done" and with_groups:
            with self.lock:
                groups = [g for g in self.groups if len(g["files"]) > 1]
            d["groups"] = groups
            d["summary"] = {
                "groups": len(groups),
                "files": sum(len(g["files"]) for g in groups),
                "reclaimable": sum(g["size"] * (len(g["files"]) - 1) for g in groups),
            }
        return d


# ───────────────────────── routes ─────────────────────────

@router.post("/scan")
async def scan(req: ScanReq):
    folder = (req.folder or "").strip().strip('"')
    if not folder:
        return _err("Indiquez un dossier.")
    p = Path(folder)
    if not p.is_absolute():
        return _err("Le chemin doit être absolu (ex. D:\\Photos).")
    if not p.is_dir():
        return _err("Dossier introuvable.")
    if req.min_size < 0:
        return _err("Taille minimale invalide.")
    job = Job(p, req.recursive, req.min_size)
    with _jobs_lock:
        for j in _jobs.values():
            if j.state == "running":
                j.cancel.set()  # une seule analyse à la fois
        _jobs[job.id] = job
        while len(_jobs) > MAX_JOBS:
            _jobs.pop(next(iter(_jobs)))
    threading.Thread(target=job.run, daemon=True, name=f"dupscan-{job.id}").start()
    return {"job_id": job.id}


@router.get("/status/{job_id}")
async def status(job_id: str, groups: bool = True):
    job = _jobs.get(job_id)
    if not job:
        return _err("Analyse introuvable (expirée ?).", 404)
    return job.to_dict(with_groups=groups)


@router.post("/cancel/{job_id}")
async def cancel(job_id: str):
    job = _jobs.get(job_id)
    if not job:
        return _err("Analyse introuvable.", 404)
    job.cancel.set()
    return {"ok": True}


@router.post("/trash")
async def trash(req: TrashReq):
    job = _jobs.get(req.job_id)
    if not job or job.state != "done":
        return _err("Analyse introuvable ou non terminée. Relancez l'analyse.", 404)
    if not req.paths:
        return _err("Aucun fichier sélectionné.")
    try:
        from send2trash import send2trash
    except ImportError:
        return _err("send2trash n'est pas installé. Installez les dépendances du module.", 400, "MISSING_DEP")

    def fn():
        with job.lock:
            index = {}
            for g in job.groups:
                for f in g["files"]:
                    index[os.path.normcase(f["path"])] = g
            wanted = {os.path.normcase(p) for p in req.paths}
            unknown = [p for p in req.paths if os.path.normcase(p) not in index]
            if unknown:
                return {"_status": 400, "error": f"{len(unknown)} fichier(s) ne font pas partie des résultats de l'analyse."}
            for g in job.groups:
                if all(os.path.normcase(f["path"]) in wanted for f in g["files"]):
                    return {"_status": 400, "error": "Sécurité : au moins un exemplaire de chaque groupe doit être conservé."}
            moved, errors, freed = [], [], 0
            for p in req.paths:
                g = index[os.path.normcase(p)]
                entry = next(f for f in g["files"] if os.path.normcase(f["path"]) == os.path.normcase(p))
                try:
                    if not os.path.exists(p):
                        raise FileNotFoundError("fichier introuvable")
                    # vérifie que le fichier n'a pas changé depuis l'analyse
                    if os.path.getsize(p) != g["size"]:
                        raise ValueError("le fichier a été modifié depuis l'analyse")
                    send2trash(p)
                    moved.append(p)
                    freed += g["size"]
                    g["files"].remove(entry)
                except Exception as e:
                    errors.append({"path": p, "error": str(e)})
            return {"moved": len(moved), "moved_paths": moved, "errors": errors, "freed": freed}

    res = await asyncio.to_thread(fn)
    st = res.pop("_status", None)
    if st:
        return JSONResponse(status_code=st, content=res)
    return res
