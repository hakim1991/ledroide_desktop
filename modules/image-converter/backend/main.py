import asyncio
import io
import re
import tempfile
import time
import uuid
import zipfile
from pathlib import Path
from typing import List

from ledroide_sdk.http import File, Form, UploadFile
from ledroide_sdk.http import FileResponse, JSONResponse
from ledroide_sdk import Module

module = Module(__file__)
router = module.router

TEMP_DIR = Path(tempfile.gettempdir()) / "ledroide_image-converter"
TEMP_DIR.mkdir(parents=True, exist_ok=True)
MAX_AGE = 3600

FORMATS = {
    # clé : (format Pillow, extension, supporte alpha, supporte EXIF)
    "png": ("PNG", "png", True, True),
    "jpg": ("JPEG", "jpg", False, True),
    "webp": ("WEBP", "webp", True, True),
    "bmp": ("BMP", "bmp", False, False),
    "ico": ("ICO", "ico", True, False),
}
ICO_SIZES = [16, 24, 32, 48, 64, 128, 256]

MISSING = {"error": "Pillow n'est pas installé. Installez les dépendances du module.", "code": "MISSING_DEP"}


def _err(msg, status=400):
    return JSONResponse(status_code=status, content={"error": msg})


def _cleanup():
    now = time.time()
    for p in TEMP_DIR.iterdir():
        try:
            if p.is_file() and now - p.stat().st_mtime > MAX_AGE:
                p.unlink()
        except OSError:
            pass


def _safe_stem(name: str) -> str:
    stem = Path(name or "image").stem
    stem = re.sub(r'[^\w\-. ]+', "_", stem).strip(" ._") or "image"
    return stem[:80]


def _target_size(w, h, mode, max_w, max_h, percent):
    if mode == "percent" and percent and percent != 100:
        f = percent / 100.0
        return max(1, round(w * f)), max(1, round(h * f))
    if mode == "max" and (max_w or max_h):
        f = 1.0
        if max_w and w > max_w:
            f = min(f, max_w / w)
        if max_h and h > max_h:
            f = min(f, max_h / h)
        if f < 1.0:
            return max(1, round(w * f)), max(1, round(h * f))
    return w, h


def convert_one(data: bytes, fmt: str, resize_mode: str, max_w: int, max_h: int,
                percent: float, quality: int, strip_exif: bool):
    from PIL import Image, ImageOps

    pil_fmt, ext, alpha_ok, exif_ok = FORMATS[fmt]
    im = Image.open(io.BytesIO(data))
    im.load()
    src_fmt = im.format or "?"
    exif = im.info.get("exif") if not strip_exif else None
    icc = im.info.get("icc_profile")
    if strip_exif:
        # applique l'orientation EXIF aux pixels avant de supprimer les métadonnées
        im = ImageOps.exif_transpose(im)

    has_alpha = im.mode in ("RGBA", "LA", "PA") or (im.mode == "P" and "transparency" in im.info)
    if has_alpha and alpha_ok:
        im = im.convert("RGBA")
    elif has_alpha:
        rgba = im.convert("RGBA")
        bg = Image.new("RGB", rgba.size, (255, 255, 255))
        bg.paste(rgba, mask=rgba.split()[-1])
        im = bg
    elif im.mode not in ("RGB", "L"):
        im = im.convert("RGB")
    elif im.mode == "L" and fmt == "ico":
        im = im.convert("RGBA")

    w0, h0 = im.size
    w, h = _target_size(w0, h0, resize_mode, max_w, max_h, percent)
    if (w, h) != (w0, h0):
        im = im.resize((w, h), Image.LANCZOS)

    kw = {}
    if fmt == "ico":
        if max(im.size) > 256:
            im.thumbnail((256, 256), Image.LANCZOS)
        side = max(im.size)
        if im.size[0] != im.size[1]:  # icône carrée, fond transparent
            sq = Image.new("RGBA", (side, side), (0, 0, 0, 0))
            sq.paste(im, ((side - im.size[0]) // 2, (side - im.size[1]) // 2))
            im = sq
        kw["sizes"] = [(s, s) for s in ICO_SIZES if s <= side] or [(side, side)]
    elif fmt == "jpg":
        kw.update(quality=quality, optimize=True, progressive=True)
    elif fmt == "webp":
        kw.update(quality=quality, method=6)
    elif fmt == "png":
        kw.update(optimize=True)
    if exif and exif_ok:
        kw["exif"] = exif
    if icc and fmt in ("png", "jpg", "webp"):
        kw["icc_profile"] = icc  # profil couleur conservé (pas une donnée privée)

    out = io.BytesIO()
    im.save(out, pil_fmt, **kw)
    return out.getvalue(), ext, {"src_format": src_fmt, "src_w": w0, "src_h": h0, "w": im.size[0], "h": im.size[1]}


@router.get("/health")
async def health():
    try:
        import PIL
        return {"ok": True, "pillow": PIL.__version__}
    except ImportError:
        return {"ok": False}


@router.post("/convert")
async def convert(
    files: List[UploadFile] = File(...),
    format: str = Form("png"),
    resize_mode: str = Form("none"),  # none | max | percent
    max_width: int = Form(0),
    max_height: int = Form(0),
    percent: float = Form(100),
    quality: int = Form(85),
    strip_exif: bool = Form(False),
):
    fmt = format.lower().replace("jpeg", "jpg")
    if fmt not in FORMATS:
        return _err("Format de sortie non pris en charge.")
    if resize_mode not in ("none", "max", "percent"):
        return _err("Mode de redimensionnement invalide.")
    if resize_mode == "percent" and not (1 <= percent <= 1000):
        return _err("Le pourcentage doit être compris entre 1 et 1000.")
    if max_width < 0 or max_height < 0:
        return _err("Dimensions invalides.")
    quality = max(1, min(100, int(quality)))
    blobs = [(f.filename or "image", await f.read()) for f in files]
    if not blobs:
        return _err("Aucun fichier reçu.")

    try:
        import PIL  # noqa: F401
    except ImportError:
        return JSONResponse(status_code=400, content=MISSING)

    def _run():
        _cleanup()
        results, outputs, used = [], [], set()
        for name, data in blobs:
            entry = {"name": name, "before": len(data)}
            try:
                out, ext, meta = convert_one(data, fmt, resize_mode, max_width, max_height,
                                             percent, quality, strip_exif)
            except Exception as e:
                msg = "format non reconnu" if "cannot identify" in str(e) else str(e)
                entry["error"] = f"Impossible de convertir : {msg}"
                results.append(entry)
                continue
            oname = f"{_safe_stem(name)}.{ext}"
            k = 2
            while oname.lower() in used:
                oname = f"{_safe_stem(name)}_{k}.{ext}"
                k += 1
            used.add(oname.lower())
            entry.update(meta, after=len(out), out_name=oname)
            results.append(entry)
            outputs.append((oname, out))

        if not outputs:
            return {"error": "Aucune image n'a pu être convertie.", "results": results}
        tag = uuid.uuid4().hex[:6]
        if len(outputs) == 1:
            oname, out = outputs[0]
            fname = f"{Path(oname).stem}_{tag}{Path(oname).suffix}"
            (TEMP_DIR / fname).write_bytes(out)
        else:
            fname = f"images_{fmt}_{tag}.zip"
            with zipfile.ZipFile(TEMP_DIR / fname, "w", zipfile.ZIP_STORED) as zf:
                for oname, out in outputs:
                    zf.writestr(oname, out)
        return {
            "filename": fname, "results": results,
            "converted": len(outputs), "failed": len(results) - len(outputs),
            "total_before": sum(r["before"] for r in results if "error" not in r),
            "total_after": sum(r["after"] for r in results if "error" not in r),
            "download_size": (TEMP_DIR / fname).stat().st_size,
        }

    try:
        res = await asyncio.to_thread(_run)
    except Exception as e:
        return _err(f"Erreur de conversion : {e}", 500)
    if "error" in res:
        return JSONResponse(status_code=400, content=res)
    return res


MIME = {"png": "image/png", "jpg": "image/jpeg", "webp": "image/webp", "bmp": "image/bmp",
        "ico": "image/x-icon", "zip": "application/zip"}


@router.get("/file/{filename}")
async def get_file(filename: str):
    if "/" in filename or "\\" in filename or ".." in filename:
        return _err("Nom de fichier invalide.")
    path = TEMP_DIR / filename
    if not path.is_file():
        return _err("Fichier introuvable ou expiré.", 404)
    ext = path.suffix.lstrip(".").lower()
    nice = re.sub(r"_[0-9a-f]{6}(\.\w+)$", r"\1", filename)
    return FileResponse(path, media_type=MIME.get(ext, "application/octet-stream"), filename=nice)
