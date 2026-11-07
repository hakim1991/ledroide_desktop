"""Historique presse-papier — capture en arrière-plan du presse-papier Windows."""
import asyncio
import ctypes
import hashlib
import io
import sqlite3
import sys
import threading
import time
import types
from contextlib import contextmanager
from ctypes import wintypes
from pathlib import Path
from typing import Optional

from ledroide_sdk.http import Response
from ledroide_sdk.http import JSONResponse
from pydantic import BaseModel
from ledroide_sdk import Module, data_path

module = Module(__file__)
router = module.router

DB_PATH = data_path(__file__, "clipboard-history.db")
THREAD_NAME = "ledroide-clipboard-history-poller"
POLL_INTERVAL = 0.7
MAX_TEXT = 1_000_000          # caractères max conservés par entrée texte
MAX_IMAGE_PIXELS = 40_000_000  # ignore les images gigantesques
THUMB_SIZE = (320, 240)
IS_WINDOWS = sys.platform == "win32"

# État partagé qui survit à une ré-importation du module (désactivation/réactivation) :
# le thread de capture ne doit être démarré qu'une seule fois par processus.
_STATE_KEY = "_ledroide_clipboard_history_state"
_STATE = sys.modules.get(_STATE_KEY)
if _STATE is None:
    _STATE = types.ModuleType(_STATE_KEY)
    _STATE.lock = threading.Lock()
    _STATE.version = 0          # incrémenté à chaque modification de l'historique
    _STATE.ignore_seq = None    # numéro de séquence produit par notre propre copie
    _STATE.last_seq = None
    _STATE.last_error = None
    _STATE.thread = None
    sys.modules[_STATE_KEY] = _STATE
if not hasattr(_STATE, "clip_lock"):
    _STATE.clip_lock = threading.Lock()  # sérialise capture et recopie


# ── Base de données ───────────────────────────────────────────────────────────

@contextmanager
def _db():
    conn = sqlite3.connect(str(DB_PATH), timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _init():
    with _db() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS items (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                kind       TEXT    NOT NULL,            -- 'text' | 'image'
                content    TEXT,
                image      BLOB,
                thumb      BLOB,
                width      INTEGER,
                height     INTEGER,
                hash       TEXT    NOT NULL,
                pinned     INTEGER NOT NULL DEFAULT 0,
                created_at REAL    NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_items_created ON items(created_at);
            CREATE TABLE IF NOT EXISTS settings (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
        """)


_init()

DEFAULT_SETTINGS = {"paused": "0", "max_entries": "200", "capture_images": "1"}


def _get_settings() -> dict:
    with _db() as conn:
        rows = dict(conn.execute("SELECT key, value FROM settings").fetchall())
    s = {**DEFAULT_SETTINGS, **rows}
    return {
        "paused": s["paused"] == "1",
        "max_entries": int(s["max_entries"]),
        "capture_images": s["capture_images"] == "1",
    }


def _set_setting(key: str, value: str):
    with _db() as conn:
        conn.execute("INSERT INTO settings (key, value) VALUES (?, ?) "
                     "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))


def _bump():
    with _STATE.lock:
        _STATE.version += 1


def _prune(conn, max_entries: int):
    conn.execute("""
        DELETE FROM items WHERE pinned=0 AND id NOT IN (
            SELECT id FROM items WHERE pinned=0 ORDER BY created_at DESC, id DESC LIMIT ?
        )""", (max_entries,))


def _store(kind: str, hash_: str, content=None, image=None, thumb=None, width=None, height=None) -> bool:
    """Insère une entrée sauf si identique à la plus récente. Retourne True si ajout."""
    settings = _get_settings()
    with _db() as conn:
        last = conn.execute("SELECT hash FROM items ORDER BY created_at DESC, id DESC LIMIT 1").fetchone()
        if last and last["hash"] == hash_:
            return False
        conn.execute(
            "INSERT INTO items (kind, content, image, thumb, width, height, hash, created_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (kind, content, image, thumb, width, height, hash_, time.time()))
        _prune(conn, settings["max_entries"])
    _bump()
    return True


# ── Presse-papier Windows (ctypes) ────────────────────────────────────────────

CF_UNICODETEXT = 13
CF_DIB = 8
CF_BITMAP = 2
GMEM_MOVEABLE = 0x0002

if IS_WINDOWS:
    _user32 = ctypes.WinDLL("user32", use_last_error=True)
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    _user32.OpenClipboard.argtypes = [wintypes.HWND]
    _user32.OpenClipboard.restype = wintypes.BOOL
    _user32.CloseClipboard.argtypes = []
    _user32.CloseClipboard.restype = wintypes.BOOL
    _user32.EmptyClipboard.argtypes = []
    _user32.EmptyClipboard.restype = wintypes.BOOL
    _user32.GetClipboardData.argtypes = [wintypes.UINT]
    _user32.GetClipboardData.restype = wintypes.HANDLE
    _user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
    _user32.SetClipboardData.restype = wintypes.HANDLE
    _user32.IsClipboardFormatAvailable.argtypes = [wintypes.UINT]
    _user32.IsClipboardFormatAvailable.restype = wintypes.BOOL
    _user32.GetClipboardSequenceNumber.argtypes = []
    _user32.GetClipboardSequenceNumber.restype = wintypes.DWORD

    _kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
    _kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
    _kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
    _kernel32.GlobalLock.restype = wintypes.LPVOID
    _kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
    _kernel32.GlobalUnlock.restype = wintypes.BOOL
    _kernel32.GlobalFree.argtypes = [wintypes.HGLOBAL]
    _kernel32.GlobalFree.restype = wintypes.HGLOBAL
    _kernel32.GlobalSize.argtypes = [wintypes.HGLOBAL]
    _kernel32.GlobalSize.restype = ctypes.c_size_t


def _open_clipboard(retries: int = 10) -> bool:
    for _ in range(retries):
        if _user32.OpenClipboard(None):
            return True
        time.sleep(0.03)
    return False


def _seq() -> int:
    return int(_user32.GetClipboardSequenceNumber()) if IS_WINDOWS else 0


def _read_text() -> Optional[str]:
    if not _user32.IsClipboardFormatAvailable(CF_UNICODETEXT):
        return None
    if not _open_clipboard():
        return None
    try:
        h = _user32.GetClipboardData(CF_UNICODETEXT)
        if not h:
            return None
        p = _kernel32.GlobalLock(h)
        if not p:
            return None
        try:
            size = _kernel32.GlobalSize(h)
            n = max(0, size // 2)
            text = ctypes.wstring_at(p, n) if n else ""
            return text.split("\x00", 1)[0]
        finally:
            _kernel32.GlobalUnlock(h)
    finally:
        _user32.CloseClipboard()


def _set_clipboard(fmt: int, data: bytes) -> bool:
    h = _kernel32.GlobalAlloc(GMEM_MOVEABLE, len(data))
    if not h:
        return False
    p = _kernel32.GlobalLock(h)
    if not p:
        _kernel32.GlobalFree(h)
        return False
    ctypes.memmove(p, data, len(data))
    _kernel32.GlobalUnlock(h)
    if not _open_clipboard(30):
        _kernel32.GlobalFree(h)
        return False
    try:
        _user32.EmptyClipboard()
        if not _user32.SetClipboardData(fmt, h):
            _kernel32.GlobalFree(h)
            return False
        return True  # le système possède désormais la mémoire
    finally:
        _user32.CloseClipboard()


def set_clipboard_text(text: str) -> bool:
    return _set_clipboard(CF_UNICODETEXT, (text + "\x00").encode("utf-16-le"))


def set_clipboard_image_png(png: bytes) -> bool:
    from PIL import Image
    img = Image.open(io.BytesIO(png))
    if img.mode not in ("RGB", "RGBA"):
        img = img.convert("RGBA" if "A" in img.getbands() else "RGB")
    if img.mode == "RGBA":
        bg = Image.new("RGB", img.size, (255, 255, 255))
        bg.paste(img, mask=img.split()[3])
        img = bg
    buf = io.BytesIO()
    img.save(buf, "BMP")
    return _set_clipboard(CF_DIB, buf.getvalue()[14:])  # retire l'en-tête BITMAPFILEHEADER


def _capture_image():
    try:
        from PIL import Image, ImageGrab
    except ImportError:
        return None
    try:
        img = ImageGrab.grabclipboard()
    except Exception:
        return None
    if not isinstance(img, Image.Image):
        return None  # None ou liste de fichiers
    if img.width * img.height > MAX_IMAGE_PIXELS:
        return None
    if img.mode not in ("RGB", "RGBA"):
        img = img.convert("RGBA")
    h = hashlib.sha1(img.tobytes()).hexdigest()
    full = io.BytesIO()
    img.save(full, "PNG", optimize=False)
    th = img.copy()
    th.thumbnail(THUMB_SIZE)
    tb = io.BytesIO()
    th.save(tb, "PNG")
    return h, full.getvalue(), tb.getvalue(), img.width, img.height


def _poll_once():
    with _STATE.clip_lock:
        _poll_once_locked()


def _poll_once_locked():
    seq = _seq()
    if seq == _STATE.last_seq:
        return
    _STATE.last_seq = seq
    if seq == _STATE.ignore_seq:
        return
    settings = _get_settings()
    if settings["paused"]:
        return
    text = _read_text()
    if text is not None:
        if text.strip():
            text = text[:MAX_TEXT]
            _store("text", "t:" + hashlib.sha1(text.encode("utf-8", "surrogatepass")).hexdigest(), content=text)
        return
    if settings["capture_images"] and (_user32.IsClipboardFormatAvailable(CF_DIB)
                                       or _user32.IsClipboardFormatAvailable(CF_BITMAP)):
        res = _capture_image()
        if res:
            h, full, thumb, w, hh = res
            _store("image", "i:" + h, image=full, thumb=thumb, width=w, height=hh)


def _poll_loop():
    while True:
        try:
            _poll_once()
            _STATE.last_error = None
        except Exception as e:  # ne jamais tuer le thread
            _STATE.last_error = str(e)
        time.sleep(POLL_INTERVAL)


def _ensure_thread():
    if not IS_WINDOWS:
        return
    with _STATE.lock:
        alive = any(t.name == THREAD_NAME and t.is_alive() for t in threading.enumerate())
        if alive:
            return
        t = threading.Thread(target=_poll_loop, name=THREAD_NAME, daemon=True)
        _STATE.thread = t
        t.start()


_ensure_thread()


# ── API ───────────────────────────────────────────────────────────────────────

def _err(msg: str, status: int = 400, code: Optional[str] = None):
    c = {"error": msg}
    if code:
        c["code"] = code
    return JSONResponse(status_code=status, content=c)


PREVIEW_LEN = 600


def _item_out(row) -> dict:
    d = {"id": row["id"], "kind": row["kind"], "pinned": bool(row["pinned"]),
         "created_at": row["created_at"]}
    if row["kind"] == "text":
        content = row["content"] or ""
        d["preview"] = content[:PREVIEW_LEN]
        d["length"] = len(content)
        d["lines"] = content.count("\n") + 1
    else:
        d["width"], d["height"] = row["width"], row["height"]
    return d


class SettingsIn(BaseModel):
    paused: Optional[bool] = None
    max_entries: Optional[int] = None
    capture_images: Optional[bool] = None


class PinIn(BaseModel):
    pinned: bool


class TextIn(BaseModel):
    text: str


@router.get("/status")
async def status():
    def _q():
        s = _get_settings()
        with _db() as conn:
            total = conn.execute("SELECT COUNT(*) FROM items").fetchone()[0]
            pinned = conn.execute("SELECT COUNT(*) FROM items WHERE pinned=1").fetchone()[0]
        try:
            import PIL  # noqa: F401
            pil = True
        except ImportError:
            pil = False
        running = any(t.name == THREAD_NAME and t.is_alive() for t in threading.enumerate())
        return {**s, "running": running, "supported": IS_WINDOWS, "version": _STATE.version,
                "count": total, "pinned_count": pinned, "images_available": pil,
                "last_error": _STATE.last_error}
    return await asyncio.to_thread(_q)


@router.post("/settings")
async def update_settings(payload: SettingsIn):
    if payload.max_entries is not None and not (10 <= payload.max_entries <= 5000):
        return _err("Le nombre maximal d'entrées doit être entre 10 et 5000")

    def _q():
        if payload.paused is not None:
            _set_setting("paused", "1" if payload.paused else "0")
        if payload.capture_images is not None:
            _set_setting("capture_images", "1" if payload.capture_images else "0")
        if payload.max_entries is not None:
            _set_setting("max_entries", str(payload.max_entries))
            with _db() as conn:
                _prune(conn, payload.max_entries)
            _bump()
        return _get_settings()
    return await asyncio.to_thread(_q)


@router.get("/items")
async def list_items(q: str = "", kind: str = "", pinned_only: bool = False, limit: int = 300, offset: int = 0):
    limit = max(1, min(limit, 1000))

    def _f():
        where, args = [], []
        if q:
            where.append("kind='text' AND content LIKE ? ESCAPE '\\'")
            esc = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            args.append(f"%{esc}%")
        if kind in ("text", "image"):
            where.append("kind=?")
            args.append(kind)
        if pinned_only:
            where.append("pinned=1")
        w = ("WHERE " + " AND ".join(where)) if where else ""
        with _db() as conn:
            total = conn.execute(f"SELECT COUNT(*) FROM items {w}", args).fetchone()[0]
            rows = conn.execute(
                f"SELECT id, kind, content, pinned, created_at, width, height FROM items {w} "
                "ORDER BY pinned DESC, created_at DESC, id DESC LIMIT ? OFFSET ?", args + [limit, offset]).fetchall()
        return {"items": [_item_out(r) for r in rows], "total": total, "version": _STATE.version}
    return await asyncio.to_thread(_f)


@router.get("/items/{item_id}")
async def get_item(item_id: int):
    def _q():
        with _db() as conn:
            return conn.execute("SELECT id, kind, content, pinned, created_at, width, height FROM items WHERE id=?",
                                (item_id,)).fetchone()
    row = await asyncio.to_thread(_q)
    if not row:
        return _err("Élément introuvable", 404)
    d = _item_out(row)
    if row["kind"] == "text":
        d["content"] = row["content"]
    return d


@router.get("/items/{item_id}/thumb")
async def get_thumb(item_id: int, full: bool = False):
    def _q():
        with _db() as conn:
            return conn.execute(f"SELECT {'image' if full else 'thumb'} AS b FROM items WHERE id=? AND kind='image'",
                                (item_id,)).fetchone()
    row = await asyncio.to_thread(_q)
    if not row or row["b"] is None:
        return _err("Image introuvable", 404)
    return Response(content=bytes(row["b"]), media_type="image/png",
                    headers={"Cache-Control": "max-age=86400"})


@router.post("/items/{item_id}/pin")
async def pin_item(item_id: int, payload: PinIn):
    def _q():
        with _db() as conn:
            n = conn.execute("UPDATE items SET pinned=? WHERE id=?", (1 if payload.pinned else 0, item_id)).rowcount
            if n and not payload.pinned:
                _prune(conn, _get_settings()["max_entries"])
            return n
    if not await asyncio.to_thread(_q):
        return _err("Élément introuvable", 404)
    _bump()
    return {"ok": True, "pinned": payload.pinned}


@router.delete("/items/{item_id}")
async def delete_item(item_id: int):
    def _q():
        with _db() as conn:
            return conn.execute("DELETE FROM items WHERE id=?", (item_id,)).rowcount
    if not await asyncio.to_thread(_q):
        return _err("Élément introuvable", 404)
    _bump()
    return {"ok": True}


@router.post("/clear")
async def clear_items():
    def _q():
        with _db() as conn:
            return conn.execute("DELETE FROM items WHERE pinned=0").rowcount
    n = await asyncio.to_thread(_q)
    _bump()
    return {"ok": True, "deleted": n}


def _copy_and_mark(fn, *args) -> bool:
    with _STATE.clip_lock:
        ok = fn(*args)
        if ok:
            _STATE.ignore_seq = _seq()
        return ok


@router.post("/items/{item_id}/copy")
async def copy_item(item_id: int):
    if not IS_WINDOWS:
        return _err("Fonction disponible uniquement sous Windows", 400, "UNSUPPORTED")

    def _q():
        with _db() as conn:
            row = conn.execute("SELECT kind, content, image FROM items WHERE id=?", (item_id,)).fetchone()
        if not row:
            return "NOT_FOUND"
        if row["kind"] == "text":
            ok = _copy_and_mark(set_clipboard_text, row["content"] or "")
        else:
            try:
                import PIL  # noqa: F401
            except ImportError:
                return "MISSING_DEP"
            ok = _copy_and_mark(set_clipboard_image_png, bytes(row["image"]))
        if not ok:
            return "BUSY"
        with _db() as conn:  # remonte l'élément en tête
            conn.execute("UPDATE items SET created_at=? WHERE id=?", (time.time(), item_id))
        _bump()
        return "OK"
    r = await asyncio.to_thread(_q)
    if r == "NOT_FOUND":
        return _err("Élément introuvable", 404)
    if r == "MISSING_DEP":
        return _err("Pillow n'est pas installé.", 400, "MISSING_DEP")
    if r == "BUSY":
        return _err("Impossible d'accéder au presse-papier (occupé par une autre application)", 409)
    return {"ok": True}


@router.post("/copy-text")
async def copy_text(payload: TextIn):
    """Copie un texte arbitraire dans le presse-papier (sans l'ajouter à l'historique)."""
    if not IS_WINDOWS:
        return _err("Fonction disponible uniquement sous Windows", 400, "UNSUPPORTED")
    ok = await asyncio.to_thread(_copy_and_mark, set_clipboard_text, payload.text)
    if not ok:
        return _err("Impossible d'accéder au presse-papier", 409)
    return {"ok": True}
