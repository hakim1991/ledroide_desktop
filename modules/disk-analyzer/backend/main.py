"""Analyseur de disque — scan os.scandir en tâche de fond, vue élaguée + exploration à la demande."""
import asyncio
import ctypes
import heapq
import logging
import os
import shutil
import stat
import subprocess
import tempfile
import threading
import time
import uuid
from collections import OrderedDict
from ctypes import wintypes
from pathlib import Path

from ledroide_sdk import Module

log = logging.getLogger("ledroide.disk-analyzer")

module = Module(__file__)
router = module.router

TOP_FILES = 100
FILES_PER_DIR = 20
MAX_CHILDREN = 150
VIEW_FRACTION = 1500
VIEW_DEPTH = 10
KEEP_SCANS = 3

_scans: "OrderedDict[str, dict]" = OrderedDict()
_stops: set[threading.Event] = set()


class Cancelled(Exception):
    pass


class Node:
    __slots__ = ("name", "parent", "own", "size", "files", "tfiles", "tdirs", "children", "top", "error")

    def __init__(self, name: str, parent: "Node | None"):
        self.name = name
        self.parent = parent
        self.own = 0
        self.size = 0
        self.files = 0
        self.tfiles = 0
        self.tdirs = 0
        self.children: list[Node] = []
        self.top: list[tuple[int, str]] = []
        self.error = False


def _is_link(entry: os.DirEntry) -> bool:
    try:
        return entry.is_symlink() or entry.is_junction()
    except OSError:
        return True


def _scan_sync(path: str, stop: threading.Event, prog: dict) -> dict:
    started = time.perf_counter()
    root = Node(path, None)
    order: list[Node] = []
    stack = [(root, path)]
    top_files: list[tuple[int, str]] = []
    exts: dict[str, list[int]] = {}
    errors = links = 0
    while stack:
        if stop.is_set():
            raise Cancelled()
        node, p = stack.pop()
        order.append(node)
        prog["current"] = p
        try:
            it = os.scandir(p)
        except OSError:
            node.error = True
            errors += 1
            continue
        with it:
            while True:
                try:
                    e = next(it)
                except StopIteration:
                    break
                except OSError:
                    errors += 1
                    break
                try:
                    if _is_link(e):
                        links += 1
                        continue
                    if e.is_dir(follow_symlinks=False):
                        child = Node(e.name, node)
                        node.children.append(child)
                        stack.append((child, e.path))
                        prog["dirs"] += 1
                        continue
                    size = e.stat(follow_symlinks=False).st_size
                except OSError:
                    errors += 1
                    continue
                node.own += size
                node.files += 1
                prog["files"] += 1
                prog["bytes"] += size
                if len(node.top) < FILES_PER_DIR:
                    heapq.heappush(node.top, (size, e.name))
                elif size > node.top[0][0]:
                    heapq.heapreplace(node.top, (size, e.name))
                if len(top_files) < TOP_FILES:
                    heapq.heappush(top_files, (size, e.path))
                elif size > top_files[0][0]:
                    heapq.heapreplace(top_files, (size, e.path))
                dot = e.name.rfind(".")
                ext = e.name[dot:].lower() if 0 < dot < len(e.name) - 1 and len(e.name) - dot <= 12 else ""
                bucket = exts.get(ext)
                if bucket is None:
                    exts[ext] = [size, 1]
                else:
                    bucket[0] += size
                    bucket[1] += 1
    for node in reversed(order):
        node.size += node.own
        node.tfiles += node.files
        node.children.sort(key=lambda n: n.size, reverse=True)
        if node.parent is not None:
            node.parent.size += node.size
            node.parent.tfiles += node.tfiles
            node.parent.tdirs += node.tdirs + 1
    ext_rows = sorted(([k or "(sans extension)", v[0], v[1]] for k, v in exts.items()), key=lambda r: r[1], reverse=True)
    return {
        "root": root,
        "top_files": [{"path": p, "size": s} for s, p in sorted(top_files, reverse=True)],
        "extensions": [{"ext": k, "size": s, "count": c} for k, s, c in ext_rows[:200]],
        "ext_total": len(ext_rows),
        "stats": {"files": root.tfiles, "dirs": root.tdirs, "size": root.size, "errors": errors,
                  "links_skipped": links, "duration_s": round(time.perf_counter() - started, 2)},
    }


def _view(node: Node, min_size: int, depth: int) -> dict:
    out = {"n": node.name, "s": node.size, "f": node.tfiles, "d": node.tdirs, "t": "d"}
    if node.error:
        out["e"] = 1
    if depth <= 0 or (node.size < min_size and node.parent is not None and depth < VIEW_DEPTH):
        if node.children or node.files:
            out["m"] = 1
        return out
    kids = []
    for child in node.children[:MAX_CHILDREN]:
        if child.size >= min_size:
            kids.append(_view(child, min_size, depth - 1))
        else:
            leaf = {"n": child.name, "s": child.size, "f": child.tfiles, "d": child.tdirs, "t": "d"}
            if child.children or child.files:
                leaf["m"] = 1
            if child.error:
                leaf["e"] = 1
            kids.append(leaf)
    rest = node.children[MAX_CHILDREN:]
    if rest:
        kids.append({"n": f"({len(rest)} autres dossiers)", "s": sum(c.size for c in rest), "t": "o"})
    top = sorted(node.top, reverse=True)
    for size, name in top:
        kids.append({"n": name, "s": size, "t": "f"})
    others = node.files - len(top)
    if others > 0:
        kids.append({"n": f"({others} autres fichiers)", "s": node.own - sum(s for s, _ in top), "t": "o"})
    kids.sort(key=lambda k: k["s"], reverse=True)
    out["c"] = kids
    return out


def _node_view(node: Node) -> dict:
    return _view(node, max(1, node.size // VIEW_FRACTION), VIEW_DEPTH)


def _validate_dir(path: str) -> str:
    path = os.path.expandvars(str(path or "").strip().strip('"'))
    if not path:
        raise ValueError("Indiquez un lecteur ou un dossier à analyser")
    if len(path) == 2 and path[1] == ":":
        path += "\\"
    if not os.path.isdir(path):
        raise ValueError(f"Dossier introuvable : {path}")
    return os.path.abspath(path)


@module.on("check_path")
async def check_path(payload: dict):
    try:
        return {"path": await asyncio.to_thread(_validate_dir, payload.get("path"))}
    except ValueError as exc:
        return {"error": str(exc), "code": "BAD_PATH"}


@module.job("scan")
async def scan(payload: dict, job):
    try:
        path = _validate_dir(payload.get("path"))
    except ValueError as exc:
        raise RuntimeError(str(exc))
    used = None
    if os.path.splitdrive(path)[1] in ("\\", "/"):
        try:
            usage = await asyncio.to_thread(shutil.disk_usage, path)
            used = usage.used
        except OSError:
            pass
    stop = threading.Event()
    _stops.add(stop)
    prog = {"files": 0, "dirs": 0, "bytes": 0, "current": path}
    fut = asyncio.ensure_future(asyncio.to_thread(_scan_sync, path, stop, prog))
    log.info("scan started")
    try:
        while not fut.done():
            await asyncio.wait({fut}, timeout=0.4)
            pct = min(99.0, prog["bytes"] * 100 / used) if used else None
            await job.progress(pct, prog["current"], files=prog["files"], dirs=prog["dirs"], bytes=prog["bytes"])
        try:
            res = fut.result()
        except Cancelled:
            raise asyncio.CancelledError()
    except asyncio.CancelledError:
        stop.set()
        fut.add_done_callback(lambda f: f.cancelled() or f.exception())
        raise
    finally:
        _stops.discard(stop)
    scan_id = uuid.uuid4().hex
    _scans[scan_id] = {"root": res["root"], "path": path}
    while len(_scans) > KEEP_SCANS:
        _scans.popitem(last=False)
    log.info("scan done: %s files in %ss", res["stats"]["files"], res["stats"]["duration_s"])
    tree = await asyncio.to_thread(_node_view, res["root"])
    return {"scan_id": scan_id, "path": path, "tree": tree, "top_files": res["top_files"],
            "extensions": res["extensions"], "ext_total": res["ext_total"], "stats": res["stats"]}


@module.on("subtree")
async def subtree(payload: dict):
    s = _scans.get(str(payload.get("scan_id") or ""))
    if not s:
        return {"error": "Analyse expirée : relancez l'analyse", "code": "SCAN_EXPIRED"}
    node = s["root"]
    for name in payload.get("path") or []:
        node = next((c for c in node.children if c.name == name), None)
        if node is None:
            return {"error": "Dossier introuvable dans l'analyse", "code": "NOT_FOUND"}
    return {"tree": await asyncio.to_thread(_node_view, node)}


# ── Drives ─────────────────────────────────────────────────────────────────

DRIVE_TYPES = {2: "Amovible", 3: "Disque local", 4: "Réseau", 5: "CD/DVD", 6: "Disque virtuel (RAM)"}


def _drive_info(root: str) -> dict:
    k32 = ctypes.windll.kernel32
    dtype = k32.GetDriveTypeW(root)
    label = ctypes.create_unicode_buffer(261)
    fs = ctypes.create_unicode_buffer(261)
    ok = k32.GetVolumeInformationW(root, label, 261, None, None, None, fs, 261)
    info = {"root": root, "type": DRIVE_TYPES.get(dtype, "Inconnu"), "label": label.value if ok else "",
            "fs": fs.value if ok else "", "total": None, "used": None, "free": None}
    if ok:
        try:
            u = shutil.disk_usage(root)
            info.update(total=u.total, used=u.used, free=u.free)
        except OSError:
            pass
    return info


@module.on("drives")
async def drives(payload: dict):
    roots = os.listdrives() if hasattr(os, "listdrives") else [f"{c}:\\" for c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
                                                               if os.path.exists(f"{c}:\\")]
    async def one(r):
        try:
            return await asyncio.wait_for(asyncio.to_thread(_drive_info, r), timeout=4)
        except (asyncio.TimeoutError, OSError):
            return {"root": r, "type": "Indisponible", "label": "", "fs": "", "total": None, "used": None, "free": None}
    items = await asyncio.gather(*(one(r) for r in roots))
    return {"items": list(items), "home": str(Path.home())}


@module.on("reveal")
async def reveal(payload: dict):
    path = str(payload.get("path") or "")
    if not path or not os.path.exists(path):
        return {"error": "Élément introuvable (supprimé depuis l'analyse ?)", "code": "NOT_FOUND"}
    path = os.path.normpath(path)
    args = ["explorer.exe", path] if os.path.isdir(path) and payload.get("open") else ["explorer.exe", f"/select,{path}"]
    await asyncio.to_thread(subprocess.Popen, args, creationflags=subprocess.CREATE_NO_WINDOW)
    return {"ok": True}


# ── Cleanup ────────────────────────────────────────────────────────────────

def _temp_dir() -> Path:
    return Path(tempfile.gettempdir())


def _safe_temp() -> Path:
    temp = _temp_dir().resolve()
    if temp.parent == temp or len(temp.parts) < 3 or not any(t in temp.name.lower() for t in ("temp", "tmp")):
        raise ValueError(f"Dossier temporaire inhabituel ({temp}) : nettoyage refusé par sécurité")
    if temp == Path.home().resolve() or temp == Path(os.environ.get("SystemRoot", "C:\\Windows")).resolve():
        raise ValueError("Dossier temporaire invalide : nettoyage refusé")
    return temp


def _tree_size(path: str) -> tuple[int, int]:
    size = count = 0
    stack = [path]
    while stack:
        p = stack.pop()
        try:
            with os.scandir(p) as it:
                for e in it:
                    try:
                        if _is_link(e):
                            continue
                        if e.is_dir(follow_symlinks=False):
                            stack.append(e.path)
                        else:
                            size += e.stat(follow_symlinks=False).st_size
                            count += 1
                    except OSError:
                        pass
        except OSError:
            pass
    return size, count


class _SHQUERYRBINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("i64Size", ctypes.c_longlong), ("i64NumItems", ctypes.c_longlong)]


def _recycle_bin() -> dict:
    info = _SHQUERYRBINFO()
    info.cbSize = ctypes.sizeof(info)
    hr = ctypes.windll.shell32.SHQueryRecycleBinW(None, ctypes.byref(info))
    if hr != 0:
        return {"size": None, "count": None}
    return {"size": info.i64Size, "count": info.i64NumItems}


def _cleanup_info_sync() -> dict:
    out: dict = {}
    try:
        temp = _safe_temp()
        size, count = _tree_size(str(temp))
        out["temp"] = {"path": str(temp), "size": size, "count": count}
    except ValueError as exc:
        out["temp"] = {"path": str(_temp_dir()), "error": str(exc)}
    explorer = Path(os.environ.get("LOCALAPPDATA", ""), "Microsoft", "Windows", "Explorer")
    thumbs = size = 0
    if explorer.is_dir():
        for f in explorer.iterdir():
            n = f.name.lower()
            if (n.startswith("thumbcache_") or n.startswith("iconcache_")) and n.endswith(".db"):
                try:
                    size += f.stat().st_size
                    thumbs += 1
                except OSError:
                    pass
    out["thumbnails"] = {"path": str(explorer), "size": size, "count": thumbs}
    out["recycle_bin"] = _recycle_bin()
    return out


@module.on("cleanup_info")
async def cleanup_info(payload: dict):
    return await asyncio.to_thread(_cleanup_info_sync)


def _temp_entries(min_age_h: float) -> tuple[Path, list[dict]]:
    temp = _safe_temp()
    limit = time.time() - max(0.0, min_age_h) * 3600
    rows = []
    with os.scandir(temp) as it:
        for e in it:
            try:
                st = e.stat(follow_symlinks=False)
                link = _is_link(e)
                is_dir = not link and e.is_dir(follow_symlinks=False)
            except OSError:
                continue
            if st.st_mtime > limit:
                continue
            size, count = _tree_size(e.path) if is_dir else (0 if link else st.st_size, 1)
            rows.append({"name": e.name, "path": e.path, "dir": is_dir, "link": link, "size": size,
                         "count": count, "mtime": st.st_mtime})
    rows.sort(key=lambda r: r["size"], reverse=True)
    return temp, rows


def _age(payload: dict) -> float:
    try:
        return max(0.0, float(payload.get("min_age_hours", 24)))
    except (TypeError, ValueError):
        return 24.0


@module.on("temp_preview")
async def temp_preview(payload: dict):
    try:
        temp, rows = await asyncio.to_thread(_temp_entries, _age(payload))
    except (ValueError, OSError) as exc:
        return {"error": str(exc), "code": "TEMP_UNAVAILABLE"}
    return {"path": str(temp), "items": rows[:500], "count": len(rows),
            "size": sum(r["size"] for r in rows), "files": sum(r["count"] for r in rows)}


def _purge(path: str, is_dir: bool, link: bool, acc: dict):
    if link:
        try:
            os.rmdir(path) if os.path.isdir(path) else os.unlink(path)
            acc["deleted"] += 1
        except OSError:
            acc["failed"] += 1
        return
    if is_dir:
        try:
            with os.scandir(path) as it:
                children = list(it)
        except OSError:
            acc["failed"] += 1
            return
        for e in children:
            try:
                clink = _is_link(e)
                cdir = not clink and e.is_dir(follow_symlinks=False)
            except OSError:
                acc["failed"] += 1
                continue
            _purge(e.path, cdir, clink, acc)
        try:
            os.rmdir(path)
        except OSError:
            pass
        return
    try:
        size = os.stat(path, follow_symlinks=False).st_size
    except OSError:
        size = 0
    try:
        try:
            os.unlink(path)
        except PermissionError:
            os.chmod(path, stat.S_IWRITE)
            os.unlink(path)
        acc["deleted"] += 1
        acc["freed"] += size
    except OSError:
        acc["failed"] += 1


def _temp_clean_sync(min_age_h: float) -> dict:
    temp, rows = _temp_entries(min_age_h)
    acc = {"deleted": 0, "failed": 0, "freed": 0}
    for r in rows:
        p = Path(r["path"])
        if p.parent.resolve() != temp:
            continue
        _purge(str(p), r["dir"], r["link"], acc)
    return acc


@module.on("temp_clean")
async def temp_clean(payload: dict):
    if payload.get("confirm") is not True:
        return {"error": "Confirmation requise", "code": "CONFIRM_REQUIRED"}
    try:
        res = await asyncio.to_thread(_temp_clean_sync, _age(payload))
    except (ValueError, OSError) as exc:
        return {"error": str(exc), "code": "TEMP_UNAVAILABLE"}
    log.info("temp cleanup: %s files deleted, %s locked/failed", res["deleted"], res["failed"])
    return {"ok": True, **res}


@module.on("open_tool")
async def open_tool(payload: dict):
    tools = {"recycle_bin": ["explorer.exe", "shell:RecycleBinFolder"], "cleanmgr": ["cleanmgr.exe"],
             "temp": ["explorer.exe", str(_temp_dir())]}
    args = tools.get(str(payload.get("tool")))
    if not args:
        return {"error": "Outil inconnu", "code": "BAD_TOOL"}
    try:
        await asyncio.to_thread(subprocess.Popen, args, creationflags=subprocess.CREATE_NO_WINDOW)
    except OSError as exc:
        return {"error": f"Lancement impossible : {exc}", "code": "LAUNCH_FAILED"}
    return {"ok": True}


@module.on_shutdown
async def _shutdown():
    for stop in list(_stops):
        stop.set()
    _scans.clear()
