import asyncio
import time
from pathlib import Path
from typing import List
import tempfile

from ledroide_sdk.http import UploadFile, File, HTTPException
from ledroide_sdk.http import FileResponse
from ledroide_sdk import Module

module = Module(__file__)
router = module.router

TEMP_DIR = Path(tempfile.gettempdir()) / "ledroide_image_to_pdf"
TEMP_DIR.mkdir(parents=True, exist_ok=True)


def _cleanup_old():
    cutoff = time.time() - 600  # 10 minutes
    for f in TEMP_DIR.glob("*.pdf"):
        if f.stat().st_mtime < cutoff:
            f.unlink(missing_ok=True)


@router.post("/merge")
async def merge_images(files: List[UploadFile] = File(...)):
    if not files:
        raise HTTPException(400, detail={"error": "Aucun fichier fourni", "code": "NO_FILES"})

    def _do_merge():
        from PIL import Image
        _cleanup_old()
        images = []
        for uf in files:
            data = uf.file.read()
            try:
                img = Image.open(__import__("io").BytesIO(data)).convert("RGB")
                images.append(img)
            except Exception as e:
                raise ValueError(f"Image invalide ({uf.filename}): {e}")

        if not images:
            raise ValueError("Aucune image valide")

        filename = f"merged_{int(time.time())}.pdf"
        out_path = TEMP_DIR / filename
        images[0].save(out_path, save_all=True, append_images=images[1:])
        return filename

    try:
        filename = await asyncio.to_thread(_do_merge)
    except ValueError as e:
        raise HTTPException(422, detail={"error": str(e), "code": "INVALID_IMAGE"})

    return {"filename": filename}


@router.get("/file/{filename}")
async def get_file(filename: str):
    if "/" in filename or "\\" in filename:
        raise HTTPException(400, detail={"error": "Nom invalide", "code": "INVALID_NAME"})
    path = TEMP_DIR / filename
    if not path.exists():
        raise HTTPException(404, detail={"error": "Fichier introuvable", "code": "NOT_FOUND"})
    return FileResponse(
        path,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
