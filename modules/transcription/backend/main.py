import asyncio
import base64
import importlib.util
import logging
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
import wave
from datetime import datetime, timezone
from pathlib import Path

from ledroide_sdk import Module

log = logging.getLogger("ledroide.transcription")

module = Module(__file__)
router = module.router

IS_WINDOWS = sys.platform == "win32"
NO_WINDOW = subprocess.CREATE_NO_WINDOW if IS_WINDOWS else 0
UPLOAD_DIR = module.data_dir / "uploads"
WORK_DIR = module.data_dir / "work"
MODELS_DIR = module.data_dir / "models"
UPLOAD_TTL_S = 24 * 3600
MAX_UPLOAD_BYTES = 4 * 1024 ** 3
SAMPLE_RATE = 16000

MODELS = {
    "tiny": {"label": "Tiny", "size_mb": 75, "hint": "Très rapide, précision limitée"},
    "base": {"label": "Base", "size_mb": 145, "hint": "Rapide, correct pour une voix claire"},
    "small": {"label": "Small", "size_mb": 485, "hint": "Bon compromis (recommandé)"},
    "medium": {"label": "Medium", "size_mb": 1530, "hint": "Précis mais lent sur CPU"},
}
LANGUAGES = {
    "fr": "Français", "en": "Anglais", "es": "Espagnol", "de": "Allemand", "it": "Italien", "pt": "Portugais",
    "nl": "Néerlandais", "ar": "Arabe", "zh": "Chinois", "ja": "Japonais", "ko": "Coréen", "ru": "Russe",
    "pl": "Polonais", "tr": "Turc", "uk": "Ukrainien", "sv": "Suédois", "ro": "Roumain", "el": "Grec",
    "he": "Hébreu", "hi": "Hindi", "vi": "Vietnamien", "id": "Indonésien", "ca": "Catalan", "br": "Breton",
}

_uploads: dict[str, dict] = {}
_model_cache: dict = {"key": None, "model": None}
_model_lock = threading.Lock()


class TxError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _fail(exc: TxError) -> dict:
    return {"error": exc.message, "code": exc.code}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── Timestamps / exports ──────────────────────────────────────────────────────

def fmt_ts(seconds: float, sep: str = ",", hours: bool = True) -> str:
    ms = max(0, int(round(float(seconds) * 1000)))
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    if hours or h:
        return f"{h:02d}:{m:02d}:{s:02d}{sep}{ms:03d}"
    return f"{m:02d}:{s:02d}{sep}{ms:03d}"


def _clean_segments(segments) -> list[dict]:
    out = []
    for s in segments or []:
        if not isinstance(s, dict):
            continue
        try:
            start, end = float(s.get("start", 0)), float(s.get("end", 0))
        except (TypeError, ValueError):
            continue
        text = str(s.get("text") or "").strip()
        if not text:
            continue
        out.append({"start": max(0.0, start), "end": max(start, end), "text": text})
    return out


def to_srt(segments) -> str:
    blocks = []
    for i, s in enumerate(_clean_segments(segments), 1):
        blocks.append(f"{i}\n{fmt_ts(s['start'])} --> {fmt_ts(s['end'])}\n{s['text']}\n")
    return "\n".join(blocks)


def to_vtt(segments) -> str:
    lines = ["WEBVTT", ""]
    for s in _clean_segments(segments):
        text = s["text"].replace("-->", "→")
        lines += [f"{fmt_ts(s['start'], '.')} --> {fmt_ts(s['end'], '.')}", text, ""]
    return "\n".join(lines)


def to_txt(segments, timestamps: bool = False) -> str:
    segs = _clean_segments(segments)
    if timestamps:
        return "\n".join(f"[{fmt_ts(s['start'], '.', hours=False)[:-4]}] {s['text']}" for s in segs) + ("\n" if segs else "")
    paragraphs, cur, last_end = [], [], None
    for s in segs:
        if cur and last_end is not None and s["start"] - last_end > 2.0:
            paragraphs.append(" ".join(cur))
            cur = []
        cur.append(s["text"])
        last_end = s["end"]
    if cur:
        paragraphs.append(" ".join(cur))
    return "\n\n".join(paragraphs) + ("\n" if paragraphs else "")


@module.on("export")
async def export(payload: dict):
    fmt = str(payload.get("format") or "txt").lower()
    segments = payload.get("segments")
    if not isinstance(segments, list) or not _clean_segments(segments):
        return {"error": "Aucun segment à exporter", "code": "EMPTY"}
    if fmt == "srt":
        content, mime = to_srt(segments), "application/x-subrip"
    elif fmt == "vtt":
        content, mime = to_vtt(segments), "text/vtt"
    elif fmt == "txt":
        content, mime = to_txt(segments, bool(payload.get("timestamps"))), "text/plain"
    else:
        return {"error": f"Format inconnu : {fmt}", "code": "BAD_FORMAT"}
    return {"content": content, "mime": mime, "ext": fmt}


# ── Environment ───────────────────────────────────────────────────────────────

def _has_faster_whisper() -> bool:
    try:
        return importlib.util.find_spec("faster_whisper") is not None
    except (ImportError, ValueError):
        return False


def _ffmpeg() -> tuple[str | None, str | None]:
    found = shutil.which("ffmpeg")
    if found:
        return found, "PATH"
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe(), "imageio-ffmpeg"
    except Exception:
        return None, None


def _model_dir(name: str) -> Path:
    return MODELS_DIR / f"models--Systran--faster-whisper-{name}"


def _dir_size(path: Path) -> int:
    total = 0
    if path.exists():
        for f in path.rglob("*"):
            try:
                if f.is_file() and not f.is_symlink():
                    total += f.stat().st_size
            except OSError:
                pass
    return total


def _is_cached(name: str) -> bool:
    snaps = _model_dir(name) / "snapshots"
    return snaps.is_dir() and any((d / "model.bin").exists() for d in snaps.iterdir())


def _models_info() -> list[dict]:
    return [{"id": k, **v, "cached": _is_cached(k), "disk_mb": round(_dir_size(_model_dir(k)) / 1048576)}
            for k, v in MODELS.items()]


@module.on("status")
async def status(payload: dict):
    def _work():
        ff, src = _ffmpeg()
        return {
            "faster_whisper": _has_faster_whisper(),
            "ffmpeg": ff, "ffmpeg_source": src,
            "models": _models_info(),
            "models_dir": str(MODELS_DIR),
            "languages": [{"code": k, "name": v} for k, v in LANGUAGES.items()],
            "cpu_count": os.cpu_count(),
            "compute_type": module.config.get("compute_type", "int8"),
        }
    return await asyncio.to_thread(_work)


@module.on("delete_model")
async def delete_model(payload: dict):
    name = str(payload.get("model") or "")
    if name not in MODELS:
        return {"error": "Modèle inconnu", "code": "BAD_MODEL"}
    with _model_lock:
        if _model_cache["key"] and _model_cache["key"][0] == name:
            _model_cache.update(key=None, model=None)
    await asyncio.to_thread(shutil.rmtree, _model_dir(name), True)
    return {"ok": True, "models": await asyncio.to_thread(_models_info)}


@module.on("open_models_dir")
async def open_models_dir(payload: dict):
    MODELS_DIR.mkdir(parents=True, exist_ok=True)
    if not IS_WINDOWS:
        return {"error": "Fonction disponible uniquement sous Windows", "code": "UNSUPPORTED"}
    await asyncio.to_thread(os.startfile, str(MODELS_DIR))
    return {"ok": True}


# ── Uploads (chunked through the bridge) ──────────────────────────────────────

def _cleanup_dir(d: Path, ttl: float):
    if not d.exists():
        return
    now = time.time()
    for f in d.iterdir():
        try:
            if f.is_file() and now - f.stat().st_mtime > ttl:
                f.unlink()
        except OSError:
            pass


_cleanup_dir(UPLOAD_DIR, UPLOAD_TTL_S)
_cleanup_dir(WORK_DIR, 0)


def _safe_name(name) -> str:
    name = Path(str(name or "audio")).name
    return re.sub(r"[^\w.\- ]+", "_", name)[:100] or "audio"


@module.on("upload_begin")
async def upload_begin(payload: dict):
    size = int(payload.get("size") or 0)
    if size <= 0:
        return {"error": "Fichier vide", "code": "EMPTY_FILE"}
    if size > MAX_UPLOAD_BYTES:
        return {"error": "Fichier trop volumineux (max. 4 Go)", "code": "TOO_LARGE"}
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    uid = uuid.uuid4().hex
    name = _safe_name(payload.get("name"))
    path = UPLOAD_DIR / f"{uid}_{name}"
    path.write_bytes(b"")
    _uploads[uid] = {"path": path, "name": name, "size": size, "received": 0, "done": False}
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
    if int(payload.get("offset", -1)) != up["received"] or up["received"] + len(data) > up["size"]:
        return {"error": "Morceau de fichier hors séquence", "code": "BAD_CHUNK"}

    def _write():
        with open(up["path"], "ab") as f:
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
    return {"file_id": uid, "name": up["name"], "size": up["size"]}


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
    raw = str(payload.get("path") or "").strip().strip('"')
    if not raw:
        return {"error": "Chemin vide", "code": "BAD_SOURCE"}
    p = Path(os.path.expandvars(os.path.expanduser(raw)))
    if not await asyncio.to_thread(p.is_file):
        return {"error": f"Fichier introuvable : {raw}", "code": "NOT_FOUND"}
    return {"path": str(p), "name": p.name, "size": p.stat().st_size}


def _resolve(src) -> tuple[Path, str]:
    if not isinstance(src, dict):
        raise TxError("BAD_SOURCE", "Aucun fichier sélectionné")
    if src.get("file_id"):
        up = _uploads.get(str(src["file_id"]))
        if not up or not up["done"]:
            raise TxError("NOT_FOUND", "Fichier envoyé introuvable (expiré ?) — rechargez-le.")
        return up["path"], up["name"]
    raw = str(src.get("path") or "").strip().strip('"')
    if not raw:
        raise TxError("BAD_SOURCE", "Aucun fichier sélectionné")
    p = Path(os.path.expandvars(os.path.expanduser(raw)))
    if not p.is_file():
        raise TxError("NOT_FOUND", f"Fichier introuvable : {raw}")
    return p, p.name


# ── Audio conversion (ffmpeg) ─────────────────────────────────────────────────

def _parse_hms(s: str) -> float:
    h, m, sec = s.split(":")
    return int(h) * 3600 + int(m) * 60 + float(sec)


def _convert(ffmpeg: str, src: Path, dst: Path, state: dict, on_progress) -> float:
    """Converts any audio/video to 16 kHz mono WAV; returns the duration in seconds."""
    cmd = [ffmpeg, "-hide_banner", "-nostdin", "-y", "-i", str(src), "-vn", "-ac", "1", "-ar", str(SAMPLE_RATE),
           "-c:a", "pcm_s16le", "-progress", "pipe:1", "-nostats", str(dst)]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL,
                            creationflags=NO_WINDOW)
    state["proc"] = proc
    err_lines: list[str] = []
    info = {"duration": 0.0}

    def _stderr():
        for raw in proc.stderr:
            line = raw.decode("utf-8", "replace").rstrip()
            err_lines.append(line)
            if len(err_lines) > 200:
                del err_lines[:100]
            m = re.search(r"Duration:\s*(\d+:\d+:\d+(?:\.\d+)?)", line)
            if m and not info["duration"]:
                info["duration"] = _parse_hms(m.group(1))

    t = threading.Thread(target=_stderr, daemon=True)
    t.start()
    for raw in proc.stdout:
        line = raw.decode("ascii", "replace").strip()
        if line.startswith("out_time_us=") or line.startswith("out_time_ms="):
            try:
                pos = int(line.split("=", 1)[1]) / 1_000_000
            except ValueError:
                continue
            if info["duration"]:
                on_progress(min(1.0, pos / info["duration"]))
    proc.wait()
    t.join(timeout=2)
    state["proc"] = None
    if state.get("cancel"):
        raise TxError("CANCELLED", "Tâche annulée")
    if proc.returncode != 0:
        tail = "\n".join(x for x in err_lines[-6:] if x.strip())
        if any("does not contain any stream" in x or "Output file #0 does not contain" in x for x in err_lines):
            raise TxError("NO_AUDIO", "Ce fichier ne contient pas de piste audio.")
        raise TxError("CONVERT_FAILED", f"Conversion audio impossible (ffmpeg code {proc.returncode}).\n{tail}")
    return info["duration"]


def _read_wav(path: Path):
    import numpy as np
    with wave.open(str(path), "rb") as w:
        if w.getsampwidth() != 2 or w.getnchannels() != 1:
            raise TxError("CONVERT_FAILED", "Format WAV inattendu")
        frames = w.readframes(w.getnframes())
    return np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0


# ── Whisper ───────────────────────────────────────────────────────────────────

def _import_whisper():
    if not _has_faster_whisper():
        raise TxError("MISSING_DEP", "La bibliothèque « faster-whisper » n'est pas installée. Réactivez le module "
                                     "(installation automatique des dépendances) ou installez-la depuis le menu "
                                     "Bibliothèques.")
    try:
        from faster_whisper import WhisperModel
    except Exception as exc:
        raise TxError("MISSING_DEP", f"faster-whisper est installé mais ne se charge pas : {exc}. "
                                     "Installez le « Microsoft Visual C++ Redistributable » si nécessaire.")
    return WhisperModel


def _load_model(name: str):
    compute = str(module.config.get("compute_type") or "int8")
    threads = int(module.config.get("cpu_threads") or 0)
    key = (name, compute, threads)
    with _model_lock:
        if _model_cache["key"] == key and _model_cache["model"] is not None:
            return _model_cache["model"]
        WhisperModel = _import_whisper()
        os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
        os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
        MODELS_DIR.mkdir(parents=True, exist_ok=True)
        _model_cache.update(key=None, model=None)
        try:
            model = WhisperModel(name, device="cpu", compute_type=compute, cpu_threads=threads,
                                 download_root=str(MODELS_DIR), local_files_only=_is_cached(name))
        except Exception as exc:
            msg = str(exc)
            if not _is_cached(name):
                raise TxError("DOWNLOAD_FAILED", f"Téléchargement du modèle « {name} » impossible (connexion "
                                                 f"internet requise au premier usage) : {msg[:300]}")
            raise TxError("MODEL_FAILED", f"Chargement du modèle « {name} » impossible : {msg[:300]}")
        _model_cache.update(key=key, model=model)
        return model


@module.job("transcribe")
async def transcribe(payload: dict, job):
    model_name = str(payload.get("model") or "small")
    if model_name not in MODELS:
        raise RuntimeError("Modèle inconnu")
    language = str(payload.get("language") or "auto")
    if language != "auto" and language not in LANGUAGES:
        raise RuntimeError(f"Langue non prise en charge : {language}")
    task = "translate" if payload.get("task") == "translate" else "transcribe"
    beam = 5 if payload.get("quality", "precise") == "precise" else 1
    vad = bool(payload.get("vad", True))
    prompt = str(payload.get("prompt") or "").strip()[:800] or None
    try:
        _import_whisper()
        src, name = _resolve(payload.get("source"))
    except TxError as exc:
        raise RuntimeError(exc.message) from exc

    loop = asyncio.get_running_loop()
    queue: asyncio.Queue = asyncio.Queue()
    state: dict = {"cancel": False, "proc": None}
    put = lambda item: loop.call_soon_threadsafe(queue.put_nowait, item)  # noqa: E731
    wav_path = WORK_DIR / f"{job.id}.wav"
    started = time.perf_counter()

    def _worker():
        try:
            audio, duration = str(src), 0.0
            ff, _ = _ffmpeg()
            if ff:
                WORK_DIR.mkdir(parents=True, exist_ok=True)
                put(("stage", {"stage": "convert", "message": "Conversion audio (ffmpeg)…"}))
                duration = _convert(ff, src, wav_path, state, lambda f: put(("convert", f)))
                audio = _read_wav(wav_path)
                duration = len(audio) / SAMPLE_RATE or duration
                if len(audio) < SAMPLE_RATE // 10:
                    raise TxError("NO_AUDIO", "Le fichier audio est vide ou trop court.")
            if state["cancel"]:
                return
            cached = _is_cached(model_name)
            put(("stage", {"stage": "model", "cached": cached,
                           "message": "Chargement du modèle…" if cached else
                           f"Téléchargement du modèle « {model_name} » (~{MODELS[model_name]['size_mb']} Mo)…"}))
            model = _load_model(model_name)
            if state["cancel"]:
                return
            put(("stage", {"stage": "transcribe", "message": "Transcription…"}))
            segments, info = model.transcribe(audio, language=None if language == "auto" else language, task=task,
                                              beam_size=beam, vad_filter=vad, initial_prompt=prompt)
            duration = float(info.duration or duration)
            put(("info", {"language": info.language, "language_probability": round(info.language_probability or 0, 3),
                          "duration": duration}))
            for seg in segments:
                if state["cancel"]:
                    return
                put(("segment", {"id": seg.id, "start": round(seg.start, 2), "end": round(seg.end, 2),
                                 "text": seg.text.strip()}))
        except TxError as exc:
            put(("error", exc.message))
        except Exception as exc:
            log.exception("transcription failed")
            put(("error", f"Erreur de transcription : {exc}"))
        finally:
            put(("end", None))

    thread = threading.Thread(target=_worker, name=f"transcription-{job.id[:8]}", daemon=True)
    thread.start()
    segments: list[dict] = []
    info: dict = {"duration": 0.0, "language": None, "language_probability": None}
    error = None
    download_task = None

    async def _watch_download():
        target = MODELS[model_name]["size_mb"] * 1048576
        while True:
            await asyncio.sleep(1)
            done = await asyncio.to_thread(_dir_size, _model_dir(model_name))
            pct = min(99, done / target * 100)
            await job.progress(round(pct * 0.3), f"Téléchargement du modèle… {done / 1048576:.0f} / "
                                                 f"~{MODELS[model_name]['size_mb']} Mo", stage="download")

    try:
        while True:
            kind, data = await queue.get()
            if kind == "end":
                break
            if kind == "error":
                error = data
            elif kind == "stage":
                if download_task:
                    download_task.cancel()
                    download_task = None
                if data["stage"] == "model" and not data["cached"]:
                    download_task = asyncio.create_task(_watch_download())
                base = {"convert": 0, "model": 0, "transcribe": 30}[data["stage"]]
                await job.progress(base, data["message"], stage=data["stage"])
            elif kind == "convert":
                await job.progress(round(data * 10), f"Conversion audio… {data * 100:.0f} %", stage="convert")
            elif kind == "info":
                info.update(data)
                await module.events.emit("transcription:info", {"job_id": job.id, **data})
            elif kind == "segment":
                segments.append(data)
                pct = 30 + (data["end"] / info["duration"] * 70 if info["duration"] else 0)
                await module.events.emit("transcription:segment", {"job_id": job.id, "segment": data})
                await job.progress(round(min(99.0, pct), 1), f"{fmt_ts(data['end'], '.', False)[:-4]} / "
                                   f"{fmt_ts(info['duration'], '.', False)[:-4]}", stage="transcribe",
                                   segments=len(segments))
    except asyncio.CancelledError:
        state["cancel"] = True
        proc = state.get("proc")
        if proc and proc.poll() is None:
            proc.kill()
        raise
    finally:
        if download_task:
            download_task.cancel()
        await asyncio.to_thread(_unlink, wav_path)

    if error:
        raise RuntimeError(error)
    elapsed = round(time.perf_counter() - started, 1)
    result = {"name": name, "model": model_name, "task": task, "segments": segments,
              "text": to_txt(segments).strip(), "language": info["language"],
              "language_probability": info["language_probability"], "duration": round(info["duration"], 2),
              "elapsed_s": elapsed, "created": _now()}
    log.info("transcription : %d segments, %.0f s audio en %.1f s (%s)", len(segments), info["duration"], elapsed,
             model_name)
    if int(module.config.get("history_size", 30)) > 0 and segments:
        saved = await module.db.save("transcripts", result)
        result["_id"] = saved["_id"]
        await _trim_history()
    return result


def _unlink(p: Path):
    try:
        p.unlink(missing_ok=True)
    except OSError:
        pass


async def _trim_history():
    keep = int(module.config.get("history_size", 30))
    items = await module.db.query("transcripts")
    for old in items[keep:]:
        await module.db.delete("transcripts", old["_id"])


@module.on("history_list")
async def history_list(payload: dict):
    items = await module.db.query("transcripts")
    return {"items": [{"_id": d["_id"], "name": d.get("name"), "model": d.get("model"), "language": d.get("language"),
                       "duration": d.get("duration"), "created": d.get("created"),
                       "preview": (d.get("text") or "")[:140], "segments": len(d.get("segments") or [])}
                      for d in items]}


@module.on("history_get")
async def history_get(payload: dict):
    doc = await module.db.get("transcripts", str(payload.get("id") or ""))
    if not doc:
        return {"error": "Transcription introuvable", "code": "NOT_FOUND"}
    return doc


@module.on("history_delete")
async def history_delete(payload: dict):
    return await module.db.delete("transcripts", str(payload.get("id") or ""))


@module.on("history_clear")
async def history_clear(payload: dict):
    for d in await module.db.query("transcripts"):
        await module.db.delete("transcripts", d["_id"])
    return {"ok": True}


@module.on_shutdown
async def _shutdown():
    for uid in list(_uploads):
        await _discard(uid)
    with _model_lock:
        _model_cache.update(key=None, model=None)
