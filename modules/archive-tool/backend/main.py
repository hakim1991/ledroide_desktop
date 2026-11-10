import asyncio
import logging
import os
import re
import stat
import tarfile
import threading
import time
import zipfile
from datetime import datetime, timezone

from ledroide_sdk import Module

log = logging.getLogger("ledroide.archive-tool")

module = Module(__file__)
router = module.router

FORMATS = {"zip": ".zip", "7z": ".7z", "tar": ".tar", "tar.gz": ".tar.gz", "tar.bz2": ".tar.bz2", "tar.xz": ".tar.xz"}
CHUNK = 1024 * 1024
RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}


class ArchiveError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class Cancelled(Exception):
    pass


class Ctl:
    def __init__(self):
        self.cancel = threading.Event()
        self.pct = 0.0
        self.msg = ""

    def progress(self, pct: float, msg: str):
        self.pct, self.msg = pct, msg

    def check(self):
        if self.cancel.is_set():
            raise Cancelled()


async def _run_threaded(job, fn, *args):
    """fn(ctl, *args) in a thread; on cancellation the thread is asked to stop
    (it removes its partial output) and the job ends as cancelled."""
    ctl = Ctl()
    fut = asyncio.ensure_future(asyncio.to_thread(fn, ctl, *args))
    last = None
    try:
        while not fut.done():
            await asyncio.wait({fut}, timeout=0.25)
            cur = (round(ctl.pct, 1), ctl.msg)
            if cur != last:
                last = cur
                await job.progress(cur[0], cur[1])
    except asyncio.CancelledError:
        ctl.cancel.set()
        await asyncio.wait({fut})
        fut.exception()
        raise
    exc = fut.exception()
    if isinstance(exc, Cancelled):
        raise asyncio.CancelledError()
    if isinstance(exc, ArchiveError):
        raise ValueError(exc.message)
    return fut.result()


# ── Helpers ──────────────────────────────────────────────────────────────────

def _detect(path: str) -> str:
    low = path.lower()
    if low.endswith(".zip"):
        return "zip"
    if low.endswith(".7z"):
        return "7z"
    if low.endswith((".tar.gz", ".tgz")):
        return "tar.gz"
    if low.endswith((".tar.bz2", ".tbz2", ".tbz")):
        return "tar.bz2"
    if low.endswith((".tar.xz", ".txz")):
        return "tar.xz"
    if low.endswith(".tar"):
        return "tar"
    try:
        if zipfile.is_zipfile(path):
            return "zip"
        with open(path, "rb") as f:
            if f.read(6) == b"7z\xbc\xaf\x27\x1c":
                return "7z"
        if tarfile.is_tarfile(path):
            return "tar.gz"
    except OSError:
        pass
    raise ArchiveError("UNSUPPORTED_FORMAT", "Format d'archive non reconnu (ZIP, 7z, TAR, TAR.GZ, TAR.BZ2, TAR.XZ)")


def _archive_path(path: str) -> str:
    path = (path or "").strip().strip('"')
    if not path:
        raise ArchiveError("PATH_REQUIRED", "Indiquez le chemin de l'archive")
    if not os.path.isabs(path):
        raise ArchiveError("BAD_PATH", "Le chemin doit être absolu")
    if not os.path.isfile(path):
        raise ArchiveError("NOT_FOUND", f"Archive introuvable : {path}")
    return path


def _py7zr():
    try:
        import py7zr
    except ImportError:
        raise ArchiveError("MISSING_DEP", "py7zr n'est pas installé : installez les dépendances du module pour le format 7z")
    return py7zr


def _pyzipper(required: bool = True):
    try:
        import pyzipper
        return pyzipper
    except ImportError:
        if required:
            raise ArchiveError("MISSING_DEP", "pyzipper n'est pas installé : nécessaire pour les ZIP chiffrés AES "
                                              "(installez les dépendances du module)")
        return None


def _zip_open(path: str, password: str | None):
    zf = zipfile.ZipFile(path)
    if any(i.compress_type == 99 for i in zf.infolist()):
        zf.close()
        zf = _pyzipper().AESZipFile(path)
    if password:
        zf.setpassword(password.encode("utf-8"))
    return zf


def _zip_is_link(info) -> bool:
    return info.create_system == 3 and stat.S_ISLNK(info.external_attr >> 16)


def _safe_parts(name: str) -> list[str] | None:
    """Relative path components of an archive entry, or None if the name is unsafe."""
    n = name.replace("\\", "/")
    if n.startswith("/") or re.match(r"^[A-Za-z]:", n):
        return None
    parts = [p for p in n.split("/") if p not in ("", ".")]
    if not parts:
        return None
    for p in parts:
        if p == ".." or ":" in p or any(ord(c) < 32 for c in p) or re.search(r'[<>"|?*]', p):
            return None
        if p.rstrip(" .") != p or p.split(".")[0].upper() in RESERVED:
            return None
    return parts


def _safe_target(dest_real: str, name: str) -> str | None:
    parts = _safe_parts(name)
    if parts is None:
        return None
    target = os.path.realpath(os.path.join(dest_real, *parts))
    if not target.startswith(dest_real.rstrip(os.sep) + os.sep):
        return None
    return target


def _translate(exc: Exception, encrypted_7z: bool = False) -> ArchiveError:
    msg = str(exc)
    low = msg.lower()
    if isinstance(exc, ArchiveError):
        return exc
    if encrypted_7z and not isinstance(exc, OSError) and type(exc).__name__ != "PasswordRequired":
        return ArchiveError("BAD_PASSWORD", "Mot de passe incorrect (ou archive corrompue)")
    if "password" in low or "bad password" in low or type(exc).__name__ == "PasswordRequired":
        if "required" in low or "requires" in low or type(exc).__name__ == "PasswordRequired":
            return ArchiveError("PASSWORD_REQUIRED", "Cette archive est protégée : saisissez le mot de passe")
        return ArchiveError("BAD_PASSWORD", "Mot de passe incorrect")
    if type(exc).__name__ in ("CrcError", "DecompressionError") or "crc" in low:
        return ArchiveError("BAD_PASSWORD", "Données invalides : mot de passe incorrect ou archive corrompue")
    if type(exc).__name__ == "DecompressionBombError":
        return ArchiveError("TOO_LARGE", "Extraction interrompue : taille décompressée supérieure à la limite configurée")
    if isinstance(exc, (zipfile.BadZipFile, tarfile.TarError)) or type(exc).__name__ in ("Bad7zFile", "ArchiveError"):
        return ArchiveError("BAD_ARCHIVE", f"Archive invalide ou corrompue : {msg}")
    if isinstance(exc, NotImplementedError) or type(exc).__name__ == "UnsupportedCompressionMethodError":
        return ArchiveError("UNSUPPORTED_METHOD", f"Méthode de compression non prise en charge : {msg}")
    if isinstance(exc, PermissionError):
        return ArchiveError("ACCESS_DENIED", f"Accès refusé : {exc.filename or msg}")
    if isinstance(exc, OSError):
        return ArchiveError("IO_ERROR", f"Erreur disque : {exc.strerror or msg}")
    return ArchiveError("ARCHIVE_ERROR", msg or type(exc).__name__)


# ── Listing ──────────────────────────────────────────────────────────────────

def _list(path: str, password: str | None, max_entries: int) -> dict:
    path = _archive_path(path)
    fmt = _detect(path)
    entries: list[dict] = []
    encrypted = False
    try:
        if fmt == "zip":
            with _zip_open(path, password) as zf:
                for i in zf.infolist():
                    enc = bool(i.flag_bits & 0x1)
                    encrypted |= enc
                    entries.append({"name": i.filename, "size": i.file_size, "csize": i.compress_size,
                                    "dir": i.is_dir(), "link": _zip_is_link(i), "enc": enc,
                                    "mtime": datetime(*i.date_time).isoformat() if i.date_time[0] >= 1980 else None})
        elif fmt == "7z":
            py7zr = _py7zr()
            with py7zr.SevenZipFile(path, "r", password=password or None) as zf:
                encrypted = zf.needs_password()
                for i in zf.list():
                    entries.append({"name": i.filename, "size": i.uncompressed or 0, "csize": i.compressed,
                                    "dir": i.is_directory, "link": i.is_symlink, "enc": encrypted,
                                    "mtime": i.creationtime.isoformat() if i.creationtime else None})
        else:
            with tarfile.open(path, "r:*") as tf:
                for m in tf:
                    entries.append({"name": m.name, "size": m.size if m.isfile() else 0, "csize": None,
                                    "dir": m.isdir(), "link": m.issym() or m.islnk(), "enc": False,
                                    "mtime": datetime.fromtimestamp(m.mtime, timezone.utc).isoformat() if m.mtime else None})
                    if len(entries) > max_entries:
                        break
    except Exception as exc:
        raise _translate(exc, fmt == "7z" and bool(password))
    truncated = len(entries) > max_entries
    entries = entries[:max_entries]
    for e in entries:
        e["unsafe"] = _safe_parts(e["name"]) is None
    total = sum(e["size"] for e in entries)
    csizes = [e["csize"] for e in entries if e["csize"] is not None]
    archive_size = os.path.getsize(path)
    packed = sum(csizes) if fmt == "zip" else archive_size
    return {
        "path": path, "name": os.path.basename(path), "format": fmt, "archive_size": archive_size,
        "entries": entries, "truncated": truncated, "encrypted": encrypted,
        "files": sum(1 for e in entries if not e["dir"]), "dirs": sum(1 for e in entries if e["dir"]),
        "total_size": total, "packed_size": packed,
        "ratio": round(1 - packed / total, 4) if total else None,
        "unsafe": sum(1 for e in entries if e["unsafe"]),
    }


# ── Extraction ───────────────────────────────────────────────────────────────

def _selected(names: list[str], members: list[str] | None) -> set[str]:
    if not members:
        return set(names)
    wanted = [m.replace("\\", "/").rstrip("/") for m in members]
    out = set()
    for n in names:
        nn = n.replace("\\", "/").rstrip("/")
        if any(nn == w or nn.startswith(w + "/") for w in wanted):
            out.add(n)
    return out


def _stream_to(ctl: Ctl, src, target: str, budget: list[int], done: list[int], total: int, label: str):
    part = target + ".arc-part"
    try:
        with open(part, "wb") as out:
            while chunk := src.read(CHUNK):
                ctl.check()
                budget[0] -= len(chunk)
                if budget[0] < 0:
                    raise ArchiveError("TOO_LARGE", "Extraction interrompue : la taille décompressée dépasse la limite "
                                                    "configurée (protection anti zip-bomb)")
                out.write(chunk)
                done[0] += len(chunk)
                ctl.progress(min(99, 100 * done[0] / max(1, total)), label)
        os.replace(part, target)
    finally:
        if os.path.exists(part):
            os.remove(part)


class _NoFactory(Exception):
    pass


def _extract_7z_stream(ctl: Ctl, py7zr, zf, targets: list[str], dest_real: str, budget: list[int],
                       done: list[int], total: int, written: list[str]):
    """py7zr >= 1.0: each member is streamed to its checked destination, so the
    size limit and cancellation apply while decompressing."""
    try:
        from py7zr.io import Py7zIO, WriterFactory
    except ImportError:
        raise _NoFactory()
    wanted = set(targets)
    opened: list = []

    class Sink(Py7zIO):
        def __init__(self, name: str):
            if os.path.isabs(name):
                name = os.path.relpath(name, dest_real).replace(os.sep, "/")
            self.name = name
            self.target = _safe_target(dest_real, name) if name in wanted else None
            self.part = self.target + ".arc-part" if self.target else None
            self.f = open(self.part, "wb") if self.part else None
            self.n = 0
            opened.append(self)

        def write(self, s):
            ctl.check()
            budget[0] -= len(s)
            if budget[0] < 0:
                raise ArchiveError("TOO_LARGE", "Extraction interrompue : la taille décompressée dépasse la limite "
                                                "configurée (protection anti zip-bomb)")
            if self.f:
                self.f.write(s)
            self.n += len(s)
            done[0] += len(s)
            ctl.progress(min(99, 100 * done[0] / max(1, total)), self.name)
            return len(s)

        def read(self, size=None):
            return b""

        def seek(self, offset, whence=0):
            return 0

        def seekable(self):
            return False

        def flush(self):
            if self.f:
                self.f.flush()

        def size(self):
            return self.n

        def close(self):
            if self.f and not self.f.closed:
                self.f.close()
                os.replace(self.part, self.target)
                written.append(self.name)

    class Factory(WriterFactory):
        def create(self, filename):
            return Sink(filename)

    try:
        zf.extract(path=dest_real, targets=targets, factory=Factory())
    except TypeError as exc:
        if "factory" in str(exc):
            raise _NoFactory()
        raise
    finally:
        for s in opened:
            if s.f and not s.f.closed:
                s.f.close()
            if s.part and os.path.exists(s.part):
                os.remove(s.part)


def _extract_7z_staging(ctl: Ctl, zf, targets: list[str], dest_real: str, written: list[str]):
    """py7zr < 1.0 fallback: extract into a private folder, then move the checked files."""
    import shutil
    staging = os.path.join(dest_real, f".arc-staging-{os.getpid()}-{int(time.time() * 1000)}")
    os.makedirs(staging)
    try:
        zf.reset()
        zf.extract(path=staging, targets=targets)
        ctl.check()
        for name in targets:
            src = os.path.join(staging, *_safe_parts(name))
            target = _safe_target(dest_real, name)
            if os.path.isfile(src) and target:
                os.makedirs(os.path.dirname(target), exist_ok=True)
                os.replace(src, target)
                written.append(name)
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def _extract(ctl: Ctl, path: str, dest: str, password: str | None, members: list[str] | None,
             overwrite: bool, limit: int) -> dict:
    fmt = _detect(path)
    os.makedirs(dest, exist_ok=True)
    dest_real = os.path.realpath(dest)
    skipped_unsafe, skipped_existing, skipped_links, written = [], [], [], []
    budget, done = [limit], [0]

    def prepare(name: str, is_dir: bool) -> str | None:
        target = _safe_target(dest_real, name)
        if target is None:
            skipped_unsafe.append(name)
            return None
        if is_dir:
            os.makedirs(target, exist_ok=True)
            return None
        if os.path.lexists(target) and not overwrite:
            skipped_existing.append(name)
            return None
        os.makedirs(os.path.dirname(target), exist_ok=True)
        return target

    try:
        if fmt == "zip":
            with _zip_open(path, password) as zf:
                infos = zf.infolist()
                sel = _selected([i.filename for i in infos], members)
                infos = [i for i in infos if i.filename in sel]
                total = sum(i.file_size for i in infos)
                if total > limit:
                    raise ArchiveError("TOO_LARGE", f"Taille décompressée annoncée ({total // 1048576} Mo) supérieure "
                                                    f"à la limite ({limit // 1048576} Mo)")
                for i in infos:
                    ctl.check()
                    if _zip_is_link(i):
                        skipped_links.append(i.filename)
                        continue
                    target = prepare(i.filename, i.is_dir())
                    if target is None:
                        continue
                    with zf.open(i) as src:
                        _stream_to(ctl, src, target, budget, done, total, i.filename)
                    if i.date_time[0] >= 1980:
                        ts = time.mktime(i.date_time + (0, 0, -1))
                        os.utime(target, (ts, ts))
                    written.append(i.filename)
        elif fmt == "7z":
            py7zr = _py7zr()
            with py7zr.SevenZipFile(path, "r", password=password or None) as zf:
                infos = zf.list()
                sel = _selected([i.filename for i in infos], members)
                infos = [i for i in infos if i.filename in sel]
                total = sum(i.uncompressed or 0 for i in infos)
                if total > limit:
                    raise ArchiveError("TOO_LARGE", f"Taille décompressée annoncée ({total // 1048576} Mo) supérieure "
                                                    f"à la limite ({limit // 1048576} Mo)")
                targets = []
                for i in infos:
                    if i.is_symlink:
                        skipped_links.append(i.filename)
                    elif prepare(i.filename, i.is_directory) is not None:
                        targets.append(i.filename)
                if targets:
                    zf.max_extract_size = limit
                    zf.reset()
                    mtimes = {i.filename: i.creationtime for i in infos}
                    try:
                        _extract_7z_stream(ctl, py7zr, zf, targets, dest_real, budget, done, total, written)
                    except _NoFactory:
                        _extract_7z_staging(ctl, zf, targets, dest_real, written)
                    for name in targets:
                        target = _safe_target(dest_real, name)
                        if target and name not in written and not os.path.exists(target):
                            open(target, "wb").close()
                            written.append(name)
                        dt = mtimes.get(name)
                        if target and dt and os.path.isfile(target):
                            ts = dt.timestamp()
                            os.utime(target, (ts, ts))
        else:
            with tarfile.open(path, "r:*") as tf:
                all_members = tf.getmembers()
                sel = _selected([m.name for m in all_members], members)
                chosen = [m for m in all_members if m.name in sel]
                total = sum(m.size for m in chosen if m.isfile())
                if total > limit:
                    raise ArchiveError("TOO_LARGE", f"Taille décompressée ({total // 1048576} Mo) supérieure "
                                                    f"à la limite ({limit // 1048576} Mo)")
                for m in chosen:
                    ctl.check()
                    try:
                        safe = tarfile.data_filter(m, dest_real)
                    except tarfile.FilterError:
                        skipped_unsafe.append(m.name)
                        continue
                    if safe is None:
                        continue
                    if m.issym() or m.islnk():
                        if prepare(m.name, False) is None:
                            continue
                        try:
                            tf.extract(m, dest_real, filter="data")
                            written.append(m.name)
                        except (tarfile.FilterError, OSError):
                            skipped_links.append(m.name)
                        continue
                    if not (m.isfile() or m.isdir()):
                        skipped_unsafe.append(m.name)
                        continue
                    target = prepare(safe.name, m.isdir())
                    if target is None:
                        continue
                    src = tf.extractfile(m)
                    with src:
                        _stream_to(ctl, src, target, budget, done, total, m.name)
                    if m.mtime:
                        os.utime(target, (m.mtime, m.mtime))
                    written.append(m.name)
    except (ArchiveError, Cancelled):
        raise
    except Exception as exc:
        raise _translate(exc, fmt == "7z" and bool(password))
    ctl.progress(100, "Terminé")
    return {"dest": dest_real, "written": len(written), "bytes": done[0],
            "skipped_unsafe": skipped_unsafe[:200], "skipped_existing": skipped_existing[:200],
            "skipped_links": skipped_links[:200],
            "counts": {"unsafe": len(skipped_unsafe), "existing": len(skipped_existing), "links": len(skipped_links)}}


# ── Creation ─────────────────────────────────────────────────────────────────

def _collect_sources(sources: list[str], exclude_real: str) -> list[tuple[str, str]]:
    items: list[tuple[str, str]] = []
    seen = set()
    for src in sources:
        src = os.path.abspath(src)
        base = os.path.dirname(src.rstrip(os.sep)) or src
        if os.path.isfile(src):
            items.append((src, os.path.basename(src)))
            continue
        for root, dirs, files in os.walk(src):
            dirs[:] = sorted(d for d in dirs if not os.path.islink(os.path.join(root, d)))
            rel_root = os.path.relpath(root, base).replace(os.sep, "/")
            if not files and not dirs:
                items.append((root, rel_root + "/"))
            for name in sorted(files):
                full = os.path.join(root, name)
                if os.path.islink(full) or os.path.normcase(os.path.realpath(full)) == exclude_real:
                    continue
                items.append((full, f"{rel_root}/{name}"))
    out = []
    for full, arc in items:
        key = arc.lower()
        if key not in seen:
            seen.add(key)
            out.append((full, arc))
    return out


def _create(ctl: Ctl, sources: list[str], output: str, fmt: str, level: int, password: str | None) -> dict:
    part = output + ".arc-part"
    ctl.progress(0, "Inventaire des fichiers…")
    items = _collect_sources(sources, os.path.normcase(os.path.realpath(output)))
    if not items:
        raise ArchiveError("EMPTY", "Aucun fichier à archiver")
    total = max(1, sum(os.path.getsize(f) for f, a in items if not a.endswith("/")))
    done = [0]

    def tick(arc: str, n: int = 0):
        done[0] += n
        ctl.progress(min(99, 100 * done[0] / total), arc)

    try:
        if fmt == "zip":
            method = zipfile.ZIP_STORED if level == 0 else zipfile.ZIP_DEFLATED
            if password:
                pz = _pyzipper()
                with pz.AESZipFile(part, "w", compression=method, encryption=pz.WZ_AES) as zf:
                    zf.setpassword(password.encode("utf-8"))
                    if level:
                        zf.compresslevel = level
                    for full, arc in items:
                        ctl.check()
                        tick(arc)
                        zf.write(full, arc)
                        if not arc.endswith("/"):
                            tick(arc, os.path.getsize(full))
            else:
                with zipfile.ZipFile(part, "w", compression=method, compresslevel=level or None) as zf:
                    for full, arc in items:
                        ctl.check()
                        if arc.endswith("/"):
                            zf.write(full, arc)
                            continue
                        info = zipfile.ZipInfo.from_file(full, arc)
                        info.compress_type = method
                        if level:
                            for attr in ("compress_level", "_compresslevel"):
                                try:
                                    setattr(info, attr, level)
                                except AttributeError:
                                    pass
                        with open(full, "rb") as src, zf.open(info, "w", force_zip64=True) as dst:
                            while chunk := src.read(CHUNK):
                                ctl.check()
                                dst.write(chunk)
                                tick(arc, len(chunk))
        elif fmt == "7z":
            py7zr = _py7zr()
            filters = [{"id": py7zr.FILTER_COPY}] if level == 0 else [{"id": py7zr.FILTER_LZMA2, "preset": level}]
            if password:
                filters.append({"id": py7zr.FILTER_CRYPTO_AES256_SHA256})
            with py7zr.SevenZipFile(part, "w", filters=filters, password=password or None,
                                    header_encryption=bool(password)) as zf:
                for full, arc in items:
                    ctl.check()
                    tick(arc)
                    zf.write(full, arc.rstrip("/"))
                    if not arc.endswith("/"):
                        tick(arc, os.path.getsize(full))
        else:
            mode = {"tar": "w", "tar.gz": "w:gz", "tar.bz2": "w:bz2", "tar.xz": "w:xz"}[fmt]
            kwargs = {}
            if fmt in ("tar.gz", "tar.bz2"):
                kwargs["compresslevel"] = max(1, level)
            elif fmt == "tar.xz":
                kwargs["preset"] = level
            with tarfile.open(part, mode, **kwargs) as tf:
                for full, arc in items:
                    ctl.check()
                    ti = tf.gettarinfo(full, arc.rstrip("/"))
                    if ti.isfile():
                        with open(full, "rb") as src:
                            reader = _CountingReader(src, ctl, lambda n, a=arc: tick(a, n))
                            tf.addfile(ti, reader)
                    else:
                        tf.addfile(ti)
        os.replace(part, output)
    except Cancelled:
        raise
    except ArchiveError:
        raise
    except Exception as exc:
        raise _translate(exc)
    finally:
        if os.path.exists(part):
            try:
                os.remove(part)
            except OSError:
                pass
    ctl.progress(100, "Terminé")
    size = os.path.getsize(output)
    return {"output": output, "name": os.path.basename(output), "format": fmt, "entries": len(items),
            "total_size": total if total > 1 else 0, "archive_size": size,
            "ratio": round(1 - size / total, 4) if total > 1 else None, "encrypted": bool(password)}


class _CountingReader:
    def __init__(self, f, ctl: Ctl, on_read):
        self.f, self.ctl, self.on_read = f, ctl, on_read

    def read(self, n=-1):
        self.ctl.check()
        data = self.f.read(n)
        self.on_read(len(data))
        return data


# ── Handlers ─────────────────────────────────────────────────────────────────

async def _remember(path: str):
    try:
        docs = await module.db.query("recents", {"path": path})
        doc = {"path": path, "at": time.time()}
        if docs:
            doc["_id"] = docs[0]["_id"]
        await module.db.save("recents", doc)
        for old in (await module.db.query("recents"))[15:]:
            await module.db.delete("recents", old["_id"])
    except Exception:
        log.exception("recents update failed")


@module.on("recents")
async def recents(payload: dict):
    items = await module.db.query("recents")
    return {"items": [{"path": d["path"], "exists": os.path.isfile(d["path"]), "_id": d["_id"]} for d in items]}


@module.on("recent_delete")
async def recent_delete(payload: dict):
    return await module.db.delete("recents", str(payload.get("id", "")))


@module.on("info")
async def info(payload: dict):
    return {"formats": list(FORMATS), "py7zr": _has("py7zr"), "pyzipper": _has("pyzipper"),
            "max_extract_mb": int(module.config.get("max_extract_mb", 8192))}


def _has(mod: str) -> bool:
    try:
        __import__(mod)
        return True
    except ImportError:
        return False


@module.on("list")
async def list_archive(payload: dict):
    try:
        res = await asyncio.to_thread(_list, str(payload.get("path") or ""), payload.get("password") or None,
                                      int(module.config.get("max_entries", 50000)))
    except ArchiveError as exc:
        return {"error": exc.message, "code": exc.code}
    await _remember(res["path"])
    return res


@module.job("extract")
async def extract(payload: dict, job):
    try:
        path = _archive_path(str(payload.get("path") or ""))
    except ArchiveError as exc:
        raise ValueError(exc.message)
    dest = str(payload.get("dest") or "").strip().strip('"')
    if not dest:
        dest = os.path.dirname(path)
    if not os.path.isabs(dest):
        raise ValueError("Le dossier de destination doit être un chemin absolu")
    if payload.get("subfolder", True):
        stem = os.path.basename(path)
        for ext in (".tar.gz", ".tar.bz2", ".tar.xz", ".tgz", ".tbz2", ".txz", ".zip", ".7z", ".tar"):
            if stem.lower().endswith(ext):
                stem = stem[: -len(ext)]
                break
        dest = os.path.join(dest, _safe_parts(stem)[0] if _safe_parts(stem) else "archive")
    members = [str(m) for m in payload.get("members") or []] or None
    limit = int(module.config.get("max_extract_mb", 8192)) * 1024 * 1024
    res = await _run_threaded(job, _extract, path, dest, payload.get("password") or None, members,
                              bool(payload.get("overwrite")), limit)
    log.info("extract %s: %d written, %s", os.path.splitext(path)[1], res["written"], res["counts"])
    return res


@module.job("create")
async def create(payload: dict, job):
    fmt = str(payload.get("format") or "zip")
    if fmt not in FORMATS:
        raise ValueError(f"Format inconnu : {fmt}")
    sources = [str(s).strip().strip('"') for s in payload.get("sources") or [] if str(s).strip()]
    if not sources:
        raise ValueError("Ajoutez au moins un fichier ou dossier à archiver")
    for s in sources:
        if not os.path.isabs(s) or not os.path.exists(s):
            raise ValueError(f"Source introuvable : {s}")
    output = str(payload.get("output") or "").strip().strip('"')
    if not output or not os.path.isabs(output):
        raise ValueError("Indiquez le chemin absolu de l'archive à créer")
    if not output.lower().endswith(FORMATS[fmt]):
        output += FORMATS[fmt]
    if not os.path.isdir(os.path.dirname(output)):
        raise ValueError(f"Dossier de destination introuvable : {os.path.dirname(output)}")
    if os.path.exists(output) and not payload.get("overwrite"):
        raise ValueError(f"Le fichier existe déjà : {output}")
    if os.path.isdir(output):
        raise ValueError("La destination est un dossier")
    password = str(payload.get("password") or "") or None
    if password and fmt.startswith("tar"):
        raise ValueError("Le format TAR ne prend pas en charge les mots de passe (utilisez ZIP ou 7z)")
    if password and fmt == "zip" and not _has("pyzipper"):
        raise ValueError("Mot de passe ZIP indisponible : pyzipper n'est pas installé (installez les dépendances "
                         "du module ou utilisez 7z)")
    try:
        level = max(0, min(9, int(payload.get("level", 6))))
    except (TypeError, ValueError):
        level = 6
    res = await _run_threaded(job, _create, sources, output, fmt, level, password)
    log.info("create %s: %d entries", fmt, res["entries"])
    await _remember(res["output"])
    return res
