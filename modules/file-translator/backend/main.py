import asyncio
import base64
import binascii
import importlib.util
import io
import json
import logging
import os
import re
import shutil
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from html import unescape as html_unescape
from typing import Any, Callable

from pydantic import BaseModel, Field, ValidationError, field_validator

from ledroide_sdk import Module

log = logging.getLogger("ledroide.file-translator")

module = Module(__file__)
router = module.router

KEY_NAME = "ANTHROPIC_API_KEY"
FALLBACK_BETA = "server-side-fallback-2026-07-01"
MODELS = [
    {"id": "claude-opus-5-5", "label": "Claude Opus 5.5", "hint": "Meilleure qualité (recommandé)", "fallbacks": True},
    {"id": "claude-sonnet-5-5", "label": "Claude Sonnet 5.5", "hint": "Rapide et économique", "fallbacks": True},
    {"id": "claude-haiku-5-5", "label": "Claude Haiku 5.5", "hint": "Très rapide, très bon marché", "fallbacks": False},
]
MODEL_IDS = [m["id"] for m in MODELS]
EFFORTS = ("low", "medium", "high", "xhigh", "max")
TONES = {
    "neutre": "neutre et naturel",
    "formel": "formel et professionnel (vouvoiement le cas échéant)",
    "familier": "familier et décontracté (tutoiement le cas échéant)",
    "technique": "technique et précis, en conservant la terminologie du domaine",
    "marketing": "engageant et persuasif, adapté à un public de consommateurs",
}
FORMATS = {
    ".srt": "srt", ".vtt": "vtt", ".md": "markdown", ".markdown": "markdown", ".txt": "text", ".text": "text",
    ".docx": "docx", ".json": "json",
}
FORMAT_LABELS = {"srt": "Sous-titres SRT", "vtt": "Sous-titres WebVTT", "markdown": "Markdown", "text": "Texte",
                 "docx": "Word (.docx)", "json": "JSON i18n"}
MIMES = {"srt": "application/x-subrip", "vtt": "text/vtt", "markdown": "text/markdown", "text": "text/plain",
         "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document", "json": "application/json"}
LANG_CODES = {
    "français": "fr", "anglais": "en", "espagnol": "es", "allemand": "de", "italien": "it", "portugais": "pt",
    "néerlandais": "nl", "polonais": "pl", "arabe": "ar", "chinois": "zh", "japonais": "ja", "coréen": "ko",
    "russe": "ru", "turc": "tr", "ukrainien": "uk", "suédois": "sv", "danois": "da", "norvégien": "no",
    "finnois": "fi", "grec": "el", "tchèque": "cs", "roumain": "ro", "hongrois": "hu", "hébreu": "he", "hindi": "hi",
    "vietnamien": "vi", "thaï": "th", "indonésien": "id",
}

MAX_FILE_BYTES = 10 * 1024 * 1024
MAX_XML_BYTES = 80 * 1024 * 1024
MAX_SEGMENTS = 50_000
MAX_CHARS = 2_000_000
MAX_BATCH_SEGMENTS = 60
BATCH_MAX_TOKENS = 32000
MAX_API_ATTEMPTS = 3
CONTEXT_BEFORE = 3
CONTEXT_AFTER = 2
LANG_RE = re.compile(r"^[\w\s'()-]{1,40}$", re.UNICODE)
LETTER_RE = re.compile(r"[^\W\d_]", re.UNICODE)

PH = "⟦{}⟧"
PH_RE = re.compile(r"⟦(\d+)⟧")
RUN_RE = re.compile(r"⟪(\d+)⟫(.*?)⟪/\1⟫", re.S)
RUN_TAG_RE = re.compile(r"⟪/?\d+⟫")
SEG_RE = re.compile(r"<seg id=\"(\d+)\">(.*?)</seg>", re.S)

_client_factory: Callable[[str], Any] | None = None
_running: dict[int, str] = {}
_ready = False
_lock = asyncio.Lock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS translations (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT    NOT NULL,
    format        TEXT    NOT NULL,
    size          INTEGER NOT NULL,
    n_segments    INTEGER NOT NULL,
    n_todo        INTEGER NOT NULL,
    chars         INTEGER NOT NULL,
    source_lang   TEXT    NOT NULL DEFAULT 'auto',
    target_lang   TEXT    NOT NULL DEFAULT '',
    tone          TEXT    NOT NULL DEFAULT 'neutre',
    glossary      TEXT    NOT NULL DEFAULT '[]',
    instructions  TEXT    NOT NULL DEFAULT '',
    model         TEXT    NOT NULL DEFAULT '',
    status        TEXT    NOT NULL DEFAULT 'draft',
    done_segments INTEGER NOT NULL DEFAULT 0,
    warnings      INTEGER NOT NULL DEFAULT 0,
    error         TEXT    NOT NULL DEFAULT '',
    error_code    TEXT    NOT NULL DEFAULT '',
    usage         TEXT    NOT NULL DEFAULT '{}',
    created_at    TEXT    NOT NULL,
    updated_at    TEXT    NOT NULL
);
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


class TranslatorError(Exception):
    def __init__(self, code: str, message: str, **extra):
        super().__init__(message)
        self.code = code
        self.message = message
        self.extra = extra


def _err(message: str, code: str = "BAD_REQUEST", **extra) -> dict:
    return {"error": message, "code": code, **extra}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _validation_message(exc: ValidationError) -> str:
    first = exc.errors()[0]
    loc = ".".join(str(p) for p in first.get("loc", ()))
    msg = str(first.get("msg", "Données invalides")).removeprefix("Value error, ")
    return f"{loc} : {msg}" if loc else msg


async def _db():
    global _ready
    if not _ready:
        async with _lock:
            if not _ready:
                await module.db.executescript(SCHEMA)
                await module.db.execute("UPDATE translations SET status = 'cancelled' WHERE status = 'running'")
                _ready = True
    return module.db


# ── Masking ─────────────────────────────────────────────────────────────────

URL_PAT = r"https?://[^\s<>()\[\]\"'`]+[^\s<>()\[\]\"'`.,;:!?]"
SEG_TAG_PAT = r"</?seg\b[^>]*>|[⟦⟧⟪⟫]"
MASKS = {
    "text": [SEG_TAG_PAT, URL_PAT],
    "srt": [SEG_TAG_PAT, r"<[^<>\n]+>", r"\{\\[^{}\n]*\}"],
    "markdown": [SEG_TAG_PAT, r"(`+)[^`]*?\1", r"\]\([^()\s]*(?:\([^()\s]*\))?[^()]*?\)", r"\]\[[^\]\n]*\]",
                 r"\[\^[^\]\n]+\]", r"<!--.*?-->", r"</?[A-Za-z][^<>\n]*>", r"<" + URL_PAT + ">", URL_PAT,
                 r"\{#[\w-]+\}"],
    "docx": [SEG_TAG_PAT, URL_PAT],
    "json": [SEG_TAG_PAT, r"\{\{.*?\}\}", r"%\d+\$[sdifu@]", r"%\(\w+\)[sdif]", r"%[sdifu@]", r"\$t\([^)]*\)",
             r"@:[\w.]+", r"</?[A-Za-z][^<>\n]*>", URL_PAT],
}
MASKS["vtt"] = MASKS["srt"]
_MASK_RE = {k: re.compile("|".join(f"(?:{p})" for p in v), re.S) for k, v in MASKS.items()}


def _brace_spans(text: str) -> list[tuple[int, int]]:
    spans, depth, start = [], 0, 0
    for i, ch in enumerate(text):
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}" and depth:
            depth -= 1
            if depth == 0:
                spans.append((start, i + 1))
    return spans


def mask(text: str, fmt: str, ph: list[str]) -> str:
    spans = [m.span() for m in _MASK_RE[fmt].finditer(text)]
    if fmt == "json":
        spans += _brace_spans(text)
    spans.sort(key=lambda s: (s[0], -s[1]))
    merged: list[tuple[int, int]] = []
    for s, e in spans:
        if e <= s:
            continue
        if merged and s < merged[-1][1]:
            if e > merged[-1][1]:
                merged[-1] = (merged[-1][0], e)
            continue
        merged.append((s, e))
    out, pos = [], 0
    for s, e in merged:
        out.append(text[pos:s])
        out.append(PH.format(len(ph)))
        ph.append(text[s:e])
        pos = e
    out.append(text[pos:])
    return "".join(out)


def unmask(text: str, ph: list[str]) -> str:
    return PH_RE.sub(lambda m: ph[int(m.group(1))] if int(m.group(1)) < len(ph) else "", text)


def needs_translation(masked: str) -> bool:
    return bool(LETTER_RE.search(PH_RE.sub("", RUN_TAG_RE.sub("", masked))))


# ── Segments & encoding ─────────────────────────────────────────────────────

@dataclass
class Segment:
    units: list[str]
    note: str = ""


@dataclass
class Parsed:
    fmt: str
    segments: list[Segment]
    build: Callable[[list[list[str] | None]], bytes]
    meta: dict = field(default_factory=dict)


def _split_ws(s: str) -> tuple[str, str, str]:
    core = s.strip()
    if not core:
        return s, "", ""
    lead = s[:len(s) - len(s.lstrip())]
    trail = s[len(s.rstrip()):]
    return lead, core, trail


def encode_segment(seg: Segment, fmt: str) -> tuple[str, list[str]]:
    ph: list[str] = []
    cores = [mask(_split_ws(u)[1], fmt, ph) for u in seg.units]
    if len(cores) == 1:
        return cores[0], ph
    return "".join(f"⟪{i}⟫{c}⟪/{i}⟫" for i, c in enumerate(cores)), ph


def decode_segment(seg: Segment, fmt: str, translated: str, ph: list[str]) -> tuple[list[str], list[str]]:
    warnings = []
    found = [int(x) for x in PH_RE.findall(translated)]
    missing = [i for i in range(len(ph)) if found.count(i) == 0]
    dup = sorted({i for i in found if found.count(i) > 1 or i >= len(ph)})
    if dup:
        warnings.append("marqueurs en double")
        seen: set[int] = set()

        def dedupe(m):
            i = int(m.group(1))
            if i >= len(ph) or i in seen:
                return ""
            seen.add(i)
            return m.group(0)
        translated = PH_RE.sub(dedupe, translated)
    if missing:
        warnings.append("éléments protégés réinsérés en fin de segment")
        translated = translated.rstrip() + " " + " ".join(PH.format(i) for i in missing)
    n = len(seg.units)
    if n == 1:
        parts = [RUN_TAG_RE.sub("", translated)]
    else:
        found_runs = {int(m.group(1)): m.group(2) for m in RUN_RE.finditer(translated)}
        if sorted(found_runs) == list(range(n)):
            parts = [found_runs[i] for i in range(n)]
        else:
            warnings.append("mise en forme simplifiée")
            parts = [RUN_TAG_RE.sub("", translated)] + [""] * (n - 1)
    out = []
    for src, part in zip(seg.units, parts):
        lead, core, trail = _split_ws(src)
        text = unmask(part, ph).strip()
        if not core:
            out.append(src)
        else:
            out.append(lead + text + trail if text else "")
    return out, warnings


# ── Text formats ────────────────────────────────────────────────────────────

def _decode(data: bytes) -> tuple[str, bool]:
    if data.startswith(b"\xef\xbb\xbf"):
        return data[3:].decode("utf-8", errors="replace"), True
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16", errors="replace"), False
    try:
        return data.decode("utf-8"), False
    except UnicodeDecodeError:
        return data.decode("cp1252", errors="replace"), False


def _encode(text: str, bom: bool) -> bytes:
    return (b"\xef\xbb\xbf" if bom else b"") + text.encode("utf-8")


def _template(parts: list, segments: list[Segment], nl: str, bom: bool, joiner: str = "") -> Callable:
    def build(trs: list[list[str] | None]) -> bytes:
        out = []
        for p in parts:
            if isinstance(p, int):
                tr = trs[p] if p < len(trs) and trs[p] else segments[p].units
                out.append(tr[0])
            else:
                out.append(p)
        text = joiner.join(out)
        if nl != "\n":
            text = text.replace("\n", nl)
        return _encode(text, bom)
    return build


def parse_text(data: bytes) -> Parsed:
    text, bom = _decode(data)
    nl = "\r\n" if "\r\n" in text else "\n"
    text = text.replace("\r\n", "\n")
    parts: list = []
    segments: list[Segment] = []
    for piece in re.split(r"(\n[ \t]*\n+)", text):
        if not piece:
            continue
        if piece.strip() and not re.fullmatch(r"\n[ \t]*\n+", piece):
            parts.append(len(segments))
            segments.append(Segment([piece]))
        else:
            parts.append(piece)
    return Parsed("text", segments, _template(parts, segments, nl, bom))


TIMING_RE = re.compile(r"-->")


def parse_subtitles(data: bytes, fmt: str) -> Parsed:
    text, bom = _decode(data)
    nl = "\r\n" if "\r\n" in text else "\n"
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    parts: list = []
    segments: list[Segment] = []
    blocks = re.split(r"(\n[ \t]*\n+)", text)
    first_real = True
    for block in blocks:
        if not block.strip() or re.fullmatch(r"\n[ \t]*\n+", block):
            parts.append(block)
            continue
        lines = block.split("\n")
        if fmt == "vtt" and first_real and lines[0].lstrip("﻿").startswith("WEBVTT"):
            parts.append(block)
            first_real = False
            continue
        first_real = False
        if fmt == "vtt" and re.match(r"^(NOTE|STYLE|REGION)(\s|$)", lines[0]):
            parts.append(block)
            continue
        ti = next((i for i, l in enumerate(lines) if TIMING_RE.search(l)), None)
        if ti is None or ti > 2:
            parts.append(block)
            continue
        head = "\n".join(lines[:ti + 1])
        body = "\n".join(lines[ti + 1:])
        if not body.strip():
            parts.append(block)
            continue
        parts.append(head + "\n")
        parts.append(len(segments))
        segments.append(Segment([body], note=lines[ti].strip()))
    return Parsed(fmt, segments, _template(parts, segments, nl, bom))


MD_PREFIX_RE = re.compile(r"^(\s*(?:>\s?)*\s*(?:#{1,6}\s+|[-*+]\s+(?:\[[ xX]\]\s+)?|\d{1,9}[.)]\s+)?)(.*)$")
FENCE_RE = re.compile(r"^\s*(`{3,}|~{3,})")
TABLE_SEP_RE = re.compile(r"^\s*\|?\s*:?-{1,}:?\s*(\|\s*:?-{1,}:?\s*)+\|?\s*$")


def parse_markdown(data: bytes) -> Parsed:
    text, bom = _decode(data)
    nl = "\r\n" if "\r\n" in text else "\n"
    lines = text.replace("\r\n", "\n").split("\n")
    parts: list = []
    segments: list[Segment] = []

    def seg(s: str):
        parts.append(len(segments))
        segments.append(Segment([s]))

    i = 0
    if lines and lines[0].strip() == "---":
        j = next((k for k in range(1, len(lines)) if lines[k].strip() in ("---", "...")), None)
        if j is not None:
            parts.append("\n".join(lines[:j + 1]) + "\n")
            i = j + 1
    para: list[str] = []
    prefix_para = ""

    def flush_para():
        nonlocal para
        if para:
            parts.append(prefix_para)
            seg("\n".join(para))
            parts.append("\n")
            para = []

    prev_blank = True
    while i < len(lines):
        line = lines[i]
        m = FENCE_RE.match(line)
        if m:
            flush_para()
            fence = m.group(1)
            j = i + 1
            while j < len(lines) and not re.match(r"^\s*" + re.escape(fence[0]) + "{" + str(len(fence)) + r",}\s*$", lines[j]):
                j += 1
            parts.append("\n".join(lines[i:j + 1]) + ("\n" if j + 1 < len(lines) or j >= len(lines) else "\n"))
            i = j + 1
            prev_blank = False
            continue
        stripped = line.strip()
        if not stripped:
            flush_para()
            parts.append(line + "\n")
            i += 1
            prev_blank = True
            continue
        if (prev_blank and re.match(r"^( {4}|\t)", line) and not para) or \
                re.match(r"^\s*\[[^\]]+\]:\s*\S+", line) or re.match(r"^\s*([-*_])(\s*\1){2,}\s*$", line) or \
                re.match(r"^\s*(=+|-+)\s*$", line) or re.match(r"^\s*<(!--|/?[A-Za-z][\w-]*[\s>/])", line) or \
                stripped in ("$$",):
            flush_para()
            parts.append(line + "\n")
            i += 1
            prev_blank = False
            continue
        if "|" in line and (TABLE_SEP_RE.match(line) or (i + 1 < len(lines) and TABLE_SEP_RE.match(lines[i + 1]))
                            or (i > 0 and "|" in lines[i - 1] and not prev_blank)):
            flush_para()
            if TABLE_SEP_RE.match(line):
                parts.append(line + "\n")
            else:
                cells = re.split(r"(?<!\\)(\|)", line)
                for c in cells:
                    if c == "|" or not c.strip():
                        parts.append(c)
                    else:
                        seg(c)
                parts.append("\n")
            i += 1
            prev_blank = False
            continue
        pm = MD_PREFIX_RE.match(line)
        prefix, rest = pm.group(1), pm.group(2)
        is_plain = not prefix.strip()
        if is_plain and para and prefix == prefix_para:
            para.append(rest)
        else:
            flush_para()
            if not is_plain:
                parts.append(prefix)
                if rest.strip():
                    seg(rest)
                parts.append("\n")
            else:
                prefix_para = prefix
                para = [rest]
        i += 1
        prev_blank = False
    flush_para()
    if parts and isinstance(parts[-1], str) and parts[-1].endswith("\n") and not text.replace("\r\n", "\n").endswith("\n"):
        parts[-1] = parts[-1][:-1]
    return Parsed("markdown", segments, _template(parts, segments, nl, bom))


def parse_json(data: bytes) -> Parsed:
    text, bom = _decode(data)
    try:
        obj = json.loads(text)
    except json.JSONDecodeError as exc:
        raise TranslatorError("BAD_FILE", f"JSON invalide (ligne {exc.lineno}, colonne {exc.colno}).")
    paths: list[tuple] = []
    segments: list[Segment] = []

    def walk(node, path):
        if isinstance(node, dict):
            for k, v in node.items():
                walk(v, path + (k,))
        elif isinstance(node, list):
            for idx, v in enumerate(node):
                walk(v, path + (idx,))
        elif isinstance(node, str) and node.strip():
            paths.append(path)
            segments.append(Segment([node], note=".".join(str(p) for p in path)))

    walk(obj, ())
    m = re.search(r"\n([ \t]+)[\"\[{]", text)
    indent = (m.group(1) if m.group(1) != " " * len(m.group(1)) else len(m.group(1))) if m else (None if "\n" not in text.strip() else 2)
    trailing_nl = text.endswith("\n")
    nl = "\r\n" if "\r\n" in text else "\n"

    def build(trs):
        clone = json.loads(text)
        for idx, path in enumerate(paths):
            tr = trs[idx] if idx < len(trs) and trs[idx] else None
            if not tr:
                continue
            node = clone
            for p in path[:-1]:
                node = node[p]
            node[path[-1]] = tr[0]
        out = json.dumps(clone, ensure_ascii=False, indent=indent)
        if trailing_nl:
            out += "\n"
        if nl != "\n":
            out = out.replace("\n", nl)
        return _encode(out, bom)

    return Parsed("json", segments, build)


# ── DOCX ────────────────────────────────────────────────────────────────────

DOCX_PART_RE = re.compile(r"^word/(document|header\d*|footer\d*|footnotes|endnotes)\.xml$")
DOCX_TOKEN_RE = re.compile(
    r"<w:p(?=[\s>/])[^>]*?/>|<w:p(?=[\s>])[^>]*>|</w:p>|<w:r(?=[\s>])[^>]*>|<w:t(?=[\s>/])[^>]*?/>|"
    r"<w:t(?=[\s>])[^>]*>(.*?)</w:t>", re.S)
RPR_RE = re.compile(r"<w:rPr>.*?</w:rPr>|<w:rPr/>", re.S)
BETWEEN_OK_RE = re.compile(r"^(?:\s|</w:r>|<w:r(?:\s[^>]*)?>|<w:rPr>.*?</w:rPr>|<w:rPr/>|<w:proofErr[^>]*/>|"
                           r"<w:bookmark(?:Start|End)[^>]*/>|<w:lastRenderedPageBreak/>)*$", re.S)


def _xml_escape(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _scan_docx_part(xml: str) -> list[list[list[tuple[int, int, str]]]]:
    paragraphs: list[list[list[tuple[int, int, str]]]] = []
    stack: list[int] = []
    last_run_start = None
    meta: dict[int, dict] = {}
    for m in DOCX_TOKEN_RE.finditer(xml):
        tok = m.group(0)
        if tok.startswith("</w:p"):
            if stack:
                stack.pop()
        elif tok.startswith("<w:p"):
            if not tok.endswith("/>"):
                paragraphs.append([])
                stack.append(len(paragraphs) - 1)
                meta[len(paragraphs) - 1] = {"last_end": None, "last_rpr": None}
        elif tok.startswith("<w:r"):
            last_run_start = m.end()
        elif tok.startswith("<w:t") and not tok.endswith("/>") and stack:
            pid = stack[-1]
            rpr_zone = xml[last_run_start:m.start()] if last_run_start is not None else ""
            rm = RPR_RE.search(rpr_zone)
            rpr = rm.group(0) if rm else ""
            info = meta[pid]
            node = (m.start(), m.end(), html_unescape(m.group(1)))
            groups = paragraphs[pid]
            if groups and info["last_end"] is not None and info["last_rpr"] == rpr and \
                    BETWEEN_OK_RE.match(xml[info["last_end"]:m.start()]):
                groups[-1].append(node)
            else:
                groups.append([node])
            info["last_end"] = m.end()
            info["last_rpr"] = rpr
    return [p for p in paragraphs if p]


def parse_docx(data: bytes) -> Parsed:
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise TranslatorError("BAD_FILE", "Fichier Word invalide (ce n'est pas une archive .docx).")
    with zf:
        infos = zf.infolist()
        names = [i.filename for i in infos]
        if "word/document.xml" not in names:
            raise TranslatorError("BAD_FILE", "Fichier Word invalide (word/document.xml absent).")
        if sum(i.file_size for i in infos) > MAX_XML_BYTES * 2:
            raise TranslatorError("TOO_LARGE", "Document Word trop volumineux une fois décompressé.")
        raw = {i.filename: zf.read(i) for i in infos}
    order = sorted([n for n in names if DOCX_PART_RE.match(n)],
                   key=lambda n: (0 if n == "word/document.xml" else 1, n))
    xml_parts: dict[str, str] = {}
    locs: list[tuple[str, list[list[tuple[int, int, str]]]]] = []
    segments: list[Segment] = []
    for name in order:
        xml = raw[name].decode("utf-8", errors="replace")
        xml_parts[name] = xml
        for groups in _scan_docx_part(xml):
            units = ["".join(n[2] for n in g) for g in groups]
            if not "".join(units).strip():
                continue
            locs.append((name, groups))
            where = "corps" if name == "word/document.xml" else name.split("/")[1].rsplit(".", 1)[0]
            segments.append(Segment(units, note=where))

    def build(trs):
        edits: dict[str, list[tuple[int, int, str]]] = {}
        for idx, (name, groups) in enumerate(locs):
            tr = trs[idx] if idx < len(trs) and trs[idx] else None
            if not tr:
                continue
            for g, text in zip(groups, tr):
                for k, (s, e, _old) in enumerate(g):
                    edits.setdefault(name, []).append(
                        (s, e, f'<w:t xml:space="preserve">{_xml_escape(text if k == 0 else "")}</w:t>'))
        out = io.BytesIO()
        with zipfile.ZipFile(out, "w") as z:
            for info in infos:
                content = raw[info.filename]
                if info.filename in edits:
                    xml = xml_parts[info.filename]
                    pieces, pos = [], 0
                    for s, e, rep in sorted(edits[info.filename]):
                        pieces.append(xml[pos:s])
                        pieces.append(rep)
                        pos = e
                    pieces.append(xml[pos:])
                    content = "".join(pieces).encode("utf-8")
                zi = zipfile.ZipInfo(info.filename, date_time=info.date_time)
                zi.compress_type = info.compress_type
                zi.external_attr = info.external_attr
                z.writestr(zi, content)
        return out.getvalue()

    return Parsed("docx", segments, build)


def parse_file(name: str, data: bytes) -> Parsed:
    ext = ("." + name.rsplit(".", 1)[-1].lower()) if "." in name else ""
    fmt = FORMATS.get(ext)
    if fmt is None:
        if ext == ".doc":
            raise TranslatorError("UNSUPPORTED", "L'ancien format .doc n'est pas pris en charge : enregistrez-le en .docx.")
        raise TranslatorError("UNSUPPORTED", "Format non pris en charge : SRT, VTT, Markdown, TXT, DOCX ou JSON.")
    if fmt != "docx" and b"\x00" in data[:4096] and not data.startswith((b"\xff\xfe", b"\xfe\xff")):
        raise TranslatorError("BAD_FILE", "Ce fichier semble binaire et ne correspond pas à son extension.")
    if fmt == "docx":
        return parse_docx(data)
    if fmt in ("srt", "vtt"):
        return parse_subtitles(data, fmt)
    if fmt == "markdown":
        return parse_markdown(data)
    if fmt == "json":
        return parse_json(data)
    return parse_text(data)


# ── Batching & prompts ──────────────────────────────────────────────────────

def initial_state(parsed: Parsed) -> dict:
    tr = {str(i): list(s.units) for i, s in enumerate(parsed.segments)
          if not needs_translation(encode_segment(s, parsed.fmt)[0])}
    return {"tr": tr, "warn": {}, "manual": []}


def make_batches(items: list[tuple[int, str]], max_chars: int, max_segments: int = MAX_BATCH_SEGMENTS) -> list[list[tuple[int, str]]]:
    batches, cur, size = [], [], 0
    for idx, text in items:
        if cur and (size + len(text) > max_chars or len(cur) >= max_segments):
            batches.append(cur)
            cur, size = [], 0
        cur.append((idx, text))
        size += len(text)
    if cur:
        batches.append(cur)
    return batches


FORMAT_RULES = {
    "srt": "Le fichier est un sous-titrage : traduis de façon concise et naturelle à l'oral, lisible à l'écran ; "
           "garde au plus le même nombre de lignes que l'original pour chaque sous-titre (sauts de ligne).",
    "vtt": "Le fichier est un sous-titrage : traduis de façon concise et naturelle à l'oral, lisible à l'écran ; "
           "garde au plus le même nombre de lignes que l'original pour chaque sous-titre (sauts de ligne).",
    "markdown": "Le fichier est un document Markdown : conserve la syntaxe Markdown présente dans les segments "
                "(gras, italique, crochets de liens) et les sauts de ligne.",
    "text": "Le fichier est un texte brut : conserve les sauts de ligne internes des paragraphes.",
    "docx": "Le fichier est un document Word.",
    "json": "Le fichier contient les chaînes d'interface d'une application (i18n) : traductions courtes, "
            "idiomatiques et cohérentes pour une interface utilisateur ; la référence de la clé est donnée à titre "
            "indicatif dans l'attribut « cle ».",
}


def build_system(fmt: str, source_lang: str, target_lang: str, tone: str, glossary: list[dict], instructions: str) -> str:
    src = "détecte la langue source" if source_lang in ("", "auto") else f"la langue source est : {source_lang}"
    lines = [
        f"Tu es un traducteur professionnel. Traduis vers : {target_lang} ({src}).",
        f"Ton souhaité : {TONES.get(tone, TONES['neutre'])}.",
        FORMAT_RULES.get(fmt, ""),
        "Règles impératives :",
        "1. Chaque segment est fourni sous la forme <seg id=\"N\">texte</seg>. Réponds uniquement par les segments "
        "traduits, au même format, avec exactement les mêmes id, dans le même ordre, sans aucun autre texte.",
        "2. Ne fusionne pas et ne divise pas les segments. Un segment qui ne doit pas être traduit (code, nom propre, "
        "nombre) est recopié tel quel.",
        "3. Les marqueurs ⟦0⟧, ⟦1⟧… remplacent des éléments protégés (code, liens, variables, balises) : recopie "
        "chacun d'eux exactement une fois, sans le modifier, à l'endroit grammaticalement approprié.",
        "4. Les balises ⟪0⟫…⟪/0⟫, ⟪1⟫…⟪/1⟫… délimitent des portions de mise en forme : conserve chaque paire et "
        "place à l'intérieur la traduction de la portion correspondante (tu peux en changer l'ordre si la langue "
        "l'exige).",
        "5. Le bloc <contexte> éventuel ne sert qu'à comprendre la suite : ne le traduis pas.",
    ]
    if glossary:
        lines.append("Glossaire imposé (terme source → traduction obligatoire, à respecter strictement) :")
        lines += [f"- {g['source']} → {g['target']}" for g in glossary]
    if instructions.strip():
        lines.append("Consignes supplémentaires de l'utilisateur : " + instructions.strip())
    return "\n".join(l for l in lines if l)


def _xml_attr(s: str) -> str:
    return s.replace("&", "&amp;").replace('"', "&quot;").replace("<", "&lt;")


def build_user(batch: list[tuple[int, str]], fmt: str, segments: list[Segment], context: tuple[list[str], list[str]]) -> str:
    out = []
    before, after = context
    if before or after:
        out.append("<contexte>")
        if before:
            out.append("Avant : " + " / ".join(" ".join(t.split()) for t in before))
        if after:
            out.append("Après : " + " / ".join(" ".join(t.split()) for t in after))
        out.append("</contexte>")
    for idx, text in batch:
        if fmt == "json" and segments[idx].note:
            out.append(f'<seg id="{idx}" cle="{_xml_attr(segments[idx].note)}">{text}</seg>')
        else:
            out.append(f'<seg id="{idx}">{text}</seg>')
    if fmt == "json":
        out.append("(Dans ta réponse, n'écris pas l'attribut cle : uniquement <seg id=\"N\">…</seg>.)")
    return "\n".join(out)


def parse_response(text: str) -> dict[int, str]:
    res = {}
    for m in SEG_RE.finditer(text):
        body = m.group(2)
        if body.startswith("\n"):
            body = body[1:]
        if body.endswith("\n"):
            body = body[:-1]
        res[int(m.group(1))] = body
    return res


# ── Client & API ────────────────────────────────────────────────────────────

def _api_key() -> str | None:
    key = (module.env(KEY_NAME) or "").strip()
    return key or None


def _sdk_available() -> bool:
    return _client_factory is not None or importlib.util.find_spec("anthropic") is not None


def _make_client(api_key: str):
    if _client_factory is not None:
        return _client_factory(api_key)
    try:
        import anthropic
    except ImportError:
        raise TranslatorError("DEPENDENCY_MISSING", "La bibliothèque Python « anthropic » n'est pas installée. Désactivez "
                                                    "puis réactivez le module, ou installez-la depuis « Bibliothèques ».")
    return anthropic.AsyncAnthropic(api_key=api_key)


def _int_cfg(key: str, default: int, lo: int, hi: int) -> int:
    try:
        v = int(module.config.get(key, default))
    except (TypeError, ValueError):
        v = default
    return max(lo, min(v, hi))


def _settings() -> dict:
    model = module.config.get("default_model", MODEL_IDS[0])
    effort = module.config.get("effort", "low")
    return {"model": model if model in MODEL_IDS else MODEL_IDS[0],
            "effort": effort if effort in EFFORTS else "low",
            "batch_chars": _int_cfg("batch_chars", 6000, 1000, 40000),
            "concurrency": _int_cfg("concurrency", 3, 1, 8)}


def _model_info(model_id: str) -> dict:
    return next((m for m in MODELS if m["id"] == model_id), MODELS[0])


TRANSIENT = {"RATE_LIMITED", "OVERLOADED", "SERVER_ERROR", "NETWORK", "TIMEOUT"}


def _map_api_error(exc: Exception) -> TranslatorError:
    if isinstance(exc, TranslatorError):
        return exc
    try:
        import anthropic
    except ImportError:
        return TranslatorError("API_ERROR", "Erreur inattendue lors de l'appel à l'API.")
    if isinstance(exc, anthropic.AuthenticationError):
        return TranslatorError("INVALID_KEY", "Clé d'API refusée (401). Vérifiez ANTHROPIC_API_KEY dans le menu « Variables ».")
    if isinstance(exc, anthropic.PermissionDeniedError):
        return TranslatorError("PERMISSION_DENIED", "Accès refusé (403) : cette clé n'a pas accès à ce modèle.")
    if isinstance(exc, anthropic.NotFoundError):
        return TranslatorError("MODEL_NOT_FOUND", "Modèle introuvable ou non disponible pour votre organisation (404).")
    if isinstance(exc, anthropic.RateLimitError):
        retry = None
        try:
            raw = exc.response.headers.get("retry-after")
            retry = int(float(raw)) if raw else None
        except (TypeError, ValueError, AttributeError):
            retry = None
        return TranslatorError("RATE_LIMITED", "Limite de débit ou quota atteint (429).", retry_after=retry)
    if getattr(exc, "status_code", None) == 529:
        return TranslatorError("OVERLOADED", "L'API Anthropic est momentanément surchargée (529).")
    if isinstance(exc, anthropic.BadRequestError):
        return TranslatorError("BAD_REQUEST", "Requête refusée par l'API (400).")
    if isinstance(exc, anthropic.APIStatusError):
        if exc.status_code == 402:
            return TranslatorError("BILLING", "Problème de facturation sur le compte Anthropic (402).")
        if exc.status_code >= 500:
            return TranslatorError("SERVER_ERROR", f"Erreur du service Anthropic ({exc.status_code}).")
        return TranslatorError("API_ERROR", f"Erreur de l'API ({exc.status_code}).")
    if isinstance(exc, anthropic.APITimeoutError):
        return TranslatorError("TIMEOUT", "Délai dépassé : l'API n'a pas répondu à temps.")
    if isinstance(exc, anthropic.APIConnectionError):
        return TranslatorError("NETWORK", "Impossible de joindre l'API Anthropic. Vérifiez votre connexion Internet.")
    return TranslatorError("API_ERROR", "Erreur inattendue lors de l'appel à l'API.")


async def _complete(model: str, system: str, user: str, effort: str) -> dict:
    key = _api_key()
    if not key:
        raise TranslatorError("API_KEY_MISSING", "Clé d'API absente : définissez ANTHROPIC_API_KEY dans le menu « Variables ».")
    client = _make_client(key)
    params: dict[str, Any] = {"model": model, "max_tokens": BATCH_MAX_TOKENS, "system": system,
                              "messages": [{"role": "user", "content": user}], "output_config": {"effort": effort}}
    if _model_info(model)["fallbacks"]:
        params["fallbacks"] = "default"
        params["betas"] = [FALLBACK_BETA]
    try:
        async with client.beta.messages.stream(**params) as stream:
            final = await stream.get_final_message()
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        mapped = _map_api_error(exc)
        log.warning("API call failed (%s, %s)", mapped.code, type(exc).__name__)
        raise mapped
    if final.stop_reason == "refusal":
        raise TranslatorError("REFUSED", "Le modèle a refusé de traduire une partie de ce fichier (filtre de sécurité).")
    usage = final.usage
    return {"text": "".join(b.text for b in final.content if getattr(b, "type", None) == "text"),
            "stop_reason": final.stop_reason,
            "usage": {"input": int(getattr(usage, "input_tokens", 0) or 0),
                      "output": int(getattr(usage, "output_tokens", 0) or 0)}}


async def _complete_retry(model: str, system: str, user: str, effort: str, sleep=asyncio.sleep) -> dict:
    for attempt in range(1, MAX_API_ATTEMPTS + 1):
        try:
            return await _complete(model, system, user, effort)
        except TranslatorError as exc:
            if exc.code not in TRANSIENT or attempt == MAX_API_ATTEMPTS:
                raise
            wait = exc.extra.get("retry_after") or 4 * attempt
            log.info("transient API error %s, retry in %ss", exc.code, wait)
            await sleep(min(wait, 60))
    raise TranslatorError("API_ERROR", "Erreur inattendue lors de l'appel à l'API.")


# ── Storage ─────────────────────────────────────────────────────────────────

def _dir(tid: int):
    return module.data_path("translations") / str(int(tid))


def _read_state(tid: int) -> dict:
    p = _dir(tid) / "state.json"
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"tr": {}, "warn": {}, "manual": []}


def _write_state(tid: int, st: dict) -> None:
    d = _dir(tid)
    d.mkdir(parents=True, exist_ok=True)
    tmp = d / "state.json.tmp"
    tmp.write_text(json.dumps(st, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, d / "state.json")


def _read_source(tid: int) -> bytes:
    try:
        return (_dir(tid) / "source.bin").read_bytes()
    except OSError:
        raise TranslatorError("NOT_FOUND", "Fichier source introuvable : importez-le de nouveau.")


async def _get(tid) -> dict | None:
    if not isinstance(tid, int) or isinstance(tid, bool):
        return None
    db = await _db()
    return await db.fetch_one("SELECT * FROM translations WHERE id = ?", (tid,))


def _public(row: dict) -> dict:
    out = dict(row)
    out["glossary"] = json.loads(row["glossary"] or "[]")
    out["usage"] = json.loads(row["usage"] or "{}")
    out["format_label"] = FORMAT_LABELS.get(row["format"], row["format"])
    out["running"] = row["id"] in _running
    out["job_id"] = _running.get(row["id"])
    return out


async def _parsed(row: dict) -> Parsed:
    data = await asyncio.to_thread(_read_source, row["id"])
    return await asyncio.to_thread(parse_file, row["name"], data)


def _trs_list(st: dict, n: int) -> list[list[str] | None]:
    tr = st.get("tr", {})
    return [tr.get(str(i)) for i in range(n)]


def _output_name(name: str, target: str, fmt: str) -> str:
    stem, ext = (name.rsplit(".", 1) + [""])[:2] if "." in name else (name, "")
    code = LANG_CODES.get(target.lower()) or re.sub(r"[^\w-]+", "-", target.lower()).strip("-") or "trad"
    if fmt == "json" and re.fullmatch(r"[a-z]{2}(?:[-_][A-Za-z]{2})?", stem):
        stem = code
    else:
        stem = re.sub(r"\.[a-z]{2}(?:[-_][A-Za-z]{2})?$", "", stem) + "." + code
    return f"{stem}.{ext}" if ext else stem


# ── Payloads ────────────────────────────────────────────────────────────────

class GlossaryEntry(BaseModel):
    source: str = Field(..., min_length=1, max_length=200)
    target: str = Field(..., min_length=1, max_length=200)

    @field_validator("source", "target")
    @classmethod
    def _strip(cls, v: str) -> str:
        v = " ".join(v.split())
        if not v:
            raise ValueError("Terme vide")
        return v


class OptionsPayload(BaseModel):
    id: int
    target_lang: str = Field(..., max_length=40)
    source_lang: str = Field("auto", max_length=40)
    tone: str = "neutre"
    glossary: list[GlossaryEntry] = Field(default_factory=list, max_length=300)
    instructions: str = Field("", max_length=2000)
    model: str | None = None
    restart: bool = False

    @field_validator("target_lang", "source_lang")
    @classmethod
    def _lang(cls, v: str) -> str:
        v = " ".join(v.split())
        if not v or not LANG_RE.match(v):
            raise ValueError("Langue invalide")
        return v

    @field_validator("tone")
    @classmethod
    def _tone(cls, v: str) -> str:
        if v not in TONES:
            raise ValueError("Ton inconnu")
        return v

    @field_validator("model")
    @classmethod
    def _model(cls, v):
        if v is not None and v not in MODEL_IDS:
            raise ValueError("Modèle inconnu")
        return v


# ── Handlers ────────────────────────────────────────────────────────────────

@module.on("status")
async def status(payload: dict):
    return {"has_key": _api_key() is not None, "sdk_available": _sdk_available(), "key_name": KEY_NAME,
            "models": MODELS, "settings": _settings(), "tones": [{"id": k, "label": k.capitalize()} for k in TONES],
            "formats": FORMAT_LABELS, "max_file_mb": MAX_FILE_BYTES // (1024 * 1024)}


@module.on("glossary:get")
async def glossary_get(payload: dict):
    db = await _db()
    row = await db.fetch_one("SELECT value FROM settings WHERE key = 'glossary'")
    return {"glossary": json.loads(row["value"]) if row else []}


@module.on("glossary:set")
async def glossary_set(payload: dict):
    raw = (payload or {}).get("glossary")
    if not isinstance(raw, list) or len(raw) > 300:
        return _err("Glossaire invalide (300 entrées maximum)", "INVALID_PAYLOAD")
    try:
        entries = [GlossaryEntry(**e).model_dump() for e in raw if isinstance(e, dict)]
    except ValidationError as exc:
        return _err(_validation_message(exc), "INVALID_PAYLOAD")
    db = await _db()
    await db.execute("INSERT INTO settings (key, value) VALUES ('glossary', ?) ON CONFLICT(key) DO UPDATE SET "
                     "value = excluded.value", (json.dumps(entries, ensure_ascii=False),))
    return {"glossary": entries}


@module.on("file:load")
async def file_load(payload: dict):
    p = payload or {}
    name = re.sub(r"[\\/:*?\"<>|\x00-\x1f]", "_", str(p.get("name") or "").strip())[:200]
    data_b64 = p.get("data")
    if not name or not isinstance(data_b64, str):
        return _err("Fichier manquant", "INVALID_PAYLOAD")
    if len(data_b64) > (MAX_FILE_BYTES * 4) // 3 + 8:
        return _err(f"Fichier trop volumineux (maximum {MAX_FILE_BYTES // (1024 * 1024)} Mo).", "TOO_LARGE")
    try:
        data = base64.b64decode(data_b64, validate=True)
    except (binascii.Error, ValueError):
        return _err("Contenu de fichier invalide", "INVALID_PAYLOAD")
    if not data:
        return _err("Le fichier est vide", "INVALID_PAYLOAD")
    try:
        parsed = await asyncio.to_thread(parse_file, name, data)
    except TranslatorError as exc:
        return _err(exc.message, exc.code)
    n = len(parsed.segments)
    if n == 0:
        return _err("Aucun texte à traduire n'a été trouvé dans ce fichier.", "NO_TEXT")
    if n > MAX_SEGMENTS:
        return _err(f"Trop de segments ({n} ; maximum {MAX_SEGMENTS}).", "TOO_LARGE")
    chars = sum(len(u) for s in parsed.segments for u in s.units)
    if chars > MAX_CHARS:
        return _err("Fichier trop long pour être traduit en une fois (2 millions de caractères maximum).", "TOO_LARGE")
    init = initial_state(parsed)
    todo = n - len(init["tr"])
    db = await _db()
    now = _now()
    row = await db.fetch_one(
        "INSERT INTO translations (name, format, size, n_segments, n_todo, chars, done_segments, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) RETURNING *", (name, parsed.fmt, len(data), n, todo, chars, n - todo, now, now))
    d = _dir(row["id"])

    def save():
        d.mkdir(parents=True, exist_ok=True)
        (d / "source.bin").write_bytes(data)
        _write_state(row["id"], init)
    await asyncio.to_thread(save)
    log.info("file loaded (%s, %d segments)", parsed.fmt, n)
    return {"translation": _public(row)}


@module.on("translation:list")
async def translation_list(payload: dict):
    db = await _db()
    rows = await db.fetch_all("SELECT * FROM translations ORDER BY updated_at DESC")
    return {"items": [_public(r) for r in rows]}


@module.on("translation:get")
async def translation_get(payload: dict):
    p = payload or {}
    row = await _get(p.get("id"))
    if row is None:
        return _err("Traduction introuvable", "NOT_FOUND")
    try:
        offset = max(0, int(p.get("offset", 0)))
        limit = max(1, min(int(p.get("limit", 200)), 1000))
    except (TypeError, ValueError):
        return _err("Pagination invalide", "INVALID_PAYLOAD")
    only = p.get("filter") if p.get("filter") in ("warn", "todo") else None
    try:
        parsed = await _parsed(row)
    except TranslatorError as exc:
        return _err(exc.message, exc.code)
    st = await asyncio.to_thread(_read_state, row["id"])
    trs = _trs_list(st, len(parsed.segments))
    indices = range(len(parsed.segments))
    if only == "warn":
        indices = [i for i in indices if str(i) in st.get("warn", {})]
    elif only == "todo":
        indices = [i for i in indices if trs[i] is None]
    indices = list(indices)
    page = []
    for i in indices[offset:offset + limit]:
        seg = parsed.segments[i]
        page.append({"index": i, "note": seg.note, "source": "".join(seg.units),
                     "target": "".join(trs[i]) if trs[i] is not None else None,
                     "warning": st.get("warn", {}).get(str(i)), "manual": i in st.get("manual", [])})
    return {"translation": _public(row), "total": len(indices), "offset": offset, "segments": page}


@module.on("translation:delete")
async def translation_delete(payload: dict):
    row = await _get((payload or {}).get("id"))
    if row is None:
        return _err("Traduction introuvable", "NOT_FOUND")
    if row["id"] in _running:
        return _err("Annulez d'abord la traduction en cours", "BUSY")
    db = await _db()
    await db.execute("DELETE FROM translations WHERE id = ?", (row["id"],))
    await asyncio.to_thread(shutil.rmtree, _dir(row["id"]), True)
    return {"ok": True}


@module.on("translation:download")
async def translation_download(payload: dict):
    row = await _get((payload or {}).get("id"))
    if row is None:
        return _err("Traduction introuvable", "NOT_FOUND")
    try:
        parsed = await _parsed(row)
    except TranslatorError as exc:
        return _err(exc.message, exc.code)
    st = await asyncio.to_thread(_read_state, row["id"])
    trs = _trs_list(st, len(parsed.segments))
    data = await asyncio.to_thread(parsed.build, trs)
    missing = sum(1 for t in trs if t is None)
    return {"filename": _output_name(row["name"], row["target_lang"] or "trad", row["format"]),
            "mime": MIMES.get(row["format"], "application/octet-stream"),
            "data": base64.b64encode(data).decode("ascii"), "untranslated": missing}


@module.on("segment:update")
async def segment_update(payload: dict):
    p = payload or {}
    row = await _get(p.get("id"))
    if row is None:
        return _err("Traduction introuvable", "NOT_FOUND")
    if row["id"] in _running:
        return _err("Attendez la fin de la traduction en cours", "BUSY")
    idx, text = p.get("index"), p.get("text")
    if not isinstance(idx, int) or not isinstance(text, str) or len(text) > 100_000:
        return _err("Segment invalide", "INVALID_PAYLOAD")
    try:
        parsed = await _parsed(row)
    except TranslatorError as exc:
        return _err(exc.message, exc.code)
    if not 0 <= idx < len(parsed.segments):
        return _err("Segment introuvable", "NOT_FOUND")
    seg = parsed.segments[idx]
    st = await asyncio.to_thread(_read_state, row["id"])
    if p.get("reset"):
        st["tr"].pop(str(idx), None)
        st["manual"] = [i for i in st.get("manual", []) if i != idx]
    else:
        st["tr"][str(idx)] = [text] + [""] * (len(seg.units) - 1)
        st.setdefault("manual", [])
        if idx not in st["manual"]:
            st["manual"].append(idx)
    st.get("warn", {}).pop(str(idx), None)
    await asyncio.to_thread(_write_state, row["id"], st)
    done = len(st["tr"])
    db = await _db()
    await db.execute("UPDATE translations SET done_segments = ?, warnings = ?, updated_at = ? WHERE id = ?",
                     (done, len(st.get("warn", {})), _now(), row["id"]))
    return {"ok": True, "done_segments": done}


async def _apply_options(p: OptionsPayload) -> dict:
    row = await _get(p.id)
    if row is None:
        raise TranslatorError("NOT_FOUND", "Traduction introuvable")
    if row["id"] in _running:
        raise TranslatorError("BUSY", "Cette traduction est déjà en cours")
    model = p.model or _settings()["model"]
    db = await _db()
    changed_target = row["target_lang"] and row["target_lang"].lower() != p.target_lang.lower()
    reset = bool(p.restart or changed_target)
    if reset:
        await asyncio.to_thread(_write_state, row["id"], initial_state(await _parsed(row)))
    saved = await db.fetch_one(
        "UPDATE translations SET target_lang = ?, source_lang = ?, tone = ?, glossary = ?, instructions = ?, model = ?, "
        "updated_at = ?, done_segments = CASE WHEN ? THEN n_segments - n_todo ELSE done_segments END, "
        "warnings = CASE WHEN ? THEN 0 ELSE warnings END WHERE id = ? RETURNING *",
        (p.target_lang, p.source_lang, p.tone, json.dumps([g.model_dump() for g in p.glossary], ensure_ascii=False),
         p.instructions.strip(), model, _now(), reset, reset, row["id"]))
    if not _sdk_available():
        raise TranslatorError("DEPENDENCY_MISSING", "La bibliothèque Python « anthropic » n'est pas installée.")
    if not _api_key():
        raise TranslatorError("API_KEY_MISSING", "Clé d'API absente : définissez ANTHROPIC_API_KEY dans le menu « Variables ».")
    return saved


@module.on("translation:prepare")
async def translation_prepare(payload: dict):
    try:
        p = OptionsPayload(**(payload or {}))
    except ValidationError as exc:
        return _err(_validation_message(exc), "INVALID_PAYLOAD")
    try:
        row = await _apply_options(p)
    except TranslatorError as exc:
        return _err(exc.message, exc.code)
    return {"translation": _public(row)}


async def _set_status(tid: int, status: str, **fields) -> None:
    db = await _db()
    cols = ", ".join(f"{k} = ?" for k in fields)
    await db.execute(f"UPDATE translations SET status = ?, updated_at = ?{', ' + cols if cols else ''} WHERE id = ?",
                     (status, _now(), *fields.values(), tid))


async def run_translation(tid: int, progress: Callable, sleep=asyncio.sleep) -> dict:
    row = await _get(tid)
    if row is None:
        raise TranslatorError("NOT_FOUND", "Traduction introuvable")
    if not row["target_lang"]:
        raise TranslatorError("INVALID_STATE", "Choisissez d'abord la langue cible")
    parsed = await _parsed(row)
    fmt = parsed.fmt
    segs = parsed.segments
    st = await asyncio.to_thread(_read_state, tid)
    st.setdefault("warn", {})
    settings = _settings()
    glossary = json.loads(row["glossary"] or "[]")
    system = build_system(fmt, row["source_lang"], row["target_lang"], row["tone"], glossary, row["instructions"])
    model = row["model"] if row["model"] in MODEL_IDS else settings["model"]
    encoded: dict[int, tuple[str, list[str]]] = {}
    todo: list[tuple[int, str]] = []
    for i, s in enumerate(segs):
        if str(i) in st["tr"]:
            continue
        text, ph = encode_segment(s, fmt)
        if not needs_translation(text):
            st["tr"][str(i)] = list(s.units)
            continue
        encoded[i] = (text, ph)
        todo.append((i, text))
    total = len(segs)
    usage = json.loads(row["usage"] or "{}")
    save_lock = asyncio.Lock()
    counters = {"done": len(st["tr"])}

    async def save():
        async with save_lock:
            await asyncio.to_thread(_write_state, tid, st)
            db = await _db()
            await db.execute("UPDATE translations SET done_segments = ?, warnings = ?, usage = ?, updated_at = ? "
                             "WHERE id = ?", (len(st["tr"]), len(st["warn"]), json.dumps(usage), _now(), tid))

    await save()
    batches = make_batches(todo, settings["batch_chars"], 40 if fmt in ("srt", "vtt") else MAX_BATCH_SEGMENTS)
    await progress(round(100 * counters["done"] / total, 1), f"{len(batches)} lot(s) à traduire",
                   done=counters["done"], total=total, changed=[])

    def context_for(batch):
        first, last = batch[0][0], batch[-1][0]
        before = ["".join(segs[k].units) for k in range(max(0, first - CONTEXT_BEFORE), first)]
        after = ["".join(segs[k].units) for k in range(last + 1, min(total, last + 1 + CONTEXT_AFTER))]
        if fmt in ("json", "docx"):
            after = []
        return before, after

    async def translate_batch(batch: list[tuple[int, str]], retry: bool) -> list[int]:
        res = await _complete_retry(model, system, build_user(batch, fmt, segs, context_for(batch)), settings["effort"],
                                    sleep=sleep)
        for k in ("input", "output"):
            usage[k] = usage.get(k, 0) + res["usage"][k]
        got = parse_response(res["text"])
        missing, changed = [], []
        for idx, _src in batch:
            if idx not in got or (not got[idx].strip() and encoded[idx][0].strip()):
                missing.append(idx)
                continue
            units, warns = decode_segment(segs[idx], fmt, got[idx], encoded[idx][1])
            st["tr"][str(idx)] = units
            if warns:
                st["warn"][str(idx)] = ", ".join(warns)
            else:
                st["warn"].pop(str(idx), None)
            changed.append(idx)
        if missing and retry:
            for chunk in make_batches([(i, encoded[i][0]) for i in missing], settings["batch_chars"], 10):
                still = await translate_batch(chunk, False)
                for i in still:
                    st["warn"][str(i)] = "non traduit (réponse incomplète du modèle)"
            missing = []
        counters["done"] = len(st["tr"])
        await save()
        await progress(round(100 * counters["done"] / total, 1), f"{counters['done']} / {total} segments",
                       done=counters["done"], total=total, changed=changed)
        return missing

    sem = asyncio.Semaphore(settings["concurrency"])

    async def worker(batch):
        async with sem:
            await translate_batch(batch, True)

    tasks = [asyncio.create_task(worker(b)) for b in batches]
    try:
        await asyncio.gather(*tasks)
    except BaseException:
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await save()
        raise
    untranslated = total - len(st["tr"])
    return {"done": len(st["tr"]), "total": total, "untranslated": untranslated, "warnings": len(st["warn"]),
            "usage": usage}


@module.job("translate")
async def translate_job(payload: dict, job):
    try:
        p = OptionsPayload(**(payload or {}))
    except ValidationError as exc:
        raise RuntimeError(_validation_message(exc))
    try:
        row = await _apply_options(p)
    except TranslatorError as exc:
        raise RuntimeError(exc.message)
    tid = row["id"]
    _running[tid] = job.id
    await _set_status(tid, "running", error="", error_code="")

    async def progress(pct, msg, **extra):
        await job.progress(pct, msg, translation_id=tid, **extra)

    try:
        result = await run_translation(tid, progress)
    except asyncio.CancelledError:
        await _set_status(tid, "cancelled")
        raise
    except TranslatorError as exc:
        await _set_status(tid, "error", error=exc.message, error_code=exc.code)
        raise RuntimeError(exc.message)
    except Exception:
        log.exception("translation failed")
        await _set_status(tid, "error", error="Erreur interne inattendue", error_code="INTERNAL")
        raise RuntimeError("Erreur interne inattendue")
    finally:
        _running.pop(tid, None)
    await _set_status(tid, "done" if result["untranslated"] == 0 else "partial")
    log.info("translation %d finished (%d/%d)", tid, result["done"], result["total"])
    return {"translation_id": tid, **result}


@module.on_shutdown
async def cleanup():
    _running.clear()
