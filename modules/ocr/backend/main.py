import asyncio
import base64
import io
import logging
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

from ledroide_sdk import Module

log = logging.getLogger("ledroide.ocr")

module = Module(__file__)
router = module.router

IS_WINDOWS = sys.platform == "win32"
NO_WINDOW = subprocess.CREATE_NO_WINDOW if IS_WINDOWS else 0
UPLOAD_DIR = module.data_dir / "uploads"
UPLOAD_TTL_S = 24 * 3600
MAX_UPLOAD_BYTES = 200 * 1024 * 1024
MAX_PIXELS = 120_000_000
PREVIEW_SIDE = 1600
PAGE_TIMEOUT_S = 300
IMAGE_EXT = {".png", ".jpg", ".jpeg", ".bmp", ".gif", ".tif", ".tiff", ".webp", ".jfif", ".pbm", ".pgm", ".ppm"}
LANG_RE = re.compile(r"^[A-Za-z0-9_\-]+$")
HELP_URLS = {
    "installer": "https://github.com/UB-Mannheim/tesseract/wiki",
    "tessdata": "https://github.com/tesseract-ocr/tessdata",
    "tessdata_best": "https://github.com/tesseract-ocr/tessdata_best",
}
LANG_NAMES = {
    "fra": "Français", "eng": "Anglais", "deu": "Allemand", "spa": "Espagnol", "ita": "Italien",
    "por": "Portugais", "nld": "Néerlandais", "ara": "Arabe", "rus": "Russe", "chi_sim": "Chinois simplifié",
    "chi_tra": "Chinois traditionnel", "jpn": "Japonais", "kor": "Coréen", "pol": "Polonais", "tur": "Turc",
    "osd": "Orientation (OSD)", "equ": "Équations", "lat": "Latin", "grc": "Grec ancien", "ell": "Grec",
}

_uploads: dict[str, dict] = {}
_tess_cache: dict = {}


class OcrError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _fail(exc: OcrError) -> dict:
    return {"error": exc.message, "code": exc.code}


# ── Tesseract discovery ───────────────────────────────────────────────────────

def _candidates() -> list[str]:
    out = []
    configured = str(module.config.get("tesseract_path") or "").strip().strip('"')
    if configured:
        out.append(configured)
    found = shutil.which("tesseract")
    if found:
        out.append(found)
    if IS_WINDOWS:
        for base in (os.environ.get("ProgramFiles", r"C:\Program Files"),
                     os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
                     os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs"),
                     os.path.join(os.environ.get("LOCALAPPDATA", ""), "Tesseract-OCR")):
            if base:
                out.append(os.path.join(base, "Tesseract-OCR", "tesseract.exe"))
    return out


def _run(args: list[str], timeout: float = 15) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, timeout=timeout, creationflags=NO_WINDOW)


def _probe_tesseract(path: str) -> dict | None:
    p = Path(path)
    if p.is_dir():
        p = p / ("tesseract.exe" if IS_WINDOWS else "tesseract")
    if not p.is_file():
        return None
    try:
        ver = _run([str(p), "--version"])
        langs = _run([str(p), "--list-langs"])
    except (OSError, subprocess.TimeoutExpired):
        return None
    out = (ver.stdout or ver.stderr).decode("utf-8", "replace")
    m = re.search(r"tesseract\s+v?([\d.]+\S*)", out, re.I)
    lang_lines = langs.stdout.decode("utf-8", "replace").splitlines()
    tessdata = None
    if lang_lines:
        m2 = re.search(r'"(.+?)"', lang_lines[0])
        tessdata = m2.group(1) if m2 else None
    lang_list = sorted({ln.strip() for ln in lang_lines[1:] if LANG_RE.match(ln.strip())})
    return {"path": str(p), "version": m.group(1) if m else out.split("\n", 1)[0].strip(),
            "langs": lang_list, "tessdata": tessdata}


def _detect(force: bool = False) -> dict | None:
    key = str(module.config.get("tesseract_path") or "")
    if not force and _tess_cache.get("key") == key and time.time() - _tess_cache.get("at", 0) < 60:
        return _tess_cache["info"]
    info = None
    for cand in _candidates():
        info = _probe_tesseract(cand)
        if info:
            break
    _tess_cache.update(key=key, at=time.time(), info=info)
    return info


def _pytesseract(info: dict):
    try:
        import pytesseract
    except ImportError:
        raise OcrError("MISSING_DEP", "Le paquet Python « pytesseract » n'est pas installé. Réactivez le module "
                                      "ou installez-le depuis le menu Bibliothèques.")
    pytesseract.pytesseract.tesseract_cmd = info["path"]
    return pytesseract


def _require_tesseract() -> dict:
    info = _detect()
    if not info:
        raise OcrError("TESSERACT_MISSING", "Tesseract est introuvable. Installez-le (voir l'aide du module) "
                                            "ou indiquez le chemin de tesseract.exe.")
    return info


# ── Sources (uploads / local paths) ───────────────────────────────────────────

def _cleanup_uploads():
    if not UPLOAD_DIR.exists():
        return
    now = time.time()
    for f in UPLOAD_DIR.iterdir():
        try:
            if now - f.stat().st_mtime > UPLOAD_TTL_S:
                f.unlink()
        except OSError:
            pass


_cleanup_uploads()


def _safe_name(name: str) -> str:
    name = Path(str(name or "fichier")).name
    return re.sub(r"[^\w.\- ]+", "_", name)[:100] or "fichier"


def _resolve(src) -> tuple[Path, str]:
    if not isinstance(src, dict):
        raise OcrError("BAD_SOURCE", "Source manquante")
    if src.get("file_id"):
        up = _uploads.get(str(src["file_id"]))
        if not up or not up.get("done"):
            raise OcrError("NOT_FOUND", "Fichier envoyé introuvable (expiré ?) — rechargez-le.")
        return up["path"], up["name"]
    raw = str(src.get("path") or "").strip().strip('"')
    if not raw:
        raise OcrError("BAD_SOURCE", "Aucun fichier sélectionné")
    p = Path(os.path.expandvars(os.path.expanduser(raw)))
    if not p.is_file():
        raise OcrError("NOT_FOUND", f"Fichier introuvable : {raw}")
    return p, p.name


def _is_pdf(path: Path) -> bool:
    if path.suffix.lower() == ".pdf":
        return True
    try:
        with open(path, "rb") as f:
            return f.read(5) == b"%PDF-"
    except OSError:
        return False


def _pdfium():
    try:
        import pypdfium2 as pdfium
    except ImportError:
        raise OcrError("MISSING_DEP", "Le paquet Python « pypdfium2 » n'est pas installé (nécessaire pour les PDF). "
                                      "Réactivez le module ou installez-le depuis le menu Bibliothèques.")
    return pdfium


def _open_pdf(path: Path):
    pdfium = _pdfium()
    try:
        return pdfium.PdfDocument(str(path))
    except Exception as exc:
        msg = str(exc)
        if "password" in msg.lower():
            raise OcrError("PDF_PASSWORD", "Ce PDF est protégé par un mot de passe.")
        raise OcrError("BAD_PDF", f"PDF illisible : {msg}")


def _open_image(path: Path):
    from PIL import Image, UnidentifiedImageError
    Image.MAX_IMAGE_PIXELS = MAX_PIXELS
    try:
        return Image.open(path)
    except UnidentifiedImageError:
        raise OcrError("BAD_IMAGE", "Format d'image non reconnu")
    except Image.DecompressionBombError:
        raise OcrError("TOO_LARGE", "Image trop grande")
    except OSError as exc:
        raise OcrError("BAD_IMAGE", f"Image illisible : {exc}")


def _file_info(path: Path, name: str) -> dict:
    if _is_pdf(path):
        pdf = _open_pdf(path)
        try:
            n = len(pdf)
            w, h = pdf[0].get_size() if n else (0, 0)
            has_text = False
            for i in range(min(n, 3)):
                page = pdf[i]
                tp = page.get_textpage()
                if len(tp.get_text_range().strip()) > 20:
                    has_text = True
                tp.close()
                page.close()
                if has_text:
                    break
        finally:
            pdf.close()
        return {"name": name, "kind": "pdf", "pages": n, "width": round(w), "height": round(h),
                "text_layer": has_text, "size": path.stat().st_size}
    img = _open_image(path)
    try:
        return {"name": name, "kind": "image", "pages": getattr(img, "n_frames", 1), "width": img.width,
                "height": img.height, "format": img.format, "text_layer": False, "size": path.stat().st_size}
    finally:
        img.close()


def _load_page(path: Path, index: int, dpi: int):
    """Returns a PIL image for page `index` (0-based) of a PDF or (multi-frame) image."""
    from PIL import ImageOps
    if _is_pdf(path):
        pdf = _open_pdf(path)
        try:
            if not 0 <= index < len(pdf):
                raise OcrError("BAD_PAGE", f"Page {index + 1} inexistante")
            page = pdf[index]
            bitmap = page.render(scale=dpi / 72)
            img = bitmap.to_pil().convert("RGB")
            page.close()
            return img
        finally:
            pdf.close()
    img = _open_image(path)
    try:
        n = getattr(img, "n_frames", 1)
        if not 0 <= index < n:
            raise OcrError("BAD_PAGE", f"Page {index + 1} inexistante")
        if n > 1:
            img.seek(index)
        img.load()
        out = ImageOps.exif_transpose(img)
        if out.mode in ("RGBA", "LA", "P"):
            out = out.convert("RGBA")
            from PIL import Image
            bg = Image.new("RGB", out.size, (255, 255, 255))
            bg.paste(out, mask=out.split()[3])
            return bg
        if out.mode not in ("RGB", "L"):
            out = out.convert("RGB")
        return out.copy()
    finally:
        img.close()


def _page_text_layer(path: Path, index: int) -> str:
    pdf = _open_pdf(path)
    try:
        page = pdf[index]
        tp = page.get_textpage()
        text = tp.get_text_range()
        tp.close()
        page.close()
        return text.replace("\r\n", "\n").replace("\r", "\n")
    finally:
        pdf.close()


def _parse_pages(spec: str, total: int) -> list[int]:
    spec = str(spec or "").strip()
    if not spec:
        return list(range(total))
    pages: list[int] = []
    for part in spec.replace(";", ",").split(","):
        part = part.strip()
        if not part:
            continue
        m = re.fullmatch(r"(\d+)\s*-\s*(\d*)", part)
        if m:
            a = int(m.group(1))
            b = int(m.group(2)) if m.group(2) else total
        elif part.isdigit():
            a = b = int(part)
        else:
            raise OcrError("BAD_PAGES", f"Plage de pages invalide : « {part} » (ex. 1-3,5)")
        if a < 1 or b < a:
            raise OcrError("BAD_PAGES", f"Plage de pages invalide : « {part} »")
        for p in range(a, min(b, total) + 1):
            if p - 1 not in pages:
                pages.append(p - 1)
    if not pages:
        raise OcrError("BAD_PAGES", f"Aucune page valide (le document en compte {total})")
    return pages


# ── Preprocessing (Pillow) ────────────────────────────────────────────────────

def _otsu(gray) -> int:
    import numpy as np
    hist = np.bincount(np.asarray(gray, dtype=np.uint8).ravel(), minlength=256).astype(float)
    total = hist.sum()
    if total == 0:
        return 128
    levels = np.arange(256)
    w0 = np.cumsum(hist)
    w1 = total - w0
    m0 = np.cumsum(hist * levels)
    mt = m0[-1]
    with np.errstate(divide="ignore", invalid="ignore"):
        between = (mt * w0 / total - m0) ** 2 / (w0 * w1)
    between = np.nan_to_num(between)
    return int(np.argmax(between))


def preprocess(img, opts: dict | None):
    from PIL import Image, ImageEnhance, ImageFilter, ImageOps
    o = opts or {}
    rotate = int(o.get("rotate") or 0) % 360
    if rotate:
        img = img.rotate(-rotate, expand=True)
    angle = float(o.get("angle") or 0)
    if angle:
        angle = max(-45.0, min(45.0, angle))
        fill = 255 if img.mode == "L" else (255, 255, 255)
        img = img.rotate(-angle, expand=True, resample=Image.BICUBIC, fillcolor=fill)
    scale = float(o.get("scale") or 1)
    if scale != 1:
        scale = max(0.25, min(4.0, scale))
        new = (max(1, round(img.width * scale)), max(1, round(img.height * scale)))
        if new[0] * new[1] > MAX_PIXELS:
            raise OcrError("TOO_LARGE", "Agrandissement trop important pour cette image")
        img = img.resize(new, Image.LANCZOS)
    threshold = o.get("threshold")
    binarize = threshold not in (None, "", False, "none")
    if o.get("grayscale") or binarize:
        img = ImageOps.grayscale(img)
    if o.get("denoise"):
        img = img.filter(ImageFilter.MedianFilter(3))
    if o.get("autocontrast"):
        img = ImageOps.autocontrast(img, cutoff=1)
    contrast = float(o.get("contrast") or 1)
    if contrast != 1:
        img = ImageEnhance.Contrast(img).enhance(max(0.1, min(5.0, contrast)))
    brightness = float(o.get("brightness") or 1)
    if brightness != 1:
        img = ImageEnhance.Brightness(img).enhance(max(0.1, min(3.0, brightness)))
    if o.get("sharpen"):
        img = img.filter(ImageFilter.SHARPEN)
    if binarize:
        t = _otsu(img) if str(threshold) == "otsu" else max(0, min(255, int(threshold)))
        img = img.point(lambda v: 255 if v > t else 0, mode="L")
    if o.get("invert"):
        img = ImageOps.invert(img if img.mode in ("L", "RGB") else img.convert("L"))
    return img


def _to_data_url(img, max_side: int = PREVIEW_SIDE) -> tuple[str, float]:
    disp = img
    ratio = 1.0
    if max(img.size) > max_side:
        ratio = max_side / max(img.size)
        disp = img.resize((max(1, round(img.width * ratio)), max(1, round(img.height * ratio))))
    buf = io.BytesIO()
    if disp.mode == "L" or disp.mode == "1":
        disp.convert("L").save(buf, "PNG", optimize=False)
        mime = "image/png"
    else:
        disp.convert("RGB").save(buf, "JPEG", quality=85)
        mime = "image/jpeg"
    return f"data:{mime};base64,{base64.b64encode(buf.getvalue()).decode()}", ratio


# ── OCR ───────────────────────────────────────────────────────────────────────

def _lang_arg(langs, info: dict) -> str:
    if isinstance(langs, str):
        langs = [x for x in re.split(r"[+,\s]+", langs) if x]
    langs = [str(x) for x in (langs or []) if LANG_RE.match(str(x)) and str(x) != "osd"]
    if not langs:
        defaults = [x for x in re.split(r"[+,\s]+", str(module.config.get("default_langs") or "")) if x]
        langs = [x for x in defaults if x in info["langs"]] or [x for x in info["langs"] if x != "osd"][:1]
    missing = [x for x in langs if x not in info["langs"]]
    if missing:
        raise OcrError("LANG_MISSING", f"Langue non installée dans Tesseract : {', '.join(missing)}. "
                                       "Ajoutez le fichier <langue>.traineddata dans le dossier tessdata.")
    if not langs:
        raise OcrError("LANG_MISSING", "Aucune langue Tesseract installée")
    return "+".join(langs)


def _ocr_image(img, info: dict, lang: str, psm: int, want_words: bool) -> dict:
    pt = _pytesseract(info)
    try:
        data = pt.image_to_data(img, lang=lang, config=f"--psm {psm}", output_type=pt.Output.DICT,
                                timeout=PAGE_TIMEOUT_S)
    except RuntimeError as exc:
        if "timeout" in str(exc).lower():
            raise OcrError("TIMEOUT", f"Tesseract a dépassé {PAGE_TIMEOUT_S} s sur cette page")
        raise OcrError("TESSERACT_FAILED", f"Échec de Tesseract : {exc}")
    except pt.TesseractError as exc:
        raise OcrError("TESSERACT_FAILED", f"Échec de Tesseract : {exc.message or exc}")
    except pt.TesseractNotFoundError:
        _tess_cache.clear()
        raise OcrError("TESSERACT_MISSING", "Tesseract est introuvable")

    lines: list[str] = []
    words = []
    confs = []
    cur_key = None
    cur_words: list[str] = []
    last_block = None
    for i, text in enumerate(data["text"]):
        if data["level"][i] != 5:
            continue
        key = (data["block_num"][i], data["par_num"][i], data["line_num"][i])
        if key != cur_key:
            if cur_words:
                lines.append(" ".join(cur_words))
            if last_block is not None and key[:2] != last_block:
                lines.append("")
            cur_key, cur_words, last_block = key, [], key[:2]
        t = (text or "").strip()
        if not t:
            continue
        cur_words.append(t)
        conf = float(data["conf"][i])
        if conf >= 0:
            confs.append(conf)
        if want_words:
            words.append({"t": t, "c": round(conf), "x": data["left"][i], "y": data["top"][i],
                          "w": data["width"][i], "h": data["height"][i]})
    if cur_words:
        lines.append(" ".join(cur_words))
    text = re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
    return {"text": text, "words": words, "confidence": round(sum(confs) / len(confs), 1) if confs else None,
            "word_count": len(confs)}


@module.on("status")
async def status(payload: dict):
    info = await asyncio.to_thread(_detect, bool(payload.get("refresh")))
    deps = {}
    for name in ("pytesseract", "pypdfium2"):
        import importlib.util
        deps[name] = importlib.util.find_spec(name) is not None
    langs = [{"code": c, "name": LANG_NAMES.get(c, c)} for c in (info or {}).get("langs", [])]
    default = [x for x in re.split(r"[+,\s]+", str(module.config.get("default_langs") or "")) if x]
    return {
        "installed": bool(info),
        "tesseract": info,
        "langs": langs,
        "default_langs": [x for x in default if info and x in info["langs"]],
        "has_fra": bool(info and "fra" in info["langs"]),
        "deps": deps,
        "configured_path": str(module.config.get("tesseract_path") or ""),
        "searched": _candidates(),
        "pdf_dpi": int(module.config.get("pdf_dpi", 300)),
    }


@module.on("set_tesseract_path")
async def set_tesseract_path(payload: dict):
    path = str(payload.get("path") or "").strip().strip('"')
    if path:
        info = await asyncio.to_thread(_probe_tesseract, path)
        if not info:
            return {"error": f"Aucun exécutable Tesseract valide à cet emplacement : {path}", "code": "NOT_FOUND"}
        path = info["path"]
    module.config.set("tesseract_path", path)
    _tess_cache.clear()
    return await status({"refresh": True})


@module.on("open_help")
async def open_help(payload: dict):
    url = HELP_URLS.get(str(payload.get("what") or ""))
    if not url:
        return {"error": "Lien inconnu", "code": "BAD_REQUEST"}
    if IS_WINDOWS:
        await asyncio.to_thread(os.startfile, url)
    else:
        import webbrowser
        await asyncio.to_thread(webbrowser.open, url)
    return {"ok": True, "url": url}


@module.on("open_tessdata")
async def open_tessdata(payload: dict):
    info = await asyncio.to_thread(_detect)
    if not info or not info.get("tessdata") or not Path(info["tessdata"]).is_dir():
        return {"error": "Dossier tessdata introuvable", "code": "NOT_FOUND"}
    if not IS_WINDOWS:
        return {"error": "Fonction disponible uniquement sous Windows", "code": "UNSUPPORTED"}
    await asyncio.to_thread(os.startfile, info["tessdata"])
    return {"ok": True}


# ── Chunked upload through the bridge ─────────────────────────────────────────

@module.on("upload_begin")
async def upload_begin(payload: dict):
    size = int(payload.get("size") or 0)
    if size <= 0:
        return {"error": "Fichier vide", "code": "EMPTY_FILE"}
    if size > MAX_UPLOAD_BYTES:
        return {"error": f"Fichier trop volumineux (max. {MAX_UPLOAD_BYTES // 1024 // 1024} Mo)", "code": "TOO_LARGE"}
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    uid = uuid.uuid4().hex
    name = _safe_name(payload.get("name"))
    path = UPLOAD_DIR / f"{uid}_{name}"
    path.write_bytes(b"")
    _uploads[uid] = {"path": path, "name": name, "size": size, "received": 0, "done": False,
                     "lock": threading.Lock()}
    return {"upload_id": uid}


@module.on("upload_chunk")
async def upload_chunk(payload: dict):
    up = _uploads.get(str(payload.get("upload_id") or ""))
    if not up or up["done"]:
        return {"error": "Envoi inconnu ou terminé", "code": "NOT_FOUND"}
    try:
        data = base64.b64decode(str(payload.get("data") or ""), validate=True)
    except ValueError:
        return {"error": "Morceau de fichier invalide", "code": "BAD_CHUNK"}
    offset = int(payload.get("offset", up["received"]))
    if offset != up["received"] or up["received"] + len(data) > up["size"]:
        return {"error": "Morceau de fichier hors séquence", "code": "BAD_CHUNK"}

    def _write():
        with up["lock"], open(up["path"], "ab") as f:
            f.write(data)
    await asyncio.to_thread(_write)
    up["received"] += len(data)
    return {"received": up["received"]}


@module.on("upload_end")
async def upload_end(payload: dict):
    uid = str(payload.get("upload_id") or "")
    up = _uploads.get(uid)
    if not up:
        return {"error": "Envoi inconnu", "code": "NOT_FOUND"}
    if up["received"] != up["size"]:
        return {"error": "Fichier incomplet", "code": "INCOMPLETE"}
    up["done"] = True
    try:
        info = await asyncio.to_thread(_file_info, up["path"], up["name"])
    except OcrError as exc:
        await _discard(uid)
        return _fail(exc)
    return {"file_id": uid, **info}


async def _discard(uid: str):
    up = _uploads.pop(uid, None)
    if up:
        try:
            await asyncio.to_thread(up["path"].unlink, True)
        except OSError:
            pass


@module.on("upload_discard")
async def upload_discard(payload: dict):
    await _discard(str(payload.get("file_id") or payload.get("upload_id") or ""))
    return {"ok": True}


@module.on("open_path")
async def open_path(payload: dict):
    try:
        path, name = _resolve({"path": payload.get("path")})
        info = await asyncio.to_thread(_file_info, path, name)
    except OcrError as exc:
        return _fail(exc)
    if info["kind"] == "image" and path.suffix.lower() not in IMAGE_EXT and not info.get("format"):
        return {"error": "Format non pris en charge", "code": "BAD_IMAGE"}
    return {"path": str(path), **info}


@module.on("preview")
async def preview(payload: dict):
    try:
        path, _ = _resolve(payload.get("source"))
        page = int(payload.get("page") or 0)
        dpi = int(module.config.get("pdf_dpi", 300))

        def _work():
            img = preprocess(_load_page(path, page, dpi), payload.get("preprocess"))
            url, ratio = _to_data_url(img)
            return {"image": url, "width": img.width, "height": img.height, "ratio": ratio}
        return await asyncio.to_thread(_work)
    except OcrError as exc:
        return _fail(exc)
    except (ValueError, TypeError) as exc:
        return {"error": f"Paramètres invalides : {exc}", "code": "BAD_REQUEST"}


@module.on("detect_orientation")
async def detect_orientation(payload: dict):
    try:
        info = await asyncio.to_thread(_require_tesseract)
        if "osd" not in info["langs"]:
            return {"error": "Le fichier osd.traineddata est absent du dossier tessdata", "code": "LANG_MISSING"}
        path, _ = _resolve(payload.get("source"))
        page = int(payload.get("page") or 0)

        def _work():
            pt = _pytesseract(info)
            img = _load_page(path, page, int(module.config.get("pdf_dpi", 300)))
            try:
                osd = pt.image_to_osd(img, output_type=pt.Output.DICT, timeout=60)
            except pt.TesseractError as exc:
                raise OcrError("OSD_FAILED", "Orientation non détectable (trop peu de texte ?) : "
                                             f"{exc.message or exc}")
            return {"rotate": int(osd.get("rotate", 0)) % 360, "confidence": osd.get("orientation_conf"),
                    "script": osd.get("script")}
        return await asyncio.to_thread(_work)
    except OcrError as exc:
        return _fail(exc)


@module.job("ocr")
async def ocr(payload: dict, job):
    try:
        info = await asyncio.to_thread(_require_tesseract)
        _pytesseract(info)
        lang = _lang_arg(payload.get("langs"), info)
        path, name = _resolve(payload.get("source"))
        psm = int(payload.get("psm") or 3)
        if psm not in (1, 3, 4, 6, 7, 8, 11, 12, 13):
            raise OcrError("BAD_REQUEST", "Mode de segmentation (PSM) invalide")
        dpi = int(module.config.get("pdf_dpi", 300))
        meta = await asyncio.to_thread(_file_info, path, name)
        pages = _parse_pages(payload.get("pages"), meta["pages"])
    except OcrError as exc:
        raise RuntimeError(exc.message) from exc

    want_words = bool(payload.get("boxes"))
    use_layer = bool(payload.get("use_text_layer")) and meta["kind"] == "pdf"
    opts = payload.get("preprocess") or {}
    results = []
    started = time.perf_counter()
    for n, idx in enumerate(pages):
        await job.progress(round(n / len(pages) * 100), f"Page {idx + 1} ({n + 1}/{len(pages)})…",
                           page=idx + 1, done=n, total=len(pages))
        try:
            if use_layer:
                native = await asyncio.to_thread(_page_text_layer, path, idx)
                if len(native.strip()) > 20:
                    results.append({"page": idx + 1, "text": native.strip(), "words": [], "confidence": None,
                                    "source": "pdf", "width": None, "height": None})
                    continue

            def _work():
                img = preprocess(_load_page(path, idx, dpi), opts)
                r = _ocr_image(img, info, lang, psm, want_words)
                r.update(width=img.width, height=img.height)
                return r
            r = await asyncio.to_thread(_work)
        except OcrError as exc:
            raise RuntimeError(f"Page {idx + 1} : {exc.message}") from exc
        results.append({"page": idx + 1, "source": "ocr", **r})
        await module.events.emit("ocr:page", {"job_id": job.id, "page": results[-1]})

    log.info("OCR %s : %d page(s), %s, %.1f s", meta["kind"], len(results), lang, time.perf_counter() - started)
    sep = "\n\n"
    full = sep.join((f"── Page {r['page']} ──\n{r['text']}" if len(results) > 1 else r["text"]) for r in results)
    return {"name": name, "kind": meta["kind"], "lang": lang, "pages": results, "text": full,
            "elapsed_s": round(time.perf_counter() - started, 2)}


@module.on_shutdown
async def _shutdown():
    for uid in list(_uploads):
        await _discard(uid)
