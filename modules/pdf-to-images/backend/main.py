import asyncio
import io
import time
import zipfile
from pathlib import Path
import tempfile

from ledroide_sdk.http import UploadFile, File
from ledroide_sdk.http import FileResponse, JSONResponse
from ledroide_sdk import Module

module = Module(__file__)
router = module.router

TEMP_DIR = Path(tempfile.gettempdir()) / "ledroide_pdf_to_images"
TEMP_DIR.mkdir(parents=True, exist_ok=True)


@router.post("/convert")
async def convert(file: UploadFile = File(...)):
    pdf_bytes = await file.read()

    def _convert():
        try:
            from pdf2image import convert_from_bytes
        except ImportError:
            return {"error": "pdf2image non installé.", "code": "MISSING_DEP"}

        try:
            pages = convert_from_bytes(pdf_bytes, dpi=150)
        except Exception as e:
            msg = str(e)
            if "poppler" in msg.lower() or "pdftoppm" in msg.lower() or "pdfinfo" in msg.lower():
                return {"error": "Poppler non installé. Installez-le et redémarrez.", "code": "POPPLER_MISSING"}
            return {"error": msg, "code": "CONVERT_ERROR"}

        ts = int(time.time())
        zip_name = f"pages_{ts}.zip"
        zip_path = TEMP_DIR / zip_name

        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for i, page in enumerate(pages, 1):
                buf = io.BytesIO()
                page.save(buf, format="PNG")
                zf.writestr(f"page_{i:03d}.png", buf.getvalue())

        return {"filename": zip_name, "page_count": len(pages)}

    result = await asyncio.to_thread(_convert)
    if "error" in result:
        return JSONResponse(status_code=400, content=result)
    return result


@router.get("/file/{filename}")
async def get_file(filename: str):
    path = TEMP_DIR / filename
    if not path.exists():
        return JSONResponse(status_code=404, content={"error": "Fichier introuvable"})
    mt = "application/zip" if filename.endswith(".zip") else "image/png"
    return FileResponse(path, media_type=mt, headers={"Content-Disposition": f"attachment; filename={filename}"})
