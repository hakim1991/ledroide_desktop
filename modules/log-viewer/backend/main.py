import asyncio
import bisect
import codecs
import hashlib
import logging
import os
import random
import re
import string
import sys
import threading
import time
import uuid
from array import array
from datetime import datetime, timedelta
from pathlib import Path

from ledroide_sdk import Module

log = logging.getLogger("ledroide.log-viewer")

module = Module(__file__)
router = module.router

CP = 256
BLOCK = 1 << 20
MAX_LINE = 5000
MAX_PAGE = 1000
MAX_SESSIONS = 24
MAX_MATCHES = 20_000_000
POLL_S = 0.5
HEAD_BYTES = 512
ENCODINGS = {"utf-8": "UTF-8", "latin-1": "Latin-1 (ISO-8859-1)", "cp1252": "Windows-1252",
             "utf-16-le": "UTF-16 LE", "utf-16-be": "UTF-16 BE"}
LEVELS = {0: "none", 1: "debug", 2: "info", 3: "warn", 4: "error"}
WORD_LEVEL = {
    "fatal": 4, "critical": 4, "crit": 4, "severe": 4, "panic": 4, "emerg": 4, "emergency": 4, "alert": 4,
    "error": 4, "err": 4, "e": 4, "f": 4, "c": 4,
    "warn": 3, "warning": 3, "w": 3,
    "info": 2, "information": 2, "informational": 2, "notice": 2, "i": 2, "n": 2,
    "debug": 1, "trace": 1, "verbose": 1, "fine": 1, "finer": 1, "finest": 1, "d": 1, "t": 1, "v": 1,
}

RE_STRUCT = re.compile(r"""(?i)["']?(?:level|lvl|severity|loglevel|log_level|levelname)["']?\s*[:=]\s*["']?([a-z]+)""")
RE_BRACKET = re.compile(r"(?i)[\[<(](fatal|critical|crit|error|err|warn|warning|info|notice|debug|trace|verbose|emerg|alert|severe)[\]>)]")
RE_UPPER = re.compile(r"\b(FATAL|CRITICAL|CRIT|SEVERE|PANIC|EMERG|ALERT|ERROR|ERR|WARNING|WARN|INFO|NOTICE|DEBUG|TRACE|VERBOSE)\b")
RE_ACCESS = re.compile(r'"(?:GET|POST|PUT|DELETE|PATCH|HEAD|OPTIONS|CONNECT|TRACE) [^"]*" (\d{3}) ')
RE_GLOG = re.compile(r"^([EWIDFV])\d{4} ")
RE_ANDROID = re.compile(r"^\S+ \S+\s+\d+\s+\d+ ([EWIDFV]) ")
RE_CONT = re.compile(r"^(?:\s|at |Caused by|\.\.\. \d+ more|Traceback|  File |\w+(?:\.\w+)*(?:Error|Exception)\b)")

MONTHS = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}
RE_T_ISO = re.compile(r"(\d{4})-(\d{2})-(\d{2})[T _](\d{2}):(\d{2})(?::(\d{2}))?")
RE_T_SLASH = re.compile(r"(\d{4})/(\d{2})/(\d{2})[ T](\d{2}):(\d{2})(?::(\d{2}))?")
RE_T_CLF = re.compile(r"(\d{2})/([A-Za-z]{3})/(\d{4}):(\d{2}):(\d{2}):(\d{2})")
RE_T_SYSLOG = re.compile(r"^(?:<\d+>)?([A-Z][a-z]{2})\s+(\d{1,2}) (\d{2}):(\d{2}):(\d{2})")
RE_T_FR = re.compile(r"(\d{2})/(\d{2})/(\d{4})[ ,T]+(\d{2}):(\d{2})(?::(\d{2}))?")
RE_T_EPOCH = re.compile(r'"(?:ts|time|timestamp)"\s*:\s*(\d{10})(?:\.\d+)?')


class LogError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _err(message: str, code: str = "BAD_REQUEST") -> dict:
    return {"error": message, "code": code}


# ── Detection ────────────────────────────────────────────────────────────────

def detect_level(text: str) -> int:
    head = text[:400]
    m = RE_STRUCT.search(head)
    if m and m.group(1).lower() in WORD_LEVEL and len(m.group(1)) > 1:
        return WORD_LEVEL[m.group(1).lower()]
    m = RE_BRACKET.search(head)
    if m:
        return WORD_LEVEL[m.group(1).lower()]
    m = RE_UPPER.search(head[:200])
    if m:
        return WORD_LEVEL[m.group(1).lower()]
    m = RE_ACCESS.search(head)
    if m:
        code = int(m.group(1))
        return 4 if code >= 500 else 3 if code >= 400 else 2
    m = RE_GLOG.match(head) or RE_ANDROID.match(head)
    if m:
        return WORD_LEVEL[m.group(1).lower()]
    return 0


def is_continuation(text: str) -> bool:
    return not text.strip() or bool(RE_CONT.match(text))


def parse_time(text: str, year: int) -> str | None:
    head = text[:200]
    m = RE_T_ISO.search(head) or RE_T_SLASH.search(head)
    if m:
        y, mo, d, h, mi, s = m.groups()
        return f"{y}-{mo}-{d} {h}:{mi}:{s or '00'}"
    m = RE_T_CLF.search(head)
    if m:
        d, mon, y, h, mi, s = m.groups()
        mo = MONTHS.get(mon.lower())
        if mo:
            return f"{y}-{mo:02d}-{d} {h}:{mi}:{s}"
    m = RE_T_SYSLOG.match(head)
    if m:
        mon, d, h, mi, s = m.groups()
        mo = MONTHS.get(mon.lower())
        if mo:
            return f"{year}-{mo:02d}-{int(d):02d} {h}:{mi}:{s}"
    m = RE_T_FR.search(head)
    if m:
        d, mo, y, h, mi, s = m.groups()
        return f"{y}-{mo}-{d} {h}:{mi}:{s or '00'}"
    m = RE_T_EPOCH.search(head)
    if m:
        return datetime.fromtimestamp(int(m.group(1))).strftime("%Y-%m-%d %H:%M:%S")
    return None


def detect_format(lines: list[str]) -> str:
    sample = [l for l in lines if l.strip()][:50]
    if not sample:
        return "Vide"
    def share(pred):
        return sum(1 for l in sample if pred(l)) / len(sample)
    if share(lambda l: l.lstrip().startswith("{") and l.rstrip().endswith("}")) > .6:
        return "JSON (une entrée par ligne)"
    if share(lambda l: RE_ACCESS.search(l) is not None) > .6:
        return "Accès HTTP (Apache / nginx)"
    if share(lambda l: RE_T_SYSLOG.match(l) is not None) > .6:
        return "Syslog"
    if share(lambda l: RE_GLOG.match(l) is not None) > .6:
        return "glog / klog"
    if share(lambda l: RE_T_ISO.search(l[:40]) is not None or RE_T_SLASH.search(l[:40]) is not None) > .5:
        return "Horodaté (ISO)"
    if share(lambda l: RE_STRUCT.search(l) is not None) > .5:
        return "Clé=valeur (logfmt)"
    return "Texte"


def detect_encoding(head: bytes) -> tuple[str, int]:
    if head.startswith(codecs.BOM_UTF8):
        return "utf-8", 3
    if head.startswith(codecs.BOM_UTF16_LE):
        return "utf-16-le", 2
    if head.startswith(codecs.BOM_UTF16_BE):
        return "utf-16-be", 2
    if len(head) >= 4:
        sample = head[:4096]
        odd_zero = sample[1::2].count(0) / max(1, len(sample) // 2)
        even_zero = sample[0::2].count(0) / max(1, len(sample) // 2)
        if odd_zero > .4 and even_zero < .05:
            return "utf-16-le", 0
        if even_zero > .4 and odd_zero < .05:
            return "utf-16-be", 0
    try:
        codecs.getincrementaldecoder("utf-8")().decode(head, final=False)
        return "utf-8", 0
    except UnicodeDecodeError:
        return "latin-1", 0


def bom_length(head: bytes, encoding: str) -> int:
    if encoding == "utf-8" and head.startswith(codecs.BOM_UTF8):
        return 3
    if encoding == "utf-16-le" and head.startswith(codecs.BOM_UTF16_LE):
        return 2
    if encoding == "utf-16-be" and head.startswith(codecs.BOM_UTF16_BE):
        return 2
    return 0


# ── Session ──────────────────────────────────────────────────────────────────

class Ctl:
    def __init__(self):
        self.cancelled = False
        self.percent = 0.0


class Session:
    def __init__(self, path: Path, encoding: str | None):
        self.id = uuid.uuid4().hex
        self.path = path
        self.lock = threading.RLock()
        self.forced_encoding = encoding
        self.follow = False
        self.follow_task: asyncio.Task | None = None
        self.index_task: asyncio.Task | None = None
        self.index_ctl: Ctl | None = None
        self.filter: dict | None = None
        self.filter_ctl: Ctl | None = None
        self.filter_task: asyncio.Task | None = None
        self.missing = False
        self._reset_state()

    def _reset_state(self):
        st = self.path.stat()
        with open(self.path, "rb") as f:
            head = f.read(65536)
        if self.forced_encoding:
            self.encoding = self.forced_encoding
            self.bom = bom_length(head, self.encoding)
        else:
            self.encoding, self.bom = detect_encoding(head)
        self.unit = 2 if self.encoding.startswith("utf-16") else 1
        self.nl = {"utf-16-le": b"\n\x00", "utf-16-be": b"\x00\n"}.get(self.encoding, b"\n")
        self.checkpoints = array("Q")
        self.levels = bytearray()
        self.lines_complete = 0
        self.complete_end = self.bom
        self.size = st.st_size
        self.mtime = st.st_mtime
        self.year = datetime.fromtimestamp(st.st_mtime).year
        self.ident = (st.st_ino, st.st_dev)
        self.head_hash = hashlib.sha1(head[:HEAD_BYTES]).hexdigest() if len(head) >= HEAD_BYTES else None
        self.last_level = 0
        self.indexing = True
        self.format = detect_format([self.decode(r) for _, r, _ in self._iter_raw(self.bom, min(self.size, self.bom + 65536), partial=False)][:60])
        self.matches = array("I")
        self.filter_upto = 0
        self.filter_last_time = None
        self.filter_levels_only = True

    def decode(self, raw: bytes) -> str:
        text = raw.decode(self.encoding, "replace")
        return text[:-1] if text.endswith("\r") else text

    def _find_nl(self, buf: bytes, start: int) -> int:
        i = buf.find(self.nl, start)
        if self.unit == 1:
            return i
        while i >= 0 and i % 2:
            i = buf.find(self.nl, i + 1)
        return i

    def _iter_raw(self, offset: int, end: int, partial: bool = True):
        nl_len = len(self.nl)
        with open(self.path, "rb") as f:
            f.seek(offset)
            pos, buf_start, buf = offset, offset, b""
            while pos < end:
                chunk = f.read(min(BLOCK, end - pos))
                if not chunk:
                    break
                pos += len(chunk)
                buf += chunk
                start = 0
                while True:
                    i = self._find_nl(buf, start)
                    if i < 0:
                        break
                    yield buf_start + start, buf[start:i], True
                    start = i + nl_len
                buf = buf[start:]
                buf_start += start
            if buf and partial:
                yield buf_start, buf, False

    @property
    def total(self) -> int:
        return self.lines_complete + (1 if self.size > self.complete_end else 0)

    def index(self, ctl: Ctl | None = None):
        start_offset = self.complete_end
        end = self.path.stat().st_size
        span = max(1, end - start_offset)
        n = 0
        for off, raw, complete in self._iter_raw(start_offset, end, partial=False):
            with self.lock:
                if self.lines_complete % CP == 0:
                    self.checkpoints.append(off)
                text = self.decode(raw)
                lv = detect_level(text)
                if not lv and self.last_level and is_continuation(text):
                    lv = self.last_level
                elif lv:
                    self.last_level = lv
                else:
                    self.last_level = 0
                self.levels.append(lv)
                self.lines_complete += 1
                self.complete_end = off + len(raw) + len(self.nl)
            n += 1
            if ctl is not None and n % 2000 == 0:
                ctl.percent = round((off - start_offset) / span * 100, 1)
                if ctl.cancelled:
                    break
        self.size = end
        if self.head_hash is None and end >= HEAD_BYTES:
            with open(self.path, "rb") as f:
                self.head_hash = hashlib.sha1(f.read(HEAD_BYTES)).hexdigest()
        if ctl is not None:
            ctl.percent = 100.0

    def _seek_iter(self, line: int):
        k = min(line // CP, len(self.checkpoints) - 1)
        if k < 0:
            return self.bom, iter(())
        first = k * CP
        return first, self._iter_raw(self.checkpoints[k], self.size)

    def read_lines(self, nums: list[int]) -> dict[int, tuple[str, int]]:
        out: dict[int, tuple[str, int]] = {}
        total = self.total
        cur, it = None, None
        for n in sorted(set(x for x in nums if 0 <= x < total)):
            if cur is None or n < cur or n - cur > CP:
                cur, it = self._seek_iter(n)
            for off, raw, _ in it:
                if cur == n:
                    text = self.decode(raw)
                    lv = self.levels[n] if n < len(self.levels) else detect_level(text)
                    out[n] = (text, lv)
                    cur += 1
                    break
                cur += 1
            else:
                cur = None
        return out

    def iter_lines(self, start: int, stop: int):
        if start >= stop:
            return
        first, it = self._seek_iter(start)
        n = first
        for off, raw, _ in it:
            if n >= stop:
                return
            if n >= start:
                yield n, self.decode(raw)
            n += 1

    def stats(self) -> dict:
        counts = {name: self.levels.count(lv) for lv, name in LEVELS.items()}
        return {"id": self.id, "path": str(self.path), "name": self.path.name, "encoding": self.encoding,
                "encoding_label": ENCODINGS.get(self.encoding, self.encoding), "bom": self.bom > 0,
                "size": self.size, "total": self.total, "indexing": self.indexing, "format": self.format,
                "levels": counts, "follow": self.follow, "missing": self.missing,
                "filtered": len(self.matches) if self.filter else None,
                "filter_running": bool(self.filter_task and not self.filter_task.done())}


def compile_filter(spec: dict) -> dict:
    levels = spec.get("levels")
    levels = {int(x) for x in levels} if isinstance(levels, list) and levels else None
    if levels is not None and not levels <= set(LEVELS):
        raise LogError("BAD_FILTER", "Niveau invalide")
    text = str(spec.get("text") or "")
    if len(text) > 1000:
        raise LogError("BAD_FILTER", "Filtre trop long")
    case = bool(spec.get("case"))
    pattern = None
    if text:
        if spec.get("regex"):
            try:
                pattern = re.compile(text, 0 if case else re.IGNORECASE)
            except re.error as exc:
                raise LogError("BAD_REGEX", f"Expression régulière invalide : {exc}")
        else:
            pattern = re.compile(re.escape(text), 0 if case else re.IGNORECASE)
    t_from = _norm_time(spec.get("from"))
    t_to = _norm_time(spec.get("to"), end=True)
    if t_from and t_to and t_from > t_to:
        raise LogError("BAD_FILTER", "La date de début est postérieure à la date de fin")
    if levels is None and not pattern and not t_from and not t_to:
        return {}
    return {"levels": levels, "pattern": pattern, "invert": bool(spec.get("invert")) and pattern is not None,
            "from": t_from, "to": t_to, "spec": spec}


def _norm_time(value, end: bool = False) -> str | None:
    if not value:
        return None
    text = str(value).strip().replace("T", " ")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(text, fmt)
        except ValueError:
            continue
        if end and fmt == "%Y-%m-%d %H:%M":
            dt = dt.replace(second=59)
        elif end and fmt == "%Y-%m-%d":
            dt = dt.replace(hour=23, minute=59, second=59)
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    raise LogError("BAD_FILTER", "Date invalide")


def run_filter(s: Session, flt: dict, start: int, stop: int, ctl: Ctl | None, last_time: str | None):
    levels, pattern, invert = flt["levels"], flt["pattern"], flt["invert"]
    t_from, t_to = flt["from"], flt["to"]
    need_text = pattern is not None or t_from or t_to
    out = array("I")
    if not need_text:
        lv_bytes = s.levels
        for n in range(start, min(stop, len(lv_bytes))):
            if lv_bytes[n] in levels:
                out.append(n)
        return out, last_time
    span = max(1, stop - start)
    for n, text in s.iter_lines(start, stop):
        if t_from or t_to:
            t = parse_time(text, s.year)
            if t:
                last_time = t
            t = t or last_time
            if t is None or (t_from and t < t_from) or (t_to and t > t_to):
                continue
        if levels is not None:
            lv = s.levels[n] if n < len(s.levels) else detect_level(text)
            if lv not in levels:
                continue
        if pattern is not None and (pattern.search(text) is None) != invert:
            continue
        out.append(n)
        if len(out) >= MAX_MATCHES:
            break
        if ctl is not None and (n - start) % 5000 == 0:
            ctl.percent = round((n - start) / span * 100, 1)
            if ctl.cancelled:
                break
    return out, last_time


_sessions: dict[str, Session] = {}
_demo_task: asyncio.Task | None = None
_ready = False
_lock = asyncio.Lock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS bookmarks (
    id    INTEGER PRIMARY KEY AUTOINCREMENT,
    file  TEXT NOT NULL,
    line  INTEGER NOT NULL,
    note  TEXT NOT NULL DEFAULT '',
    text  TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    UNIQUE(file, line)
);
CREATE TABLE IF NOT EXISTS recent (
    file  TEXT PRIMARY KEY,
    path  TEXT NOT NULL,
    encoding TEXT NOT NULL DEFAULT '',
    opened_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS prefs (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


async def _db():
    global _ready
    if not _ready:
        async with _lock:
            if not _ready:
                await module.db.executescript(SCHEMA)
                _ready = True
    return module.db


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _file_key(path: Path | str) -> str:
    return os.path.normcase(os.path.abspath(str(path)))


def _clean_path(raw) -> Path:
    text = str(raw or "").strip().strip('"')
    if not text:
        raise LogError("BAD_PATH", "Chemin requis")
    if "\x00" in text:
        raise LogError("BAD_PATH", "Chemin invalide")
    path = Path(os.path.expandvars(os.path.expanduser(text)))
    if not path.is_absolute():
        raise LogError("BAD_PATH", "Indiquez un chemin absolu")
    try:
        path = path.resolve()
    except OSError:
        raise LogError("BAD_PATH", "Chemin invalide")
    return path


def _session(payload: dict) -> Session:
    s = _sessions.get(str(payload.get("id") or ""))
    if s is None:
        raise LogError("NO_SESSION", "Fichier non ouvert (rouvrez-le)")
    return s


async def _drive(ctl: Ctl, fn, *args, event: str, sid: str):
    task = asyncio.ensure_future(asyncio.to_thread(fn, *args))
    while not task.done():
        await asyncio.wait({task}, timeout=0.3)
        if not task.done():
            await module.events.emit(event, {"id": sid, "percent": ctl.percent})
    return task.result()


async def _index_session(s: Session):
    ctl = Ctl()
    s.index_ctl = ctl
    try:
        await _drive(ctl, s.index, ctl, event="log:index", sid=s.id)
    except FileNotFoundError:
        s.missing = True
    except Exception:
        log.exception("indexing failed")
    finally:
        s.indexing = False
    if s.filter:
        await _start_filter(s, s.filter)
    await module.events.emit("log:indexed", s.stats())


async def _start_filter(s: Session, flt: dict):
    if s.filter_task and not s.filter_task.done():
        s.filter_ctl.cancelled = True
        try:
            await s.filter_task
        except Exception:
            pass
    s.filter = flt
    s.matches = array("I")
    s.filter_upto = 0
    s.filter_last_time = None
    if not flt:
        s.filter = None
        return
    ctl = Ctl()
    s.filter_ctl = ctl

    async def go():
        stop = s.lines_complete
        try:
            matches, last = await _drive(ctl, run_filter, s, flt, 0, stop, ctl, None, event="log:filtering", sid=s.id)
        except FileNotFoundError:
            return
        if ctl.cancelled:
            return
        s.matches, s.filter_upto, s.filter_last_time = matches, stop, last
        await module.events.emit("log:filtered", {**s.stats(), "filter_running": False})

    s.filter_task = asyncio.create_task(go())


async def _catch_up_filter(s: Session):
    if not s.filter or (s.filter_task and not s.filter_task.done()):
        return
    stop = s.lines_complete
    if stop <= s.filter_upto:
        return
    more, last = await asyncio.to_thread(run_filter, s, s.filter, s.filter_upto, stop, None, s.filter_last_time)
    s.matches.extend(more)
    s.filter_upto, s.filter_last_time = stop, last


def _probe(s: Session) -> str:
    try:
        st = s.path.stat()
    except FileNotFoundError:
        return "missing"
    except OSError:
        return "same"
    if (st.st_ino, st.st_dev) != s.ident and st.st_ino:
        return "rotated"
    if st.st_size < s.complete_end or st.st_size < s.size:
        return "truncated"
    if s.head_hash:
        try:
            with open(s.path, "rb") as f:
                head = f.read(HEAD_BYTES)
        except OSError:
            return "same"
        if hashlib.sha1(head).hexdigest() != s.head_hash:
            return "rotated"
    if st.st_size > s.size or (st.st_size == s.size and st.st_mtime != s.mtime and st.st_size > s.complete_end):
        s.mtime = st.st_mtime
        return "grown"
    return "same"


async def follow_step(s: Session) -> str:
    if s.indexing:
        return "busy"
    state = await asyncio.to_thread(_probe, s)
    if state == "missing":
        if not s.missing:
            s.missing = True
            await module.events.emit("log:missing", {"id": s.id})
        return state
    if s.missing:
        s.missing = False
        state = "rotated"
    if state in ("rotated", "truncated"):
        log.info("log %s %s, reindexing", s.path.name, state)
        await asyncio.to_thread(s._reset_state)
        await module.events.emit("log:reset", {"id": s.id, "reason": state})
        flt = s.filter
        await asyncio.to_thread(s.index)
        s.indexing = False
        if flt:
            await _start_filter(s, flt)
        await module.events.emit("log:append", s.stats())
        return state
    if state == "grown":
        await asyncio.to_thread(s.index)
        await _catch_up_filter(s)
        await module.events.emit("log:append", s.stats())
    return state


async def _follow_loop(s: Session):
    try:
        while s.follow:
            try:
                await follow_step(s)
            except Exception:
                log.exception("follow failed")
            await asyncio.sleep(POLL_S)
    except asyncio.CancelledError:
        pass


async def _close_session(s: Session):
    s.follow = False
    for ctl in (s.index_ctl, s.filter_ctl):
        if ctl:
            ctl.cancelled = True
    for task in (s.follow_task, s.index_task, s.filter_task):
        if task and not task.done():
            task.cancel()
    _sessions.pop(s.id, None)


def _line_payload(n: int, text: str, lv: int) -> dict:
    long = len(text) > MAX_LINE
    return {"n": n, "t": text[:MAX_LINE] if long else text, "lv": lv, "long": long}


# ── Handlers ─────────────────────────────────────────────────────────────────

@module.on("open")
async def open_file(payload: dict):
    try:
        path = _clean_path(payload.get("path"))
        if not path.exists():
            raise LogError("NOT_FOUND", f"Fichier introuvable : {path}")
        if not path.is_file():
            raise LogError("NOT_A_FILE", f"Ce chemin n'est pas un fichier : {path}")
        encoding = payload.get("encoding") or None
        if encoding and encoding not in ENCODINGS:
            raise LogError("BAD_ENCODING", "Encodage non pris en charge")
        try:
            s = await asyncio.to_thread(Session, path, encoding)
        except PermissionError:
            raise LogError("ACCESS_DENIED", "Accès refusé au fichier")
        except OSError as exc:
            raise LogError("READ_ERROR", f"Lecture impossible : {exc.strerror or exc}")
    except LogError as exc:
        return _err(exc.message, exc.code)
    while len(_sessions) >= MAX_SESSIONS:
        await _close_session(next(iter(_sessions.values())))
    _sessions[s.id] = s
    s.index_task = asyncio.create_task(_index_session(s))
    db = await _db()
    await db.execute("INSERT INTO recent(file, path, encoding, opened_at) VALUES (?, ?, ?, ?) "
                     "ON CONFLICT(file) DO UPDATE SET path=excluded.path, encoding=excluded.encoding, opened_at=excluded.opened_at",
                     (_file_key(path), str(path), encoding or "", _now()))
    await db.execute("DELETE FROM recent WHERE file NOT IN (SELECT file FROM recent ORDER BY opened_at DESC LIMIT 30)")
    if payload.get("follow"):
        await set_follow({"id": s.id, "follow": True})
    log.info("opened %s (%s)", path.name, s.encoding)
    await asyncio.sleep(0.05)
    return s.stats()


@module.on("reopen")
async def reopen(payload: dict):
    try:
        s = _session(payload)
    except LogError as exc:
        return _err(exc.message, exc.code)
    follow = s.follow
    await _close_session(s)
    return await open_file({"path": str(s.path), "encoding": payload.get("encoding") or None, "follow": follow})


@module.on("close")
async def close_file(payload: dict):
    s = _sessions.get(str(payload.get("id") or ""))
    if s:
        await _close_session(s)
    return {"ok": True}


@module.on("info")
async def info(payload: dict):
    try:
        return _session(payload).stats()
    except LogError as exc:
        return _err(exc.message, exc.code)


@module.on("lines")
async def lines(payload: dict):
    try:
        s = _session(payload)
        start = int(payload.get("start", 0))
        count = int(payload.get("count", 200))
    except LogError as exc:
        return _err(exc.message, exc.code)
    except (TypeError, ValueError):
        return _err("Paramètres invalides")
    count = max(0, min(count, MAX_PAGE))
    start = max(0, start)
    filtered = bool(payload.get("filtered")) and s.filter is not None
    if filtered:
        nums = list(s.matches[start:start + count])
        total = len(s.matches)
    else:
        total = s.total
        nums = list(range(start, min(start + count, total)))
    try:
        got = await asyncio.to_thread(s.read_lines, nums)
    except FileNotFoundError:
        return _err("Le fichier a disparu", "NOT_FOUND")
    rows = [_line_payload(n, *got[n]) for n in nums if n in got]
    return {"start": start, "total": total, "lines": rows, "file_total": s.total}


@module.on("line_full")
async def line_full(payload: dict):
    try:
        s = _session(payload)
        n = int(payload.get("n"))
    except LogError as exc:
        return _err(exc.message, exc.code)
    except (TypeError, ValueError):
        return _err("Numéro de ligne invalide")
    got = await asyncio.to_thread(s.read_lines, [n])
    if n not in got:
        return _err("Ligne hors limites", "OUT_OF_RANGE")
    text, lv = got[n]
    return {"n": n, "t": text[:1_000_000], "lv": lv, "time": parse_time(text, s.year)}


@module.on("filter")
async def set_filter(payload: dict):
    try:
        s = _session(payload)
        flt = compile_filter(payload.get("filter") or {})
    except LogError as exc:
        return _err(exc.message, exc.code)
    await _start_filter(s, flt)
    return s.stats()


@module.on("locate")
async def locate(payload: dict):
    try:
        s = _session(payload)
        n = int(payload.get("line"))
    except LogError as exc:
        return _err(exc.message, exc.code)
    except (TypeError, ValueError):
        return _err("Numéro de ligne invalide")
    if not s.filter:
        return {"index": max(0, min(n, s.total - 1)), "exact": 0 <= n < s.total}
    i = bisect.bisect_left(s.matches, n)
    exact = i < len(s.matches) and s.matches[i] == n
    return {"index": min(i, max(0, len(s.matches) - 1)), "exact": exact}


@module.on("find")
async def find(payload: dict):
    try:
        s = _session(payload)
        flt = compile_filter({"text": payload.get("text"), "regex": payload.get("regex"), "case": payload.get("case")})
        start = int(payload.get("from", 0))
    except LogError as exc:
        return _err(exc.message, exc.code)
    except (TypeError, ValueError):
        return _err("Paramètres invalides")
    if not flt:
        return _err("Texte à rechercher requis")
    pattern = flt["pattern"]
    backward = payload.get("direction") == "prev"
    allowed = set(s.matches) if s.filter and len(s.matches) < 2_000_000 else None

    def scan():
        total = s.total
        if backward:
            region = min(start, total) // CP
            while region >= 0:
                lo = region * CP
                hit = None
                for n, text in s.iter_lines(lo, min(start, lo + CP)):
                    if (allowed is None or n in allowed) and pattern.search(text):
                        hit = n
                if hit is not None:
                    return hit
                region -= 1
            return None
        for n, text in s.iter_lines(start + 1, total):
            if (allowed is None or n in allowed) and pattern.search(text):
                return n
        return None

    hit = await asyncio.to_thread(scan)
    return {"line": hit}


@module.on("follow")
async def set_follow(payload: dict):
    try:
        s = _session(payload)
    except LogError as exc:
        return _err(exc.message, exc.code)
    want = bool(payload.get("follow"))
    s.follow = want
    if want and (s.follow_task is None or s.follow_task.done()):
        s.follow_task = asyncio.create_task(_follow_loop(s))
    return s.stats()


@module.job("export")
async def export(payload: dict, job):
    s = _session(payload)
    dest = _clean_path(payload.get("dest"))
    if _file_key(dest) == _file_key(s.path):
        raise LogError("BAD_PATH", "La destination doit être différente du fichier source")
    if not dest.parent.is_dir():
        raise LogError("BAD_PATH", f"Dossier introuvable : {dest.parent}")
    if dest.exists() and not payload.get("overwrite"):
        raise LogError("EXISTS", "Le fichier de destination existe déjà")
    filtered = bool(payload.get("filtered")) and s.filter is not None
    rng = payload.get("range")
    nums = s.matches if filtered else None
    total = len(nums) if nums is not None else s.total
    lo, hi = 0, total
    if isinstance(rng, list) and len(rng) == 2:
        lo, hi = max(0, int(rng[0])), min(total, int(rng[1]) + 1)
    ctl = Ctl()
    with_numbers = bool(payload.get("line_numbers"))

    def write():
        count = 0
        with open(dest, "w", encoding="utf-8", newline="\n") as out:
            if nums is None:
                for n, text in s.iter_lines(lo, hi):
                    out.write(f"{n + 1}\t{text}\n" if with_numbers else text + "\n")
                    count += 1
                    if count % 5000 == 0:
                        ctl.percent = round(count / max(1, hi - lo) * 100, 1)
            else:
                sel = nums[lo:hi]
                for i in range(0, len(sel), 2000):
                    batch = list(sel[i:i + 2000])
                    got = s.read_lines(batch)
                    for n in batch:
                        if n in got:
                            out.write(f"{n + 1}\t{got[n][0]}\n" if with_numbers else got[n][0] + "\n")
                            count += 1
                    ctl.percent = round(count / max(1, len(sel)) * 100, 1)
        return count

    task = asyncio.ensure_future(asyncio.to_thread(write))
    while not task.done():
        await asyncio.wait({task}, timeout=0.4)
        await job.progress(ctl.percent, "Export en cours…")
    count = task.result()
    log.info("exported %d lines", count)
    return {"path": str(dest), "lines": count}


@module.on("export_text")
async def export_text(payload: dict):
    try:
        s = _session(payload)
    except LogError as exc:
        return _err(exc.message, exc.code)
    filtered = bool(payload.get("filtered")) and s.filter is not None
    total = len(s.matches) if filtered else s.total
    if total > 200_000:
        return _err("Trop de lignes pour un téléchargement direct : utilisez « Enregistrer vers un fichier ».", "TOO_LARGE")
    nums = list(s.matches) if filtered else list(range(total))
    got = await asyncio.to_thread(s.read_lines, nums)
    text = "\n".join(got[n][0] for n in nums if n in got)
    if len(text) > 30_000_000:
        return _err("Contenu trop volumineux : utilisez « Enregistrer vers un fichier ».", "TOO_LARGE")
    return {"text": text, "lines": len(nums)}


@module.on("browse")
async def browse(payload: dict):
    raw = str(payload.get("path") or "").strip()

    def listing():
        if not raw:
            roots = []
            if sys.platform == "win32":
                roots = [f"{d}:\\" for d in string.ascii_uppercase if os.path.isdir(f"{d}:\\")]
            else:
                roots = ["/"]
            home = str(Path.home())
            extra = [p for p in (home, os.path.join(home, "Desktop"), os.path.join(home, "Documents"),
                                 os.environ.get("ProgramData", ""), "/var/log") if p and os.path.isdir(p)]
            return {"path": "", "parent": None, "dirs": [{"name": r, "path": r} for r in extra + roots], "files": []}
        path = _clean_path(raw)
        if not path.is_dir():
            raise LogError("NOT_A_DIR", f"Dossier introuvable : {path}")
        dirs, files = [], []
        try:
            entries = list(os.scandir(path))
        except PermissionError:
            raise LogError("ACCESS_DENIED", "Accès refusé à ce dossier")
        for e in entries[:5000]:
            try:
                if e.is_dir():
                    dirs.append({"name": e.name, "path": e.path})
                elif e.is_file():
                    st = e.stat()
                    files.append({"name": e.name, "path": e.path, "size": st.st_size, "mtime": st.st_mtime})
            except OSError:
                continue
        dirs.sort(key=lambda d: d["name"].lower())
        files.sort(key=lambda f: f["name"].lower())
        parent = str(path.parent) if path.parent != path else ""
        return {"path": str(path), "parent": parent, "dirs": dirs, "files": files}

    try:
        return await asyncio.to_thread(listing)
    except LogError as exc:
        return _err(exc.message, exc.code)


@module.on("recent")
async def recent(payload: dict):
    db = await _db()
    rows = await db.fetch_all("SELECT path, encoding, opened_at FROM recent ORDER BY opened_at DESC LIMIT 30")
    for r in rows:
        try:
            st = os.stat(r["path"])
            r["exists"], r["size"] = True, st.st_size
        except OSError:
            r["exists"], r["size"] = False, 0
    return {"recent": rows}


@module.on("recent.remove")
async def recent_remove(payload: dict):
    db = await _db()
    await db.execute("DELETE FROM recent WHERE file = ?", (_file_key(str(payload.get("path") or "")),))
    return {"ok": True}


@module.on("bookmarks")
async def bookmarks(payload: dict):
    db = await _db()
    key = _file_key(str(payload.get("path") or ""))
    rows = await db.fetch_all("SELECT id, line, note, text FROM bookmarks WHERE file = ? ORDER BY line", (key,))
    return {"bookmarks": rows}


@module.on("bookmark.toggle")
async def bookmark_toggle(payload: dict):
    try:
        s = _session(payload)
        n = int(payload.get("line"))
    except LogError as exc:
        return _err(exc.message, exc.code)
    except (TypeError, ValueError):
        return _err("Numéro de ligne invalide")
    if not 0 <= n < s.total:
        return _err("Ligne hors limites", "OUT_OF_RANGE")
    db = await _db()
    key = _file_key(s.path)
    existing = await db.fetch_one("SELECT id FROM bookmarks WHERE file = ? AND line = ?", (key, n))
    if existing and payload.get("note") is None:
        await db.execute("DELETE FROM bookmarks WHERE id = ?", (existing["id"],))
    else:
        got = await asyncio.to_thread(s.read_lines, [n])
        text = got.get(n, ("", 0))[0][:300]
        note = str(payload.get("note") or "")[:500]
        await db.execute("INSERT INTO bookmarks(file, line, note, text, created_at) VALUES (?, ?, ?, ?, ?) "
                         "ON CONFLICT(file, line) DO UPDATE SET note=excluded.note",
                         (key, n, note, text, _now()))
    return await bookmarks({"path": str(s.path)})


@module.on("bookmarks.clear")
async def bookmarks_clear(payload: dict):
    db = await _db()
    await db.execute("DELETE FROM bookmarks WHERE file = ?", (_file_key(str(payload.get("path") or "")),))
    return {"bookmarks": []}


@module.on("prefs.get")
async def prefs_get(payload: dict):
    db = await _db()
    rows = await db.fetch_all("SELECT key, value FROM prefs")
    return {r["key"]: r["value"] for r in rows}


@module.on("prefs.set")
async def prefs_set(payload: dict):
    db = await _db()
    for key, value in (payload or {}).items():
        if isinstance(key, str) and len(key) < 64:
            await db.execute("INSERT INTO prefs(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                             (key, str(value)[:100_000]))
    return {"ok": True}


DEMO_MSGS = [
    (2, "INFO", "Requête traitée en {ms} ms (GET /api/users/{n})"),
    (2, "INFO", "Utilisateur {n} connecté depuis 192.168.1.{b}"),
    (1, "DEBUG", "Cache hit ratio={r}% taille={n}"),
    (3, "WARN", "Temps de réponse élevé : {ms} ms sur /api/orders"),
    (3, "WARNING", "Pool de connexions presque saturé ({b}/100)"),
    (4, "ERROR", "Échec de la connexion à la base : timeout après {ms} ms"),
    (2, "INFO", "Tâche planifiée « purge » terminée ({n} éléments)"),
    (1, "DEBUG", "Payload reçu : {{\"id\": {n}, \"ok\": true}}"),
]


def _demo_line(dt: datetime, rng: random.Random) -> list[str]:
    _, lvl, msg = rng.choice(DEMO_MSGS)
    text = msg.format(ms=rng.randint(3, 4000), n=rng.randint(1, 9999), b=rng.randint(2, 254), r=rng.randint(40, 99))
    out = [f"{dt.strftime('%Y-%m-%d %H:%M:%S')},{rng.randint(0, 999):03d} {lvl:<7} [{rng.choice(['main', 'worker-1', 'worker-2', 'http'])}] app.{rng.choice(['api', 'db', 'auth', 'jobs'])} - {text}"]
    if lvl == "ERROR" and rng.random() < .6:
        out += ["Traceback (most recent call last):", '  File "app/db.py", line 42, in connect',
                "    conn = pool.acquire(timeout=5)", "TimeoutError: délai dépassé"]
    return out


@module.on("demo")
async def demo(payload: dict):
    path = module.data_path("demo")
    path.mkdir(exist_ok=True)
    target = path / "application.log"
    rng = random.Random(42)
    n = max(100, min(int(payload.get("lines", 20000) or 20000), 2_000_000))

    def write():
        dt = datetime.now() - timedelta(seconds=n * 3)
        with open(target, "w", encoding="utf-8", newline="\n") as f:
            for _ in range(n):
                dt += timedelta(seconds=rng.randint(0, 6))
                f.write("\n".join(_demo_line(dt, rng)) + "\n")
    await asyncio.to_thread(write)
    return {"path": str(target)}


@module.on("demo.live")
async def demo_live(payload: dict):
    global _demo_task
    if _demo_task and not _demo_task.done():
        _demo_task.cancel()
        _demo_task = None
    if not payload.get("on"):
        return {"live": False}
    target = module.data_path("demo") / "application.log"
    if not target.exists():
        await demo({"lines": 2000})
    rng = random.Random()

    async def writer():
        end = time.monotonic() + 600
        while time.monotonic() < end:
            lines = _demo_line(datetime.now(), rng)
            await asyncio.to_thread(_append, target, lines)
            await asyncio.sleep(rng.uniform(0.2, 1.0))

    _demo_task = asyncio.create_task(writer())
    return {"live": True, "path": str(target)}


def _append(target: Path, lines: list[str]):
    with open(target, "a", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(lines) + "\n")


@module.on_shutdown
async def cleanup():
    global _demo_task
    if _demo_task:
        _demo_task.cancel()
        _demo_task = None
    for s in list(_sessions.values()):
        await _close_session(s)
