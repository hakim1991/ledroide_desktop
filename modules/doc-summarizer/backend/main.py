import asyncio
import base64
import binascii
import importlib.util
import io
import json
import logging
import math
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from collections import Counter
from datetime import datetime, timezone
from html import unescape as html_unescape
from html.parser import HTMLParser
from typing import Any, Callable, Literal
from xml.etree import ElementTree as ET

from pydantic import BaseModel, Field, ValidationError, field_validator

from ledroide_sdk import Module

log = logging.getLogger("ledroide.doc-summarizer")

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

MAX_FILE_BYTES = 10 * 1024 * 1024
MAX_URL_BYTES = 15 * 1024 * 1024
MAX_DOCX_XML_BYTES = 80 * 1024 * 1024
MAX_TEXT_CHARS = 3_000_000
MIN_TEXT_CHARS = 20
CHARS_PER_TOKEN = 3.5
CHUNK_TOKENS = 35_000
CHUNK_OVERLAP_CHARS = 800
MAP_CONCURRENCY = 3
MAP_MAX_TOKENS = 8000
MAX_REDUCE_LEVELS = 4
RETRIEVE_CHARS = 280_000
CHAT_HISTORY_TURNS = 12
URL_TIMEOUT_S = 20
FLUSH_INTERVAL_S = 0.05
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) LedroideDesktop/1.0"

TEXT_EXTS = {".txt", ".md", ".markdown", ".rst", ".csv", ".log", ".json", ".xml", ".yaml", ".yml", ".tex"}
HTML_EXTS = {".html", ".htm", ".xhtml"}
REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
LANG_RE = re.compile(r"^[\w\s'()-]{1,40}$", re.UNICODE)

_client_factory: Callable[[str], Any] | None = None
_active: dict[str, dict] = {}
_ready = False
_lock = asyncio.Lock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    title        TEXT    NOT NULL,
    source_type  TEXT    NOT NULL,
    source_ref   TEXT    NOT NULL DEFAULT '',
    pages        INTEGER NOT NULL DEFAULT 0,
    chars        INTEGER NOT NULL,
    words        INTEGER NOT NULL,
    est_tokens   INTEGER NOT NULL,
    text         TEXT    NOT NULL,
    digest       TEXT,
    digest_parts INTEGER NOT NULL DEFAULT 0,
    digest_limit INTEGER NOT NULL DEFAULT 0,
    created_at   TEXT    NOT NULL,
    updated_at   TEXT    NOT NULL
);
CREATE TABLE IF NOT EXISTS outputs (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    doc_id     INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    kind       TEXT    NOT NULL,
    variant    TEXT    NOT NULL DEFAULT '',
    lang       TEXT    NOT NULL,
    content    TEXT    NOT NULL,
    model      TEXT    NOT NULL,
    usage      TEXT    NOT NULL DEFAULT '{}',
    truncated  INTEGER NOT NULL DEFAULT 0,
    strategy   TEXT    NOT NULL DEFAULT 'direct',
    created_at TEXT    NOT NULL,
    UNIQUE(doc_id, kind, variant)
);
CREATE TABLE IF NOT EXISTS messages (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    doc_id     INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    role       TEXT    NOT NULL,
    content    TEXT    NOT NULL,
    model      TEXT    NOT NULL DEFAULT '',
    stopped    INTEGER NOT NULL DEFAULT 0,
    created_at TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_outputs_doc ON outputs(doc_id);
CREATE INDEX IF NOT EXISTS idx_messages_doc ON messages(doc_id);
"""


class SummarizerError(Exception):
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
                _ready = True
    await module.db.execute("PRAGMA foreign_keys = ON")
    return module.db


# ── Extraction ──────────────────────────────────────────────────────────────

def normalize_text(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\x00", "")
    text = re.sub(r"[ \t ]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def decode_bytes(data: bytes, charset: str | None = None) -> str:
    if data.startswith(b"\xef\xbb\xbf"):
        return data[3:].decode("utf-8", errors="replace")
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16", errors="replace")
    for enc in filter(None, [charset, "utf-8"]):
        try:
            return data.decode(enc)
        except (LookupError, UnicodeDecodeError):
            continue
    return data.decode("cp1252", errors="replace")


def extract_pdf(data: bytes) -> tuple[str, int]:
    try:
        from pypdf import PdfReader
        from pypdf.errors import PdfReadError
    except ImportError:
        raise SummarizerError("DEPENDENCY_MISSING", "La bibliothèque « pypdf » n'est pas installée : désactivez puis "
                                                    "réactivez le module pour installer ses dépendances.")
    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            try:
                ok = reader.decrypt("")
            except Exception:
                ok = 0
            if not ok:
                raise SummarizerError("PDF_ENCRYPTED", "Ce PDF est protégé par un mot de passe.")
        parts = []
        for i, page in enumerate(reader.pages, 1):
            try:
                t = page.extract_text() or ""
            except Exception:
                t = ""
            t = normalize_text(t)
            if t:
                parts.append(f"--- Page {i} ---\n{t}")
        pages = len(reader.pages)
    except SummarizerError:
        raise
    except (PdfReadError, ValueError, KeyError, TypeError, OSError) as exc:
        raise SummarizerError("BAD_FILE", f"PDF illisible ou endommagé ({type(exc).__name__}).")
    text = "\n\n".join(parts)
    if len(text.strip()) < MIN_TEXT_CHARS:
        raise SummarizerError("NO_TEXT", "Ce PDF ne contient pas de texte extractible (document scanné ?). "
                                         "Passez-le d'abord dans le module OCR.")
    return text, pages


W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


def _w(tag: str) -> str:
    return f"{{{W_NS}}}{tag}"


def _docx_paragraph(p) -> str:
    out = []
    for el in p.iter():
        if el.tag == _w("t") and el.text:
            out.append(el.text)
        elif el.tag == _w("tab"):
            out.append("\t")
        elif el.tag in (_w("br"), _w("cr")):
            out.append("\n")
    return "".join(out)


def _docx_prefix(p) -> str:
    ppr = p.find(_w("pPr"))
    if ppr is None:
        return ""
    style = ppr.find(_w("pStyle"))
    sval = (style.get(_w("val")) if style is not None else "") or ""
    m = re.match(r"^(?:heading|titre|berschrift|título|titolo)\s*(\d)$", sval, re.I)
    if m:
        return "#" * min(int(m.group(1)), 6) + " "
    if sval.lower() in ("title", "titre"):
        return "# "
    if ppr.find(_w("numPr")) is not None or sval.lower().startswith(("listparagraph", "paragraphedeliste")):
        return "- "
    return ""


def extract_docx(data: bytes) -> str:
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise SummarizerError("BAD_FILE", "Fichier Word invalide (ce n'est pas une archive .docx).")
    with zf:
        try:
            info = zf.getinfo("word/document.xml")
        except KeyError:
            raise SummarizerError("BAD_FILE", "Fichier Word invalide (word/document.xml absent).")
        if info.file_size > MAX_DOCX_XML_BYTES:
            raise SummarizerError("TOO_LARGE", "Document Word trop volumineux.")
        try:
            root = ET.fromstring(zf.read(info))
        except ET.ParseError:
            raise SummarizerError("BAD_FILE", "Contenu XML du document Word illisible.")
    body = root.find(_w("body"))
    if body is None:
        return ""
    lines: list[str] = []

    def walk(container):
        for child in container:
            if child.tag == _w("p"):
                t = _docx_paragraph(child).strip()
                if t:
                    lines.append(_docx_prefix(child) + t)
            elif child.tag == _w("tbl"):
                for row in child.iter(_w("tr")):
                    cells = []
                    for cell in row.findall(_w("tc")):
                        cells.append(" ".join(_docx_paragraph(p).strip() for p in cell.iter(_w("p"))).strip())
                    if any(cells):
                        lines.append("| " + " | ".join(cells) + " |")
            elif child.tag == _w("sdt"):
                content = child.find(_w("sdtContent"))
                if content is not None:
                    walk(content)

    walk(body)
    return "\n\n".join(lines)


SKIP_TAGS = {"script", "style", "noscript", "svg", "nav", "header", "footer", "aside", "form", "button",
             "iframe", "template", "select", "option", "canvas", "dialog", "menu"}
VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"}
BLOCK_TAGS = {"p", "div", "section", "article", "main", "li", "ul", "ol", "h1", "h2", "h3", "h4", "h5", "h6",
              "blockquote", "pre", "table", "tr", "td", "th", "dl", "dt", "dd", "figure", "figcaption", "br", "hr",
              "address", "details", "summary", "caption", "tbody", "thead"}
NOISE_RE = re.compile(r"(^|[\s_-])(nav|navbar|menu|footer|sidebar|comment|comments|cookie|consent|share|sharing|social|"
                      r"related|advert|ads|promo|breadcrumb|banner|popup|modal|newsletter|subscribe|skip)([\s_-]|$)", re.I)
CONTENT_RE = re.compile(r"(^|[\s_-])(content|article|post|entry|story|main|body-text|text)([\s_-]|$)", re.I)


class _Node:
    __slots__ = ("tag", "attrs", "children", "parent")

    def __init__(self, tag: str, attrs: dict, parent=None):
        self.tag = tag
        self.attrs = attrs
        self.children: list = []
        self.parent = parent


class _DomBuilder(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = _Node("#root", {})
        self.cur = self.root
        self.title = ""
        self.meta_title = ""
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        a = {k: (v or "") for k, v in attrs}
        if tag == "meta" and a.get("property", a.get("name", "")).lower() in ("og:title", "twitter:title"):
            self.meta_title = self.meta_title or a.get("content", "").strip()
        if tag == "title":
            self._in_title = True
        node = _Node(tag, a, self.cur)
        self.cur.children.append(node)
        if tag not in VOID_TAGS:
            self.cur = node

    def handle_startendtag(self, tag, attrs):
        self.cur.children.append(_Node(tag, {k: (v or "") for k, v in attrs}, self.cur))

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False
        n = self.cur
        while n is not None and n.tag != tag:
            n = n.parent
        if n is not None and n.parent is not None:
            self.cur = n.parent

    def handle_data(self, data):
        if self._in_title:
            self.title += data
            return
        self.cur.children.append(data)


def _node_text(node) -> str:
    parts = []
    stack = [node]
    while stack:
        n = stack.pop()
        if isinstance(n, str):
            parts.append(n)
        elif n.tag not in SKIP_TAGS:
            stack.extend(reversed(n.children))
    return " ".join("".join(parts).split())


def _link_text_len(node) -> int:
    total = 0
    stack = [node]
    while stack:
        n = stack.pop()
        if isinstance(n, str):
            continue
        if n.tag == "a":
            total += len(_node_text(n))
        elif n.tag not in SKIP_TAGS:
            stack.extend(n.children)
    return total


def _is_noise(node) -> bool:
    if node.tag in SKIP_TAGS:
        return True
    marker = node.attrs.get("id", "") + " " + node.attrs.get("class", "") + " " + node.attrs.get("role", "")
    if node.attrs.get("aria-hidden") == "true" or "hidden" in node.attrs:
        return True
    if node.attrs.get("role") in ("navigation", "banner", "contentinfo", "complementary", "search"):
        return True
    return bool(NOISE_RE.search(marker)) and node.tag not in ("body", "main", "article")


def _find_all(node, pred) -> list:
    out = []
    stack = [node]
    while stack:
        n = stack.pop()
        if isinstance(n, str):
            continue
        if pred(n):
            out.append(n)
        stack.extend(n.children)
    return out


def _pick_main(root):
    for tag in ("article", "main"):
        cands = _find_all(root, lambda n, t=tag: n.tag == t)
        if cands:
            best = max(cands, key=lambda n: len(_node_text(n)))
            if len(_node_text(best)) >= 400:
                return best
    cands = _find_all(root, lambda n: n.tag in ("div", "section") and
                      CONTENT_RE.search(n.attrs.get("id", "") + " " + n.attrs.get("class", "")) and not _is_noise(n))
    if cands:
        best = max(cands, key=lambda n: len(_node_text(n)) - 2 * _link_text_len(n))
        if len(_node_text(best)) >= 400:
            return best
    bodies = _find_all(root, lambda n: n.tag == "body")
    return bodies[0] if bodies else root


def _render(node, out: list[str]):
    for child in node.children:
        if isinstance(child, str):
            out.append(child)
            continue
        tag = child.tag
        if _is_noise(child):
            continue
        if tag in ("div", "section", "ul", "ol", "table"):
            txt_len = len(_node_text(child))
            if 0 < txt_len < 1500 and _link_text_len(child) / txt_len > 0.6:
                continue
        if tag == "br":
            out.append("\n")
            continue
        if tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            t = _node_text(child)
            if t:
                out.append("\n\n" + "#" * int(tag[1]) + " " + t + "\n\n")
            continue
        if tag == "li":
            out.append("\n- ")
            _render(child, out)
            out.append("\n")
            continue
        if tag == "pre":
            out.append("\n\n" + "".join(_raw_text(child)) + "\n\n")
            continue
        if tag in ("td", "th"):
            out.append(" | ")
            _render(child, out)
            continue
        block = tag in BLOCK_TAGS
        if block:
            out.append("\n\n")
        _render(child, out)
        if block:
            out.append("\n\n")


def _raw_text(node) -> list[str]:
    out = []
    for c in node.children:
        if isinstance(c, str):
            out.append(c)
        else:
            out.extend(_raw_text(c))
    return out


def extract_html(html: str) -> tuple[str, str]:
    builder = _DomBuilder()
    try:
        builder.feed(html)
        builder.close()
    except Exception:
        log.debug("HTML parser stopped early")
    main = _pick_main(builder.root)
    out: list[str] = []
    _render(main, out)
    raw = "".join(out)
    lines = []
    for line in raw.split("\n"):
        line = re.sub(r"[ \t ]+", " ", line).strip()
        line = re.sub(r"^\|\s*", "| ", line) if line.startswith("|") else line
        lines.append(line)
    text = normalize_text("\n".join(lines))
    title = " ".join((builder.meta_title or builder.title or "").split())
    return text, html_unescape(title)[:200]


def _charset_from_html(data: bytes) -> str | None:
    m = re.search(rb"<meta[^>]+charset=[\"']?([\w-]+)", data[:4096], re.I)
    return m.group(1).decode("ascii", "ignore") if m else None


def extract_file(name: str, data: bytes) -> dict:
    ext = ("." + name.rsplit(".", 1)[-1].lower()) if "." in name else ""
    stem = name.rsplit(".", 1)[0] if "." in name else name
    pages = 0
    if ext == ".pdf" or data[:5] == b"%PDF-":
        text, pages = extract_pdf(data)
        kind = "pdf"
    elif ext == ".docx":
        text = extract_docx(data)
        kind = "docx"
    elif ext in HTML_EXTS:
        text, title = extract_html(decode_bytes(data, _charset_from_html(data)))
        stem = title or stem
        kind = "html"
    elif ext in TEXT_EXTS or ext == "":
        if b"\x00" in data[:4096] and not data.startswith((b"\xff\xfe", b"\xfe\xff")):
            raise SummarizerError("UNSUPPORTED", "Ce fichier semble binaire : formats acceptés PDF, DOCX, TXT, MD, HTML.")
        text = decode_bytes(data)
        kind = "markdown" if ext in (".md", ".markdown") else "text"
    elif ext == ".doc":
        raise SummarizerError("UNSUPPORTED", "L'ancien format Word .doc n'est pas pris en charge : enregistrez-le en .docx.")
    else:
        raise SummarizerError("UNSUPPORTED", f"Format « {ext} » non pris en charge : PDF, DOCX, TXT, MD ou HTML.")
    return {"text": normalize_text(text), "title": stem.strip() or "Document", "source_type": kind, "pages": pages}


def _validate_url(url: str) -> str:
    url = (url or "").strip()
    if not re.match(r"^https?://", url, re.I):
        if re.match(r"^[a-z][a-z0-9+.-]*:(?!\d)", url, re.I) or "://" in url:
            raise SummarizerError("BAD_URL", "Adresse invalide : saisissez une URL http(s)://…")
        url = "https://" + url
    parts = urllib.parse.urlsplit(url)
    if parts.scheme.lower() not in ("http", "https") or not parts.hostname:
        raise SummarizerError("BAD_URL", "Adresse invalide : saisissez une URL http(s)://…")
    return url


def fetch_url(url: str) -> dict:
    url = _validate_url(url)
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT,
                                               "Accept": "text/html,application/xhtml+xml,text/plain,application/pdf;q=0.9,*/*;q=0.5",
                                               "Accept-Language": "fr,en;q=0.8"})
    try:
        with urllib.request.urlopen(req, timeout=URL_TIMEOUT_S) as resp:
            final = resp.geturl()
            if urllib.parse.urlsplit(final).scheme.lower() not in ("http", "https"):
                raise SummarizerError("BAD_URL", "Redirection vers une adresse non autorisée.")
            ctype = (resp.headers.get("Content-Type") or "").lower()
            charset = resp.headers.get_content_charset()
            data = resp.read(MAX_URL_BYTES + 1)
    except SummarizerError:
        raise
    except urllib.error.HTTPError as exc:
        raise SummarizerError("FETCH_FAILED", f"La page a répondu par une erreur HTTP {exc.code}.")
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        raise SummarizerError("FETCH_FAILED", f"Impossible de charger la page ({type(reason).__name__}).")
    if len(data) > MAX_URL_BYTES:
        raise SummarizerError("TOO_LARGE", "La page est trop volumineuse (plus de 15 Mo).")
    host = urllib.parse.urlsplit(final).hostname or "Page web"
    if "pdf" in ctype or data[:5] == b"%PDF-":
        text, pages = extract_pdf(data)
        name = urllib.parse.unquote(urllib.parse.urlsplit(final).path.rsplit("/", 1)[-1]) or host
        return {"text": normalize_text(text), "title": name.rsplit(".", 1)[0], "source_type": "url",
                "pages": pages, "source_ref": final}
    if ctype.startswith("text/plain"):
        return {"text": normalize_text(decode_bytes(data, charset)), "title": host, "source_type": "url",
                "pages": 0, "source_ref": final}
    if ctype and not any(t in ctype for t in ("html", "xml", "text")):
        raise SummarizerError("UNSUPPORTED", f"Type de contenu non pris en charge ({ctype.split(';')[0]}).")
    text, title = extract_html(decode_bytes(data, charset or _charset_from_html(data)))
    return {"text": text, "title": title or host, "source_type": "url", "pages": 0, "source_ref": final}


# ── Chunking & retrieval ────────────────────────────────────────────────────

def estimate_tokens(text: str) -> int:
    return math.ceil(len(text) / CHARS_PER_TOKEN)


def _split_units(text: str, max_chars: int) -> list[str]:
    units = []
    for para in re.split(r"(?<=\n)\n+", text):
        if len(para) <= max_chars:
            units.append(para)
            continue
        for sent in re.split(r"(?<=[.!?…])\s+|\n", para):
            while len(sent) > max_chars:
                cut = sent.rfind(" ", 0, max_chars)
                cut = cut if cut > max_chars // 2 else max_chars
                units.append(sent[:cut])
                sent = sent[cut:]
            if sent:
                units.append(sent)
    return units


def chunk_text(text: str, max_chars: int, overlap: int = CHUNK_OVERLAP_CHARS) -> list[str]:
    if len(text) <= max_chars:
        return [text]
    chunks: list[str] = []
    cur: list[str] = []
    size = 0
    for unit in _split_units(text, max(1000, max_chars - overlap - 2)):
        add = len(unit) + 1
        if cur and size + add > max_chars:
            chunks.append("\n".join(cur).strip())
            tail = chunks[-1][-overlap:] if overlap else ""
            sp = tail.find(" ")
            tail = tail[sp + 1:] if 0 <= sp < len(tail) - 1 else tail
            cur = [tail] if tail else []
            size = len(tail)
        cur.append(unit)
        size += add
    if cur and "".join(cur).strip():
        chunks.append("\n".join(cur).strip())
    return chunks


WORD_RE = re.compile(r"\w{3,}", re.UNICODE)
STOPWORDS = set("""les des une est que qui dans pour par pas sur avec son ses sont ont aux the and for are was with
this that from have has not but you your what which when where how quoi quel quelle quels quelles comment
pourquoi est-ce cela ceci elle ils elles nous vous leur leurs mais donc car ainsi aussi plus moins tout tous
toute toutes être avoir fait faire peut document texte""".split())


def _terms(text: str) -> list[str]:
    return [w for w in WORD_RE.findall(text.lower()) if w not in STOPWORDS]


def select_passages(chunks: list[str], question: str, budget_chars: int) -> list[int]:
    q = set(_terms(question))
    if not q:
        return []
    n = len(chunks)
    df = Counter()
    tfs = []
    for c in chunks:
        tf = Counter(_terms(c))
        tfs.append(tf)
        df.update(set(tf) & q)
    scores = []
    for i, tf in enumerate(tfs):
        s = sum((1 + math.log(tf[t])) * math.log(1 + n / df[t]) for t in q if tf.get(t))
        if s > 0:
            scores.append((s, i))
    scores.sort(reverse=True)
    picked, used = [], 0
    for _, i in scores:
        if used + len(chunks[i]) > budget_chars:
            continue
        picked.append(i)
        used += len(chunks[i])
    return sorted(picked)


# ── Client & API ────────────────────────────────────────────────────────────

def _api_key() -> str | None:
    key = (module.env(KEY_NAME) or "").strip()
    return key or None


def _sdk_available() -> bool:
    return _client_factory is not None or importlib.util.find_spec("anthropic") is not None


def _pdf_available() -> bool:
    return importlib.util.find_spec("pypdf") is not None


def _make_client(api_key: str):
    if _client_factory is not None:
        return _client_factory(api_key)
    try:
        import anthropic
    except ImportError:
        raise SummarizerError("DEPENDENCY_MISSING", "La bibliothèque Python « anthropic » n'est pas installée. Désactivez "
                                                    "puis réactivez le module, ou installez-la depuis « Bibliothèques ».")
    return anthropic.AsyncAnthropic(api_key=api_key)


def _settings() -> dict:
    model = module.config.get("default_model", MODEL_IDS[0])
    if model not in MODEL_IDS:
        model = MODEL_IDS[0]
    try:
        max_tokens = int(module.config.get("max_tokens", 32000))
    except (TypeError, ValueError):
        max_tokens = 32000
    effort = module.config.get("effort", "medium")
    if effort not in EFFORTS:
        effort = "medium"
    try:
        direct = int(module.config.get("direct_max_tokens", 150000))
    except (TypeError, ValueError):
        direct = 150000
    return {"model": model, "max_tokens": max(1024, min(max_tokens, 128000)), "effort": effort,
            "direct_max_tokens": max(20000, min(direct, 800000))}


def _model_info(model_id: str) -> dict:
    return next((m for m in MODELS if m["id"] == model_id), MODELS[0])


def _is_context_error(exc) -> bool:
    if getattr(exc, "status_code", None) == 413:
        return True
    msg = (getattr(exc, "message", None) or "").lower()
    return "too long" in msg or "context window" in msg or "context length" in msg


def _map_api_error(exc: Exception) -> SummarizerError:
    if isinstance(exc, SummarizerError):
        return exc
    try:
        import anthropic
    except ImportError:
        return SummarizerError("API_ERROR", "Erreur inattendue lors de l'appel à l'API.")
    if isinstance(exc, anthropic.AuthenticationError):
        return SummarizerError("INVALID_KEY", "Clé d'API refusée (401). Vérifiez ANTHROPIC_API_KEY dans le menu « Variables ».")
    if isinstance(exc, anthropic.PermissionDeniedError):
        return SummarizerError("PERMISSION_DENIED", "Accès refusé (403) : cette clé n'a pas accès à ce modèle.")
    if isinstance(exc, anthropic.NotFoundError):
        return SummarizerError("MODEL_NOT_FOUND", "Modèle introuvable ou non disponible pour votre organisation (404).")
    if isinstance(exc, anthropic.RateLimitError):
        retry = None
        try:
            raw = exc.response.headers.get("retry-after")
            retry = int(float(raw)) if raw else None
        except (TypeError, ValueError, AttributeError):
            retry = None
        wait = f" Réessayez dans {retry} s." if retry else " Réessayez dans quelques instants."
        return SummarizerError("RATE_LIMITED", "Limite de débit ou quota atteint (429)." + wait, retry_after=retry)
    if getattr(exc, "status_code", None) == 529:
        return SummarizerError("OVERLOADED", "L'API Anthropic est momentanément surchargée (529). Réessayez dans un instant.")
    if isinstance(exc, anthropic.APIStatusError) and _is_context_error(exc):
        return SummarizerError("CONTEXT_TOO_LONG", "Le texte envoyé dépasse le contexte du modèle : réduisez le réglage "
                                                   "« Taille maximale envoyée d'un bloc » du module.")
    if isinstance(exc, anthropic.BadRequestError):
        return SummarizerError("BAD_REQUEST", "Requête refusée par l'API (400).")
    if isinstance(exc, anthropic.APIStatusError):
        if exc.status_code == 402:
            return SummarizerError("BILLING", "Problème de facturation sur le compte Anthropic (402).")
        if exc.status_code >= 500:
            return SummarizerError("SERVER_ERROR", f"Erreur du service Anthropic ({exc.status_code}). Réessayez plus tard.")
        return SummarizerError("API_ERROR", f"Erreur de l'API ({exc.status_code}).")
    if isinstance(exc, anthropic.APITimeoutError):
        return SummarizerError("TIMEOUT", "Délai dépassé : l'API n'a pas répondu à temps.")
    if isinstance(exc, anthropic.APIConnectionError):
        return SummarizerError("NETWORK", "Impossible de joindre l'API Anthropic. Vérifiez votre connexion Internet.")
    return SummarizerError("API_ERROR", "Erreur inattendue lors de l'appel à l'API.")


async def _complete(model: str, system: str, messages: list[dict], *, max_tokens: int | None = None,
                    effort: str | None = None, on_text: Callable[[str], Any] | None = None) -> dict:
    key = _api_key()
    if not key:
        raise SummarizerError("API_KEY_MISSING", "Clé d'API absente : définissez ANTHROPIC_API_KEY dans le menu « Variables ».")
    settings = _settings()
    client = _make_client(key)
    params: dict[str, Any] = {
        "model": model,
        "max_tokens": max_tokens or settings["max_tokens"],
        "system": system,
        "messages": messages,
        "output_config": {"effort": effort or settings["effort"]},
    }
    if _model_info(model)["fallbacks"]:
        params["fallbacks"] = "default"
        params["betas"] = [FALLBACK_BETA]
    try:
        async with client.beta.messages.stream(**params) as stream:
            async for event in stream:
                if on_text is not None and getattr(event, "type", None) == "text":
                    r = on_text(event.text)
                    if asyncio.iscoroutine(r):
                        await r
            final = await stream.get_final_message()
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        mapped = _map_api_error(exc)
        log.warning("API call failed (%s, %s)", mapped.code, type(exc).__name__)
        raise mapped
    if final.stop_reason == "refusal":
        raise SummarizerError("REFUSED", "Le modèle a refusé de traiter ce document (filtre de sécurité).")
    usage = final.usage
    return {
        "text": "".join(b.text for b in final.content if getattr(b, "type", None) == "text"),
        "model": getattr(final, "model", model) or model,
        "stop_reason": final.stop_reason,
        "usage": {"input": int(getattr(usage, "input_tokens", 0) or 0),
                  "output": int(getattr(usage, "output_tokens", 0) or 0),
                  "cache_read": int(getattr(usage, "cache_read_input_tokens", 0) or 0)},
    }


def _add_usage(total: dict, u: dict) -> None:
    for k in ("input", "output", "cache_read"):
        total[k] = total.get(k, 0) + u.get(k, 0)


# ── Prompts ─────────────────────────────────────────────────────────────────

SYSTEM = ("Tu es un analyste documentaire rigoureux. Tu travailles uniquement à partir du document fourni entre balises "
          "<document> : tu n'inventes rien, tu restes fidèle aux faits, chiffres, noms et nuances du texte, et tu "
          "signales clairement quand une information n'y figure pas. Tu réponds en Markdown propre, sans préambule "
          "du type « Voici le résumé ».")

LENGTHS = {
    "short": "un résumé court : un seul paragraphe de 3 à 5 phrases qui donne l'idée centrale et la conclusion.",
    "medium": "un résumé de longueur moyenne : une phrase d'introduction qui situe le document (nature, auteur ou "
              "source si connus, objet), puis 2 à 4 paragraphes couvrant les idées principales et la conclusion.",
    "detailed": "un résumé détaillé et structuré : courte introduction, puis une section par grande partie du "
                "document avec des intertitres « ## », en gardant les arguments, exemples, chiffres et définitions "
                "importants, et une conclusion. Environ 600 à 1200 mots selon la richesse du document.",
}

KINDS = {
    "summary": "Rédige {length}",
    "keypoints": ("Dresse la liste des points clés du document : 6 à 15 puces, de la plus importante à la moins "
                  "importante, chacune en une ou deux phrases précises (avec les chiffres utiles). Termine par une "
                  "ligne « **À retenir :** » suivie de la conclusion principale en une phrase."),
    "outline": ("Reconstitue le plan du document sous forme de liste hiérarchique numérotée (1., 1.1, 1.1.1). "
                "Pour chaque entrée, donne le titre de la section puis, après un tiret cadratin, une phrase qui "
                "décrit son contenu. Si le document n'a pas de titres explicites, déduis un plan logique."),
}
KIND_LABELS = {"summary": "Résumé", "keypoints": "Points clés", "outline": "Plan"}
LENGTH_LABELS = {"short": "court", "medium": "moyen", "detailed": "détaillé"}

MAP_PROMPT = ("Voici la partie {i} sur {n} d'un long document. Prends des notes de synthèse fidèles et détaillées de "
              "cette partie uniquement, dans la langue du document : reprends sa structure (titres), ses idées, "
              "arguments, définitions, faits, chiffres, dates et noms propres importants. Ne commente pas, ne "
              "conclus pas sur l'ensemble du document. Environ un dixième de la longueur de la partie.")


def _lang_clause(lang: str) -> str:
    if not lang or lang.lower() in ("auto", "document", "langue du document"):
        return "Réponds dans la langue du document."
    return f"Réponds en {lang}."


def _doc_block(title: str, body: str, digest: bool, parts: int) -> dict:
    head = f'<document titre="{title}"'
    if digest:
        head += f' nature="notes de synthèse d\'un long document découpé en {parts} parties"'
    return {"type": "text", "text": head + ">\n" + body + "\n</document>", "cache_control": {"type": "ephemeral"}}


def instruction_for(kind: str, length: str, lang: str, digest: bool) -> str:
    text = KINDS[kind].format(length=LENGTHS.get(length, LENGTHS["medium"]))
    if digest:
        text += (" Le document étant très long, tu disposes de notes de synthèse établies partie par partie : "
                 "appuie-toi sur elles comme si tu lisais le document entier.")
    return text + " " + _lang_clause(lang)


# ── Documents ───────────────────────────────────────────────────────────────

def _doc_public(row: dict, settings: dict | None = None) -> dict:
    settings = settings or _settings()
    limit = settings["direct_max_tokens"]
    direct = row["est_tokens"] <= limit
    parts = len(chunk_text(row["text"], _chunk_chars(limit))) if not direct and "text" in row else row.get("digest_parts", 0)
    return {
        "id": row["id"], "title": row["title"], "source_type": row["source_type"], "source_ref": row["source_ref"],
        "pages": row["pages"], "chars": row["chars"], "words": row["words"], "est_tokens": row["est_tokens"],
        "strategy": "direct" if direct else "chunked", "parts": 0 if direct else parts,
        "digest_ready": bool(row.get("digest")) and row.get("digest_limit") == limit,
        "created_at": row["created_at"], "updated_at": row["updated_at"],
    }


def _chunk_chars(limit_tokens: int) -> int:
    return int(min(CHUNK_TOKENS, limit_tokens // 2) * CHARS_PER_TOKEN)


async def _get_doc(doc_id) -> dict | None:
    if not isinstance(doc_id, int) or isinstance(doc_id, bool):
        return None
    db = await _db()
    return await db.fetch_one("SELECT * FROM documents WHERE id = ?", (doc_id,))


async def _store_document(info: dict) -> dict:
    text = info["text"]
    if len(text) < MIN_TEXT_CHARS:
        raise SummarizerError("NO_TEXT", "Aucun texte exploitable n'a été trouvé dans cette source.")
    if len(text) > MAX_TEXT_CHARS:
        raise SummarizerError("TOO_LARGE", f"Document trop long ({len(text):,} caractères ; maximum "
                                           f"{MAX_TEXT_CHARS:,}).".replace(",", " "))
    db = await _db()
    now = _now()
    row = await db.fetch_one(
        "INSERT INTO documents (title, source_type, source_ref, pages, chars, words, est_tokens, text, created_at, "
        "updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) RETURNING *",
        (info["title"][:200], info["source_type"], info.get("source_ref", "")[:2000], info.get("pages", 0), len(text),
         len(text.split()), estimate_tokens(text), text, now, now))
    return row


async def _doc_view(row: dict) -> dict:
    db = await _db()
    outputs = await db.fetch_all("SELECT * FROM outputs WHERE doc_id = ? ORDER BY id", (row["id"],))
    msgs = await db.fetch_all("SELECT id, role, content, model, stopped, created_at FROM messages WHERE doc_id = ? "
                              "ORDER BY id", (row["id"],))
    for o in outputs:
        o["usage"] = json.loads(o["usage"] or "{}")
        o["truncated"] = bool(o["truncated"])
    for m in msgs:
        m["stopped"] = bool(m["stopped"])
    busy = [{"request_id": rid, "scope": e["scope"], "kind": e.get("kind"), "variant": e.get("variant"),
             "text": e["text"], "progress": e.get("progress")}
            for rid, e in _active.items() if e["doc_id"] == row["id"]]
    return {"document": _doc_public(row), "outputs": outputs, "messages": msgs,
            "preview": row["text"][:4000], "busy": busy}


# ── Digest (map-reduce) ─────────────────────────────────────────────────────

async def build_digest(row: dict, model: str, progress: Callable[[str, int, int, str], Any], usage: dict) -> tuple[str, int]:
    limit = _settings()["direct_max_tokens"]
    if row.get("digest") and row.get("digest_limit") == limit:
        return row["digest"], row["digest_parts"]
    text = row["text"]
    chunk_chars = _chunk_chars(limit)
    level = 0
    first_parts = 0
    while estimate_tokens(text) > limit:
        level += 1
        if level > MAX_REDUCE_LEVELS:
            raise SummarizerError("TOO_LARGE", "Document trop long pour être condensé.")
        chunks = chunk_text(text, chunk_chars)
        if level == 1:
            first_parts = len(chunks)
        n = len(chunks)
        done = 0
        sem = asyncio.Semaphore(MAP_CONCURRENCY)
        notes: list[str | None] = [None] * n
        phase = "map" if level == 1 else "reduce"
        await progress(phase, 0, n, f"Lecture des parties (niveau {level})" if level > 1 else "Lecture des parties")

        async def one(i: int):
            nonlocal done
            async with sem:
                res = await _complete(model, SYSTEM, [{"role": "user", "content": [
                    {"type": "text", "text": f"<document titre=\"{row['title']}\" partie=\"{i + 1}/{n}\">\n{chunks[i]}\n</document>"},
                    {"type": "text", "text": MAP_PROMPT.format(i=i + 1, n=n)},
                ]}], max_tokens=MAP_MAX_TOKENS, effort="low")
            _add_usage(usage, res["usage"])
            notes[i] = res["text"].strip()
            done += 1
            await progress(phase, done, n, f"Partie {done}/{n} lue")

        tasks = [asyncio.create_task(one(i)) for i in range(n)]
        try:
            await asyncio.gather(*tasks)
        except BaseException:
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise
        new_text = "\n\n".join(f"## Partie {i + 1}/{n}\n{notes[i]}" for i in range(n))
        if level > 1 and len(new_text) > 0.8 * len(text):
            raise SummarizerError("TOO_LARGE", "Les notes de synthèse ne se réduisent plus : augmentez le réglage "
                                               "« Taille maximale envoyée d'un bloc » du module.")
        text = new_text
    parts = first_parts or 1
    db = await _db()
    await db.execute("UPDATE documents SET digest = ?, digest_parts = ?, digest_limit = ? WHERE id = ?",
                     (text, parts, limit, row["id"]))
    return text, parts


# ── Active requests ─────────────────────────────────────────────────────────

def _check_request_id(request_id: str) -> str | None:
    if not REQUEST_ID_RE.match(request_id or ""):
        return "Identifiant de requête invalide"
    if request_id in _active:
        return "Identifiant de requête déjà utilisé"
    return None


def _start(request_id: str, entry: dict, coro_factory) -> None:
    entry.update({"text": "", "started": time.time()})
    _active[request_id] = entry
    task = asyncio.create_task(coro_factory(entry))
    entry["task"] = task
    task.add_done_callback(lambda _t, rid=request_id: _active.pop(rid, None))


def _make_streamer(request_id: str, entry: dict):
    pending: list[str] = []
    state = {"last": time.monotonic()}

    async def flush():
        if pending:
            chunk = "".join(pending)
            pending.clear()
            await module.events.emit("stream:delta", {"request_id": request_id, "text": chunk})
        state["last"] = time.monotonic()

    async def on_text(t: str):
        entry["text"] += t
        pending.append(t)
        if time.monotonic() - state["last"] >= FLUSH_INTERVAL_S:
            await flush()

    return on_text, flush


async def _emit_error(request_id: str, entry: dict, exc: Exception):
    if isinstance(exc, SummarizerError):
        payload = {"error": exc.message, "code": exc.code, **exc.extra}
    else:
        log.exception("request failed")
        payload = {"error": "Erreur interne inattendue", "code": "INTERNAL"}
    await module.events.emit("stream:error", {"request_id": request_id, "scope": entry["scope"],
                                              "doc_id": entry["doc_id"], **payload})


# ── Payloads ────────────────────────────────────────────────────────────────

class LangMixin(BaseModel):
    lang: str = Field("français", max_length=40)
    model: str | None = None

    @field_validator("lang")
    @classmethod
    def _lang(cls, v: str) -> str:
        v = " ".join(v.split())
        if not v or not LANG_RE.match(v):
            raise ValueError("Langue invalide")
        return v

    @field_validator("model")
    @classmethod
    def _model(cls, v):
        if v is not None and v not in MODEL_IDS:
            raise ValueError("Modèle inconnu")
        return v


class ImportPayload(BaseModel):
    source: Literal["file", "url", "text"]
    name: str = Field("", max_length=260)
    data: str = ""
    url: str = Field("", max_length=4000)
    text: str = Field("", max_length=MAX_TEXT_CHARS)
    title: str = Field("", max_length=200)


class GenPayload(LangMixin):
    doc_id: int
    request_id: str
    kind: Literal["summary", "keypoints", "outline"]
    length: Literal["short", "medium", "detailed"] = "medium"


class AskPayload(LangMixin):
    doc_id: int
    request_id: str
    text: str = Field(..., min_length=1, max_length=20000)


# ── Handlers ────────────────────────────────────────────────────────────────

@module.on("status")
async def status(payload: dict):
    return {"has_key": _api_key() is not None, "sdk_available": _sdk_available(), "pdf_available": _pdf_available(),
            "key_name": KEY_NAME, "models": MODELS, "settings": _settings(),
            "max_file_mb": MAX_FILE_BYTES // (1024 * 1024)}


@module.on("doc:list")
async def doc_list(payload: dict):
    db = await _db()
    rows = await db.fetch_all(
        "SELECT d.id, d.title, d.source_type, d.source_ref, d.pages, d.chars, d.words, d.est_tokens, d.digest_parts, "
        "d.digest_limit, d.created_at, d.updated_at, (d.digest IS NOT NULL) AS digest, "
        "(SELECT COUNT(*) FROM outputs o WHERE o.doc_id = d.id) AS n_outputs, "
        "(SELECT COUNT(*) FROM messages m WHERE m.doc_id = d.id AND m.role = 'user') AS n_questions "
        "FROM documents d ORDER BY d.updated_at DESC")
    settings = _settings()
    items = []
    for r in rows:
        pub = _doc_public(r, settings)
        pub.update({"n_outputs": r["n_outputs"], "n_questions": r["n_questions"],
                    "busy": any(e["doc_id"] == r["id"] for e in _active.values())})
        items.append(pub)
    return {"items": items}


@module.on("doc:import")
async def doc_import(payload: dict):
    try:
        p = ImportPayload(**(payload or {}))
    except ValidationError as exc:
        return _err(_validation_message(exc), "INVALID_PAYLOAD")
    try:
        if p.source == "file":
            name = re.sub(r"[\\/:*?\"<>|\x00-\x1f]", "_", p.name.strip()) or "document"
            if len(p.data) > (MAX_FILE_BYTES * 4) // 3 + 8:
                return _err(f"Fichier trop volumineux (maximum {MAX_FILE_BYTES // (1024 * 1024)} Mo).", "TOO_LARGE")
            try:
                data = base64.b64decode(p.data, validate=True)
            except (binascii.Error, ValueError):
                return _err("Contenu de fichier invalide", "INVALID_PAYLOAD")
            if not data:
                return _err("Le fichier est vide", "INVALID_PAYLOAD")
            info = await asyncio.to_thread(extract_file, name, data)
            info["source_ref"] = name
        elif p.source == "url":
            if not p.url.strip():
                return _err("Saisissez une adresse web", "INVALID_PAYLOAD")
            info = await asyncio.to_thread(fetch_url, p.url)
        else:
            text = normalize_text(p.text)
            if not text:
                return _err("Collez un texte à résumer", "INVALID_PAYLOAD")
            first = " ".join(text.split("\n", 1)[0].split()).lstrip("# ")
            title = " ".join(p.title.split()) or ((first[:60] + "…") if len(first) > 60 else first) or "Texte collé"
            info = {"text": text, "title": title, "source_type": "paste", "pages": 0}
        if p.title.strip() and p.source != "text":
            info["title"] = " ".join(p.title.split())
        row = await _store_document(info)
    except SummarizerError as exc:
        return _err(exc.message, exc.code)
    log.info("document imported (%s, %d chars)", row["source_type"], row["chars"])
    return await _doc_view(row)


@module.on("doc:get")
async def doc_get(payload: dict):
    row = await _get_doc((payload or {}).get("id"))
    if row is None:
        return _err("Document introuvable", "NOT_FOUND")
    return await _doc_view(row)


@module.on("doc:text")
async def doc_text(payload: dict):
    row = await _get_doc((payload or {}).get("id"))
    if row is None:
        return _err("Document introuvable", "NOT_FOUND")
    limit = 400_000
    return {"text": row["text"][:limit], "truncated": len(row["text"]) > limit, "chars": row["chars"]}


@module.on("doc:rename")
async def doc_rename(payload: dict):
    title = " ".join(str((payload or {}).get("title") or "").split())[:200]
    if not title:
        return _err("Le titre est requis", "INVALID_PAYLOAD")
    row = await _get_doc((payload or {}).get("id"))
    if row is None:
        return _err("Document introuvable", "NOT_FOUND")
    db = await _db()
    row = await db.fetch_one("UPDATE documents SET title = ?, updated_at = ? WHERE id = ? RETURNING *",
                             (title, _now(), row["id"]))
    return {"document": _doc_public(row)}


async def _cancel_doc_requests(doc_id: int):
    tasks = [e["task"] for e in list(_active.values()) if e["doc_id"] == doc_id and e.get("task")]
    for t in tasks:
        t.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


@module.on("doc:delete")
async def doc_delete(payload: dict):
    row = await _get_doc((payload or {}).get("id"))
    if row is None:
        return _err("Document introuvable", "NOT_FOUND")
    await _cancel_doc_requests(row["id"])
    db = await _db()
    await db.execute("DELETE FROM documents WHERE id = ?", (row["id"],))
    return {"ok": True}


@module.on("doc:clear_chat")
async def doc_clear_chat(payload: dict):
    row = await _get_doc((payload or {}).get("id"))
    if row is None:
        return _err("Document introuvable", "NOT_FOUND")
    if any(e["doc_id"] == row["id"] and e["scope"] == "chat" for e in _active.values()):
        return _err("Une réponse est en cours", "BUSY")
    db = await _db()
    await db.execute("DELETE FROM messages WHERE doc_id = ?", (row["id"],))
    return {"ok": True}


def export_markdown(doc: dict, outputs: list[dict], messages: list[dict]) -> str:
    lines = [f"# {doc['title']}", ""]
    src = {"pdf": "PDF", "docx": "Word", "text": "Texte", "markdown": "Markdown", "html": "HTML",
           "url": "Page web", "paste": "Texte collé"}.get(doc["source_type"], doc["source_type"])
    meta = f"- Source : {src}"
    if doc.get("source_ref"):
        meta += f" — {doc['source_ref']}"
    lines += [meta, f"- Taille : {doc['words']:,} mots".replace(",", " ") +
              (f", {doc['pages']} pages" if doc.get("pages") else ""),
              f"- Exporté le : {datetime.now().strftime('%d/%m/%Y %H:%M')}", ""]
    order = {"summary": 0, "keypoints": 1, "outline": 2}
    for o in sorted(outputs, key=lambda o: (order.get(o["kind"], 9), o["variant"])):
        title = KIND_LABELS.get(o["kind"], o["kind"])
        if o["kind"] == "summary":
            title += f" ({LENGTH_LABELS.get(o['variant'], o['variant'])})"
        lines += [f"## {title}", "", o["content"].strip(), ""]
    qa = [m for m in messages if m["content"].strip()]
    if qa:
        lines += ["## Questions-réponses", ""]
        for m in qa:
            if m["role"] == "user":
                lines += [f"**Q : {' '.join(m['content'].split())}**", ""]
            else:
                lines += [m["content"].strip(), ""]
    return "\n".join(lines).rstrip() + "\n"


@module.on("doc:export")
async def doc_export(payload: dict):
    row = await _get_doc((payload or {}).get("id"))
    if row is None:
        return _err("Document introuvable", "NOT_FOUND")
    view = await _doc_view(row)
    md = export_markdown(row, view["outputs"], view["messages"])
    stem = re.sub(r"[\\/:*?\"<>|\x00-\x1f]+", "_", row["title"]).strip(" ._") or "document"
    return {"filename": f"{stem[:80]} - résumé.md", "markdown": md}


@module.on("gen:run")
async def gen_run(payload: dict):
    try:
        p = GenPayload(**(payload or {}))
    except ValidationError as exc:
        return _err(_validation_message(exc), "INVALID_PAYLOAD")
    if not _api_key():
        return _err("Clé d'API absente : définissez ANTHROPIC_API_KEY dans le menu « Variables ».", "API_KEY_MISSING")
    if bad := _check_request_id(p.request_id):
        return _err(bad, "INVALID_PAYLOAD")
    row = await _get_doc(p.doc_id)
    if row is None:
        return _err("Document introuvable", "NOT_FOUND")
    variant = p.length if p.kind == "summary" else ""
    if any(e["doc_id"] == row["id"] and e["scope"] == "gen" and e["kind"] == p.kind and e["variant"] == variant
           for e in _active.values()):
        return _err("Cette synthèse est déjà en cours de génération", "BUSY")
    model = p.model or _settings()["model"]
    rid = p.request_id

    async def run(entry: dict):
        usage: dict = {}
        on_text, flush = _make_streamer(rid, entry)

        async def progress(phase: str, done: int, total: int, message: str):
            entry["progress"] = {"phase": phase, "done": done, "total": total, "message": message}
            await module.events.emit("stream:status", {"request_id": rid, **entry["progress"]})

        try:
            limit = _settings()["direct_max_tokens"]
            digest = row["est_tokens"] > limit
            if digest:
                body, parts = await build_digest(row, model, progress, usage)
            else:
                body, parts = row["text"], 0
            await progress("write", 0, 0, "Rédaction")
            res = await _complete(model, SYSTEM, [{"role": "user", "content": [
                _doc_block(row["title"], body, digest, parts),
                {"type": "text", "text": instruction_for(p.kind, p.length, p.lang, digest)},
            ]}], on_text=on_text)
            await flush()
            _add_usage(usage, res["usage"])
        except asyncio.CancelledError:
            await flush()
            await module.events.emit("stream:done", {"request_id": rid, "scope": "gen", "doc_id": row["id"],
                                                     "stopped": True, "kind": p.kind, "variant": variant})
            raise
        except Exception as exc:
            await _emit_error(rid, entry, exc)
            return
        db = await _db()
        out = await db.fetch_one(
            "INSERT INTO outputs (doc_id, kind, variant, lang, content, model, usage, truncated, strategy, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(doc_id, kind, variant) DO UPDATE SET "
            "lang = excluded.lang, content = excluded.content, model = excluded.model, usage = excluded.usage, "
            "truncated = excluded.truncated, strategy = excluded.strategy, created_at = excluded.created_at RETURNING *",
            (row["id"], p.kind, variant, p.lang, res["text"], res["model"], json.dumps(usage),
             int(res["stop_reason"] == "max_tokens"), "chunked" if digest else "direct", _now()))
        await db.execute("UPDATE documents SET updated_at = ? WHERE id = ?", (_now(), row["id"]))
        out["usage"] = usage
        out["truncated"] = bool(out["truncated"])
        await module.events.emit("stream:done", {"request_id": rid, "scope": "gen", "doc_id": row["id"], "output": out})

    _start(rid, {"doc_id": row["id"], "scope": "gen", "kind": p.kind, "variant": variant}, run)
    return {"request_id": rid, "model": model}


def _chat_messages(history: list[dict], first_block: dict, question: str) -> list[dict]:
    turns = [m for m in history if m["content"].strip()][-CHAT_HISTORY_TURNS * 2:]
    while turns and turns[0]["role"] != "user":
        turns.pop(0)
    msgs: list[dict] = []
    for m in turns:
        if msgs and msgs[-1]["role"] == m["role"]:
            continue
        msgs.append({"role": m["role"], "content": m["content"]})
    if msgs and msgs[-1]["role"] == "user":
        msgs.pop()
    msgs.append({"role": "user", "content": question})
    first = msgs[0]
    msgs[0] = {"role": "user", "content": [first_block, {"type": "text", "text": first["content"]}]}
    return msgs


CHAT_RULES = ("Réponds aux questions de l'utilisateur sur ce document. Appuie-toi uniquement sur son contenu ; cite "
              "de courts passages entre guillemets (avec la page si elle est indiquée) quand cela aide. Si la réponse "
              "ne figure pas dans le document, dis-le clairement.")


@module.on("chat:ask")
async def chat_ask(payload: dict):
    try:
        p = AskPayload(**(payload or {}))
    except ValidationError as exc:
        return _err(_validation_message(exc), "INVALID_PAYLOAD")
    question = p.text.strip()
    if not question:
        return _err("La question est vide", "INVALID_PAYLOAD")
    if not _api_key():
        return _err("Clé d'API absente : définissez ANTHROPIC_API_KEY dans le menu « Variables ».", "API_KEY_MISSING")
    if bad := _check_request_id(p.request_id):
        return _err(bad, "INVALID_PAYLOAD")
    row = await _get_doc(p.doc_id)
    if row is None:
        return _err("Document introuvable", "NOT_FOUND")
    if any(e["doc_id"] == row["id"] and e["scope"] == "chat" for e in _active.values()):
        return _err("Une réponse est déjà en cours pour ce document", "BUSY")
    db = await _db()
    history = await db.fetch_all("SELECT role, content FROM messages WHERE doc_id = ? ORDER BY id", (row["id"],))
    user_msg = await db.fetch_one("INSERT INTO messages (doc_id, role, content, created_at) VALUES (?, 'user', ?, ?) "
                                  "RETURNING id, role, content, model, stopped, created_at", (row["id"], question, _now()))
    user_msg["stopped"] = False
    model = p.model or _settings()["model"]
    rid = p.request_id

    async def run(entry: dict):
        usage: dict = {}
        on_text, flush = _make_streamer(rid, entry)

        async def progress(phase, done, total, message):
            entry["progress"] = {"phase": phase, "done": done, "total": total, "message": message}
            await module.events.emit("stream:status", {"request_id": rid, **entry["progress"]})

        try:
            limit = _settings()["direct_max_tokens"]
            digest = row["est_tokens"] > limit
            prompt = question
            if digest:
                body, parts = await build_digest(row, model, progress, usage)
                chunks = chunk_text(row["text"], _chunk_chars(limit))
                budget = int(min(RETRIEVE_CHARS, max(0, limit * CHARS_PER_TOKEN - len(body))))
                picked = select_passages(chunks, question, budget)
                if picked:
                    extracts = "\n\n".join(f"<extrait partie=\"{i + 1}/{len(chunks)}\">\n{chunks[i]}\n</extrait>"
                                           for i in picked)
                    prompt = ("Extraits du texte intégral sélectionnés pour cette question :\n" + extracts +
                              "\n\nQuestion : " + question)
            else:
                body, parts = row["text"], 0
            await progress("write", 0, 0, "Rédaction")
            first = _doc_block(row["title"], body, digest, parts)
            first["text"] += "\n\n" + CHAT_RULES + " " + _lang_clause(p.lang)
            res = await _complete(model, SYSTEM, _chat_messages(history, first, prompt), on_text=on_text)
            await flush()
            _add_usage(usage, res["usage"])
        except asyncio.CancelledError:
            await flush()
            partial = entry["text"]
            msg = None
            if partial.strip():
                msg = await db.fetch_one("INSERT INTO messages (doc_id, role, content, model, stopped, created_at) "
                                         "VALUES (?, 'assistant', ?, ?, 1, ?) RETURNING *",
                                         (row["id"], partial, model, _now()))
            await module.events.emit("stream:done", {"request_id": rid, "scope": "chat", "doc_id": row["id"],
                                                     "stopped": True, "message": msg})
            raise
        except Exception as exc:
            await _emit_error(rid, entry, exc)
            return
        msg = await db.fetch_one("INSERT INTO messages (doc_id, role, content, model, created_at) VALUES "
                                 "(?, 'assistant', ?, ?, ?) RETURNING *", (row["id"], res["text"], res["model"], _now()))
        msg["stopped"] = False
        msg["usage"] = usage
        await db.execute("UPDATE documents SET updated_at = ? WHERE id = ?", (_now(), row["id"]))
        await module.events.emit("stream:done", {"request_id": rid, "scope": "chat", "doc_id": row["id"], "message": msg})

    _start(rid, {"doc_id": row["id"], "scope": "chat", "kind": None, "variant": None}, run)
    return {"request_id": rid, "model": model, "user_message": user_msg}


@module.on("req:stop")
async def req_stop(payload: dict):
    entry = _active.get(str((payload or {}).get("request_id") or ""))
    if entry is None or entry.get("task") is None:
        return _err("Aucune génération en cours avec cet identifiant", "NOT_FOUND")
    entry["task"].cancel()
    return {"ok": True}


@module.on_shutdown
async def cleanup():
    tasks = [e["task"] for e in _active.values() if e.get("task")]
    for t in tasks:
        t.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
