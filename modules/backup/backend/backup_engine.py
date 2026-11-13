import asyncio
import fnmatch
import gzip
import hashlib
import io
import json
import logging
import os
import posixpath
import shutil
import stat
import threading
import time
import uuid
import zipfile
import zlib
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Iterator, Optional

log = logging.getLogger("ledroide.backup.engine")

FORMAT = "ledroide-backup"
CHUNK = 1024 * 1024
SNAP_FMT = "%Y-%m-%d_%H%M%S"
NO_COMPRESS_EXT = {
    ".zip", ".7z", ".rar", ".gz", ".bz2", ".xz", ".zst", ".jpg", ".jpeg", ".png", ".gif", ".webp", ".heic",
    ".mp3", ".mp4", ".m4a", ".mkv", ".avi", ".mov", ".webm", ".ogg", ".flac", ".docx", ".xlsx", ".pptx",
    ".odt", ".ods", ".pdf", ".jar", ".apk", ".cab", ".msi",
}


class BackupError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class Cancelled(Exception):
    pass


class Ctl:
    """Shared state between a worker thread and the coroutine that relays progress."""

    def __init__(self):
        self.cancel = threading.Event()
        self.pct = 0.0
        self.msg = ""
        self.log: list[str] = []

    def progress(self, pct: float, msg: str):
        self.pct, self.msg = max(0.0, min(100.0, pct)), msg

    def check(self):
        if self.cancel.is_set():
            raise Cancelled()

    def note(self, line: str):
        if len(self.log) < 2000:
            self.log.append(line)
        elif len(self.log) == 2000:
            self.log.append("… journal tronqué")


# ── Exclusions ───────────────────────────────────────────────────────────────

def clean_excludes(raw) -> list[str]:
    if isinstance(raw, str):
        raw = raw.replace(";", "\n").splitlines()
    out: list[str] = []
    for p in raw or []:
        p = str(p).strip().replace("\\", "/").strip("/")
        if p and p not in out:
            out.append(p[:200])
    return out[:200]


def excluded(rel: str, name: str, patterns: list[str]) -> bool:
    low_rel, low_name = rel.lower(), name.lower()
    for p in patterns:
        lp = p.lower()
        if "/" in lp:
            if fnmatch.fnmatchcase(low_rel, lp) or fnmatch.fnmatchcase(low_rel, lp + "/*"):
                return True
        elif fnmatch.fnmatchcase(low_name, lp):
            return True
    return False


def source_keys(sources: list[str]) -> dict[str, str]:
    keys: dict[str, str] = {}
    for src in sources:
        base = Path(src).name or (src.replace(":", "").strip("\\/") or "racine")
        base = "".join(c if c.isalnum() or c in " ._-" else "_" for c in base).strip() or "source"
        key, n = base, 2
        while key.lower() in (k.lower() for k in keys):
            key, n = f"{base}_{n}", n + 1
        keys[key] = src
    return keys


def scan(ctl: Ctl, sources: dict[str, str], patterns: list[str]) -> tuple[dict, list[str], list[str]]:
    files: dict[str, dict] = {}
    dirs: list[str] = []
    errors: list[str] = []
    count = 0
    for key, root in sources.items():
        if not os.path.isdir(root):
            raise BackupError("SOURCE_NOT_FOUND", f"Dossier source introuvable : {root}")
        dirs.append(key)
        stack = [(root, "")]
        while stack:
            ctl.check()
            cur, rel = stack.pop()
            try:
                it = list(os.scandir(cur))
            except OSError as exc:
                errors.append(f"{key}/{rel} : {exc.strerror or exc}")
                continue
            for e in it:
                r = f"{rel}/{e.name}" if rel else e.name
                if excluded(r, e.name, patterns):
                    continue
                try:
                    if e.is_symlink() or _is_junction(e):
                        continue
                    if e.is_dir(follow_symlinks=False):
                        dirs.append(f"{key}/{r}")
                        stack.append((e.path, r))
                    elif e.is_file(follow_symlinks=False):
                        st = e.stat(follow_symlinks=False)
                        files[f"{key}/{r}"] = {"abs": e.path, "s": st.st_size, "m": st.st_mtime_ns}
                        count += 1
                        if count % 500 == 0:
                            ctl.progress(2, f"Analyse : {count} fichiers")
                except OSError as exc:
                    errors.append(f"{key}/{r} : {exc.strerror or exc}")
    return files, dirs, errors


def _is_junction(entry: os.DirEntry) -> bool:
    try:
        return bool(getattr(os.path, "isjunction", lambda _p: False)(entry.path))
    except OSError:
        return False


def hash_file(path: str, ctl: Optional[Ctl] = None) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(CHUNK):
            if ctl:
                ctl.check()
            h.update(chunk)
    return h.hexdigest()


# ── Stores ───────────────────────────────────────────────────────────────────

class LocalStore:
    """Destination on a local disk, external drive or UNC share."""

    remote = False

    def __init__(self, root: str):
        self.root = Path(root)

    def label(self) -> str:
        return str(self.root)

    def open(self, create: bool = False):
        parent = self.root.parent
        if not self.root.exists():
            if not create:
                raise BackupError("DEST_UNAVAILABLE", f"Destination introuvable : {self.root}")
            if not parent.exists():
                raise BackupError("DEST_UNAVAILABLE", f"Destination indisponible (disque débranché ?) : {parent}")
            self.root.mkdir(parents=True, exist_ok=True)
        (self.root / "snapshots").mkdir(exist_ok=True)

    def close(self):
        pass

    def can_hardlink(self) -> bool:
        a = self.root / f".linktest-{uuid.uuid4().hex[:8]}"
        b = a.with_name(a.name + "-2")
        try:
            a.write_bytes(b"x")
            os.link(a, b)
            return True
        except OSError:
            return False
        finally:
            for p in (a, b):
                try:
                    p.unlink()
                except OSError:
                    pass

    def read_meta(self, name: str) -> Optional[dict]:
        p = self.root / name
        if not p.exists():
            return None
        data = p.read_bytes()
        if name.endswith(".gz"):
            data = gzip.decompress(data)
        return json.loads(data.decode("utf-8"))

    def write_meta(self, name: str, obj: dict):
        p = self.root / name
        p.parent.mkdir(parents=True, exist_ok=True)
        data = json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if name.endswith(".gz"):
            data = gzip.compress(data, 6)
        tmp = p.with_name(p.name + ".tmp")
        tmp.write_bytes(data)
        os.replace(tmp, p)

    def list_snapshots(self) -> list[str]:
        base = self.root / "snapshots"
        if not base.is_dir():
            return []
        return sorted(d.name for d in base.iterdir()
                      if d.is_dir() and not d.name.endswith(".partial") and (d / "manifest.json.gz").exists())

    def list_partials(self) -> list[str]:
        base = self.root / "snapshots"
        if not base.is_dir():
            return []
        return [d.name for d in base.iterdir() if d.is_dir() and (d.name.endswith(".partial")
                                                                   or not (d / "manifest.json.gz").exists())]

    def snap_dir(self, snap: str) -> Path:
        return self.root / "snapshots" / snap

    def rename_snapshot(self, old: str, new: str):
        os.replace(self.snap_dir(old), self.snap_dir(new))

    def remove_snapshot(self, snap: str):
        _rmtree(self.snap_dir(snap))

    def _blob(self, h: str, z: bool) -> Path:
        return self.root / "store" / h[:2] / (h + (".z" if z else ""))

    def blobs(self) -> dict[str, bool]:
        out: dict[str, bool] = {}
        base = self.root / "store"
        if not base.is_dir():
            return out
        for sub in base.iterdir():
            if sub.is_dir() and len(sub.name) == 2:
                for f in sub.iterdir():
                    n = f.name
                    if n.endswith(".z"):
                        out[n[:-2]] = True
                    elif len(n) == 64:
                        out.setdefault(n, False)
        return out

    def put_blob_stream(self, src: str, ctl: Ctl, compress: bool, known: dict) -> tuple[str, bool, int]:
        tmp_dir = self.root / "store" / "tmp"
        tmp_dir.mkdir(parents=True, exist_ok=True)
        tmp = tmp_dir / uuid.uuid4().hex
        h = hashlib.sha256()
        comp = zlib.compressobj(6) if compress else None
        written = 0
        try:
            with open(src, "rb") as fi, open(tmp, "wb") as fo:
                while chunk := fi.read(CHUNK):
                    ctl.check()
                    h.update(chunk)
                    out = comp.compress(chunk) if comp else chunk
                    fo.write(out)
                    written += len(out)
                if comp:
                    tail = comp.flush()
                    fo.write(tail)
                    written += len(tail)
            digest = h.hexdigest()
            if digest in known:
                tmp.unlink()
                return digest, known[digest], 0
            dst = self._blob(digest, compress)
            dst.parent.mkdir(parents=True, exist_ok=True)
            os.replace(tmp, dst)
            known[digest] = compress
            return digest, compress, written
        except BaseException:
            try:
                tmp.unlink()
            except OSError:
                pass
            raise

    def read_blob(self, h: str, z: bool) -> Iterator[bytes]:
        with open(self._blob(h, z), "rb") as f:
            d = zlib.decompressobj() if z else None
            while chunk := f.read(CHUNK):
                yield d.decompress(chunk) if d else chunk
            if d:
                tail = d.flush()
                if tail:
                    yield tail

    def remove_blob(self, h: str, z: bool):
        try:
            self._blob(h, z).unlink()
        except FileNotFoundError:
            pass

    def cleanup_tmp(self):
        tmp = self.root / "store" / "tmp"
        if tmp.is_dir():
            _rmtree(tmp)


class SftpStore:
    """Deduplicated store on an SFTP server (asyncssh, own event loop in the worker thread)."""

    remote = True

    def __init__(self, cfg: dict, password: Optional[str], passphrase: Optional[str]):
        self.cfg = cfg
        self.password = password
        self.passphrase = passphrase
        self.root = posixpath.join(cfg["path"].rstrip("/") or "/", cfg["folder"])
        self.loop: Optional[asyncio.AbstractEventLoop] = None
        self.conn = None
        self.sftp = None
        self.fingerprint: Optional[str] = None

    def label(self) -> str:
        return f"sftp://{self.cfg['user']}@{self.cfg['host']}:{self.cfg.get('port', 22)}{self.root}"

    def _run(self, coro):
        return self.loop.run_until_complete(coro)

    def open(self, create: bool = False, trust_new: bool = False):
        try:
            import asyncssh
        except ImportError:
            raise BackupError("SFTP_UNAVAILABLE", "La bibliothèque asyncssh n'est pas installée (menu « Bibliothèques »).")
        expected = self.cfg.get("fingerprint") or None
        seen: dict = {}

        class Client(asyncssh.SSHClient):
            def validate_host_public_key(self, host, addr, port, key):
                fp = key.get_fingerprint("sha256")
                seen["fp"] = fp
                return fp == expected if expected else trust_new

        opts = {"port": int(self.cfg.get("port") or 22), "username": self.cfg["user"],
                "known_hosts": ([], [], []), "client_factory": Client, "connect_timeout": 20}
        if self.cfg.get("key_file"):
            try:
                opts["client_keys"] = [asyncssh.read_private_key(self.cfg["key_file"], self.passphrase)]
            except (OSError, asyncssh.KeyImportError) as exc:
                raise BackupError("SFTP_KEY", f"Clé privée illisible : {exc}")
        else:
            opts["client_keys"] = None
        if self.password:
            opts["password"] = self.password
        self.loop = asyncio.new_event_loop()
        try:
            self.conn = self._run(asyncssh.connect(self.cfg["host"], **opts))
        except Exception as exc:
            fp = seen.get("fp")
            self.close()
            if fp and expected and fp != expected:
                raise BackupError("HOSTKEY_CHANGED", f"La clé du serveur a changé ({fp}) : connexion refusée.")
            if fp and not expected and not trust_new:
                raise BackupError("HOSTKEY_UNKNOWN", f"Serveur non vérifié ({fp}) : utilisez « Tester la connexion ».")
            if isinstance(exc, asyncssh.PermissionDenied):
                raise BackupError("SFTP_AUTH", "Authentification SFTP refusée")
            raise BackupError("SFTP_CONNECT", f"Connexion SFTP impossible : {exc}")
        self.fingerprint = seen.get("fp")
        self.sftp = self._run(self.conn.start_sftp_client())
        for d in (self.root, self.root + "/snapshots", self.root + "/store"):
            if not self._run(self.sftp.exists(d)):
                if not create:
                    raise BackupError("DEST_UNAVAILABLE", f"Destination SFTP introuvable : {d}")
                self._run(self.sftp.makedirs(d, exist_ok=True))

    def close(self):
        try:
            if self.sftp:
                self.sftp.exit()
            if self.conn:
                self.conn.close()
                self._run(self.conn.wait_closed())
        except Exception:
            pass
        finally:
            if self.loop:
                try:
                    pending = [t for t in asyncio.all_tasks(self.loop) if not t.done()]
                    for t in pending:
                        t.cancel()
                    if pending:
                        self.loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
                    self.loop.run_until_complete(self.loop.shutdown_asyncgens())
                except Exception:
                    pass
                self.loop.close()
            self.loop = self.conn = self.sftp = None

    def can_hardlink(self) -> bool:
        return False

    def read_meta(self, name: str) -> Optional[dict]:
        p = self.root + "/" + name
        if not self._run(self.sftp.exists(p)):
            return None

        async def get():
            async with self.sftp.open(p, "rb") as f:
                return await f.read()
        data = self._run(get())
        if name.endswith(".gz"):
            data = gzip.decompress(data)
        return json.loads(data.decode("utf-8"))

    def write_meta(self, name: str, obj: dict):
        p = self.root + "/" + name
        data = json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if name.endswith(".gz"):
            data = gzip.compress(data, 6)

        async def put():
            await self.sftp.makedirs(posixpath.dirname(p), exist_ok=True)
            async with self.sftp.open(p + ".tmp", "wb") as f:
                await f.write(data)
            if await self.sftp.exists(p):
                await self.sftp.remove(p)
            await self.sftp.rename(p + ".tmp", p)
        self._run(put())

    def list_snapshots(self) -> list[str]:
        base = self.root + "/snapshots"
        names = [n for n in self._run(self.sftp.listdir(base)) if n not in (".", "..") and not n.endswith(".partial")]
        return sorted(n for n in names if self._run(self.sftp.exists(f"{base}/{n}/manifest.json.gz")))

    def list_partials(self) -> list[str]:
        base = self.root + "/snapshots"
        return [n for n in self._run(self.sftp.listdir(base)) if n.endswith(".partial")]

    def rename_snapshot(self, old: str, new: str):
        base = self.root + "/snapshots"
        self._run(self.sftp.rename(f"{base}/{old}", f"{base}/{new}"))

    def remove_snapshot(self, snap: str):
        self._run(self.sftp.rmtree(f"{self.root}/snapshots/{snap}", ignore_errors=True))

    def _blob(self, h: str, z: bool) -> str:
        return f"{self.root}/store/{h[:2]}/{h}{'.z' if z else ''}"

    def blobs(self) -> dict[str, bool]:
        out: dict[str, bool] = {}
        base = self.root + "/store"
        for sub in self._run(self.sftp.listdir(base)):
            if len(sub) != 2:
                continue
            for n in self._run(self.sftp.listdir(f"{base}/{sub}")):
                if n.endswith(".z"):
                    out[n[:-2]] = True
                elif len(n) == 64:
                    out.setdefault(n, False)
        return out

    def put_blob_stream(self, src: str, ctl: Ctl, compress: bool, known: dict) -> tuple[str, bool, int]:
        digest = hash_file(src, ctl)
        if digest in known:
            return digest, known[digest], 0
        dst = self._blob(digest, compress)
        written = 0

        async def up():
            nonlocal written
            await self.sftp.makedirs(posixpath.dirname(dst), exist_ok=True)
            comp = zlib.compressobj(6) if compress else None
            async with self.sftp.open(dst + ".tmp", "wb") as fo:
                with open(src, "rb") as fi:
                    while chunk := fi.read(CHUNK):
                        ctl.check()
                        out = comp.compress(chunk) if comp else chunk
                        await fo.write(out)
                        written += len(out)
                if comp:
                    tail = comp.flush()
                    await fo.write(tail)
                    written += len(tail)
            await self.sftp.rename(dst + ".tmp", dst)
        try:
            self._run(up())
        except BaseException:
            try:
                self._run(self.sftp.remove(dst + ".tmp"))
            except Exception:
                pass
            raise
        known[digest] = compress
        return digest, compress, written

    def read_blob(self, h: str, z: bool) -> Iterator[bytes]:
        f = self._run(self.sftp.open(self._blob(h, z), "rb"))
        try:
            d = zlib.decompressobj() if z else None
            while True:
                chunk = self._run(f.read(CHUNK))
                if not chunk:
                    break
                yield d.decompress(chunk) if d else chunk
            if d:
                tail = d.flush()
                if tail:
                    yield tail
        finally:
            self._run(f.close())

    def remove_blob(self, h: str, z: bool):
        try:
            self._run(self.sftp.remove(self._blob(h, z)))
        except Exception:
            pass

    def cleanup_tmp(self):
        pass


def _rmtree(path: Path):
    def onerror(func, p, _exc):
        try:
            os.chmod(p, stat.S_IWRITE)
            func(p)
        except OSError:
            pass
    if path.exists():
        shutil.rmtree(path, onexc=lambda f, p, e: onerror(f, p, e))


# ── Backup ───────────────────────────────────────────────────────────────────

def new_snapshot_name(existing: set[str], now: Optional[datetime] = None) -> str:
    base = (now or datetime.now()).strftime(SNAP_FMT)
    name, n = base, 2
    while name in existing or name + ".partial" in existing:
        name, n = f"{base}-{n}", n + 1
    return name


def resolve_mode(store, requested: str, compress: bool) -> str:
    meta = store.read_meta("backup.json") or {}
    if meta.get("mode") in ("hardlink", "dedup"):
        return meta["mode"]
    if store.remote or compress or requested == "dedup":
        return "dedup"
    if requested == "hardlink":
        if not store.can_hardlink():
            raise BackupError("NO_HARDLINK", "Cette destination ne prend pas en charge les liens physiques (FAT/exFAT, partage réseau…) : choisissez le mode dédupliqué.")
        return "hardlink"
    return "hardlink" if store.can_hardlink() else "dedup"


def load_manifest(store, snap: str) -> dict:
    m = store.read_meta(f"snapshots/{snap}/manifest.json.gz")
    if not m or m.get("format") != FORMAT:
        raise BackupError("BAD_SNAPSHOT", f"Instantané illisible : {snap}")
    return m


def run_backup(ctl: Ctl, store, cfg: dict, now: Optional[datetime] = None) -> dict:
    """Creates one snapshot. Unchanged files (same size and mtime as in the previous
    snapshot) are hard-linked (hardlink mode) or simply referenced (dedup mode)."""
    started = time.time()
    store.open(create=True)
    meta = store.read_meta("backup.json") or {}
    mode = resolve_mode(store, cfg.get("mode", "auto"), bool(cfg.get("compress")))
    if not meta:
        store.write_meta("backup.json", {"format": FORMAT, "version": 1, "mode": mode, "name": cfg["name"],
                                         "created": datetime.now().isoformat(timespec="seconds")})
    for stale in store.list_partials():
        store.remove_snapshot(stale)
    store.cleanup_tmp()

    sources = source_keys(cfg["sources"])
    patterns = clean_excludes(cfg.get("excludes"))
    ctl.progress(1, "Analyse des sources…")
    files, dirs, errors = scan(ctl, sources, patterns)
    for e in errors:
        ctl.note("Illisible : " + e)

    snaps = store.list_snapshots()
    prev = None
    if snaps:
        try:
            prev = load_manifest(store, snaps[-1])
        except BackupError:
            prev = None
    prev_files = {f["p"]: f for f in prev["files"]} if prev else {}
    prev_mode = prev.get("mode") if prev else None

    name = new_snapshot_name(set(snaps) | set(store.list_partials()), now)
    partial = name + ".partial"
    stats = {"files": len(files), "dirs": len(dirs), "new": 0, "changed": 0, "unchanged": 0,
             "removed": sum(1 for p in prev_files if p not in files), "bytes_total": 0, "bytes_copied": 0,
             "bytes_stored": 0, "errors": len(errors)}
    entries: list[dict] = []
    total_bytes = sum(f["s"] for f in files.values()) or 1
    done_bytes = 0
    last = 0.0
    compress = bool(cfg.get("compress"))
    known = store.blobs() if mode == "dedup" else {}
    files_root = None
    if mode == "hardlink":
        files_root = store.snap_dir(partial) / "files"
        files_root.mkdir(parents=True, exist_ok=True)
        for d in dirs:
            (files_root / d).mkdir(parents=True, exist_ok=True)
        prev_root = store.snap_dir(snaps[-1]) / "files" if prev and prev_mode == "hardlink" else None
    try:
        for i, (p, f) in enumerate(sorted(files.items())):
            ctl.check()
            old = prev_files.get(p)
            same = bool(old and old["s"] == f["s"] and old["m"] == f["m"] and old.get("h"))
            entry = {"p": p, "s": f["s"], "m": f["m"]}
            try:
                if mode == "hardlink":
                    dst = files_root / p
                    linked = False
                    if same and prev_root is not None:
                        try:
                            os.link(prev_root / p, dst)
                            linked = True
                            entry["h"] = old["h"]
                        except OSError:
                            linked = False
                    if not linked:
                        entry["h"] = _copy_hash(f["abs"], dst, ctl)
                        os.utime(dst, ns=(f["m"], f["m"]))
                        stats["bytes_copied"] += f["s"]
                        stats["bytes_stored"] += f["s"]
                else:
                    if same and old["h"] in known:
                        entry["h"], entry["z"] = old["h"], known[old["h"]]
                    else:
                        z = compress and Path(p).suffix.lower() not in NO_COMPRESS_EXT
                        h, zz, written = store.put_blob_stream(f["abs"], ctl, z, known)
                        entry["h"], entry["z"] = h, zz
                        stats["bytes_copied"] += f["s"]
                        stats["bytes_stored"] += written
                if not entry.get("z"):
                    entry.pop("z", None)
            except (Cancelled, KeyboardInterrupt):
                raise
            except OSError as exc:
                stats["errors"] += 1
                ctl.note(f"Erreur : {p} : {exc.strerror or exc}")
                if old and old.get("h") and mode == "dedup" and old["h"] in known:
                    entry = {**old}
                    entries.append(entry)
                continue
            if not old:
                stats["new"] += 1
            elif same:
                stats["unchanged"] += 1
            else:
                stats["changed"] += 1
            stats["bytes_total"] += f["s"]
            entries.append(entry)
            done_bytes += f["s"]
            t = time.monotonic()
            if t - last > 0.2:
                last = t
                ctl.progress(3 + 92 * done_bytes / total_bytes, f"{i + 1}/{len(files)} · {p}")
        ctl.progress(96, "Écriture du manifeste…")
        created = (now or datetime.now()).isoformat(timespec="seconds")
        manifest = {"format": FORMAT, "version": 1, "id": name, "set": cfg["name"], "created": created,
                    "mode": mode, "sources": sources, "excludes": patterns, "dirs": dirs, "files": entries}
        summary = {"id": name, "created": created, "mode": mode, "stats": stats, "sources": sources}
        store.write_meta(f"snapshots/{partial}/manifest.json.gz", manifest)
        store.write_meta(f"snapshots/{partial}/summary.json", summary)
        store.rename_snapshot(partial, name)
    except BaseException:
        try:
            store.remove_snapshot(partial)
            store.cleanup_tmp()
        except Exception:
            log.warning("could not remove partial snapshot %s", partial)
        raise
    stats["duration_s"] = round(time.time() - started, 2)
    ctl.progress(100, "Instantané créé")
    return {"snapshot": name, "mode": mode, "stats": stats}


def _copy_hash(src: str, dst: Path, ctl: Ctl) -> str:
    h = hashlib.sha256()
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name(dst.name + ".lbtmp")
    try:
        with open(src, "rb") as fi, open(tmp, "wb") as fo:
            while chunk := fi.read(CHUNK):
                ctl.check()
                h.update(chunk)
                fo.write(chunk)
        os.replace(tmp, dst)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise
    return h.hexdigest()


# ── Reading snapshot content ─────────────────────────────────────────────────

def iter_content(store, manifest: dict, entry: dict) -> Iterator[bytes]:
    try:
        yield from _iter_content(store, manifest, entry)
    except zlib.error:
        raise OSError(f"contenu corrompu ({entry['p']})")


def _iter_content(store, manifest: dict, entry: dict) -> Iterator[bytes]:
    if manifest["mode"] == "hardlink":
        with open(store.snap_dir(manifest["id"]) / "files" / entry["p"], "rb") as f:
            while chunk := f.read(CHUNK):
                yield chunk
    else:
        yield from store.read_blob(entry["h"], bool(entry.get("z")))


def list_dir(manifest: dict, path: str) -> dict:
    path = path.strip("/")
    prefix = path + "/" if path else ""
    dirs: dict[str, dict] = {}
    files = []
    for d in manifest.get("dirs", []):
        if d.startswith(prefix) and d != path:
            rest = d[len(prefix):]
            dirs.setdefault(rest.split("/", 1)[0], {"files": 0, "size": 0})
    for f in manifest["files"]:
        p = f["p"]
        if not p.startswith(prefix):
            continue
        rest = p[len(prefix):]
        if "/" in rest:
            d = dirs.setdefault(rest.split("/", 1)[0], {"files": 0, "size": 0})
            d["files"] += 1
            d["size"] += f["s"]
        else:
            files.append({"name": rest, "path": p, "size": f["s"], "mtime": f["m"] / 1e9})
    return {"path": path,
            "dirs": [{"name": k, "path": prefix + k, **v} for k, v in sorted(dirs.items(), key=lambda x: x[0].lower())],
            "files": sorted(files, key=lambda x: x["name"].lower())}


def select_entries(manifest: dict, paths: Optional[list[str]]) -> tuple[list[dict], list[str]]:
    if not paths:
        return list(manifest["files"]), list(manifest.get("dirs", []))
    sel = [p.strip("/") for p in paths if p.strip("/")]

    def match(p: str) -> bool:
        return any(p == s or p.startswith(s + "/") for s in sel)
    return [f for f in manifest["files"] if match(f["p"])], [d for d in manifest.get("dirs", []) if match(d)]


def restore_target(manifest: dict, rel: str, target: Optional[str]) -> str:
    key, _, rest = rel.partition("/")
    if target:
        base = os.path.join(target, key)
    else:
        if key not in manifest["sources"]:
            raise BackupError("BAD_SNAPSHOT", f"Source inconnue dans l'instantané : {key}")
        base = manifest["sources"][key]
    out = os.path.normpath(os.path.join(base, *rest.split("/"))) if rest else os.path.normpath(base)
    root = os.path.normpath(base)
    if out != root and not out.startswith(root.rstrip(os.sep) + os.sep):
        raise BackupError("BAD_PATH", f"Chemin hors de la cible : {rel}")
    return out


def plan_restore(manifest: dict, paths: Optional[list[str]], target: Optional[str]) -> dict:
    files, _ = select_entries(manifest, paths)
    conflicts, identical, size = [], 0, 0
    for f in files:
        dst = restore_target(manifest, f["p"], target)
        size += f["s"]
        try:
            st = os.stat(dst)
        except FileNotFoundError:
            continue
        except OSError:
            conflicts.append({"path": f["p"], "target": dst})
            continue
        if stat.S_ISREG(st.st_mode) and st.st_size == f["s"] and st.st_mtime_ns == f["m"]:
            identical += 1
        else:
            conflicts.append({"path": f["p"], "target": dst})
    return {"files": len(files), "size": size, "identical": identical, "conflicts": len(conflicts),
            "conflict_list": conflicts[:200]}


def _renamed(path: str) -> str:
    stem, ext = os.path.splitext(path)
    n = 1
    while True:
        cand = f"{stem} (restauré{'' if n == 1 else ' ' + str(n)}){ext}"
        if not os.path.exists(cand):
            return cand
        n += 1


def run_restore(ctl: Ctl, store, manifest: dict, paths: Optional[list[str]], target: Optional[str],
                policy: str) -> dict:
    files, dirs = select_entries(manifest, paths)
    stats = {"restored": 0, "skipped": 0, "identical": 0, "renamed": 0, "overwritten": 0, "errors": 0, "bytes": 0}
    for d in dirs:
        try:
            os.makedirs(restore_target(manifest, d, target), exist_ok=True)
        except OSError as exc:
            ctl.note(f"Dossier impossible à créer : {d} : {exc.strerror or exc}")
    total = sum(f["s"] for f in files) or 1
    done = 0
    last = 0.0
    for i, f in enumerate(files):
        ctl.check()
        dst = restore_target(manifest, f["p"], target)
        try:
            if os.path.exists(dst):
                st = os.stat(dst)
                if stat.S_ISREG(st.st_mode) and st.st_size == f["s"] and st.st_mtime_ns == f["m"]:
                    stats["identical"] += 1
                    continue
                if policy == "skip":
                    stats["skipped"] += 1
                    continue
                if policy == "rename":
                    dst = _renamed(dst)
                    stats["renamed"] += 1
                elif policy == "overwrite":
                    stats["overwritten"] += 1
                else:
                    raise BackupError("CONFLICT", "Des fichiers existent déjà à la destination")
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            tmp = dst + ".lbtmp"
            h = hashlib.sha256()
            try:
                with open(tmp, "wb") as fo:
                    for chunk in iter_content(store, manifest, f):
                        ctl.check()
                        h.update(chunk)
                        fo.write(chunk)
                if f.get("h") and h.hexdigest() != f["h"]:
                    raise OSError(f"empreinte SHA-256 différente (contenu corrompu)")
                if os.path.exists(dst):
                    try:
                        os.chmod(dst, stat.S_IWRITE)
                    except OSError:
                        pass
                os.replace(tmp, dst)
            except BaseException:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise
            os.utime(dst, ns=(f["m"], f["m"]))
            stats["restored"] += 1
            stats["bytes"] += f["s"]
        except BackupError:
            raise
        except OSError as exc:
            stats["errors"] += 1
            ctl.note(f"Erreur : {f['p']} : {exc.strerror or exc}")
        done += f["s"]
        t = time.monotonic()
        if t - last > 0.2:
            last = t
            ctl.progress(100 * done / total, f"{i + 1}/{len(files)} · {f['p']}")
    ctl.progress(100, "Restauration terminée")
    return stats


# ── Verification ─────────────────────────────────────────────────────────────

def run_verify(ctl: Ctl, store, snaps: list[str], deep: bool) -> dict:
    report = {"snapshots": 0, "files": 0, "ok": 0, "missing": 0, "corrupt": 0, "problems": []}
    blobs = None
    manifests = []
    for s in snaps:
        try:
            manifests.append(load_manifest(store, s))
        except BackupError as exc:
            report["problems"].append({"snapshot": s, "path": "", "issue": exc.message})
    total = sum(sum(f["s"] for f in m["files"]) if deep else len(m["files"]) for m in manifests) or 1
    done = 0
    checked_blobs: dict[str, bool] = {}
    last = 0.0
    for m in manifests:
        report["snapshots"] += 1
        for f in m["files"]:
            ctl.check()
            report["files"] += 1
            issue = None
            try:
                if m["mode"] == "hardlink":
                    p = store.snap_dir(m["id"]) / "files" / f["p"]
                    if not p.exists():
                        issue = "manquant"
                    elif p.stat().st_size != f["s"]:
                        issue = "taille différente"
                    elif deep and f.get("h") and hash_file(str(p), ctl) != f["h"]:
                        issue = "contenu corrompu"
                else:
                    if blobs is None:
                        blobs = store.blobs()
                    if f["h"] not in blobs:
                        issue = "bloc manquant"
                    elif deep:
                        if f["h"] not in checked_blobs:
                            h = hashlib.sha256()
                            for chunk in store.read_blob(f["h"], blobs[f["h"]]):
                                ctl.check()
                                h.update(chunk)
                            checked_blobs[f["h"]] = h.hexdigest() == f["h"]
                        if not checked_blobs[f["h"]]:
                            issue = "contenu corrompu"
            except zlib.error:
                issue = "contenu corrompu"
            except OSError as exc:
                issue = f"illisible ({exc.strerror or exc})"
            if issue:
                key = "missing" if "manquant" in issue else "corrupt"
                report[key] += 1
                if len(report["problems"]) < 500:
                    report["problems"].append({"snapshot": m["id"], "path": f["p"], "issue": issue})
            else:
                report["ok"] += 1
            done += f["s"] if deep else 1
            t = time.monotonic()
            if t - last > 0.2:
                last = t
                ctl.progress(100 * done / total, f"{m['id']} · {f['p']}")
    ctl.progress(100, "Vérification terminée")
    return report


# ── Rotation ─────────────────────────────────────────────────────────────────

def snapshot_time(name: str) -> datetime:
    return datetime.strptime(name[:17], SNAP_FMT)


def rotation_keep(names: list[str], retention: dict) -> set[str]:
    """Newest-first: keep the `last` most recent, plus the newest snapshot of each of
    the `daily` most recent days, `weekly` ISO weeks and `monthly` months."""
    ordered = sorted(names, reverse=True)
    keep = set(ordered[:max(1, int(retention.get("last", 1)))])
    for key, fn in (("daily", lambda d: d.date()),
                    ("weekly", lambda d: tuple(d.isocalendar())[:2]),
                    ("monthly", lambda d: (d.year, d.month))):
        n = int(retention.get(key, 0))
        seen = []
        for name in ordered:
            if len(seen) >= n:
                break
            try:
                b = fn(snapshot_time(name))
            except ValueError:
                keep.add(name)
                continue
            if b not in seen:
                seen.append(b)
                keep.add(name)
    return keep


def run_rotation(ctl: Ctl, store, retention: dict) -> dict:
    names = store.list_snapshots()
    keep = rotation_keep(names, retention)
    doomed = [n for n in names if n not in keep]
    for i, n in enumerate(doomed):
        ctl.check()
        ctl.progress(80 * (i + 1) / max(1, len(doomed)), f"Suppression de {n}")
        store.remove_snapshot(n)
    freed_blobs = 0
    meta = store.read_meta("backup.json") or {}
    if meta.get("mode") == "dedup" and doomed:
        ctl.progress(85, "Nettoyage du stockage…")
        used: set[str] = set()
        for n in keep:
            try:
                used.update(f["h"] for f in load_manifest(store, n)["files"])
            except BackupError:
                return {"deleted": doomed, "kept": sorted(keep), "freed_blobs": 0}
        for h, z in store.blobs().items():
            if h not in used:
                store.remove_blob(h, z)
                freed_blobs += 1
    return {"deleted": doomed, "kept": sorted(keep), "freed_blobs": freed_blobs}


def delete_snapshot(store, name: str) -> dict:
    names = store.list_snapshots()
    if name not in names:
        raise BackupError("NOT_FOUND", "Instantané introuvable")
    store.remove_snapshot(name)
    freed = 0
    meta = store.read_meta("backup.json") or {}
    if meta.get("mode") == "dedup":
        used: set[str] = set()
        for n in names:
            if n != name:
                used.update(f["h"] for f in load_manifest(store, n)["files"])
        for h, z in store.blobs().items():
            if h not in used:
                store.remove_blob(h, z)
                freed += 1
    return {"deleted": name, "freed_blobs": freed}


# ── Zip export ───────────────────────────────────────────────────────────────

def export_zip(ctl: Ctl, store, manifest: dict, paths: Optional[list[str]], out_dir: str) -> dict:
    files, _ = select_entries(manifest, paths)
    base = "".join(c if c.isalnum() or c in "-_" else "_" for c in manifest["set"])[:60] or "sauvegarde"
    out = os.path.join(out_dir, f"{base}-{manifest['id']}.zip")
    n = 2
    while os.path.exists(out):
        out = os.path.join(out_dir, f"{base}-{manifest['id']}-{n}.zip")
        n += 1
    total = sum(f["s"] for f in files) or 1
    done = 0
    tmp = out + ".part"
    try:
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
            for i, f in enumerate(files):
                ctl.check()
                dt = datetime.fromtimestamp(max(f["m"] / 1e9, 315532800))
                info = zipfile.ZipInfo(f["p"], date_time=dt.timetuple()[:6])
                info.compress_type = zipfile.ZIP_STORED if Path(f["p"]).suffix.lower() in NO_COMPRESS_EXT else zipfile.ZIP_DEFLATED
                with zf.open(info, "w", force_zip64=f["s"] > 2 ** 31) as w:
                    for chunk in iter_content(store, manifest, f):
                        ctl.check()
                        w.write(chunk)
                done += f["s"]
                ctl.progress(100 * done / total, f"{i + 1}/{len(files)} · {f['p']}")
        os.replace(tmp, out)
    except OSError as exc:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise BackupError("EXPORT_FAILED", f"Export impossible : {exc.strerror or exc}")
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return {"path": out, "files": len(files), "size": os.path.getsize(out)}


# ── Scheduling ───────────────────────────────────────────────────────────────

def next_due(schedule: dict, reference: datetime) -> Optional[datetime]:
    kind = schedule.get("type", "manual")
    if kind == "manual":
        return None
    if kind == "interval":
        return reference + timedelta(hours=max(1, int(schedule.get("hours") or 24)))
    hh, mm = (int(x) for x in (schedule.get("time") or "12:00").split(":"))
    cand = reference.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if cand <= reference:
        cand += timedelta(days=1)
    if kind == "daily":
        return cand
    days = set(schedule.get("weekdays") or [0])
    for _ in range(8):
        if cand.weekday() in days:
            return cand
        cand += timedelta(days=1)
    return None
