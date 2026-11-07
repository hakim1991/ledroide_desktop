"""Outils vidéo — découpe, compression, GIF, extraction d'images et d'audio via ffmpeg."""
import asyncio
import json
import re
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
import zipfile
from pathlib import Path
from typing import Optional

from ledroide_sdk.http import UploadFile, File
from ledroide_sdk.http import FileResponse, JSONResponse
from pydantic import BaseModel
from ledroide_sdk import Module

module = Module(__file__)
router = module.router

TEMP_DIR = Path(tempfile.gettempdir()) / "ledroide_video-tools"
UPLOAD_DIR = TEMP_DIR / "uploads"
WORK_DIR = TEMP_DIR / "work"
for _d in (TEMP_DIR, UPLOAD_DIR, WORK_DIR):
    _d.mkdir(parents=True, exist_ok=True)

MAX_AGE = 24 * 3600
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

_uploads: dict[str, dict] = {}
_jobs: dict[str, dict] = {}
_lock = threading.Lock()


def _err(msg: str, status: int = 400, code: Optional[str] = None):
    content = {"error": msg}
    if code:
        content["code"] = code
    return JSONResponse(status_code=status, content=content)


# ── ffmpeg discovery ──────────────────────────────────────────────────────────

def _find_tools() -> dict:
    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    source = "PATH" if ffmpeg else None
    if not ffmpeg:
        for cand in (r"C:\ffmpeg\bin\ffmpeg.exe",):
            if Path(cand).exists():
                ffmpeg, source = cand, "C:\\ffmpeg"
                p = Path(cand).with_name("ffprobe.exe")
                if not ffprobe and p.exists():
                    ffprobe = str(p)
                break
    if not ffmpeg:
        try:
            import imageio_ffmpeg  # type: ignore
            ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
            source = "imageio-ffmpeg"
        except Exception:
            ffmpeg = None
    return {"ffmpeg": ffmpeg, "ffprobe": ffprobe, "source": source}


def _no_ffmpeg():
    return _err("ffmpeg introuvable. Installez ffmpeg (et ajoutez-le au PATH) ou installez les dépendances "
                "du module (imageio-ffmpeg).", 400, "MISSING_DEP")


# ── helpers ───────────────────────────────────────────────────────────────────

def _cleanup_old():
    now = time.time()
    for d in (UPLOAD_DIR, WORK_DIR, TEMP_DIR):
        for p in d.iterdir():
            try:
                if p.is_file() and now - p.stat().st_mtime > MAX_AGE:
                    p.unlink()
                elif p.is_dir() and p.parent == WORK_DIR and now - p.stat().st_mtime > MAX_AGE:
                    shutil.rmtree(p, ignore_errors=True)
            except OSError:
                pass


def _safe_stem(name: str) -> str:
    stem = Path(name or "video").stem
    stem = re.sub(r"[^\w\-]+", "_", stem, flags=re.UNICODE).strip("_")
    return (stem or "video")[:60]


def _parse_time(v) -> float:
    """Accepte 12.5, '12.5', '1:02', '01:02:03.5'."""
    if v is None or v == "":
        raise ValueError("temps vide")
    if isinstance(v, (int, float)):
        t = float(v)
    else:
        s = str(v).strip().replace(",", ".")
        parts = s.split(":")
        if len(parts) > 3:
            raise ValueError(s)
        t = 0.0
        for p in parts:
            t = t * 60 + float(p)
    if t < 0:
        raise ValueError("négatif")
    return t


def _fmt_time(t: float) -> str:
    return f"{t:.3f}"


def _probe(path: Path, tools: dict) -> dict:
    if tools.get("ffprobe"):
        r = subprocess.run(
            [tools["ffprobe"], "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)],
            capture_output=True, timeout=60, creationflags=_NO_WINDOW,
        )
        if r.returncode != 0:
            raise RuntimeError(r.stderr.decode("utf-8", "replace").strip()[-400:] or "ffprobe a échoué")
        data = json.loads(r.stdout.decode("utf-8", "replace") or "{}")
        fmt = data.get("format", {})
        streams = data.get("streams", [])
        v = next((s for s in streams if s.get("codec_type") == "video"
                  and not (s.get("disposition") or {}).get("attached_pic")), None)
        a = next((s for s in streams if s.get("codec_type") == "audio"), None)
        dur = fmt.get("duration") or (v or {}).get("duration") or (a or {}).get("duration")
        fps = None
        if v and v.get("avg_frame_rate") and v["avg_frame_rate"] != "0/0":
            try:
                n, d = v["avg_frame_rate"].split("/")
                fps = round(float(n) / float(d), 3) if float(d) else None
            except Exception:
                fps = None
        return {
            "duration": float(dur) if dur else None,
            "width": v.get("width") if v else None,
            "height": v.get("height") if v else None,
            "vcodec": v.get("codec_name") if v else None,
            "acodec": a.get("codec_name") if a else None,
            "fps": fps,
            "bitrate": int(fmt["bit_rate"]) if fmt.get("bit_rate") else None,
            "format": fmt.get("format_long_name") or fmt.get("format_name"),
            "has_video": v is not None,
            "has_audio": a is not None,
        }
    # Fallback : analyse de la sortie de `ffmpeg -i`
    r = subprocess.run([tools["ffmpeg"], "-hide_banner", "-i", str(path)],
                       capture_output=True, timeout=60, creationflags=_NO_WINDOW)
    txt = r.stderr.decode("utf-8", "replace")
    info = {"duration": None, "width": None, "height": None, "vcodec": None, "acodec": None,
            "fps": None, "bitrate": None, "format": None, "has_video": False, "has_audio": False}
    m = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", txt)
    if m:
        info["duration"] = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
    m = re.search(r"bitrate:\s*(\d+)\s*kb/s", txt)
    if m:
        info["bitrate"] = int(m.group(1)) * 1000
    m = re.search(r"Input #0,\s*([^,]+)", txt)
    if m:
        info["format"] = m.group(1)
    m = re.search(r"Stream #.*?Video:\s*(\w+).*?(\d{2,5})x(\d{2,5})", txt)
    if m:
        info.update(has_video=True, vcodec=m.group(1), width=int(m.group(2)), height=int(m.group(3)))
        f = re.search(r"Stream #.*?Video:.*?(\d+(?:\.\d+)?)\s*fps", txt)
        if f:
            info["fps"] = float(f.group(1))
    m = re.search(r"Stream #.*?Audio:\s*(\w+)", txt)
    if m:
        info.update(has_audio=True, acodec=m.group(1))
    if not info["has_video"] and not info["has_audio"]:
        raise RuntimeError("Fichier non reconnu comme vidéo.")
    return info


# ── job runner ────────────────────────────────────────────────────────────────

class _Cancelled(Exception):
    pass


def _run_ffmpeg(job: dict, args: list, expected: float, p0: float = 0.0, p1: float = 1.0):
    """Exécute ffmpeg en lisant `-progress pipe:1`; met à jour job['progress'] entre p0 et p1."""
    cmd = [job["ffmpeg"], "-hide_banner", "-nostdin", "-y", "-progress", "pipe:1", "-nostats"] + args
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=_NO_WINDOW)
    job["proc"] = proc
    tail: list[str] = []

    def _drain():
        for raw in proc.stderr:
            tail.append(raw.decode("utf-8", "replace").rstrip())
            if len(tail) > 40:
                del tail[:-40]

    t = threading.Thread(target=_drain, daemon=True)
    t.start()
    for raw in proc.stdout:
        line = raw.decode("utf-8", "replace").strip()
        if line.startswith(("out_time_us=", "out_time_ms=")):
            try:
                us = int(line.split("=", 1)[1])
                if expected and expected > 0:
                    frac = max(0.0, min(1.0, us / 1e6 / expected))
                    job["progress"] = p0 + (p1 - p0) * frac
            except ValueError:
                pass
        elif line == "progress=end":
            job["progress"] = p1
    proc.wait()
    t.join(timeout=5)
    job["proc"] = None
    if job.get("cancel"):
        raise _Cancelled()
    if proc.returncode != 0:
        msg = "\n".join(l for l in tail if l.strip())[-600:]
        raise RuntimeError(f"ffmpeg a échoué (code {proc.returncode}).\n{msg}")


def _job_thread(job: dict):
    try:
        up = job["upload"]
        src = up["path"]
        info = up["info"]
        dur = info.get("duration") or 0
        p = job["params"]
        stem = _safe_stem(up["name"])
        tag = uuid.uuid4().hex[:6]
        action = job["action"]

        if action == "cut":
            start, end = p["start"], p["end"]
            length = end - start
            if p.get("fast"):
                ext = Path(up["name"]).suffix.lower() or ".mp4"
                out = TEMP_DIR / f"{stem}_coupe_{tag}{ext}"
                args = ["-ss", _fmt_time(start), "-i", str(src), "-t", _fmt_time(length),
                        "-map", "0", "-c", "copy", "-avoid_negative_ts", "make_zero", str(out)]
            else:
                out = TEMP_DIR / f"{stem}_coupe_{tag}.mp4"
                args = ["-ss", _fmt_time(start), "-i", str(src), "-t", _fmt_time(length),
                        "-c:v", "libx264", "-crf", "18", "-preset", "veryfast", "-pix_fmt", "yuv420p",
                        "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", str(out)]
            _run_ffmpeg(job, args, length)

        elif action == "compress":
            out = TEMP_DIR / f"{stem}_compresse_{tag}.mp4"
            vf = []
            h = p.get("height")
            if h and info.get("height") and h < info["height"]:
                vf = ["-vf", f"scale=-2:{h}"]
            args = ["-i", str(src)] + vf + [
                "-c:v", "libx264", "-crf", str(p["crf"]), "-preset", p.get("preset", "medium"),
                "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", str(out)]
            _run_ffmpeg(job, args, dur)

        elif action == "gif":
            start, length, fps, width = p["start"], p["duration"], p["fps"], p["width"]
            palette = WORK_DIR / f"palette_{job['id']}.png"
            out = TEMP_DIR / f"{stem}_{tag}.gif"
            filt = f"fps={fps},scale={width}:-1:flags=lanczos"
            try:
                _run_ffmpeg(job, ["-ss", _fmt_time(start), "-t", _fmt_time(length), "-i", str(src),
                                  "-vf", f"{filt},palettegen=stats_mode=diff", str(palette)], length, 0.0, 0.3)
                _run_ffmpeg(job, ["-ss", _fmt_time(start), "-t", _fmt_time(length), "-i", str(src),
                                  "-i", str(palette), "-lavfi",
                                  f"{filt}[x];[x][1:v]paletteuse=dither=sierra2_4a", "-loop", "0", str(out)],
                            length, 0.3, 1.0)
            finally:
                palette.unlink(missing_ok=True)

        elif action == "frames":
            every = p["every"]
            fmt = p.get("format", "jpg")
            fdir = WORK_DIR / f"frames_{job['id']}"
            fdir.mkdir(parents=True, exist_ok=True)
            pattern = fdir / f"image_%05d.{fmt}"
            extra = ["-q:v", "2"] if fmt == "jpg" else []
            try:
                _run_ffmpeg(job, ["-i", str(src), "-vf", f"fps=1/{every}"] + extra + [str(pattern)], dur, 0.0, 0.95)
                frames = sorted(fdir.iterdir())
                if not frames:
                    raise RuntimeError("Aucune image extraite.")
                out = TEMP_DIR / f"{stem}_images_{tag}.zip"
                with zipfile.ZipFile(out, "w", zipfile.ZIP_STORED if fmt == "jpg" else zipfile.ZIP_DEFLATED) as zf:
                    for i, f in enumerate(frames):
                        t = i * every
                        zf.write(f, f"{stem}_{i + 1:05d}_{int(t // 60):02d}m{t % 60:05.2f}s.{fmt}")
                job["count"] = len(frames)
            finally:
                shutil.rmtree(fdir, ignore_errors=True)

        elif action == "audio":
            out = TEMP_DIR / f"{stem}_{tag}.mp3"
            _run_ffmpeg(job, ["-i", str(src), "-vn", "-map", "0:a:0", "-c:a", "libmp3lame",
                              "-b:a", f"{p.get('bitrate', 192)}k", str(out)], dur)
        else:
            raise RuntimeError("Action inconnue")

        job.update(status="done", progress=1.0, filename=out.name, size=out.stat().st_size)
    except _Cancelled:
        job.update(status="cancelled", error="Opération annulée.")
    except Exception as e:  # noqa: BLE001
        job.update(status="error", error=str(e) or e.__class__.__name__)
    finally:
        job["finished_at"] = time.time()
        if job["status"] != "done":
            out_p = locals().get("out")
            if isinstance(out_p, Path):
                try:
                    out_p.unlink(missing_ok=True)
                except OSError:
                    pass


# ── routes ────────────────────────────────────────────────────────────────────

@router.get("/status")
async def status():
    tools = await asyncio.to_thread(_find_tools)
    return {"available": bool(tools["ffmpeg"]), **tools}


@router.post("/upload")
async def upload(file: UploadFile = File(...)):
    tools = await asyncio.to_thread(_find_tools)
    if not tools["ffmpeg"]:
        return _no_ffmpeg()
    await asyncio.to_thread(_cleanup_old)
    file_id = uuid.uuid4().hex
    ext = Path(file.filename or "").suffix.lower()
    if not re.fullmatch(r"\.[a-z0-9]{1,6}", ext or ""):
        ext = ".bin"
    path = UPLOAD_DIR / f"{file_id}{ext}"
    size = 0
    with open(path, "wb") as fh:
        while True:
            chunk = await file.read(4 * 1024 * 1024)
            if not chunk:
                break
            size += len(chunk)
            await asyncio.to_thread(fh.write, chunk)
    if size == 0:
        path.unlink(missing_ok=True)
        return _err("Fichier vide.")
    try:
        info = await asyncio.to_thread(_probe, path, tools)
    except Exception as e:  # noqa: BLE001
        path.unlink(missing_ok=True)
        msg = str(e).replace(str(path), file.filename or "fichier")
        return _err(f"Impossible de lire la vidéo : {msg}", 400, "PROBE_ERROR")
    rec = {"id": file_id, "name": file.filename or f"video{ext}", "path": path, "size": size, "info": info}
    with _lock:
        _uploads[file_id] = rec
    return {"file_id": file_id, "name": rec["name"], "size": size, "info": info}


@router.delete("/upload/{file_id}")
async def delete_upload(file_id: str):
    with _lock:
        rec = _uploads.pop(file_id, None)
    if not rec:
        return _err("Fichier introuvable", 404)
    try:
        rec["path"].unlink(missing_ok=True)
    except OSError:
        pass
    return {"ok": True}


class JobIn(BaseModel):
    file_id: str
    action: str
    params: dict = {}


def _validate(action: str, params: dict, info: dict) -> dict:
    dur = info.get("duration") or 0
    out: dict = {}
    if action in ("cut", "compress", "gif", "frames") and not info.get("has_video"):
        if action != "cut":
            raise ValueError("Ce fichier ne contient pas de piste vidéo.")
    if action == "cut":
        start = _parse_time(params.get("start", 0))
        end = _parse_time(params.get("end", dur))
        if dur:
            end = min(end, dur)
        if end - start < 0.05:
            raise ValueError("La fin doit être après le début.")
        if dur and start >= dur:
            raise ValueError("Le début dépasse la durée de la vidéo.")
        out = {"start": start, "end": end, "fast": bool(params.get("fast"))}
    elif action == "compress":
        crf = int(params.get("crf", 28))
        if not 0 <= crf <= 51:
            raise ValueError("CRF invalide (0–51).")
        h = params.get("height")
        h = int(h) if h not in (None, "", 0, "0") else None
        if h is not None and h not in (2160, 1440, 1080, 720, 480, 360):
            raise ValueError("Résolution invalide.")
        preset = params.get("preset", "medium")
        if preset not in ("ultrafast", "veryfast", "faster", "fast", "medium", "slow"):
            preset = "medium"
        out = {"crf": crf, "height": h, "preset": preset}
    elif action == "gif":
        start = _parse_time(params.get("start", 0))
        length = float(params.get("duration", 5))
        fps = int(params.get("fps", 12))
        width = int(params.get("width", 480))
        if not 0.1 <= length <= 120:
            raise ValueError("Durée du GIF : entre 0,1 et 120 s.")
        if not 1 <= fps <= 50:
            raise ValueError("FPS : entre 1 et 50.")
        if not 16 <= width <= 1920:
            raise ValueError("Largeur : entre 16 et 1920 px.")
        if dur:
            if start >= dur:
                raise ValueError("Le début dépasse la durée de la vidéo.")
            length = min(length, dur - start)
        out = {"start": start, "duration": length, "fps": fps, "width": width}
    elif action == "frames":
        every = float(params.get("every", 1))
        if not 0.04 <= every <= 3600:
            raise ValueError("Intervalle invalide (0,04 s à 3600 s).")
        if dur and dur / every > 5000:
            raise ValueError("Trop d'images (> 5000). Augmentez l'intervalle.")
        fmt = params.get("format", "jpg")
        out = {"every": every, "format": fmt if fmt in ("jpg", "png") else "jpg"}
    elif action == "audio":
        if not info.get("has_audio"):
            raise ValueError("Cette vidéo ne contient pas de piste audio.")
        br = int(params.get("bitrate", 192))
        out = {"bitrate": br if br in (96, 128, 160, 192, 256, 320) else 192}
    else:
        raise ValueError("Action inconnue.")
    return out


@router.post("/jobs")
async def create_job(body: JobIn):
    tools = await asyncio.to_thread(_find_tools)
    if not tools["ffmpeg"]:
        return _no_ffmpeg()
    with _lock:
        up = _uploads.get(body.file_id)
    if not up or not up["path"].exists():
        return _err("Vidéo introuvable : veuillez la recharger.", 404)
    try:
        params = _validate(body.action, body.params or {}, up["info"])
    except (ValueError, TypeError) as e:
        return _err(f"Paramètres invalides : {e}")
    job_id = uuid.uuid4().hex
    job = {"id": job_id, "action": body.action, "params": params, "upload": up, "ffmpeg": tools["ffmpeg"],
           "status": "running", "progress": 0.0, "filename": None, "error": None, "size": None,
           "proc": None, "cancel": False, "started_at": time.time(), "finished_at": None}
    with _lock:
        _jobs[job_id] = job
    threading.Thread(target=_job_thread, args=(job,), daemon=True).start()
    return {"job_id": job_id}


def _job_public(job: dict) -> dict:
    return {k: job.get(k) for k in ("id", "action", "status", "progress", "filename", "error", "size", "count",
                                     "started_at", "finished_at")}


@router.get("/jobs/{job_id}")
async def get_job(job_id: str):
    job = _jobs.get(job_id)
    if not job:
        return _err("Tâche introuvable", 404)
    return _job_public(job)


@router.post("/jobs/{job_id}/cancel")
async def cancel_job(job_id: str):
    job = _jobs.get(job_id)
    if not job:
        return _err("Tâche introuvable", 404)
    job["cancel"] = True
    proc = job.get("proc")
    if proc and proc.poll() is None:
        try:
            proc.kill()
        except OSError:
            pass
    return {"ok": True}


@router.get("/file/{filename}")
async def get_file(filename: str):
    if "/" in filename or "\\" in filename or ".." in filename or not filename:
        return _err("Nom de fichier invalide", 400)
    path = TEMP_DIR / filename
    if not path.is_file():
        return _err("Fichier introuvable", 404)
    # Starlette génère `Content-Disposition: attachment` (avec encodage RFC 5987 si non-ASCII)
    return FileResponse(str(path), filename=filename, content_disposition_type="attachment")
