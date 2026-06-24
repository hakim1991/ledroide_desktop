import asyncio
import tempfile
import uuid
from pathlib import Path

from fastapi import UploadFile, File, HTTPException
from fastapi.responses import FileResponse

from ledroide_sdk import Module

module = Module(__file__)
router = module.router

TEMP_DIR = Path(tempfile.gettempdir()) / "ledroide_mp4_to_mp3"
TEMP_DIR.mkdir(parents=True, exist_ok=True)


@router.post("/convert")
async def convert(file: UploadFile = File(...)):
    suffix = Path(file.filename).suffix or ".mp4"
    job_id = uuid.uuid4().hex
    src_path = TEMP_DIR / f"{job_id}{suffix}"
    mp3_path = TEMP_DIR / f"{job_id}.mp3"

    content = await file.read()
    src_path.write_bytes(content)

    def _convert():
        from moviepy import VideoFileClip
        clip = VideoFileClip(str(src_path))
        if clip.audio is None:
            clip.close()
            raise ValueError("no_audio")
        clip.audio.write_audiofile(str(mp3_path), logger=None)
        clip.close()

    try:
        await asyncio.to_thread(_convert)
    except ValueError as e:
        src_path.unlink(missing_ok=True)
        raise HTTPException(400, detail={"error": "Cette vidéo ne contient pas de piste audio.", "code": "NO_AUDIO"})
    except Exception as e:
        src_path.unlink(missing_ok=True)
        raise HTTPException(500, detail={"error": str(e), "code": "CONVERT_ERROR"})
    finally:
        src_path.unlink(missing_ok=True)

    stem = Path(file.filename).stem or "audio"
    final_name = f"{stem}.mp3"
    final_path = TEMP_DIR / final_name
    mp3_path.rename(final_path)

    return {"filename": final_name}


@router.get("/file/{filename}")
async def get_file(filename: str):
    path = TEMP_DIR / filename
    if not path.exists():
        raise HTTPException(404, detail={"error": "Fichier introuvable", "code": "NOT_FOUND"})
    return FileResponse(
        str(path),
        media_type="audio/mpeg",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
