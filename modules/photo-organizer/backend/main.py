import asyncio
import io
import json
import logging
import os
import shutil
import time
import uuid
from datetime import datetime, timezone
from typing import Optional

from ledroide_sdk import Module
from ledroide_sdk.http import Query, Response, error

from photo_lib import IMAGE_EXTS, PhotoError, analyze, exif_details, format_pattern, pil, sha256

log = logging.getLogger("ledroide.photo-organizer")

module = Module(__file__)
router = module.router

MAX_SIMILAR = 30000
MAX_BATCH = 20000
PRECISIONS = {"1": 0.01, "10": 0.1, "100": 1.0}
SORTS = {
    "taken_desc": "taken DESC, id DESC", "taken_asc": "taken ASC, id ASC", "name": "name COLLATE NOCASE, id",
    "size_desc": "size DESC, id", "added": "id DESC",
}
PHOTO_COLS = "id, folder_id, path, name, size, mtime, width, height, taken, taken_src, camera, lat, lon"

_ready = False
_lock = asyncio.Lock()
_fs_lock = asyncio.Lock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS folders (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    path       TEXT    NOT NULL UNIQUE,
    added_at   TEXT    NOT NULL,
    last_scan  TEXT
);
CREATE TABLE IF NOT EXISTS photos (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    folder_id  INTEGER NOT NULL REFERENCES folders(id) ON DELETE CASCADE,
    path       TEXT    NOT NULL UNIQUE,
    name       TEXT    NOT NULL,
    size       INTEGER NOT NULL DEFAULT 0,
    mtime      REAL    NOT NULL DEFAULT 0,
    width      INTEGER NOT NULL DEFAULT 0,
    height     INTEGER NOT NULL DEFAULT 0,
    taken      TEXT,
    taken_src  TEXT    NOT NULL DEFAULT 'mtime',
    camera     TEXT    NOT NULL DEFAULT '',
    lat        REAL,
    lon        REAL,
    dhash      TEXT    NOT NULL DEFAULT '',
    phash      TEXT    NOT NULL DEFAULT '',
    sha256     TEXT    NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_photos_taken ON photos(taken);
CREATE INDEX IF NOT EXISTS idx_photos_folder ON photos(folder_id);
CREATE INDEX IF NOT EXISTS idx_photos_size ON photos(size);
CREATE TABLE IF NOT EXISTS ops (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    kind       TEXT    NOT NULL,
    label      TEXT    NOT NULL,
    items      TEXT    NOT NULL,
    created_at TEXT    NOT NULL,
    undone     INTEGER NOT NULL DEFAULT 0
);
"""


async def _db():
    global _ready
    if not _ready:
        async with _lock:
            if not _ready:
                await module.db.executescript(SCHEMA)
                _ready = True
    await module.db.execute("PRAGMA foreign_keys = ON")
    return module.db


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _err(message: str, code: str = "BAD_REQUEST") -> dict:
    return {"error": message, "code": code}


def _int(v, default: int = 0) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _thumbs_dir() -> str:
    path = module.data_path("thumbs")
    path.mkdir(parents=True, exist_ok=True)
    return str(path)


def _thumb(pid: int) -> str:
    return os.path.join(_thumbs_dir(), f"{pid}.jpg")


def _norm(path: str) -> str:
    return os.path.normcase(os.path.normpath(path))


def _inside(path: str, folder: str) -> bool:
    p, f = _norm(path), _norm(folder)
    return p == f or p.startswith(f.rstrip("\\/") + os.sep)


def _check_dir(raw: str) -> str:
    path = (raw or "").strip().strip('"')
    if not path:
        raise PhotoError("PATH_REQUIRED", "Indiquez le chemin d'un dossier")
    if not os.path.isabs(path):
        raise PhotoError("BAD_PATH", "Le chemin doit être absolu (ex. D:\\Photos)")
    if not os.path.isdir(path):
        raise PhotoError("NOT_FOUND", f"Dossier introuvable : {path}")
    return os.path.normpath(os.path.realpath(path))


def _ids(payload: dict) -> list[int]:
    ids = payload.get("ids")
    if not isinstance(ids, list) or not ids:
        raise PhotoError("BAD_REQUEST", "Sélectionnez au moins une photo")
    if len(ids) > MAX_BATCH:
        raise PhotoError("TOO_MANY", f"{MAX_BATCH} photos maximum par opération")
    out = []
    for i in ids:
        v = _int(i, -1)
        if v > 0 and v not in out:
            out.append(v)
    return out


async def _photos_by_ids(ids: list[int]) -> list[dict]:
    db = await _db()
    rows = []
    for i in range(0, len(ids), 500):
        chunk = ids[i:i + 500]
        rows += await db.fetch_all(f"SELECT {PHOTO_COLS} FROM photos WHERE id IN ({','.join('?' * len(chunk))})", tuple(chunk))
    order = {v: k for k, v in enumerate(ids)}
    return sorted(rows, key=lambda r: order[r["id"]])


def guarded(fn):
    async def wrapper(payload: dict):
        try:
            return await fn(payload)
        except PhotoError as exc:
            return _err(exc.message, exc.code)
    wrapper.__name__ = fn.__name__
    return wrapper


# ── Folders & indexing ───────────────────────────────────────────────────────

@module.on("folders.list")
async def folders_list(payload: dict):
    rows = await (await _db()).fetch_all(
        "SELECT f.*, (SELECT COUNT(*) FROM photos p WHERE p.folder_id=f.id) AS count FROM folders f ORDER BY f.path COLLATE NOCASE")
    for r in rows:
        r["exists"] = os.path.isdir(r["path"])
    return {"folders": rows}


async def _register_folder(path: str) -> dict:
    db = await _db()
    folders = await db.fetch_all("SELECT * FROM folders")
    for f in folders:
        if _inside(path, f["path"]):
            return {"id": f["id"], "path": f["path"], "existing": True}
    row = await db.fetch_one("INSERT INTO folders (path, added_at) VALUES (?,?) RETURNING id, path", (path, _now()))
    for f in folders:
        if _inside(f["path"], path):
            await db.execute("UPDATE photos SET folder_id=? WHERE folder_id=?", (row["id"], f["id"]))
            await db.execute("DELETE FROM folders WHERE id=?", (f["id"],))
    return {**row, "existing": False}


@module.on("folder.add")
@guarded
async def folder_add(payload: dict):
    path = _check_dir(str(payload.get("path") or ""))
    return await _register_folder(path)


@module.on("folder.remove")
async def folder_remove(payload: dict):
    db = await _db()
    fid = _int(payload.get("id"))
    ids = [r["id"] for r in await db.fetch_all("SELECT id FROM photos WHERE folder_id=?", (fid,))]
    if not await db.execute("DELETE FROM folders WHERE id=?", (fid,)):
        return _err("Dossier introuvable", "NOT_FOUND")
    await db.execute("DELETE FROM photos WHERE folder_id=?", (fid,))
    await asyncio.to_thread(_drop_thumbs, ids)
    return {"ok": True, "removed": len(ids)}


def _drop_thumbs(ids):
    for i in ids:
        try:
            os.remove(_thumb(i))
        except OSError:
            pass


def _walk(folder: str) -> list[str]:
    out = []
    for root, dirs, files in os.walk(folder):
        dirs[:] = [d for d in dirs if not d.startswith(".") and d.lower() not in ("$recycle.bin", "@eadir")]
        for f in files:
            if os.path.splitext(f)[1].lower() in IMAGE_EXTS and not f.startswith("._"):
                out.append(os.path.join(root, f))
    return out


@module.job("index")
async def index(payload: dict, job):
    db = await _db()
    fid = _int(payload.get("folder_id"))
    folders = await db.fetch_all("SELECT * FROM folders" + (" WHERE id=?" if fid else ""), (fid,) if fid else ())
    if not folders:
        raise PhotoError("NOT_FOUND", "Aucun dossier à indexer : ajoutez d'abord un dossier")
    stats = {"added": 0, "updated": 0, "removed": 0, "unchanged": 0, "failed": 0, "errors": []}
    for folder in folders:
        if not os.path.isdir(folder["path"]):
            stats["errors"].append(f"Dossier introuvable : {folder['path']}")
            continue
        await job.progress(0, f"Recherche des images dans {folder['path']}…")
        files = await asyncio.to_thread(_walk, folder["path"])
        known = {_norm(r["path"]): r for r in await db.fetch_all(
            "SELECT id, path, size, mtime FROM photos WHERE folder_id=?", (folder["id"],))}
        seen = set()
        last = 0.0
        for i, path in enumerate(files):
            key = _norm(path)
            seen.add(key)
            if time.monotonic() - last > 0.25:
                last = time.monotonic()
                await job.progress(round(i * 100 / max(1, len(files)), 1), f"{i}/{len(files)} — {os.path.basename(path)}",
                                   folder=folder["path"])
            try:
                st = os.stat(path)
            except OSError:
                continue
            row = known.get(key)
            if row and row["size"] == st.st_size and abs(row["mtime"] - st.st_mtime) < 1 and os.path.isfile(_thumb(row["id"])):
                stats["unchanged"] += 1
                continue
            tmp = os.path.join(_thumbs_dir(), f"tmp-{uuid.uuid4().hex}.jpg")
            try:
                info = await asyncio.to_thread(analyze, path, tmp)
            except Exception as exc:
                stats["failed"] += 1
                if len(stats["errors"]) < 30:
                    stats["errors"].append(f"{os.path.basename(path)} : {exc}")
                try:
                    os.remove(tmp)
                except OSError:
                    pass
                continue
            vals = (info["size"], info["mtime"], info["width"], info["height"], info["taken"], info["taken_src"],
                    info["camera"], info["lat"], info["lon"], info["dhash"], info["phash"])
            if row:
                await db.execute("UPDATE photos SET size=?, mtime=?, width=?, height=?, taken=?, taken_src=?, camera=?,"
                                 " lat=?, lon=?, dhash=?, phash=?, sha256='' WHERE id=?", (*vals, row["id"]))
                pid = row["id"]
                stats["updated"] += 1
            else:
                pid = (await db.fetch_one(
                    "INSERT INTO photos (folder_id, path, name, size, mtime, width, height, taken, taken_src, camera,"
                    " lat, lon, dhash, phash) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?) RETURNING id",
                    (folder["id"], path, os.path.basename(path), *vals)))["id"]
                stats["added"] += 1
            os.replace(tmp, _thumb(pid))
        gone = [r["id"] for k, r in known.items() if k not in seen]
        for i in range(0, len(gone), 500):
            chunk = gone[i:i + 500]
            await db.execute(f"DELETE FROM photos WHERE id IN ({','.join('?' * len(chunk))})", tuple(chunk))
        await asyncio.to_thread(_drop_thumbs, gone)
        stats["removed"] += len(gone)
        await db.execute("UPDATE folders SET last_scan=? WHERE id=?", (_now(), folder["id"]))
    await job.progress(100, "Indexation terminée")
    return stats


# ── Browsing ─────────────────────────────────────────────────────────────────

def _where(payload: dict) -> tuple[str, list]:
    conds, params = [], []
    if _int(payload.get("folder_id")):
        conds.append("folder_id=?"); params.append(_int(payload["folder_id"]))
    if payload.get("year"):
        conds.append("substr(taken,1,4)=?"); params.append(f"{_int(payload['year']):04d}")
    if payload.get("month"):
        conds.append("substr(taken,6,2)=?"); params.append(f"{_int(payload['month']):02d}")
    if payload.get("camera") is not None and payload.get("camera") != "*":
        conds.append("camera=?"); params.append(str(payload["camera"]))
    if payload.get("gps") == "with":
        conds.append("lat IS NOT NULL")
    elif payload.get("gps") == "without":
        conds.append("lat IS NULL")
    cell = payload.get("cell")
    if isinstance(cell, dict) and str(cell.get("precision")) in PRECISIONS:
        step = PRECISIONS[str(cell["precision"])]
        try:
            la, lo = float(cell["lat"]), float(cell["lon"])
        except (KeyError, TypeError, ValueError):
            raise PhotoError("BAD_REQUEST", "Cellule de lieu invalide")
        conds.append("lat IS NOT NULL AND CAST(round((lat - ?) / ?) AS INTEGER) = 0 AND CAST(round((lon - ?) / ?) AS INTEGER) = 0")
        params += [la, step, lo, step]
    q = str(payload.get("q") or "").strip()
    if q:
        conds.append("(name LIKE ? ESCAPE '\\' OR camera LIKE ? ESCAPE '\\')")
        like = "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        params += [like, like]
    return (" WHERE " + " AND ".join(conds)) if conds else "", params


@module.on("photos.list")
@guarded
async def photos_list(payload: dict):
    db = await _db()
    where, params = _where(payload)
    order = SORTS.get(payload.get("sort") or "taken_desc", SORTS["taken_desc"])
    limit = max(1, min(1000, _int(payload.get("limit"), 300)))
    offset = max(0, _int(payload.get("offset")))
    total = (await db.fetch_one(f"SELECT COUNT(*) AS n, COALESCE(SUM(size),0) AS bytes FROM photos{where}", tuple(params)))
    rows = await db.fetch_all(f"SELECT {PHOTO_COLS} FROM photos{where} ORDER BY {order} LIMIT ? OFFSET ?",
                              (*params, limit, offset))
    return {"photos": rows, "total": total["n"], "bytes": total["bytes"], "offset": offset}


@module.on("photos.ids")
@guarded
async def photos_ids(payload: dict):
    where, params = _where(payload)
    order = SORTS.get(payload.get("sort") or "taken_desc", SORTS["taken_desc"])
    rows = await (await _db()).fetch_all(f"SELECT id FROM photos{where} ORDER BY {order} LIMIT ?", (*params, MAX_BATCH))
    return {"ids": [r["id"] for r in rows]}


@module.on("stats")
async def stats(payload: dict):
    db = await _db()
    fid = _int(payload.get("folder_id"))
    w, p = ("WHERE folder_id=?", (fid,)) if fid else ("", ())
    and_ = "AND" if fid else "WHERE"
    total = await db.fetch_one(f"SELECT COUNT(*) AS n, COALESCE(SUM(size),0) AS bytes,"
                               f" SUM(lat IS NOT NULL) AS gps, SUM(taken_src='exif') AS dated FROM photos {w}", p)
    months = await db.fetch_all(f"SELECT substr(taken,1,4) AS year, substr(taken,6,2) AS month, COUNT(*) AS count,"
                                f" MAX(id) AS cover FROM photos {w} {and_} taken IS NOT NULL GROUP BY year, month"
                                f" ORDER BY year DESC, month DESC", p)
    years: dict[str, dict] = {}
    for m in months:
        y = years.setdefault(m["year"], {"year": m["year"], "count": 0, "months": [], "cover": m["cover"]})
        y["count"] += m["count"]
        y["months"].append({"month": m["month"], "count": m["count"], "cover": m["cover"]})
    cameras = await db.fetch_all(f"SELECT camera, COUNT(*) AS count, MAX(id) AS cover, MIN(taken) AS first,"
                                 f" MAX(taken) AS last FROM photos {w} GROUP BY camera ORDER BY count DESC", p)
    return {"total": total["n"], "bytes": total["bytes"], "gps": total["gps"] or 0, "dated": total["dated"] or 0,
            "years": list(years.values()), "cameras": cameras}


@module.on("places")
@guarded
async def places(payload: dict):
    precision = str(payload.get("precision") or "10")
    if precision not in PRECISIONS:
        raise PhotoError("BAD_REQUEST", "Précision inconnue (1, 10 ou 100 km)")
    step = PRECISIONS[precision]
    fid = _int(payload.get("folder_id"))
    rows = await (await _db()).fetch_all(
        "SELECT id, name, lat, lon, taken FROM photos WHERE lat IS NOT NULL" + (" AND folder_id=?" if fid else "")
        + " ORDER BY taken", (fid,) if fid else ())
    cells: dict[tuple, dict] = {}
    for r in rows:
        key = (round(r["lat"] / step), round(r["lon"] / step))
        c = cells.setdefault(key, {"lat": key[0] * step, "lon": key[1] * step, "count": 0, "slat": 0.0, "slon": 0.0,
                                   "first": r["taken"], "last": r["taken"], "covers": []})
        c["count"] += 1
        c["slat"] += r["lat"]
        c["slon"] += r["lon"]
        c["last"] = r["taken"]
        if len(c["covers"]) < 4:
            c["covers"].append(r["id"])
    out = []
    for c in cells.values():
        out.append({"cell": {"lat": c["lat"], "lon": c["lon"], "precision": precision}, "count": c["count"],
                    "center": [round(c.pop("slat") / c["count"], 5), round(c.pop("slon") / c["count"], 5)],
                    "first": c["first"], "last": c["last"], "covers": c["covers"]})
    out.sort(key=lambda c: -c["count"])
    return {"clusters": out, "total": len(rows), "precision": precision}


@module.on("photo.info")
async def photo_info(payload: dict):
    rows = await _photos_by_ids([_int(payload.get("id"))])
    if not rows:
        return _err("Photo introuvable", "NOT_FOUND")
    p = rows[0]
    p["exists"] = os.path.isfile(p["path"])
    try:
        p["details"] = await asyncio.to_thread(exif_details, p["path"]) if p["exists"] else {}
    except Exception as exc:
        p["details"] = {"Erreur": str(exc)[:200]}
    return p


@module.on("photo.open")
async def photo_open(payload: dict):
    rows = await _photos_by_ids([_int(payload.get("id"))])
    if not rows or not os.path.isfile(rows[0]["path"]):
        return _err("Photo introuvable", "NOT_FOUND")
    path = rows[0]["path"]
    if payload.get("reveal"):
        path = os.path.dirname(path)
    if not hasattr(os, "startfile"):
        return _err("Ouverture non prise en charge sur ce système", "UNSUPPORTED")
    await asyncio.to_thread(os.startfile, path)
    return {"ok": True}


# ── Duplicates ───────────────────────────────────────────────────────────────

def _group_out(groups: list[list[dict]]) -> list[dict]:
    out = []
    for g in groups:
        best = max(g, key=lambda p: (p["width"] * p["height"], p["size"], -len(p["path"])))
        out.append({"photos": g, "keep": best["id"], "bytes": sum(p["size"] for p in g) - best["size"]})
    out.sort(key=lambda x: (-x["bytes"], -len(x["photos"])))
    return out


@module.job("duplicates.exact")
async def duplicates_exact(payload: dict, job):
    db = await _db()
    fid = _int(payload.get("folder_id"))
    rows = await db.fetch_all(
        f"SELECT {PHOTO_COLS}, sha256 FROM photos WHERE size IN (SELECT size FROM photos"
        + (" WHERE folder_id=?" if fid else "") + " GROUP BY size HAVING COUNT(*) > 1)"
        + (" AND folder_id=?" if fid else "") + " ORDER BY size", (fid, fid) if fid else ())
    by_hash: dict[str, list[dict]] = {}
    total = sum(r["size"] for r in rows if not r["sha256"]) or 1
    done = 0
    for r in rows:
        if not r["sha256"]:
            await job.progress(round(done * 100 / total, 1), f"Empreinte de {r['name']}")
            try:
                r["sha256"] = await asyncio.to_thread(sha256, r["path"], lambda: job.cancelled)
            except OSError:
                continue
            done += r["size"]
            await db.execute("UPDATE photos SET sha256=? WHERE id=?", (r["sha256"], r["id"]))
        by_hash.setdefault(r.pop("sha256"), []).append(r)
    groups = [g for g in by_hash.values() if len(g) > 1]
    await job.progress(100, "Terminé")
    return {"groups": _group_out(groups), "scanned": len(rows)}


def _similar_groups(items: list[tuple[int, int]], threshold: int, progress) -> list[list[int]]:
    n = len(items)
    parent = list(range(n))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x
    hashes = [h for _, h in items]
    for i in range(n):
        hi = hashes[i]
        for j in range(i + 1, n):
            if (hi ^ hashes[j]).bit_count() <= threshold:
                ri, rj = find(i), find(j)
                if ri != rj:
                    parent[rj] = ri
        if i % 200 == 0:
            progress(i * (2 * n - i) / max(1, n * n))
    groups: dict[int, list[int]] = {}
    for i in range(n):
        groups.setdefault(find(i), []).append(items[i][0])
    return [g for g in groups.values() if len(g) > 1]


@module.job("duplicates.similar")
async def duplicates_similar(payload: dict, job):
    algo = payload.get("algo") or "dhash"
    if algo not in ("dhash", "phash"):
        raise PhotoError("BAD_REQUEST", "Algorithme inconnu (dhash ou phash)")
    threshold = _int(payload.get("threshold"), 6)
    if not 0 <= threshold <= 20:
        raise PhotoError("BAD_REQUEST", "Seuil entre 0 et 20")
    db = await _db()
    fid = _int(payload.get("folder_id"))
    rows = await db.fetch_all(f"SELECT id, {algo} AS h FROM photos WHERE {algo} != ''" + (" AND folder_id=?" if fid else ""),
                              (fid,) if fid else ())
    if len(rows) > MAX_SIMILAR:
        raise PhotoError("TOO_MANY", f"Trop de photos ({len(rows)}) : limitez la recherche à un dossier ({MAX_SIMILAR} max.)")
    items = [(r["id"], int(r["h"], 16)) for r in rows]
    loop = asyncio.get_running_loop()
    state = {"last": 0.0}

    def progress(frac):
        if time.monotonic() - state["last"] > 0.3:
            state["last"] = time.monotonic()
            asyncio.run_coroutine_threadsafe(job.progress(round(frac * 100, 1), "Comparaison des images…"), loop)
    groups = await asyncio.to_thread(_similar_groups, items, threshold, progress)
    out = []
    for g in groups:
        out.append(await _photos_by_ids(g))
    await job.progress(100, "Terminé")
    return {"groups": _group_out(out), "scanned": len(rows)}


@module.on("trash")
@guarded
async def trash(payload: dict):
    ids = _ids(payload)
    try:
        from send2trash import send2trash
    except ImportError:
        raise PhotoError("MISSING_DEP", "send2trash n'est pas installé : installez les dépendances du module")
    photos = await _photos_by_ids(ids)
    db = await _db()
    trashed, failed = [], []
    async with _fs_lock:
        for p in photos:
            try:
                if os.path.exists(p["path"]):
                    await asyncio.to_thread(send2trash, p["path"])
                trashed.append(p["id"])
            except Exception as exc:
                failed.append({"id": p["id"], "name": p["name"], "error": str(exc)[:200]})
        for i in range(0, len(trashed), 500):
            chunk = trashed[i:i + 500]
            await db.execute(f"DELETE FROM photos WHERE id IN ({','.join('?' * len(chunk))})", tuple(chunk))
    await asyncio.to_thread(_drop_thumbs, trashed)
    return {"trashed": len(trashed), "failed": failed}


# ── Rename & sort ────────────────────────────────────────────────────────────

def _unique_target(target: str, taken: set[str], own: str) -> str:
    base, ext = os.path.splitext(target)
    cand, k = target, 2
    while _norm(cand) in taken or (os.path.exists(cand) and _norm(cand) != _norm(own)):
        cand = f"{base}_{k}{ext}"
        k += 1
    return cand


def _plan_rename(photos: list[dict], pattern: str, start: int) -> list[dict]:
    if not pattern.strip():
        raise PhotoError("BAD_PATTERN", "Le modèle est vide")
    if len(pattern) > 200:
        raise PhotoError("BAD_PATTERN", "Modèle trop long")
    sources = {_norm(p["path"]) for p in photos}
    taken: set[str] = set()
    plan = []
    for n, p in enumerate(photos, start=start):
        ext = os.path.splitext(p["name"])[1].lower()
        name = format_pattern(pattern, p, n) + ext
        target = os.path.join(os.path.dirname(p["path"]), name)
        if _norm(target) != _norm(p["path"]):
            blocked = taken | (sources - {_norm(p["path"])})
            target = _unique_target(target, blocked, p["path"])
        taken.add(_norm(target))
        plan.append({"id": p["id"], "from": p["path"], "to": target, "name": os.path.basename(target),
                     "same": _norm(target) == _norm(p["path"]) and os.path.basename(target) == p["name"]})
    return plan


@module.on("rename.preview")
@guarded
async def rename_preview(payload: dict):
    photos = await _photos_by_ids(_ids(payload))
    plan = _plan_rename(photos, str(payload.get("pattern") or ""), max(0, _int(payload.get("start"), 1)))
    return {"items": plan[:2000], "count": len(plan), "changes": sum(not i["same"] for i in plan)}


def _record_label(kind: str, n: int, extra: str = "") -> str:
    return {"rename": f"Renommage de {n} photo(s)", "move": f"Déplacement de {n} photo(s){extra}",
            "copy": f"Copie de {n} photo(s){extra}"}[kind]


async def _record(kind: str, items: list[dict], extra: str = "") -> int:
    row = await (await _db()).fetch_one("INSERT INTO ops (kind, label, items, created_at) VALUES (?,?,?,?) RETURNING id",
                                        (kind, _record_label(kind, len(items), extra), json.dumps(items), _now()))
    return row["id"]


@module.on("rename.apply")
@guarded
async def rename_apply(payload: dict):
    photos = await _photos_by_ids(_ids(payload))
    plan = [i for i in _plan_rename(photos, str(payload.get("pattern") or ""), max(0, _int(payload.get("start"), 1)))
            if not i["same"]]
    if not plan:
        return {"renamed": 0, "failed": [], "op_id": None}
    db = await _db()
    done, failed = [], []
    async with _fs_lock:
        staged = []
        for it in plan:
            tmp = os.path.join(os.path.dirname(it["from"]), f".ledroide-rename-{uuid.uuid4().hex[:10]}{os.path.splitext(it['from'])[1]}")
            try:
                os.rename(it["from"], tmp)
                staged.append((it, tmp))
            except OSError as exc:
                failed.append({"name": os.path.basename(it["from"]), "error": exc.strerror or str(exc)})
        for it, tmp in staged:
            target = it["to"]
            if os.path.exists(target):
                target = _unique_target(target, set(), tmp)
            try:
                os.rename(tmp, target)
            except OSError as exc:
                os.rename(tmp, it["from"])
                failed.append({"name": os.path.basename(it["from"]), "error": exc.strerror or str(exc)})
                continue
            await db.execute("UPDATE photos SET path=?, name=? WHERE id=?", (target, os.path.basename(target), it["id"]))
            done.append({"id": it["id"], "from": it["from"], "to": target})
    op_id = await _record("rename", done) if done else None
    return {"renamed": len(done), "failed": failed, "op_id": op_id}


def _plan_sort(photos: list[dict], dest: str, pattern: str) -> list[dict]:
    taken: set[str] = set()
    plan = []
    for p in photos:
        sub = format_pattern(pattern, p, 0, allow_sep=True)
        target = os.path.join(dest, sub, p["name"])
        if _norm(target) == _norm(p["path"]):
            plan.append({"id": p["id"], "from": p["path"], "to": target, "rel": os.path.join(sub, p["name"]), "same": True})
            continue
        target = _unique_target(target, taken, "")
        taken.add(_norm(target))
        plan.append({"id": p["id"], "from": p["path"], "to": target, "rel": os.path.relpath(target, dest), "same": False})
    return plan


def _sort_args(payload: dict) -> tuple[str, str, str]:
    dest = _check_dir(str(payload.get("dest") or ""))
    mode = payload.get("mode") or "move"
    if mode not in ("move", "copy"):
        raise PhotoError("BAD_REQUEST", "Mode inconnu (move ou copy)")
    pattern = str(payload.get("pattern") or "{annee}/{mois}").strip()
    if len(pattern) > 200:
        raise PhotoError("BAD_PATTERN", "Modèle trop long")
    return dest, mode, pattern


@module.on("sort.preview")
@guarded
async def sort_preview(payload: dict):
    dest, mode, pattern = _sort_args(payload)
    plan = _plan_sort(await _photos_by_ids(_ids(payload)), dest, pattern)
    folders: dict[str, int] = {}
    for it in plan:
        if not it["same"]:
            d = os.path.dirname(it["rel"])
            folders[d] = folders.get(d, 0) + 1
    return {"items": plan[:2000], "count": len(plan), "changes": sum(not i["same"] for i in plan),
            "folders": [{"folder": k, "count": v} for k, v in sorted(folders.items())], "dest": dest, "mode": mode}


@module.job("sort.apply")
async def sort_apply(payload: dict, job):
    dest, mode, pattern = _sort_args(payload)
    photos = await _photos_by_ids(_ids(payload))
    plan = [i for i in _plan_sort(photos, dest, pattern) if not i["same"]]
    db = await _db()
    folder = await _register_folder(dest)
    done, failed = [], []
    async with _fs_lock:
        for k, it in enumerate(plan):
            await job.progress(round(k * 100 / max(1, len(plan)), 1), os.path.basename(it["from"]))
            try:
                if os.path.exists(it["to"]):
                    raise FileExistsError("la destination existe déjà")
                os.makedirs(os.path.dirname(it["to"]), exist_ok=True)
                if mode == "move":
                    await asyncio.to_thread(shutil.move, it["from"], it["to"])
                    await db.execute("UPDATE photos SET path=?, name=?, folder_id=? WHERE id=?",
                                     (it["to"], os.path.basename(it["to"]), folder["id"], it["id"]))
                    done.append({"id": it["id"], "from": it["from"], "to": it["to"], "root": dest})
                else:
                    await asyncio.to_thread(shutil.copy2, it["from"], it["to"])
                    src = await db.fetch_one("SELECT * FROM photos WHERE id=?", (it["id"],))
                    st = os.stat(it["to"])
                    new = await db.fetch_one(
                        "INSERT INTO photos (folder_id, path, name, size, mtime, width, height, taken, taken_src, camera,"
                        " lat, lon, dhash, phash, sha256) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) RETURNING id",
                        (folder["id"], it["to"], os.path.basename(it["to"]), st.st_size, st.st_mtime, src["width"],
                         src["height"], src["taken"], src["taken_src"], src["camera"], src["lat"], src["lon"],
                         src["dhash"], src["phash"], src["sha256"]))
                    if os.path.isfile(_thumb(it["id"])):
                        shutil.copyfile(_thumb(it["id"]), _thumb(new["id"]))
                    done.append({"id": new["id"], "from": it["from"], "to": it["to"], "root": dest})
            except (OSError, shutil.Error) as exc:
                failed.append({"name": os.path.basename(it["from"]), "error": getattr(exc, "strerror", None) or str(exc)})
    op_id = await _record(mode, done, f" vers {dest}") if done else None
    await job.progress(100, "Terminé")
    return {"done": len(done), "failed": failed[:100], "failed_count": len(failed), "op_id": op_id, "mode": mode}


@module.on("ops.list")
async def ops_list(payload: dict):
    rows = await (await _db()).fetch_all("SELECT id, kind, label, created_at, undone, items FROM ops ORDER BY id DESC LIMIT 50")
    for r in rows:
        r["count"] = len(json.loads(r.pop("items")))
    return {"ops": rows}


@module.on("op.undo")
@guarded
async def op_undo(payload: dict):
    db = await _db()
    op = await db.fetch_one("SELECT * FROM ops WHERE id=?", (_int(payload.get("id")),))
    if not op:
        raise PhotoError("NOT_FOUND", "Opération introuvable")
    if op["undone"]:
        raise PhotoError("ALREADY_UNDONE", "Cette opération a déjà été annulée")
    items = json.loads(op["items"])
    restored, failed = 0, []
    async with _fs_lock:
        for it in reversed(items):
            try:
                if not os.path.exists(it["to"]):
                    raise FileNotFoundError("fichier introuvable (déplacé ou supprimé depuis)")
                if op["kind"] == "copy":
                    from send2trash import send2trash
                    await asyncio.to_thread(send2trash, it["to"])
                    await db.execute("DELETE FROM photos WHERE id=?", (it["id"],))
                    await asyncio.to_thread(_drop_thumbs, [it["id"]])
                else:
                    if os.path.exists(it["from"]):
                        raise FileExistsError("l'emplacement d'origine est de nouveau occupé")
                    os.makedirs(os.path.dirname(it["from"]), exist_ok=True)
                    await asyncio.to_thread(shutil.move, it["to"], it["from"])
                    fid = None
                    for f in await db.fetch_all("SELECT id, path FROM folders"):
                        if _inside(it["from"], f["path"]):
                            fid = f["id"]
                    await db.execute("UPDATE photos SET path=?, name=?, folder_id=COALESCE(?, folder_id) WHERE id=?",
                                     (it["from"], os.path.basename(it["from"]), fid, it["id"]))
                restored += 1
            except (OSError, ImportError) as exc:
                failed.append({"name": os.path.basename(it["to"]), "error": getattr(exc, "strerror", None) or str(exc)})
        if op["kind"] in ("move", "copy"):
            await asyncio.to_thread(_prune_empty, [(os.path.dirname(i["to"]), i.get("root", "")) for i in items])
    await db.execute("UPDATE ops SET undone=1 WHERE id=?", (op["id"],))
    return {"restored": restored, "failed": failed}


def _prune_empty(dirs: list[tuple[str, str]]):
    for d, root in sorted(set(dirs), key=lambda x: len(x[0]), reverse=True):
        cur = d
        while root and _inside(cur, root) and _norm(cur) != _norm(root):
            try:
                if os.path.isdir(cur) and not os.listdir(cur):
                    os.rmdir(cur)
            except OSError:
                break
            cur = os.path.dirname(cur)


# ── HTTP routes ──────────────────────────────────────────────────────────────

@router.get("/thumb/{pid}")
async def thumb_route(pid: int):
    path = _thumb(pid)
    if not os.path.isfile(path):
        raise error(404, "Miniature introuvable", "NOT_FOUND")
    data = await asyncio.to_thread(lambda: open(path, "rb").read())
    return Response(data, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=3600"})


def _preview(path: str, size: int) -> bytes:
    Image, ImageOps = pil()
    with Image.open(path) as img:
        if img.format == "JPEG":
            img.draft("RGB", (size, size))
        work = ImageOps.exif_transpose(img)
        if work.mode in ("RGBA", "LA", "P"):
            rgba = work.convert("RGBA")
            bg = Image.new("RGB", work.size, (24, 24, 27))
            bg.paste(rgba, mask=rgba.split()[3])
            work = bg
        work = work.convert("RGB")
        work.thumbnail((size, size))
        buf = io.BytesIO()
        work.save(buf, "JPEG", quality=88)
    return buf.getvalue()


@router.get("/image/{pid}")
async def image_route(pid: int, size: int = Query(1600, ge=64, le=4096)):
    rows = await _photos_by_ids([pid])
    if not rows or not os.path.isfile(rows[0]["path"]):
        raise error(404, "Photo introuvable", "NOT_FOUND")
    try:
        data = await asyncio.to_thread(_preview, rows[0]["path"], size)
    except Exception as exc:
        raise error(422, f"Image illisible : {exc}", "BAD_IMAGE")
    return Response(data, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=600"})
