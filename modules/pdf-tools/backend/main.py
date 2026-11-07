import asyncio
import io
import json
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

TEMP_DIR = Path(tempfile.gettempdir()) / "ledroide_pdf-tools"
TEMP_DIR.mkdir(parents=True, exist_ok=True)
MAX_AGE = 3600  # secondes avant nettoyage des fichiers générés

MISSING = {"error": "pypdf n'est pas installé. Installez les dépendances du module.", "code": "MISSING_DEP"}


class PdfError(Exception):
    pass


def _err(msg: str, status: int = 400, code: str = None):
    content = {"error": msg}
    if code:
        content["code"] = code
    return JSONResponse(status_code=status, content=content)


def _cleanup():
    now = time.time()
    for p in TEMP_DIR.iterdir():
        try:
            if p.is_file() and now - p.stat().st_mtime > MAX_AGE:
                p.unlink()
        except OSError:
            pass


def _safe_stem(name: str) -> str:
    stem = Path(name or "document").stem
    stem = re.sub(r'[^\w\-. ]+', "_", stem).strip(" ._") or "document"
    return stem[:60]


def _out_path(base: str, ext: str) -> Path:
    return TEMP_DIR / f"{base}_{uuid.uuid4().hex[:6]}.{ext}"


def _reader(data: bytes, name: str = "fichier"):
    from pypdf import PdfReader
    try:
        r = PdfReader(io.BytesIO(data))
        if r.is_encrypted:
            try:
                if not r.decrypt(""):
                    raise PdfError(f"« {name} » est protégé par mot de passe.")
            except PdfError:
                raise
            except Exception:
                raise PdfError(f"« {name} » est protégé par mot de passe.")
        _ = len(r.pages)
        return r
    except PdfError:
        raise
    except Exception:
        raise PdfError(f"« {name} » n'est pas un PDF valide.")


def _write(writer, path: Path):
    with open(path, "wb") as f:
        writer.write(f)


def parse_ranges(spec: str, n: int):
    """'1-3,5,7-' -> [(1,3),(5,5),(7,n)] (1-based, inclusive)."""
    spec = (spec or "").replace(" ", "")
    if not spec:
        raise PdfError("Indiquez au moins une plage de pages (ex. 1-3,5).")
    out = []
    for part in spec.split(","):
        if not part:
            continue
        m = re.fullmatch(r"(\d*)-(\d*)|(\d+)", part)
        if not m:
            raise PdfError(f"Plage invalide : « {part} ».")
        if m.group(3):
            a = b = int(m.group(3))
        else:
            a = int(m.group(1)) if m.group(1) else 1
            b = int(m.group(2)) if m.group(2) else n
        if a < 1 or b > n or a > b:
            raise PdfError(f"Plage « {part} » hors limites (le document a {n} page(s)).")
        out.append((a, b))
    if not out:
        raise PdfError("Aucune plage valide.")
    return out


# ───────────────────────── routes ─────────────────────────

@router.get("/health")
async def health():
    try:
        import pypdf  # noqa: F401
        return {"ok": True, "pypdf": pypdf.__version__}
    except ImportError:
        return {"ok": False, "pypdf": None}


@router.post("/info")
async def info(files: List[UploadFile] = File(...)):
    blobs = [(f.filename, await f.read()) for f in files]

    def _run():
        try:
            import pypdf  # noqa: F401
        except ImportError:
            return None
        res = []
        for name, data in blobs:
            try:
                r = _reader(data, name)
                pages = []
                for p in r.pages:
                    box = p.mediabox
                    pages.append({
                        "w": round(float(box.width)), "h": round(float(box.height)),
                        "rotation": int(p.rotation or 0) % 360,
                    })
                res.append({"name": name, "size": len(data), "pages": len(r.pages), "page_info": pages})
            except PdfError as e:
                res.append({"name": name, "size": len(data), "error": str(e)})
        return res

    res = await asyncio.to_thread(_run)
    if res is None:
        return JSONResponse(status_code=400, content=MISSING)
    return {"files": res}


@router.post("/merge")
async def merge(files: List[UploadFile] = File(...)):
    if len(files) < 2:
        return _err("Sélectionnez au moins deux PDF à fusionner.")
    blobs = [(f.filename, await f.read()) for f in files]

    def _run():
        from pypdf import PdfWriter
        writer = PdfWriter()
        total = 0
        for name, data in blobs:
            r = _reader(data, name)
            writer.append(r)
            total += len(r.pages)
        out = _out_path("fusion", "pdf")
        _write(writer, out)
        return {"filename": out.name, "pages": total, "size": out.stat().st_size}

    return await _guard(_run)


@router.post("/split")
async def split(file: UploadFile = File(...), mode: str = Form("ranges"), ranges: str = Form("")):
    data = await file.read()
    stem = _safe_stem(file.filename)

    def _run():
        from pypdf import PdfWriter
        r = _reader(data, file.filename)
        n = len(r.pages)
        if mode == "each":
            parts = [(i, i) for i in range(1, n + 1)]
        else:
            parts = parse_ranges(ranges, n)
        width = len(str(n))
        out = _out_path(f"{stem}_decoupe", "zip")
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
            used = set()
            for a, b in parts:
                w = PdfWriter()
                for i in range(a - 1, b):
                    w.add_page(r.pages[i])
                buf = io.BytesIO()
                w.write(buf)
                label = f"p{a:0{width}d}" if a == b else f"p{a:0{width}d}-{b:0{width}d}"
                fname = f"{stem}_{label}.pdf"
                k = 2
                while fname in used:
                    fname = f"{stem}_{label}_{k}.pdf"
                    k += 1
                used.add(fname)
                zf.writestr(fname, buf.getvalue())
        return {"filename": out.name, "parts": len(parts), "size": out.stat().st_size}

    return await _guard(_run)


@router.post("/reorganize")
async def reorganize(file: UploadFile = File(...), pages: str = Form(...)):
    """pages = JSON list [{"page": 1-based index source, "rotate": 0|90|180|270}, ...] dans l'ordre voulu."""
    data = await file.read()
    stem = _safe_stem(file.filename)
    try:
        spec = json.loads(pages)
        assert isinstance(spec, list)
    except Exception:
        return _err("Liste de pages invalide.")
    if not spec:
        return _err("Le document résultant ne contiendrait aucune page.")

    def _run():
        from pypdf import PdfWriter
        r = _reader(data, file.filename)
        n = len(r.pages)
        w = PdfWriter()
        for item in spec:
            try:
                idx = int(item["page"])
                rot = int(item.get("rotate", 0)) % 360
            except Exception:
                raise PdfError("Entrée de page invalide.")
            if not 1 <= idx <= n:
                raise PdfError(f"Page {idx} inexistante (le document a {n} page(s)).")
            if rot % 90:
                raise PdfError("La rotation doit être un multiple de 90°.")
            page = w.add_page(r.pages[idx - 1])
            if rot:
                page.rotate(rot)
        out = _out_path(f"{stem}_reorganise", "pdf")
        _write(w, out)
        return {"filename": out.name, "pages": len(spec), "size": out.stat().st_size}

    return await _guard(_run)


@router.post("/compress")
async def compress(
    file: UploadFile = File(...),
    images: bool = Form(False),
    quality: int = Form(70),
    max_dim: int = Form(2000),
):
    data = await file.read()
    stem = _safe_stem(file.filename)
    quality = max(10, min(95, int(quality)))
    max_dim = max(0, int(max_dim))

    def _run():
        from pypdf import PdfWriter
        r = _reader(data, file.filename)
        w = PdfWriter(clone_from=r)
        imgs_done = 0
        img_note = None
        if images:
            try:
                from PIL import Image  # noqa: F401
            except ImportError:
                img_note = "Pillow absent : images non recompressées."
            else:
                imgs_done = _recompress_images(w, quality, max_dim)
        for page in w.pages:
            try:
                page.compress_content_streams(level=9)
            except Exception:
                pass
        try:
            w.compress_identical_objects(remove_identicals=True, remove_orphans=True)
        except Exception:
            pass
        out = _out_path(f"{stem}_compresse", "pdf")
        _write(w, out)
        after = out.stat().st_size
        res = {
            "filename": out.name, "before": len(data), "after": after,
            "images_recompressed": imgs_done, "pages": len(r.pages),
        }
        if img_note:
            res["note"] = img_note
        return res

    return await _guard(_run)


def _recompress_images(writer, quality: int, max_dim: int) -> int:
    from PIL import Image
    done = 0
    seen = set()
    for page in writer.pages:
        try:
            images = list(page.images)
        except Exception:
            continue
        for img in images:
            try:
                ref = img.indirect_reference
                if ref is None or ref.idnum in seen:
                    continue
                seen.add(ref.idnum)
                obj = ref.get_object()
                if "/SMask" in obj or "/Mask" in obj:
                    continue  # transparence : on n'y touche pas
                pil = img.image
                if pil is None:
                    continue
                if pil.mode not in ("RGB", "L"):
                    if pil.mode in ("RGBA", "LA", "PA") or "transparency" in pil.info:
                        continue
                    pil = pil.convert("RGB")
                if max_dim and max(pil.size) > max_dim:
                    pil = pil.copy()
                    pil.thumbnail((max_dim, max_dim), Image.LANCZOS)
                buf = io.BytesIO()
                pil.save(buf, "JPEG", quality=quality, optimize=True)
                try:
                    orig_len = len(obj._data)
                except Exception:
                    orig_len = 0
                if orig_len and buf.tell() >= orig_len * 0.95:
                    continue  # pas de gain
                img.replace(pil, quality=quality)
                done += 1
            except Exception:
                continue
    return done


async def _guard(fn):
    try:
        import pypdf  # noqa: F401
    except ImportError:
        return JSONResponse(status_code=400, content=MISSING)
    _cleanup()
    try:
        return await asyncio.to_thread(fn)
    except PdfError as e:
        return _err(str(e))
    except Exception as e:
        return _err(f"Erreur de traitement : {e}", 500)


@router.get("/file/{filename}")
async def get_file(filename: str):
    if "/" in filename or "\\" in filename or ".." in filename:
        return _err("Nom de fichier invalide.")
    path = TEMP_DIR / filename
    if not path.is_file():
        return _err("Fichier introuvable ou expiré.", 404)
    mt = "application/zip" if filename.endswith(".zip") else "application/pdf"
    # nom proposé : retire le suffixe aléatoire
    nice = re.sub(r"_[0-9a-f]{6}(\.\w+)$", r"\1", filename)
    # FileResponse(filename=...) génère « Content-Disposition: attachment » (gère l'UTF-8)
    return FileResponse(path, media_type=mt, filename=nice)
