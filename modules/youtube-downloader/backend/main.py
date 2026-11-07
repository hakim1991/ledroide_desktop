import asyncio
import uuid
from pathlib import Path

from ledroide_sdk.http import HTTPException
from pydantic import BaseModel

from ledroide_sdk import Module

module = Module(__file__)
router = module.router

DOWNLOAD_DIR = Path.home() / "Downloads" / "LedroideYoutube"
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

jobs: dict[str, dict] = {}


class DownloadRequest(BaseModel):
    url: str
    mode: str = "audio"


async def _do_download(job_id: str, req: DownloadRequest):
    jobs[job_id] = {"status": "running", "progress": 0, "download_dir": str(DOWNLOAD_DIR), "error": None}

    def progress_hook(d):
        if d.get("status") == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            downloaded = d.get("downloaded_bytes", 0)
            if total > 0:
                jobs[job_id]["progress"] = round(downloaded / total * 100)

    if req.mode == "audio":
        opts = {
            "format": "bestaudio/best",
            "postprocessors": [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3"}],
        }
    else:
        opts = {
            "format": "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
        }

    opts["outtmpl"] = str(DOWNLOAD_DIR / "%(title)s.%(ext)s")
    opts["progress_hooks"] = [progress_hook]
    opts["quiet"] = True

    def _run():
        import yt_dlp
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([req.url])

    try:
        await asyncio.to_thread(_run)
        jobs[job_id]["status"] = "done"
        jobs[job_id]["progress"] = 100
    except Exception as e:
        jobs[job_id]["status"] = "error"
        jobs[job_id]["error"] = str(e)


@router.post("/download")
async def start_download(req: DownloadRequest):
    job_id = uuid.uuid4().hex
    asyncio.create_task(_do_download(job_id, req))
    return {"job_id": job_id}


@router.get("/status/{job_id}")
async def get_status(job_id: str):
    return jobs.get(job_id, {"status": "not_found"})


@router.get("/info")
async def get_info():
    return {"download_dir": str(DOWNLOAD_DIR)}
