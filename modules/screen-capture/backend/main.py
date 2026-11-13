import asyncio
import base64
import io
import logging
import os
import re
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional

from ledroide_sdk import Module
from ledroide_sdk.http import FileResponse, JSONResponse, Request

log = logging.getLogger("ledroide.screen-capture")

module = Module(__file__)
router = module.router

IS_WINDOWS = sys.platform == "win32"
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
MAX_UPLOAD = 80 * 1024 * 1024
MAX_RECORD_S = 1800
THUMB = (360, 220)
ID_RE = re.compile(r"^[0-9a-f]{32}$")

_ready = False
_lock = asyncio.Lock()
_recording: dict = {}

SCHEMA = """
CREATE TABLE IF NOT EXISTS captures (
    id          TEXT PRIMARY KEY,
    kind        TEXT NOT NULL,
    file        TEXT NOT NULL,
    width       INTEGER NOT NULL DEFAULT 0,
    height      INTEGER NOT NULL DEFAULT 0,
    size        INTEGER NOT NULL DEFAULT 0,
    duration    REAL,
    fps         INTEGER,
    source      TEXT NOT NULL DEFAULT '',
    title       TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL,
    edited_at   TEXT
);
CREATE INDEX IF NOT EXISTS idx_cap_created ON captures(created_at);
"""


async def _db():
    global _ready
    if not _ready:
        async with _lock:
            if not _ready:
                await module.db.executescript(SCHEMA)
                _ready = True
    return module.db


def _dir() -> Path:
    d = module.data_dir / "captures"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _thumb_path(cid: str) -> Path:
    return _dir() / f"{cid}.thumb.jpg"


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _err(message: str, code: str = "BAD_REQUEST") -> dict:
    return {"error": message, "code": code}


def _deps() -> dict:
    out = {}
    for name in ("mss", "PIL", "imageio_ffmpeg"):
        try:
            __import__(name)
            out[name] = True
        except ImportError:
            out[name] = False
    return out


def _need(*names: str):
    missing = [n for n, ok in _deps().items() if n in names and not ok]
    if missing:
        label = {"mss": "mss", "PIL": "Pillow", "imageio_ffmpeg": "imageio-ffmpeg"}
        raise RuntimeError("Bibliothèque manquante : " + ", ".join(label[m] for m in missing)
                           + " (réinstallez les dépendances du module depuis le Catalogue)")


# ── Capture ───────────────────────────────────────────────────────────────────

def _monitors() -> list[dict]:
    import mss
    with mss.mss() as sct:
        mons = sct.monitors
    out = []
    for i, m in enumerate(mons):
        out.append({"index": i, "left": m["left"], "top": m["top"], "width": m["width"], "height": m["height"],
                    "label": "Tous les écrans" if i == 0 else f"Écran {i} — {m['width']}×{m['height']}"})
    return out


def _region(target: str, monitor: int, rect: Optional[dict]) -> dict:
    import mss
    with mss.mss() as sct:
        mons = sct.monitors
    if target == "all":
        return dict(mons[0])
    if not 1 <= monitor < len(mons):
        raise ValueError("Écran introuvable")
    base = dict(mons[monitor])
    if rect:
        x, y, w, h = (int(rect.get(k, 0)) for k in ("x", "y", "w", "h"))
        x, y = max(0, x), max(0, y)
        w, h = min(w, base["width"] - x), min(h, base["height"] - y)
        if w < 16 or h < 16:
            raise ValueError("Zone trop petite (16 px minimum)")
        return {"left": base["left"] + x, "top": base["top"] + y, "width": w, "height": h}
    return base


def _grab(region: dict):
    import mss
    from PIL import Image
    with mss.mss() as sct:
        shot = sct.grab(region)
        return Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")


def _make_thumb(img, cid: str):
    t = img.copy()
    t.thumbnail(THUMB)
    if t.mode != "RGB":
        t = t.convert("RGB")
    t.save(_thumb_path(cid), "JPEG", quality=80)


def _save_image(img, cid: str) -> tuple[Path, int]:
    path = _dir() / f"{cid}.png"
    img.save(path, "PNG", optimize=False, compress_level=6)
    _make_thumb(img, cid)
    return path, path.stat().st_size


async def _insert(cid: str, kind: str, path: Path, w: int, h: int, size: int, source: str,
                  duration: Optional[float] = None, fps: Optional[int] = None, title: str = "") -> dict:
    db = await _db()
    await db.execute(
        "INSERT INTO captures (id, kind, file, width, height, size, duration, fps, source, title, created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (cid, kind, path.name, w, h, size, duration, fps, source, title, _now()))
    row = await db.fetch_one("SELECT * FROM captures WHERE id=?", (cid,))
    await module.events.emit("history:changed", {"id": cid})
    return _view(row)


def _view(r: dict) -> dict:
    return {**r, "url": f"/modules/{module.id}/api/file/{r['id']}?v={int(time.time())}",
            "thumb": f"/modules/{module.id}/api/thumb/{r['id']}?v={r.get('edited_at') or r['created_at']}"}


async def _get(cid) -> Optional[dict]:
    if not isinstance(cid, str) or not ID_RE.match(cid):
        return None
    return await (await _db()).fetch_one("SELECT * FROM captures WHERE id=?", (cid,))


@module.on("info")
async def info(payload: dict):
    deps = _deps()
    mons = []
    if deps["mss"]:
        try:
            mons = await asyncio.to_thread(_monitors)
        except Exception as exc:
            log.warning("monitors: %s", exc)
    return {"deps": deps, "monitors": mons, "recording": _rec_state(),
            "pictures": str(Path.home() / "Pictures"), "max_record_s": MAX_RECORD_S}


@module.on("monitor.preview")
async def monitor_preview(payload: dict):
    """Small JPEG previews of each monitor, to pick one."""
    _need("mss", "PIL")

    def run():
        out = []
        for m in _monitors()[1:]:
            img = _grab({k: m[k] for k in ("left", "top", "width", "height")})
            img.thumbnail((320, 200))
            buf = io.BytesIO()
            img.save(buf, "JPEG", quality=70)
            out.append({"index": m["index"], "data": "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()})
        return out
    try:
        return {"previews": await asyncio.to_thread(run)}
    except Exception as exc:
        return _err(f"Capture impossible : {exc}", "CAPTURE_FAILED")


@module.job("capture")
async def capture(payload: dict, job):
    _need("mss", "PIL")
    target = payload.get("target") or "all"
    if target not in ("all", "monitor"):
        raise ValueError("Cible inconnue")
    monitor = int(payload.get("monitor") or 1)
    delay = max(0, min(60, int(payload.get("delay") or 0)))
    for left in range(delay, 0, -1):
        await job.progress(100 * (delay - left) / delay, f"Capture dans {left} s…", countdown=left)
        await asyncio.sleep(1)
    region = await asyncio.to_thread(_region, target, monitor, None)
    img = await asyncio.to_thread(_grab, region)
    cid = uuid.uuid4().hex
    path, size = await asyncio.to_thread(_save_image, img, cid)
    source = "Tous les écrans" if target == "all" else f"Écran {monitor}"
    return await _insert(cid, "image", path, img.width, img.height, size, source)


@module.on("capture.crop")
async def capture_crop(payload: dict):
    """Keeps only `rect` (image pixels) of a capture — used by the zone selection."""
    row = await _get(payload.get("id"))
    if not row or row["kind"] != "image":
        return _err("Capture introuvable", "NOT_FOUND")
    rect = payload.get("rect") or {}
    try:
        x, y, w, h = (int(rect[k]) for k in ("x", "y", "w", "h"))
    except (KeyError, TypeError, ValueError):
        return _err("Zone invalide")
    x, y = max(0, x), max(0, y)
    w, h = min(w, row["width"] - x), min(h, row["height"] - y)
    if w < 8 or h < 8:
        return _err("Zone trop petite")

    def run():
        from PIL import Image
        with Image.open(_dir() / row["file"]) as im:
            out = im.crop((x, y, x + w, y + h))
            out.load()
        return out
    img = await asyncio.to_thread(run)
    path, size = await asyncio.to_thread(_save_image, img, row["id"])
    db = await _db()
    await db.execute("UPDATE captures SET width=?, height=?, size=?, source=?, edited_at=? WHERE id=?",
                     (w, h, size, f"Zone ({row['source']})", _now(), row["id"]))
    await module.events.emit("history:changed", {"id": row["id"]})
    return _view(await _get(row["id"]))


@router.get("/file/{cid}")
async def file_route(cid: str):
    row = await _get(cid)
    if not row:
        return JSONResponse(status_code=404, content={"error": "Capture introuvable", "code": "NOT_FOUND"})
    path = _dir() / row["file"]
    mt = {"image": "image/png", "video": "video/mp4", "gif": "image/gif"}[row["kind"]]
    return FileResponse(path, media_type=mt, headers={"Cache-Control": "no-store"})


@router.get("/thumb/{cid}")
async def thumb_route(cid: str):
    row = await _get(cid)
    tp = _thumb_path(cid) if row else None
    if not tp or not tp.exists():
        return JSONResponse(status_code=404, content={"error": "Miniature introuvable", "code": "NOT_FOUND"})
    return FileResponse(tp, media_type="image/jpeg")


async def _read_body(request: Request) -> bytes:
    data = bytearray()
    async for chunk in request.stream():
        data += chunk
        if len(data) > MAX_UPLOAD:
            raise ValueError("Image trop volumineuse")
    return bytes(data)


def _decode_png(data: bytes):
    from PIL import Image
    img = Image.open(io.BytesIO(data))
    if img.format not in ("PNG", "JPEG"):
        raise ValueError("Format d'image non pris en charge")
    img.load()
    return img.convert("RGBA") if img.mode in ("RGBA", "LA", "P") else img.convert("RGB")


@router.post("/edit/{cid}")
async def edit_route(cid: str, request: Request, mode: str = "replace"):
    """Body = annotated image (PNG) rendered by the editor. mode=replace|new."""
    row = await _get(cid)
    if not row or row["kind"] != "image":
        return JSONResponse(status_code=404, content={"error": "Capture introuvable", "code": "NOT_FOUND"})
    try:
        img = await asyncio.to_thread(_decode_png, await _read_body(request))
    except Exception as exc:
        return JSONResponse(status_code=400, content={"error": f"Image invalide : {exc}", "code": "BAD_IMAGE"})
    if mode == "new":
        nid = uuid.uuid4().hex
        path, size = await asyncio.to_thread(_save_image, img, nid)
        return await _insert(nid, "image", path, img.width, img.height, size, f"Modifiée ({row['source']})", title=row["title"])
    path, size = await asyncio.to_thread(_save_image, img, row["id"])
    db = await _db()
    await db.execute("UPDATE captures SET width=?, height=?, size=?, edited_at=? WHERE id=?",
                     (img.width, img.height, size, _now(), row["id"]))
    await module.events.emit("history:changed", {"id": row["id"]})
    return _view(await _get(row["id"]))


@router.post("/clipboard")
async def clipboard_route(request: Request):
    """Body = image (PNG) to put on the Windows clipboard."""
    if not IS_WINDOWS:
        return JSONResponse(status_code=400, content={"error": "Presse-papiers disponible sous Windows uniquement", "code": "UNSUPPORTED"})
    try:
        data = await _read_body(request)
        img = await asyncio.to_thread(_decode_png, data)
        ok = await asyncio.to_thread(_set_clipboard_image, img)
    except Exception as exc:
        return JSONResponse(status_code=400, content={"error": f"Copie impossible : {exc}", "code": "CLIPBOARD_FAILED"})
    if not ok:
        return JSONResponse(status_code=409, content={"error": "Presse-papiers occupé, réessayez", "code": "CLIPBOARD_BUSY"})
    return {"ok": True}


@module.on("clipboard.copy")
async def clipboard_copy(payload: dict):
    row = await _get(payload.get("id"))
    if not row or row["kind"] != "image":
        return _err("Capture introuvable", "NOT_FOUND")
    if not IS_WINDOWS:
        return _err("Presse-papiers disponible sous Windows uniquement", "UNSUPPORTED")

    def run():
        from PIL import Image
        with Image.open(_dir() / row["file"]) as im:
            return _set_clipboard_image(im.convert("RGB"))
    if not await asyncio.to_thread(run):
        return _err("Presse-papiers occupé, réessayez", "CLIPBOARD_BUSY")
    return {"ok": True}


def _set_clipboard_image(img) -> bool:
    import ctypes
    from ctypes import wintypes
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    user32.OpenClipboard.argtypes = [wintypes.HWND]
    user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
    user32.SetClipboardData.restype = wintypes.HANDLE
    kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
    kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
    kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalLock.restype = wintypes.LPVOID
    kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalFree.argtypes = [wintypes.HGLOBAL]
    if img.mode == "RGBA":
        from PIL import Image
        bg = Image.new("RGB", img.size, (255, 255, 255))
        bg.paste(img, mask=img.split()[3])
        img = bg
    elif img.mode != "RGB":
        img = img.convert("RGB")
    buf = io.BytesIO()
    img.save(buf, "BMP")
    dib = buf.getvalue()[14:]
    png = io.BytesIO()
    img.save(png, "PNG")
    png_fmt = user32.RegisterClipboardFormatW("PNG")

    def alloc(data: bytes):
        h = kernel32.GlobalAlloc(0x0002, len(data))
        if not h:
            return None
        p = kernel32.GlobalLock(h)
        ctypes.memmove(p, data, len(data))
        kernel32.GlobalUnlock(h)
        return h
    for _ in range(30):
        if user32.OpenClipboard(None):
            break
        time.sleep(0.03)
    else:
        return False
    try:
        user32.EmptyClipboard()
        h = alloc(dib)
        if not h or not user32.SetClipboardData(8, h):
            if h:
                kernel32.GlobalFree(h)
            return False
        if png_fmt:
            h2 = alloc(png.getvalue())
            if h2 and not user32.SetClipboardData(png_fmt, h2):
                kernel32.GlobalFree(h2)
        return True
    finally:
        user32.CloseClipboard()


@module.on("save_to")
async def save_to(payload: dict):
    """Writes a capture to a user folder (PNG/JPG for images, original format otherwise)."""
    row = await _get(payload.get("id"))
    if not row:
        return _err("Capture introuvable", "NOT_FOUND")
    folder = str(payload.get("folder") or "").strip().strip('"')
    if not folder or not os.path.isabs(folder):
        return _err("Dossier de destination non absolu", "BAD_PATH")
    fmt = (payload.get("format") or "png").lower()
    if fmt not in ("png", "jpg"):
        return _err("Format inconnu")
    quality = max(30, min(100, int(payload.get("quality") or 90)))

    def run():
        os.makedirs(folder, exist_ok=True)
        stamp = datetime.fromisoformat(row["created_at"]).strftime("%Y-%m-%d_%H-%M-%S")
        ext = {"image": fmt, "video": "mp4", "gif": "gif"}[row["kind"]]
        base = os.path.join(folder, f"Capture_{stamp}")
        out, n = f"{base}.{ext}", 2
        while os.path.exists(out):
            out, n = f"{base}_{n}.{ext}", n + 1
        src = _dir() / row["file"]
        if row["kind"] == "image":
            from PIL import Image
            with Image.open(src) as im:
                if fmt == "jpg":
                    im.convert("RGB").save(out, "JPEG", quality=quality, optimize=True)
                else:
                    im.save(out, "PNG")
        else:
            import shutil
            shutil.copyfile(src, out)
        return out
    try:
        out = await asyncio.to_thread(run)
    except OSError as exc:
        return _err(f"Enregistrement impossible : {exc.strerror or exc}", "WRITE_FAILED")
    return {"path": out}


@module.on("open_folder")
async def open_folder(payload: dict):
    folder = str(payload.get("folder") or "")
    if not IS_WINDOWS:
        return _err("Disponible sous Windows uniquement", "UNSUPPORTED")
    if not os.path.isabs(folder) or not os.path.isdir(folder):
        return _err("Dossier introuvable", "NOT_FOUND")
    await asyncio.to_thread(os.startfile, folder)
    return {"ok": True}


# ── History ───────────────────────────────────────────────────────────────────

@module.on("history.list")
async def history_list(payload: dict):
    rows = await (await _db()).fetch_all("SELECT * FROM captures ORDER BY created_at DESC, rowid DESC LIMIT 500")
    total = sum(r["size"] for r in rows)
    return {"captures": [_view(r) for r in rows], "total_size": total}


@module.on("capture.get")
async def capture_get(payload: dict):
    row = await _get(payload.get("id"))
    return _view(row) if row else _err("Capture introuvable", "NOT_FOUND")


@module.on("capture.rename")
async def capture_rename(payload: dict):
    row = await _get(payload.get("id"))
    if not row:
        return _err("Capture introuvable", "NOT_FOUND")
    title = str(payload.get("title") or "").strip()[:120]
    await (await _db()).execute("UPDATE captures SET title=? WHERE id=?", (title, row["id"]))
    return _view(await _get(row["id"]))


def _remove_files(row: dict):
    for p in (_dir() / row["file"], _thumb_path(row["id"])):
        try:
            p.unlink()
        except FileNotFoundError:
            pass


@module.on("capture.delete")
async def capture_delete(payload: dict):
    ids = payload.get("ids") or [payload.get("id")]
    db = await _db()
    n = 0
    for cid in ids:
        row = await _get(cid)
        if not row:
            continue
        await asyncio.to_thread(_remove_files, row)
        await db.execute("DELETE FROM captures WHERE id=?", (row["id"],))
        n += 1
    if not n:
        return _err("Capture introuvable", "NOT_FOUND")
    await module.events.emit("history:changed", {})
    return {"deleted": n}


# ── Screen recording ──────────────────────────────────────────────────────────

def _rec_state() -> Optional[dict]:
    if not _recording:
        return None
    return {"started": _recording["started"], "elapsed": round(time.time() - _recording["started"], 1),
            "frames": _recording.get("frames", 0), "fps": _recording["fps"], "max": _recording["max"],
            "source": _recording["source"]}


def _record(region: dict, fps: int, max_s: int, out: Path, thumb: Path, stop: threading.Event, stats: dict):
    import imageio_ffmpeg
    import mss
    from PIL import Image
    w, h = region["width"] - region["width"] % 2, region["height"] - region["height"] % 2
    region = {**region, "width": w, "height": h}
    cmd = [imageio_ffmpeg.get_ffmpeg_exe(), "-hide_banner", "-loglevel", "error", "-y",
           "-f", "rawvideo", "-pix_fmt", "bgra", "-s", f"{w}x{h}", "-r", str(fps), "-i", "-",
           "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-pix_fmt", "yuv420p",
           "-movflags", "+faststart", str(out)]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                            creationflags=NO_WINDOW)
    interval = 1.0 / fps
    started = time.perf_counter()
    frames = 0
    last = None
    try:
        with mss.mss() as sct:
            while not stop.is_set() or frames == 0:
                target = started + frames * interval
                now = time.perf_counter()
                if now - started >= max_s:
                    break
                if now < target:
                    time.sleep(min(interval, target - now))
                    continue
                shot = sct.grab(region)
                last = shot
                due = int((time.perf_counter() - started) / interval) + 1
                for _ in range(max(1, due - frames)):
                    proc.stdin.write(shot.bgra)
                    frames += 1
                if frames == 1:
                    img = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
                    img.thumbnail(THUMB)
                    img.save(thumb, "JPEG", quality=80)
                stats["frames"] = frames
    finally:
        try:
            proc.stdin.close()
        except OSError:
            pass
        err = proc.stderr.read().decode("utf-8", errors="replace")
        code = proc.wait(timeout=120)
    if code != 0 or frames == 0:
        raise RuntimeError(f"Encodage vidéo impossible : {err.strip()[:300] or code}")
    return {"frames": frames, "duration": round(frames / fps, 2), "width": w, "height": h}


@module.job("record")
async def record(payload: dict, job):
    _need("mss", "PIL", "imageio_ffmpeg")
    if _recording:
        raise RuntimeError("Un enregistrement est déjà en cours")
    fps = max(1, min(30, int(payload.get("fps") or 15)))
    max_s = max(1, min(MAX_RECORD_S, int(payload.get("max_s") or 60)))
    target = payload.get("target") or "monitor"
    monitor = int(payload.get("monitor") or 1)
    delay = max(0, min(30, int(payload.get("delay") or 0)))
    region = await asyncio.to_thread(_region, target if target in ("all", "monitor") else "monitor", monitor,
                                     payload.get("rect") or None)
    for left in range(delay, 0, -1):
        await job.progress(0, f"Enregistrement dans {left} s…", countdown=left)
        await asyncio.sleep(1)
    cid = uuid.uuid4().hex
    out = _dir() / f"{cid}.mp4"
    stop = threading.Event()
    source = ("Tous les écrans" if target == "all" else f"Écran {monitor}") + (" (zone)" if payload.get("rect") else "")
    _recording.update(started=time.time(), fps=fps, max=max_s, stop=stop, source=source, frames=0, job=job.id)
    await module.events.emit("record:started", _rec_state())
    fut = asyncio.ensure_future(asyncio.to_thread(_record, region, fps, max_s, out, _thumb_path(cid), stop, _recording))
    cancelled = False
    try:
        while not fut.done():
            await asyncio.wait({fut}, timeout=0.5)
            st = _rec_state()
            if st:
                await job.progress(min(100, 100 * st["elapsed"] / max_s), f"{st['elapsed']:.0f} s", elapsed=st["elapsed"])
        res = fut.result()
    except asyncio.CancelledError:
        cancelled = True
        stop.set()
        await asyncio.wait({fut})
        raise
    finally:
        _recording.clear()
        await module.events.emit("record:stopped", {})
        if cancelled or not fut.done() or fut.exception():
            for p in (out, _thumb_path(cid)):
                try:
                    p.unlink()
                except (FileNotFoundError, PermissionError):
                    pass
    return await _insert(cid, "video", out, res["width"], res["height"], out.stat().st_size, source,
                         duration=res["duration"], fps=fps)


@module.on("record.stop")
async def record_stop(payload: dict):
    if not _recording:
        return _err("Aucun enregistrement en cours", "NOT_FOUND")
    _recording["stop"].set()
    return {"ok": True}


@module.on("record.status")
async def record_status(payload: dict):
    return {"recording": _rec_state()}


@module.job("gif.export")
async def gif_export(payload: dict, job):
    """Short GIF from a recorded video (two-pass palette for clean colors)."""
    _need("imageio_ffmpeg", "PIL")
    row = await _get(payload.get("id"))
    if not row or row["kind"] != "video":
        raise ValueError("Vidéo introuvable")
    start = max(0.0, float(payload.get("start") or 0))
    dur = max(0.5, min(30.0, float(payload.get("duration") or 5)))
    if row["duration"] and start >= row["duration"]:
        raise ValueError("Le début dépasse la durée de la vidéo")
    fps = max(2, min(25, int(payload.get("fps") or 10)))
    width = max(120, min(1280, int(payload.get("width") or 640)))
    cid = uuid.uuid4().hex
    out = _dir() / f"{cid}.gif"
    src = _dir() / row["file"]

    def run():
        import imageio_ffmpeg
        from PIL import Image
        vf = f"fps={fps},scale={width}:-1:flags=lanczos,split[a][b];[a]palettegen=stats_mode=diff[p];[b][p]paletteuse=dither=bayer:bayer_scale=4"
        cmd = [imageio_ffmpeg.get_ffmpeg_exe(), "-hide_banner", "-loglevel", "error", "-y", "-ss", f"{start:.2f}",
               "-t", f"{dur:.2f}", "-i", str(src), "-vf", vf, "-loop", "0", str(out)]
        r = subprocess.run(cmd, capture_output=True, timeout=600, creationflags=NO_WINDOW)
        if r.returncode != 0 or not out.exists():
            raise RuntimeError("Conversion GIF impossible : " + r.stderr.decode("utf-8", errors="replace").strip()[:300])
        with Image.open(out) as im:
            size = im.size
            frames = getattr(im, "n_frames", 1)
            first = im.convert("RGB")
            _make_thumb(first, cid)
        return size, frames
    await job.progress(10, "Conversion en GIF…")
    (w, h), frames = await asyncio.to_thread(run)
    return await _insert(cid, "gif", out, w, h, out.stat().st_size, f"GIF ({row['source']})",
                         duration=round(frames / fps, 2), fps=fps)


@module.on_shutdown
async def _shutdown():
    if _recording.get("stop"):
        _recording["stop"].set()
