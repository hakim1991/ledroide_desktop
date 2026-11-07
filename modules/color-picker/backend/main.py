"""Couleurs — palette enregistrée (SQLite) et capture d'écran pour la pipette."""
import asyncio
import re
import sqlite3
import tempfile
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from ledroide_sdk.http import FileResponse, JSONResponse
from pydantic import BaseModel
from ledroide_sdk import Module, data_path

module = Module(__file__)
router = module.router

DB_PATH = data_path(__file__, "color-picker.db")
TEMP_DIR = Path(tempfile.gettempdir()) / "ledroide_color-picker"
TEMP_DIR.mkdir(parents=True, exist_ok=True)
HEX_RE = re.compile(r"^#[0-9a-f]{6}$")


def _err(msg: str, status: int = 400, code: Optional[str] = None):
    c = {"error": msg}
    if code:
        c["code"] = code
    return JSONResponse(status_code=status, content=c)


@contextmanager
def _db():
    conn = sqlite3.connect(str(DB_PATH))
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
        conn.execute("""
            CREATE TABLE IF NOT EXISTS colors (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                hex        TEXT NOT NULL,
                name       TEXT NOT NULL DEFAULT '',
                position   INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            )
        """)


_init()


def _norm_hex(h: str) -> str:
    h = (h or "").strip().lower()
    if not h.startswith("#"):
        h = "#" + h
    if re.fullmatch(r"#[0-9a-f]{3}", h):
        h = "#" + "".join(ch * 2 for ch in h[1:])
    if not HEX_RE.match(h):
        raise ValueError("Couleur invalide (format #rrggbb).")
    return h


class ColorIn(BaseModel):
    hex: str
    name: str = ""


class ColorPatch(BaseModel):
    name: Optional[str] = None
    hex: Optional[str] = None


def _list():
    with _db() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM colors ORDER BY position, id")]


@router.get("/palette")
async def list_palette():
    return {"colors": await asyncio.to_thread(_list)}


@router.post("/palette")
async def add_color(body: ColorIn):
    try:
        hx = _norm_hex(body.hex)
    except ValueError as e:
        return _err(str(e))
    name = (body.name or "").strip()[:60]

    def _q():
        with _db() as conn:
            pos = conn.execute("SELECT COALESCE(MAX(position), 0) + 1 FROM colors").fetchone()[0]
            cur = conn.execute("INSERT INTO colors (hex, name, position, created_at) VALUES (?, ?, ?, ?)",
                               (hx, name, pos, datetime.now(timezone.utc).isoformat()))
            return dict(conn.execute("SELECT * FROM colors WHERE id=?", (cur.lastrowid,)).fetchone())

    return await asyncio.to_thread(_q)


@router.patch("/palette/{color_id}")
async def patch_color(color_id: int, body: ColorPatch):
    changes = {}
    if body.name is not None:
        changes["name"] = body.name.strip()[:60]
    if body.hex is not None:
        try:
            changes["hex"] = _norm_hex(body.hex)
        except ValueError as e:
            return _err(str(e))

    def _q():
        with _db() as conn:
            if changes:
                sets = ", ".join(f"{k}=?" for k in changes)
                conn.execute(f"UPDATE colors SET {sets} WHERE id=?", [*changes.values(), color_id])
            row = conn.execute("SELECT * FROM colors WHERE id=?", (color_id,)).fetchone()
            return dict(row) if row else None

    row = await asyncio.to_thread(_q)
    if row is None:
        return _err("Couleur introuvable", 404)
    return row


@router.delete("/palette/{color_id}")
async def delete_color(color_id: int):
    def _q():
        with _db() as conn:
            return conn.execute("DELETE FROM colors WHERE id=?", (color_id,)).rowcount

    if not await asyncio.to_thread(_q):
        return _err("Couleur introuvable", 404)
    return {"ok": True}


@router.delete("/palette")
async def clear_palette():
    def _q():
        with _db() as conn:
            return conn.execute("DELETE FROM colors").rowcount

    return {"ok": True, "deleted": await asyncio.to_thread(_q)}


# ── capture d'écran (pipette de secours) ─────────────────────────────────────

def _grab() -> dict:
    try:
        from PIL import ImageGrab
    except ImportError as e:
        raise ImportError("Pillow") from e
    now = time.time()
    for p in TEMP_DIR.glob("screen_*.png"):
        try:
            if now - p.stat().st_mtime > 600:
                p.unlink()
        except OSError:
            pass
    try:
        img = ImageGrab.grab(all_screens=True)
    except TypeError:
        img = ImageGrab.grab()
    img = img.convert("RGB")
    name = f"screen_{uuid.uuid4().hex[:10]}.png"
    img.save(TEMP_DIR / name, format="PNG", compress_level=1)
    return {"filename": name, "width": img.width, "height": img.height}


@router.post("/screenshot")
async def screenshot():
    try:
        return await asyncio.to_thread(_grab)
    except ImportError:
        return _err("Pillow est requis pour la capture d'écran. Installez les dépendances du module.", 400, "MISSING_DEP")
    except Exception as e:  # noqa: BLE001
        return _err(f"Capture d'écran impossible : {e}", 500)


def _safe(filename: str) -> bool:
    return bool(filename) and "/" not in filename and "\\" not in filename and ".." not in filename


@router.get("/screen/{filename}")
async def get_screen(filename: str):
    """Image servie en ligne pour l'affichage dans la pipette."""
    if not _safe(filename):
        return _err("Nom de fichier invalide")
    path = TEMP_DIR / filename
    if not path.is_file():
        return _err("Fichier introuvable", 404)
    return FileResponse(str(path), media_type="image/png", headers={"Cache-Control": "no-store"})


@router.delete("/screen/{filename}")
async def delete_screen(filename: str):
    if not _safe(filename):
        return _err("Nom de fichier invalide")
    (TEMP_DIR / filename).unlink(missing_ok=True)
    return {"ok": True}


# ── export de la palette ─────────────────────────────────────────────────────

def _slug(s: str, i: int) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-")
    return s or f"couleur-{i}"


def _export(fmt: str) -> str:
    import json
    colors = _list()
    if fmt == "css":
        lines = [":root {"] + [f"  --{_slug(c['name'], i + 1)}: {c['hex']};" for i, c in enumerate(colors)] + ["}"]
        content = "\n".join(lines) + "\n"
    elif fmt == "json":
        content = json.dumps([{"name": c["name"], "hex": c["hex"]} for c in colors], ensure_ascii=False, indent=2)
    else:  # gpl (GIMP / Inkscape)
        lines = ["GIMP Palette", "Name: LedroideDesktop", "Columns: 8", "#"]
        for i, c in enumerate(colors):
            r, g, b = (int(c["hex"][k:k + 2], 16) for k in (1, 3, 5))
            lines.append(f"{r:3d} {g:3d} {b:3d}\t{c['name'] or c['hex']}")
        content = "\n".join(lines) + "\n"
    name = f"palette_{uuid.uuid4().hex[:6]}.{fmt}"
    (TEMP_DIR / name).write_text(content, encoding="utf-8")
    return name


@router.post("/export/{fmt}")
async def export_palette(fmt: str):
    if fmt not in ("css", "json", "gpl"):
        return _err("Format invalide (css, json, gpl).")
    return {"filename": await asyncio.to_thread(_export, fmt)}


@router.get("/file/{filename}")
async def get_file(filename: str):
    if not _safe(filename):
        return _err("Nom de fichier invalide")
    path = TEMP_DIR / filename
    if not path.is_file():
        return _err("Fichier introuvable", 404)
    return FileResponse(str(path), filename=filename, content_disposition_type="attachment")
