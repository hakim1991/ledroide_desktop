import asyncio
import base64
import logging
import mimetypes
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from datetime import datetime, timezone

from pydantic import BaseModel, Field, ValidationError, field_validator

from ledroide_sdk import Module
from ledroide_sdk.http import FileResponse, Request, Response, StreamingResponse, error

from podcast_feed import USER_AGENT, FeedError, fetch, normalize_url, opener, parse_podcast

log = logging.getLogger("ledroide.music-player")

module = Module(__file__)
router = module.router

AUDIO_EXTS = {".mp3": "audio/mpeg", ".flac": "audio/flac", ".ogg": "audio/ogg", ".oga": "audio/ogg",
              ".opus": "audio/ogg", ".m4a": "audio/mp4", ".m4b": "audio/mp4", ".aac": "audio/aac",
              ".wav": "audio/wav", ".webm": "audio/webm"}
COVER_NAMES = ("cover", "folder", "front", "album", "albumart")
MAX_DOWNLOAD = 2 * 1024 * 1024 * 1024
TRACK_COLS = ("id, folder_id, path, title, artist, album, album_artist, track_no, disc_no, year, genre, duration,"
              " size, has_cover, play_count, last_played, added_at")

_ready = False
_lock = asyncio.Lock()
_downloads: dict[int, str] = {}

SCHEMA = """
CREATE TABLE IF NOT EXISTS folders (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    path      TEXT NOT NULL UNIQUE,
    added_at  TEXT NOT NULL,
    last_scan TEXT
);
CREATE TABLE IF NOT EXISTS tracks (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    folder_id    INTEGER NOT NULL REFERENCES folders(id) ON DELETE CASCADE,
    path         TEXT    NOT NULL UNIQUE,
    title        TEXT    NOT NULL DEFAULT '',
    artist       TEXT    NOT NULL DEFAULT '',
    album        TEXT    NOT NULL DEFAULT '',
    album_artist TEXT    NOT NULL DEFAULT '',
    track_no     INTEGER NOT NULL DEFAULT 0,
    disc_no      INTEGER NOT NULL DEFAULT 0,
    year         TEXT    NOT NULL DEFAULT '',
    genre        TEXT    NOT NULL DEFAULT '',
    duration     REAL    NOT NULL DEFAULT 0,
    size         INTEGER NOT NULL DEFAULT 0,
    mtime        REAL    NOT NULL DEFAULT 0,
    has_cover    INTEGER NOT NULL DEFAULT 0,
    play_count   INTEGER NOT NULL DEFAULT 0,
    last_played  TEXT,
    added_at     TEXT    NOT NULL
);
CREATE TABLE IF NOT EXISTS playlists (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS playlist_items (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    playlist_id INTEGER NOT NULL REFERENCES playlists(id) ON DELETE CASCADE,
    track_id    INTEGER NOT NULL REFERENCES tracks(id) ON DELETE CASCADE,
    position    INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS podcasts (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    url          TEXT NOT NULL UNIQUE,
    title        TEXT NOT NULL DEFAULT '',
    author       TEXT NOT NULL DEFAULT '',
    description  TEXT NOT NULL DEFAULT '',
    image        TEXT NOT NULL DEFAULT '',
    site_url     TEXT NOT NULL DEFAULT '',
    last_refresh TEXT,
    error        TEXT NOT NULL DEFAULT '',
    added_at     TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS episodes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    podcast_id  INTEGER NOT NULL REFERENCES podcasts(id) ON DELETE CASCADE,
    guid        TEXT    NOT NULL,
    title       TEXT    NOT NULL DEFAULT '',
    description TEXT    NOT NULL DEFAULT '',
    published   REAL,
    duration    REAL,
    audio_url   TEXT    NOT NULL,
    mime        TEXT    NOT NULL DEFAULT '',
    size        INTEGER NOT NULL DEFAULT 0,
    image       TEXT    NOT NULL DEFAULT '',
    link        TEXT    NOT NULL DEFAULT '',
    position    REAL    NOT NULL DEFAULT 0,
    played      INTEGER NOT NULL DEFAULT 0,
    local_path  TEXT    NOT NULL DEFAULT '',
    UNIQUE (podcast_id, guid)
);
CREATE INDEX IF NOT EXISTS idx_tracks_folder ON tracks(folder_id);
CREATE INDEX IF NOT EXISTS idx_items_pl ON playlist_items(playlist_id);
CREATE INDEX IF NOT EXISTS idx_ep_podcast ON episodes(podcast_id);
"""


class MusicError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


async def _db():
    global _ready
    if not _ready:
        async with _lock:
            if not _ready:
                await module.db.executescript(SCHEMA)
                _ready = True
    await module.db.execute("PRAGMA foreign_keys = ON")
    return module.db


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _err(message: str, code: str = "BAD_REQUEST") -> dict:
    return {"error": message, "code": code}


def _int(v, default: int = 0) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _validation_message(exc: ValidationError) -> str:
    first = exc.errors()[0]
    loc = ".".join(str(p) for p in first.get("loc", ()))
    msg = str(first.get("msg", "Données invalides")).removeprefix("Value error, ")
    return f"{loc} : {msg}" if loc else msg


def _norm(path: str) -> str:
    return os.path.normcase(os.path.normpath(path))


def _inside(path: str, folder: str) -> bool:
    p, f = _norm(path), _norm(folder)
    return p == f or p.startswith(f.rstrip("\\/") + os.sep)


def guarded(fn):
    async def wrapper(payload: dict):
        try:
            return await fn(payload)
        except (MusicError, FeedError) as exc:
            return _err(exc.message, exc.code)
    wrapper.__name__ = fn.__name__
    return wrapper


def _mutagen():
    try:
        import mutagen
    except ImportError:
        raise MusicError("MISSING_DEP", "mutagen n'est pas installé : installez les dépendances du module")
    return mutagen


# ── Tags ─────────────────────────────────────────────────────────────────────

def _first(tags, *keys) -> str:
    if not tags:
        return ""
    for k in keys:
        try:
            v = tags.get(k)
        except (KeyError, ValueError, TypeError):
            v = None
        if v is None:
            continue
        if isinstance(v, list):
            v = v[0] if v else ""
        if hasattr(v, "text"):
            v = v.text[0] if v.text else ""
        v = str(v).replace("\x00", "").strip()
        if v:
            return v[:300]
    return ""


def _num(s: str) -> int:
    m = re.match(r"\s*(\d+)", s or "")
    return int(m.group(1)) if m else 0


def _embedded_cover(audio) -> tuple[bytes, str] | None:
    tags = getattr(audio, "tags", None)
    pics = getattr(audio, "pictures", None)
    if pics:
        return pics[0].data, pics[0].mime or "image/jpeg"
    if tags is None:
        return None
    if hasattr(tags, "getall"):
        apic = tags.getall("APIC")
        if apic:
            return apic[0].data, apic[0].mime or "image/jpeg"
    covr = tags.get("covr") if hasattr(tags, "get") else None
    if covr:
        data = bytes(covr[0])
        return data, "image/png" if data[:4] == b"\x89PNG" else "image/jpeg"
    mbp = tags.get("metadata_block_picture") if hasattr(tags, "get") else None
    if mbp:
        try:
            from mutagen.flac import Picture
            pic = Picture(base64.b64decode(mbp[0]))
            return pic.data, pic.mime or "image/jpeg"
        except Exception:
            return None
    return None


def _folder_cover(path: str) -> str | None:
    folder = os.path.dirname(path)
    try:
        names = os.listdir(folder)
    except OSError:
        return None
    for n in names:
        stem, ext = os.path.splitext(n)
        if ext.lower() in (".jpg", ".jpeg", ".png", ".webp") and stem.lower() in COVER_NAMES:
            return os.path.join(folder, n)
    return None


def read_tags(path: str) -> dict:
    mutagen = _mutagen()
    st = os.stat(path)
    out = {"title": "", "artist": "", "album": "", "album_artist": "", "track_no": 0, "disc_no": 0, "year": "",
           "genre": "", "duration": 0.0, "size": st.st_size, "mtime": st.st_mtime, "has_cover": 0}
    try:
        audio = mutagen.File(path)
    except Exception as exc:
        log.info("tags unreadable for %s: %s", os.path.basename(path), exc)
        audio = None
    if audio is not None:
        out["duration"] = float(getattr(audio.info, "length", 0) or 0)
        tags = audio.tags
        out["title"] = _first(tags, "title", "TIT2", "\xa9nam", "TITLE")
        out["artist"] = _first(tags, "artist", "TPE1", "\xa9ART", "ARTIST")
        out["album"] = _first(tags, "album", "TALB", "\xa9alb", "ALBUM")
        out["album_artist"] = _first(tags, "albumartist", "TPE2", "aART", "ALBUMARTIST")
        out["genre"] = _first(tags, "genre", "TCON", "\xa9gen", "GENRE")
        out["year"] = _first(tags, "date", "TDRC", "TYER", "\xa9day", "DATE", "year")[:4]
        trk = _first(tags, "tracknumber", "TRCK", "TRACKNUMBER")
        if not trk and tags is not None and hasattr(tags, "get") and tags.get("trkn"):
            trk = str(tags.get("trkn")[0][0])
        out["track_no"] = _num(trk)
        out["disc_no"] = _num(_first(tags, "discnumber", "TPOS", "DISCNUMBER"))
        out["has_cover"] = int(_embedded_cover(audio) is not None)
    if not out["has_cover"] and _folder_cover(path):
        out["has_cover"] = 1
    if not out["title"]:
        out["title"] = os.path.splitext(os.path.basename(path))[0]
    return out


def cover_bytes(path: str) -> tuple[bytes, str] | None:
    try:
        audio = _mutagen().File(path)
        pic = _embedded_cover(audio) if audio is not None else None
        if pic:
            return pic
    except Exception:
        pass
    alt = _folder_cover(path)
    if alt:
        with open(alt, "rb") as f:
            return f.read(), mimetypes.guess_type(alt)[0] or "image/jpeg"
    return None


# ── Library ──────────────────────────────────────────────────────────────────

def _check_dir(raw: str) -> str:
    path = (raw or "").strip().strip('"')
    if not path:
        raise MusicError("PATH_REQUIRED", "Indiquez le chemin d'un dossier")
    if not os.path.isabs(path):
        raise MusicError("BAD_PATH", "Le chemin doit être absolu (ex. D:\\Musique)")
    if not os.path.isdir(path):
        raise MusicError("NOT_FOUND", f"Dossier introuvable : {path}")
    return os.path.normpath(os.path.realpath(path))


@module.on("folders.list")
async def folders_list(payload: dict):
    rows = await (await _db()).fetch_all(
        "SELECT f.*, (SELECT COUNT(*) FROM tracks t WHERE t.folder_id=f.id) AS count FROM folders f ORDER BY f.path")
    for r in rows:
        r["exists"] = os.path.isdir(r["path"])
    return {"folders": rows}


@module.on("folder.add")
@guarded
async def folder_add(payload: dict):
    path = _check_dir(str(payload.get("path") or ""))
    db = await _db()
    folders = await db.fetch_all("SELECT * FROM folders")
    for f in folders:
        if _inside(path, f["path"]):
            return {"id": f["id"], "path": f["path"], "existing": True}
    row = await db.fetch_one("INSERT INTO folders (path, added_at) VALUES (?,?) RETURNING id, path", (path, _now()))
    for f in folders:
        if _inside(f["path"], path):
            await db.execute("UPDATE tracks SET folder_id=? WHERE folder_id=?", (row["id"], f["id"]))
            await db.execute("DELETE FROM folders WHERE id=?", (f["id"],))
    return {**row, "existing": False}


@module.on("folder.remove")
async def folder_remove(payload: dict):
    db = await _db()
    fid = _int(payload.get("id"))
    if not await db.fetch_one("SELECT id FROM folders WHERE id=?", (fid,)):
        return _err("Dossier introuvable", "NOT_FOUND")
    await db.execute("DELETE FROM tracks WHERE folder_id=?", (fid,))
    await db.execute("DELETE FROM folders WHERE id=?", (fid,))
    return {"ok": True}


def _walk(folder: str) -> list[str]:
    out = []
    for root, dirs, files in os.walk(folder):
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        for f in files:
            if os.path.splitext(f)[1].lower() in AUDIO_EXTS and not f.startswith("._"):
                out.append(os.path.join(root, f))
    return out


@module.job("scan")
async def scan(payload: dict, job):
    db = await _db()
    fid = _int(payload.get("folder_id"))
    folders = await db.fetch_all("SELECT * FROM folders" + (" WHERE id=?" if fid else ""), (fid,) if fid else ())
    if not folders:
        raise MusicError("NOT_FOUND", "Aucun dossier : ajoutez d'abord un dossier de musique")
    stats = {"added": 0, "updated": 0, "removed": 0, "unchanged": 0, "failed": 0}
    for folder in folders:
        if not os.path.isdir(folder["path"]):
            continue
        await job.progress(0, f"Recherche des fichiers dans {folder['path']}…")
        files = await asyncio.to_thread(_walk, folder["path"])
        known = {_norm(r["path"]): r for r in await db.fetch_all(
            "SELECT id, path, size, mtime FROM tracks WHERE folder_id=?", (folder["id"],))}
        seen, last = set(), 0.0
        for i, path in enumerate(files):
            key = _norm(path)
            seen.add(key)
            if time.monotonic() - last > 0.25:
                last = time.monotonic()
                await job.progress(round(i * 100 / max(1, len(files)), 1), f"{i}/{len(files)} — {os.path.basename(path)}")
            try:
                st = os.stat(path)
            except OSError:
                continue
            row = known.get(key)
            if row and row["size"] == st.st_size and abs(row["mtime"] - st.st_mtime) < 1:
                stats["unchanged"] += 1
                continue
            try:
                t = await asyncio.to_thread(read_tags, path)
            except Exception as exc:
                log.info("scan failed for %s: %s", os.path.basename(path), exc)
                stats["failed"] += 1
                continue
            vals = (t["title"], t["artist"], t["album"], t["album_artist"], t["track_no"], t["disc_no"], t["year"],
                    t["genre"], t["duration"], t["size"], t["mtime"], t["has_cover"])
            if row:
                await db.execute("UPDATE tracks SET title=?, artist=?, album=?, album_artist=?, track_no=?, disc_no=?,"
                                 " year=?, genre=?, duration=?, size=?, mtime=?, has_cover=? WHERE id=?", (*vals, row["id"]))
                stats["updated"] += 1
            else:
                await db.execute("INSERT INTO tracks (folder_id, path, title, artist, album, album_artist, track_no,"
                                 " disc_no, year, genre, duration, size, mtime, has_cover, added_at)"
                                 " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (folder["id"], path, *vals, _now()))
                stats["added"] += 1
        gone = [r["id"] for k, r in known.items() if k not in seen]
        for i in range(0, len(gone), 500):
            chunk = gone[i:i + 500]
            await db.execute(f"DELETE FROM tracks WHERE id IN ({','.join('?' * len(chunk))})", tuple(chunk))
        stats["removed"] += len(gone)
        await db.execute("UPDATE folders SET last_scan=? WHERE id=?", (_now(), folder["id"]))
    await job.progress(100, "Terminé")
    return stats


@module.on("tracks.list")
async def tracks_list(payload: dict):
    rows = await (await _db()).fetch_all(
        f"SELECT {TRACK_COLS} FROM tracks ORDER BY artist COLLATE NOCASE, album COLLATE NOCASE, disc_no, track_no, title COLLATE NOCASE")
    for r in rows:
        r.pop("path")
    return {"tracks": rows}


@module.on("track.played")
async def track_played(payload: dict):
    n = await (await _db()).execute("UPDATE tracks SET play_count=play_count+1, last_played=? WHERE id=?",
                                    (_now(), _int(payload.get("id"))))
    return {"ok": bool(n)}


@module.on("track.info")
async def track_info(payload: dict):
    row = await (await _db()).fetch_one("SELECT * FROM tracks WHERE id=?", (_int(payload.get("id")),))
    if not row:
        return _err("Morceau introuvable", "NOT_FOUND")
    row["exists"] = os.path.isfile(row["path"])
    return row


@module.on("track.reveal")
async def track_reveal(payload: dict):
    row = await (await _db()).fetch_one("SELECT path FROM tracks WHERE id=?", (_int(payload.get("id")),))
    if not row or not os.path.isfile(row["path"]):
        return _err("Morceau introuvable", "NOT_FOUND")
    if not hasattr(os, "startfile"):
        return _err("Non pris en charge sur ce système", "UNSUPPORTED")
    await asyncio.to_thread(os.startfile, os.path.dirname(row["path"]))
    return {"ok": True}


# ── Playlists ────────────────────────────────────────────────────────────────

class PlaylistIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)

    @field_validator("name")
    @classmethod
    def _strip(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("Le nom est requis")
        return v


@module.on("playlists.list")
async def playlists_list(payload: dict):
    rows = await (await _db()).fetch_all(
        "SELECT p.id, p.name, COUNT(i.id) AS count, COALESCE(SUM(t.duration),0) AS duration FROM playlists p"
        " LEFT JOIN playlist_items i ON i.playlist_id=p.id LEFT JOIN tracks t ON t.id=i.track_id"
        " GROUP BY p.id ORDER BY p.name COLLATE NOCASE")
    return {"playlists": rows}


@module.on("playlist.save")
async def playlist_save(payload: dict):
    try:
        p = PlaylistIn(name=payload.get("name") or "")
    except ValidationError as exc:
        return _err(_validation_message(exc))
    db = await _db()
    pid = _int(payload.get("id"))
    if pid:
        if not await db.execute("UPDATE playlists SET name=? WHERE id=?", (p.name, pid)):
            return _err("Playlist introuvable", "NOT_FOUND")
        return {"id": pid}
    row = await db.fetch_one("INSERT INTO playlists (name, created_at) VALUES (?,?) RETURNING id", (p.name, _now()))
    return {"id": row["id"]}


@module.on("playlist.delete")
async def playlist_delete(payload: dict):
    if not await (await _db()).execute("DELETE FROM playlists WHERE id=?", (_int(payload.get("id")),)):
        return _err("Playlist introuvable", "NOT_FOUND")
    return {"ok": True}


@module.on("playlist.get")
async def playlist_get(payload: dict):
    db = await _db()
    pl = await db.fetch_one("SELECT * FROM playlists WHERE id=?", (_int(payload.get("id")),))
    if not pl:
        return _err("Playlist introuvable", "NOT_FOUND")
    items = await db.fetch_all("SELECT i.id AS item_id, i.track_id FROM playlist_items i JOIN tracks t ON t.id=i.track_id"
                               " WHERE i.playlist_id=? ORDER BY i.position, i.id", (pl["id"],))
    return {**pl, "items": items}


@module.on("playlist.add")
async def playlist_add(payload: dict):
    db = await _db()
    pl = await db.fetch_one("SELECT id FROM playlists WHERE id=?", (_int(payload.get("id")),))
    if not pl:
        return _err("Playlist introuvable", "NOT_FOUND")
    ids = payload.get("track_ids")
    if not isinstance(ids, list) or not ids or len(ids) > 10000:
        return _err("track_ids : liste de 1 à 10000 identifiants")
    valid = {r["id"] for r in await db.fetch_all("SELECT id FROM tracks")}
    pos = (await db.fetch_one("SELECT COALESCE(MAX(position)+1,0) AS p FROM playlist_items WHERE playlist_id=?", (pl["id"],)))["p"]
    added = 0
    for tid in ids:
        tid = _int(tid)
        if tid in valid:
            await db.execute("INSERT INTO playlist_items (playlist_id, track_id, position) VALUES (?,?,?)", (pl["id"], tid, pos))
            pos += 1
            added += 1
    return {"added": added}


@module.on("playlist.set_items")
async def playlist_set_items(payload: dict):
    db = await _db()
    pl = await db.fetch_one("SELECT id FROM playlists WHERE id=?", (_int(payload.get("id")),))
    if not pl:
        return _err("Playlist introuvable", "NOT_FOUND")
    ids = payload.get("track_ids")
    if not isinstance(ids, list) or len(ids) > 10000:
        return _err("track_ids : liste attendue")
    valid = {r["id"] for r in await db.fetch_all("SELECT id FROM tracks")}
    await db.execute("DELETE FROM playlist_items WHERE playlist_id=?", (pl["id"],))
    for pos, tid in enumerate(t for t in (_int(x) for x in ids) if t in valid):
        await db.execute("INSERT INTO playlist_items (playlist_id, track_id, position) VALUES (?,?,?)", (pl["id"], tid, pos))
    return {"ok": True}


# ── Podcasts ─────────────────────────────────────────────────────────────────

def _downloads_dir(pid: int) -> str:
    path = module.data_path("podcasts") / str(pid)
    path.mkdir(parents=True, exist_ok=True)
    return str(path)


async def _store_feed(pid: int, feed: dict) -> int:
    db = await _db()
    await db.execute("UPDATE podcasts SET title=?, author=?, description=?, image=?, site_url=?, last_refresh=?, error=''"
                     " WHERE id=?", (feed["title"], feed["author"], feed["description"], feed["image"], feed["site_url"],
                                    _now(), pid))
    new = 0
    for e in feed["episodes"]:
        n = await db.execute(
            "INSERT INTO episodes (podcast_id, guid, title, description, published, duration, audio_url, mime, size, image, link)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(podcast_id, guid) DO UPDATE SET title=excluded.title,"
            " description=excluded.description, published=excluded.published, duration=COALESCE(excluded.duration, duration),"
            " audio_url=excluded.audio_url, mime=excluded.mime, size=excluded.size, image=excluded.image, link=excluded.link",
            (pid, e["guid"], e["title"], e["description"], e["published"], e["duration"], e["audio_url"], e["mime"],
             e["size"], e["image"], e["link"]))
        new += n
    return new


async def _refresh(pod: dict) -> dict:
    try:
        data, final = await asyncio.to_thread(fetch, pod["url"])
        feed = await asyncio.to_thread(parse_podcast, data, final)
    except FeedError as exc:
        await (await _db()).execute("UPDATE podcasts SET error=?, last_refresh=? WHERE id=?", (exc.message, _now(), pod["id"]))
        return {"id": pod["id"], "error": exc.message, "code": exc.code}
    before = (await (await _db()).fetch_one("SELECT COUNT(*) AS n FROM episodes WHERE podcast_id=?", (pod["id"],)))["n"]
    await _store_feed(pod["id"], feed)
    after = (await (await _db()).fetch_one("SELECT COUNT(*) AS n FROM episodes WHERE podcast_id=?", (pod["id"],)))["n"]
    return {"id": pod["id"], "new": after - before}


@module.on("podcasts.list")
async def podcasts_list(payload: dict):
    rows = await (await _db()).fetch_all(
        "SELECT p.*, (SELECT COUNT(*) FROM episodes e WHERE e.podcast_id=p.id) AS episodes,"
        " (SELECT COUNT(*) FROM episodes e WHERE e.podcast_id=p.id AND e.played=0) AS unplayed,"
        " (SELECT MAX(published) FROM episodes e WHERE e.podcast_id=p.id) AS latest FROM podcasts p ORDER BY latest DESC")
    return {"podcasts": rows}


@module.on("podcast.add")
@guarded
async def podcast_add(payload: dict):
    url = normalize_url(str(payload.get("url") or ""))
    db = await _db()
    if await db.fetch_one("SELECT id FROM podcasts WHERE url=?", (url,)):
        raise MusicError("DUPLICATE", "Vous êtes déjà abonné à ce podcast")
    data, final = await asyncio.to_thread(fetch, url)
    feed = await asyncio.to_thread(parse_podcast, data, final)
    if not feed["episodes"]:
        raise MusicError("NO_EPISODES", "Ce flux ne contient aucun épisode audio")
    row = await db.fetch_one("INSERT INTO podcasts (url, title, added_at) VALUES (?,?,?) RETURNING id", (url, feed["title"], _now()))
    await _store_feed(row["id"], feed)
    return {"id": row["id"], "title": feed["title"], "episodes": len(feed["episodes"])}


@module.on("podcast.refresh")
async def podcast_refresh(payload: dict):
    pod = await (await _db()).fetch_one("SELECT * FROM podcasts WHERE id=?", (_int(payload.get("id")),))
    if not pod:
        return _err("Podcast introuvable", "NOT_FOUND")
    return await _refresh(pod)


@module.job("podcasts.refresh_all")
async def podcasts_refresh_all(payload: dict, job):
    pods = await (await _db()).fetch_all("SELECT * FROM podcasts")
    total_new, errors = 0, []
    for i, pod in enumerate(pods):
        await job.progress(round(i * 100 / max(1, len(pods))), pod["title"])
        r = await _refresh(pod)
        total_new += r.get("new", 0)
        if "error" in r:
            errors.append({"title": pod["title"], "error": r["error"]})
    return {"new": total_new, "errors": errors, "count": len(pods)}


@module.on("podcast.delete")
async def podcast_delete(payload: dict):
    db = await _db()
    pid = _int(payload.get("id"))
    if not await db.execute("DELETE FROM podcasts WHERE id=?", (pid,)):
        return _err("Podcast introuvable", "NOT_FOUND")
    folder = module.data_path("podcasts") / str(pid)
    if folder.is_dir():
        import shutil
        await asyncio.to_thread(shutil.rmtree, str(folder), True)
    return {"ok": True}


@module.on("episodes.list")
async def episodes_list(payload: dict):
    db = await _db()
    pid = _int(payload.get("podcast_id"))
    cond, params = ("e.podcast_id=?", [pid]) if pid else ("1=1", [])
    flt = payload.get("filter")
    if flt == "unplayed":
        cond += " AND e.played=0"
    elif flt == "downloaded":
        cond += " AND e.local_path != ''"
    elif flt == "progress":
        cond += " AND e.position > 0 AND e.played=0"
    limit = max(1, min(2000, _int(payload.get("limit"), 500)))
    rows = await db.fetch_all(
        f"SELECT e.id, e.podcast_id, e.title, substr(e.description,1,600) AS summary, e.published, e.duration, e.size,"
        f" e.position, e.played, e.local_path != '' AS downloaded, e.image, e.link, p.title AS podcast, p.image AS podcast_image"
        f" FROM episodes e JOIN podcasts p ON p.id=e.podcast_id WHERE {cond} ORDER BY e.published DESC, e.id DESC LIMIT ?",
        (*params, limit))
    for r in rows:
        r["downloading"] = r["id"] in _downloads
    return {"episodes": rows}


@module.on("episode.get")
async def episode_get(payload: dict):
    row = await (await _db()).fetch_one(
        "SELECT e.*, p.title AS podcast, p.image AS podcast_image FROM episodes e JOIN podcasts p ON p.id=e.podcast_id"
        " WHERE e.id=?", (_int(payload.get("id")),))
    if not row:
        return _err("Épisode introuvable", "NOT_FOUND")
    row["downloaded"] = bool(row.pop("local_path"))
    return row


@module.on("episode.position")
async def episode_position(payload: dict):
    try:
        pos = max(0.0, float(payload.get("position") or 0))
    except (TypeError, ValueError):
        return _err("position : nombre attendu")
    db = await _db()
    eid = _int(payload.get("id"))
    if payload.get("finished"):
        n = await db.execute("UPDATE episodes SET position=0, played=1 WHERE id=?", (eid,))
    else:
        n = await db.execute("UPDATE episodes SET position=? WHERE id=?", (pos, eid))
    return {"ok": bool(n)}


@module.on("episode.mark")
async def episode_mark(payload: dict):
    db = await _db()
    played = 1 if payload.get("played") else 0
    ids = payload.get("ids") if isinstance(payload.get("ids"), list) else [payload.get("id")]
    n = 0
    for eid in ids[:5000]:
        n += await db.execute("UPDATE episodes SET played=?, position=CASE WHEN ? THEN 0 ELSE position END WHERE id=?",
                              (played, played, _int(eid)))
    return {"updated": n}


def _download_to(url: str, dest: str, progress) -> int:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    tmp = dest + ".part"
    done = 0
    try:
        with opener().open(req, timeout=30) as resp, open(tmp, "wb") as f:
            total = int(resp.headers.get("Content-Length") or 0)
            while chunk := resp.read(256 * 1024):
                done += len(chunk)
                if done > MAX_DOWNLOAD:
                    raise MusicError("TOO_LARGE", "Fichier trop volumineux (2 Go max.)")
                f.write(chunk)
                progress(done, total)
        os.replace(tmp, dest)
    except urllib.error.HTTPError as exc:
        raise MusicError("HTTP_ERROR", f"Le serveur a répondu {exc.code} {exc.reason}")
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise MusicError("NETWORK_ERROR", f"Téléchargement impossible : {getattr(exc, 'reason', exc)}")
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass
    return done


def _ext_for(url: str, mime: str) -> str:
    ext = os.path.splitext(urllib.parse.urlparse(url).path)[1].lower()
    if ext in AUDIO_EXTS or ext in (".mp4", ".m4v"):
        return ext
    return mimetypes.guess_extension(mime or "") or ".mp3"


@module.job("episode.download")
async def episode_download(payload: dict, job):
    db = await _db()
    ep = await db.fetch_one("SELECT * FROM episodes WHERE id=?", (_int(payload.get("id")),))
    if not ep:
        raise MusicError("NOT_FOUND", "Épisode introuvable")
    if ep["local_path"] and os.path.isfile(ep["local_path"]):
        return {"id": ep["id"], "size": os.path.getsize(ep["local_path"])}
    if ep["id"] in _downloads:
        raise MusicError("BUSY", "Téléchargement déjà en cours")
    dest = os.path.join(_downloads_dir(ep["podcast_id"]), f"{ep['id']}{_ext_for(ep['audio_url'], ep['mime'])}")
    loop = asyncio.get_running_loop()
    state = {"last": 0.0}

    def progress(done, total):
        if time.monotonic() - state["last"] > 0.3:
            state["last"] = time.monotonic()
            pct = round(done * 100 / total, 1) if total else None
            msg = f"{done / 1048576:.1f} Mo" + (f" / {total / 1048576:.1f} Mo" if total else "")
            asyncio.run_coroutine_threadsafe(job.progress(pct, msg, episode_id=ep["id"]), loop)
    _downloads[ep["id"]] = dest
    try:
        size = await asyncio.to_thread(_download_to, ep["audio_url"], dest, progress)
    finally:
        _downloads.pop(ep["id"], None)
    await db.execute("UPDATE episodes SET local_path=? WHERE id=?", (dest, ep["id"]))
    return {"id": ep["id"], "size": size}


@module.on("episode.delete_download")
async def episode_delete_download(payload: dict):
    db = await _db()
    ep = await db.fetch_one("SELECT * FROM episodes WHERE id=?", (_int(payload.get("id")),))
    if not ep or not ep["local_path"]:
        return _err("Aucun fichier téléchargé", "NOT_FOUND")
    if _inside(ep["local_path"], str(module.data_path("podcasts"))):
        try:
            os.remove(ep["local_path"])
        except OSError:
            pass
    await db.execute("UPDATE episodes SET local_path='' WHERE id=?", (ep["id"],))
    return {"ok": True}


@module.on("open_url")
async def open_url(payload: dict):
    url = str(payload.get("url") or "").strip()
    if not re.match(r"^https?://\S+$", url, re.I) or len(url) > 2000:
        return _err("Adresse refusée (http/https uniquement)", "BAD_URL")
    await asyncio.to_thread(webbrowser.open, url)
    return {"ok": True}


# ── HTTP routes ──────────────────────────────────────────────────────────────

async def _library_track(tid: int) -> dict:
    db = await _db()
    row = await db.fetch_one("SELECT t.path, f.path AS folder FROM tracks t JOIN folders f ON f.id=t.folder_id WHERE t.id=?", (tid,))
    if not row or not _inside(row["path"], row["folder"]) or not os.path.isfile(row["path"]):
        raise error(404, "Morceau introuvable dans la bibliothèque", "NOT_FOUND")
    return row


@router.get("/stream/{tid}")
async def stream_track(tid: int):
    row = await _library_track(tid)
    ext = os.path.splitext(row["path"])[1].lower()
    return FileResponse(row["path"], media_type=AUDIO_EXTS.get(ext, "application/octet-stream"),
                        headers={"Cache-Control": "no-store"})


@router.get("/cover/{tid}")
async def track_cover(tid: int):
    row = await _library_track(tid)
    pic = await asyncio.to_thread(cover_bytes, row["path"])
    if not pic:
        raise error(404, "Pas de pochette", "NOT_FOUND")
    data, mime = pic
    if not mime.startswith("image/") or mime == "image/svg+xml":
        mime = "image/jpeg"
    return Response(data, media_type=mime, headers={"Cache-Control": "private, max-age=3600"})


def _proxy_open(url: str, rng: str | None):
    headers = {"User-Agent": USER_AGENT}
    if rng and re.fullmatch(r"bytes=\d*-\d*", rng):
        headers["Range"] = rng
    req = urllib.request.Request(url, headers=headers)
    try:
        return opener().open(req, timeout=30)
    except urllib.error.HTTPError as exc:
        if exc.code == 416:
            return exc
        raise MusicError("HTTP_ERROR", f"Le serveur a répondu {exc.code}")
    except (urllib.error.URLError, TimeoutError, OSError, FeedError) as exc:
        raise MusicError("NETWORK_ERROR", f"Lecture impossible : {getattr(exc, 'reason', exc)}")


def _iter_body(resp):
    try:
        while chunk := resp.read(64 * 1024):
            yield chunk
    finally:
        resp.close()


@router.get("/episode/{eid}")
async def episode_audio(eid: int, request: Request):
    ep = await (await _db()).fetch_one("SELECT audio_url, local_path, mime FROM episodes WHERE id=?", (eid,))
    if not ep:
        raise error(404, "Épisode introuvable", "NOT_FOUND")
    if ep["local_path"] and os.path.isfile(ep["local_path"]) and _inside(ep["local_path"], str(module.data_path("podcasts"))):
        mime = AUDIO_EXTS.get(os.path.splitext(ep["local_path"])[1].lower()) or ep["mime"] or "audio/mpeg"
        return FileResponse(ep["local_path"], media_type=mime)
    try:
        resp = await asyncio.to_thread(_proxy_open, ep["audio_url"], request.headers.get("range"))
    except MusicError as exc:
        raise error(502, exc.message, exc.code)
    status = getattr(resp, "status", None) or getattr(resp, "code", 200)
    headers = {"Accept-Ranges": "bytes", "Cache-Control": "no-store"}
    for h in ("Content-Length", "Content-Range"):
        v = resp.headers.get(h)
        if v:
            headers[h] = v
    mime = (resp.headers.get("Content-Type") or ep["mime"] or "audio/mpeg").split(";")[0]
    if not (mime.startswith("audio/") or mime.startswith("video/")):
        mime = ep["mime"] if (ep["mime"] or "").startswith(("audio/", "video/")) else "audio/mpeg"
    return StreamingResponse(_iter_body(resp), status_code=status, media_type=mime, headers=headers)
