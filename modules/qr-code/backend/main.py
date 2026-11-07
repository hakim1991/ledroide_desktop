"""QR Code — génération (PNG / SVG) et lecture d'images."""
import asyncio
import base64
import io
import re
import tempfile
import time
import uuid
from pathlib import Path
from typing import Optional

from ledroide_sdk.http import UploadFile, File
from ledroide_sdk.http import FileResponse, JSONResponse
from pydantic import BaseModel
from ledroide_sdk import Module

module = Module(__file__)
router = module.router

TEMP_DIR = Path(tempfile.gettempdir()) / "ledroide_qr-code"
TEMP_DIR.mkdir(parents=True, exist_ok=True)

MAX_DATA = 2953  # capacité max (octets, niveau L, version 40)
HEX_RE = re.compile(r"^#[0-9a-fA-F]{6}$")


def _err(msg: str, status: int = 400, code: Optional[str] = None):
    c = {"error": msg}
    if code:
        c["code"] = code
    return JSONResponse(status_code=status, content=c)


def _missing(pkg: str):
    return _err(f"Dépendance manquante : {pkg}. Installez les dépendances du module.", 400, "MISSING_DEP")


class GenIn(BaseModel):
    data: str
    size: int = 512          # taille cible en pixels (PNG)
    ec: str = "M"            # L, M, Q, H
    fg: str = "#000000"
    bg: str = "#ffffff"
    border: int = 4          # zone de silence en modules
    transparent: bool = False


def _validate(g: GenIn):
    if not g.data:
        raise ValueError("Le contenu est vide.")
    if len(g.data.encode("utf-8")) > MAX_DATA:
        raise ValueError(f"Contenu trop long (max {MAX_DATA} octets).")
    if g.ec not in ("L", "M", "Q", "H"):
        raise ValueError("Niveau de correction invalide.")
    if not HEX_RE.match(g.fg) or not HEX_RE.match(g.bg):
        raise ValueError("Couleur invalide (format #rrggbb).")
    if not 64 <= g.size <= 4096:
        raise ValueError("Taille : entre 64 et 4096 px.")
    if not 0 <= g.border <= 20:
        raise ValueError("Marge : entre 0 et 20 modules.")


def _matrix(g: GenIn):
    import qrcode  # noqa: import inside for MISSING_DEP
    from qrcode.constants import ERROR_CORRECT_L, ERROR_CORRECT_M, ERROR_CORRECT_Q, ERROR_CORRECT_H
    ec = {"L": ERROR_CORRECT_L, "M": ERROR_CORRECT_M, "Q": ERROR_CORRECT_Q, "H": ERROR_CORRECT_H}[g.ec]
    qr = qrcode.QRCode(version=None, error_correction=ec, box_size=1, border=g.border)
    qr.add_data(g.data)
    try:
        qr.make(fit=True)
    except Exception as e:  # DataOverflowError
        raise ValueError("Contenu trop long pour ce niveau de correction.") from e
    return qr.get_matrix(), qr.version


def _hex_rgb(h: str):
    return tuple(int(h[i:i + 2], 16) for i in (1, 3, 5))


def _render_png(matrix, g: GenIn) -> tuple[bytes, int]:
    from PIL import Image
    n = len(matrix)
    box = max(1, round(g.size / n))
    mode = "RGBA" if g.transparent else "RGB"
    fg = _hex_rgb(g.fg) + ((255,) if g.transparent else ())
    bg = _hex_rgb(g.bg) + ((0,) if g.transparent else ())
    small = Image.new(mode, (n, n), bg)
    px = small.load()
    for y, row in enumerate(matrix):
        for x, v in enumerate(row):
            if v:
                px[x, y] = fg
    img = small.resize((n * box, n * box), Image.NEAREST)
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue(), n * box


def _render_svg(matrix, g: GenIn, px_size: int) -> str:
    n = len(matrix)
    parts = []
    for y, row in enumerate(matrix):
        x = 0
        while x < n:
            if row[x]:
                start = x
                while x < n and row[x]:
                    x += 1
                parts.append(f"M{start} {y}h{x - start}v1h-{x - start}z")
            else:
                x += 1
    bg = "" if g.transparent else f'<rect width="{n}" height="{n}" fill="{g.bg}"/>'
    return (f'<?xml version="1.0" encoding="UTF-8"?>\n'
            f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {n} {n}" width="{px_size}" height="{px_size}" '
            f'shape-rendering="crispEdges">{bg}<path fill="{g.fg}" d="{"".join(parts)}"/></svg>\n')


def _generate(g: GenIn) -> dict:
    matrix, version = _matrix(g)
    png, px = _render_png(matrix, g)
    svg = _render_svg(matrix, g, px)
    return {"png": png, "svg": svg, "version": version, "modules": len(matrix) - 2 * g.border, "pixels": px}


@router.get("/health")
async def health():
    deps = {}
    for name in ("qrcode", "cv2"):
        try:
            __import__(name)
            deps[name] = True
        except ImportError:
            deps[name] = False
    return {"ok": True, "deps": deps}


@router.post("/generate")
async def generate(g: GenIn):
    try:
        _validate(g)
    except ValueError as e:
        return _err(str(e))
    try:
        import qrcode  # noqa: F401
    except ImportError:
        return _missing("qrcode[pil]")
    try:
        r = await asyncio.to_thread(_generate, g)
    except ValueError as e:
        return _err(str(e))
    return {
        "png": "data:image/png;base64," + base64.b64encode(r["png"]).decode(),
        "svg": r["svg"], "version": r["version"], "modules": r["modules"], "pixels": r["pixels"],
    }


class ExportIn(GenIn):
    fmt: str = "png"
    name: str = "qrcode"


def _cleanup():
    now = time.time()
    for p in TEMP_DIR.iterdir():
        try:
            if now - p.stat().st_mtime > 3600:
                p.unlink()
        except OSError:
            pass


@router.post("/export")
async def export(body: ExportIn):
    if body.fmt not in ("png", "svg"):
        return _err("Format invalide.")
    try:
        _validate(body)
    except ValueError as e:
        return _err(str(e))
    try:
        import qrcode  # noqa: F401
    except ImportError:
        return _missing("qrcode[pil]")
    try:
        r = await asyncio.to_thread(_generate, body)
    except ValueError as e:
        return _err(str(e))
    await asyncio.to_thread(_cleanup)
    stem = re.sub(r"[^\w\-]+", "_", body.name or "qrcode").strip("_")[:40] or "qrcode"
    fname = f"{stem}_{uuid.uuid4().hex[:6]}.{body.fmt}"
    path = TEMP_DIR / fname
    if body.fmt == "png":
        path.write_bytes(r["png"])
    else:
        path.write_text(r["svg"], encoding="utf-8")
    return {"filename": fname}


@router.get("/file/{filename}")
async def get_file(filename: str):
    if not filename or "/" in filename or "\\" in filename or ".." in filename:
        return _err("Nom de fichier invalide")
    path = TEMP_DIR / filename
    if not path.is_file():
        return _err("Fichier introuvable", 404)
    mt = "image/svg+xml" if filename.endswith(".svg") else "image/png"
    return FileResponse(str(path), media_type=mt, filename=filename, content_disposition_type="attachment")


# ── lecture ───────────────────────────────────────────────────────────────────

def _classify(text: str) -> str:
    t = text.strip()
    u = t.upper()
    if u.startswith("WIFI:"):
        return "wifi"
    if u.startswith("BEGIN:VCARD"):
        return "vcard"
    if re.match(r"^https?://", t, re.I):
        return "url"
    if u.startswith("MAILTO:"):
        return "email"
    if u.startswith("TEL:"):
        return "tel"
    if u.startswith(("SMSTO:", "SMS:")):
        return "sms"
    if u.startswith("GEO:"):
        return "geo"
    return "text"


def _parse_wifi(text: str) -> dict:
    out = {}
    body = text[5:]
    for m in re.finditer(r"([A-Za-z]+):((?:\\.|[^;])*);", body):
        out[m.group(1).upper()] = re.sub(r"\\(.)", r"\1", m.group(2))
    return {"ssid": out.get("S", ""), "password": out.get("P", ""), "security": out.get("T", "nopass") or "nopass",
            "hidden": out.get("H", "").lower() == "true"}


def _decode(data: bytes) -> dict:
    import numpy as np
    import cv2
    img = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        try:
            from PIL import Image
            pil = Image.open(io.BytesIO(data))
            pil.seek(0)
            pil = pil.convert("RGBA")
            bgimg = Image.new("RGBA", pil.size, (255, 255, 255, 255))
            bgimg.alpha_composite(pil)
            img = cv2.cvtColor(np.array(bgimg.convert("RGB")), cv2.COLOR_RGB2BGR)
        except Exception as e:
            raise ValueError("Image illisible ou format non supporté.") from e
    h, w = img.shape[:2]
    det = cv2.QRCodeDetector()

    def attempt(im, scale):
        found = []
        try:
            ok, texts, pts, _ = det.detectAndDecodeMulti(im)
            if ok and texts is not None:
                for t, p in zip(texts, pts if pts is not None else [None] * len(texts)):
                    if t:
                        found.append((t, p))
        except cv2.error:
            pass
        if not found:
            try:
                t, p, _ = det.detectAndDecode(im)
                if t:
                    found.append((t, p[0] if p is not None and len(p.shape) == 3 else p))
            except cv2.error:
                pass
        res = []
        for t, p in found:
            pts = None
            if p is not None:
                pts = [[round(float(x) / scale, 1), round(float(y) / scale, 1)] for x, y in p.reshape(-1, 2)]
            res.append({"text": t, "points": pts})
        return res

    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    m = max(h, w)

    def variants(g):
        out = [(g, 1.0)]
        if m > 1600:
            s = 1600 / m
            out.append((cv2.resize(g, None, fx=s, fy=s, interpolation=cv2.INTER_AREA), s))
        if m < 400:
            s = 800 / m
            out.append((cv2.resize(g, None, fx=s, fy=s, interpolation=cv2.INTER_NEAREST), s))
        _, otsu = cv2.threshold(g, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        out += [(otsu, 1.0), (255 - g, 1.0), (255 - otsu, 1.0)]  # inversés : clair sur foncé
        return out

    # Plusieurs passes : chaque code trouvé est masqué puis on recommence (codes multiples).
    results = []
    work = gray.copy()
    for _ in range(12):
        found = []
        for im, s in variants(work):
            found = [r for r in attempt(im, s) if r["text"] not in {x["text"] for x in results}]
            if found:
                break
        if not found:
            break
        results += found
        masked = False
        for r in found:
            if r["points"]:
                poly = np.array(r["points"], dtype=np.float32)
                c = poly.mean(axis=0)
                poly = ((poly - c) * 1.15 + c).astype(np.int32)
                cv2.fillPoly(work, [poly], 255)
                masked = True
        if not masked:
            break
    seen = set()
    uniq = []
    for r in results:
        if r["text"] in seen:
            continue
        seen.add(r["text"])
        r["type"] = _classify(r["text"])
        if r["type"] == "wifi":
            r["wifi"] = _parse_wifi(r["text"])
        uniq.append(r)
    return {"width": w, "height": h, "results": uniq}


@router.post("/decode")
async def decode(file: UploadFile = File(...)):
    data = await file.read()
    if not data:
        return _err("Fichier vide.")
    if len(data) > 30 * 1024 * 1024:
        return _err("Image trop volumineuse (max 30 Mo).")
    try:
        import cv2  # noqa: F401
        import numpy  # noqa: F401
    except ImportError:
        return _missing("opencv-python-headless")
    try:
        r = await asyncio.to_thread(_decode, data)
    except ValueError as e:
        return _err(str(e))
    if not r["results"]:
        return {**r, "message": "Aucun QR code détecté dans cette image."}
    return r
