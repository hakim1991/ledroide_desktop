"""Éditeur Markdown — documents SQLite, rendu HTML (python-markdown), import / export."""
import asyncio
import html as html_lib
import re
import shutil
import sqlite3
import tempfile
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from ledroide_sdk.http import UploadFile, File
from ledroide_sdk.http import FileResponse, JSONResponse
from pydantic import BaseModel
from ledroide_sdk import Module, data_path

module = Module(__file__)
router = module.router

DB_PATH = data_path(__file__, "markdown-editor.db")
TEMP_DIR = Path(tempfile.gettempdir()) / "ledroide_markdown-editor"
TEMP_DIR.mkdir(parents=True, exist_ok=True)
MAX_CONTENT = 5 * 1024 * 1024


def _err(msg: str, status: int = 400, code: Optional[str] = None):
    c = {"error": msg}
    if code:
        c["code"] = code
    return JSONResponse(status_code=status, content=c)


def _missing():
    return _err("La bibliothèque « markdown » n'est pas installée. Installez les dépendances du module.",
                400, "MISSING_DEP")


@contextmanager
def _db():
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _init():
    with _db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS documents (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                title      TEXT NOT NULL DEFAULT 'Sans titre',
                content    TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)


_init()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── rendu ─────────────────────────────────────────────────────────────────────

_TASK_RE = re.compile(r"<li>(\s*<p>)?\[([ xX])\]\s")


def _render(text: str) -> str:
    import markdown
    from markdown.extensions import Extension
    from markdown.inlinepatterns import SimpleTagInlineProcessor

    class _Strike(Extension):
        def extendMarkdown(self, md):
            md.inlinePatterns.register(SimpleTagInlineProcessor(r"()~~(.+?)~~", "del"), "strike", 175)

    md = markdown.Markdown(
        extensions=["extra", "sane_lists", "toc", _Strike()],
        extension_configs={"toc": {"permalink": False},
                           "extra": {"footnotes": {"BACKLINK_TITLE": "Retour à la note %d"}}},
        output_format="html",
    )
    out = md.convert(text or "")

    def task(m):
        checked = " checked" if m.group(2).lower() == "x" else ""
        return f'<li class="task">{m.group(1) or ""}<input type="checkbox" disabled{checked}> '

    return _TASK_RE.sub(task, out)


def _stats(text: str) -> dict:
    return {"words": len(re.findall(r"\w+", text or "")), "chars": len(text or ""),
            "lines": (text or "").count("\n") + 1 if text else 0}


class RenderIn(BaseModel):
    text: str = ""


@router.post("/render")
async def render(body: RenderIn):
    try:
        import markdown  # noqa: F401
    except ImportError:
        return _missing()
    if len(body.text) > MAX_CONTENT:
        return _err("Document trop volumineux.")
    html = await asyncio.to_thread(_render, body.text)
    return {"html": html, **_stats(body.text)}


# ── documents ─────────────────────────────────────────────────────────────────

class DocIn(BaseModel):
    title: str = "Sans titre"
    content: str = ""


class DocPatch(BaseModel):
    title: Optional[str] = None
    content: Optional[str] = None


def _clean_title(t: Optional[str]) -> str:
    t = (t or "").strip().replace("\n", " ")[:200]
    return t or "Sans titre"


def _get(doc_id: int):
    with _db() as conn:
        row = conn.execute("SELECT * FROM documents WHERE id=?", (doc_id,)).fetchone()
        return dict(row) if row else None


def _create(title: str, content: str) -> dict:
    now = _now()
    with _db() as conn:
        cur = conn.execute("INSERT INTO documents (title, content, created_at, updated_at) VALUES (?, ?, ?, ?)",
                           (_clean_title(title), content, now, now))
        return dict(conn.execute("SELECT * FROM documents WHERE id=?", (cur.lastrowid,)).fetchone())


@router.get("/docs")
async def list_docs():
    def _q():
        with _db() as conn:
            rows = conn.execute("SELECT id, title, created_at, updated_at, LENGTH(content) AS size, "
                                "SUBSTR(content, 1, 160) AS excerpt FROM documents ORDER BY updated_at DESC").fetchall()
            return [dict(r) for r in rows]

    return {"docs": await asyncio.to_thread(_q)}


@router.post("/docs")
async def create_doc(body: DocIn):
    if len(body.content) > MAX_CONTENT:
        return _err("Document trop volumineux.")
    return await asyncio.to_thread(_create, body.title, body.content)


@router.get("/docs/{doc_id}")
async def get_doc(doc_id: int):
    d = await asyncio.to_thread(_get, doc_id)
    if not d:
        return _err("Document introuvable", 404)
    return d


@router.put("/docs/{doc_id}")
async def update_doc(doc_id: int, body: DocPatch):
    changes = {}
    if body.title is not None:
        changes["title"] = _clean_title(body.title)
    if body.content is not None:
        if len(body.content) > MAX_CONTENT:
            return _err("Document trop volumineux.")
        changes["content"] = body.content

    def _q():
        with _db() as conn:
            if not conn.execute("SELECT 1 FROM documents WHERE id=?", (doc_id,)).fetchone():
                return None
            if changes:
                ch = {**changes, "updated_at": _now()}
                conn.execute(f"UPDATE documents SET {', '.join(k + '=?' for k in ch)} WHERE id=?",
                             [*ch.values(), doc_id])
            row = conn.execute("SELECT id, title, created_at, updated_at, LENGTH(content) AS size "
                               "FROM documents WHERE id=?", (doc_id,)).fetchone()
            return dict(row)

    d = await asyncio.to_thread(_q)
    if not d:
        return _err("Document introuvable", 404)
    return d


@router.delete("/docs/{doc_id}")
async def delete_doc(doc_id: int):
    def _q():
        with _db() as conn:
            return conn.execute("DELETE FROM documents WHERE id=?", (doc_id,)).rowcount

    if not await asyncio.to_thread(_q):
        return _err("Document introuvable", 404)
    return {"ok": True}


# ── import / export ───────────────────────────────────────────────────────────

def _decode(data: bytes) -> str:
    if data[:2] in (b"\xff\xfe", b"\xfe\xff"):
        try:
            return data.decode("utf-16")
        except UnicodeDecodeError:
            pass
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return data.decode("cp1252", errors="replace")


@router.post("/import")
async def import_md(file: UploadFile = File(...)):
    data = await file.read()
    if len(data) > MAX_CONTENT:
        return _err("Fichier trop volumineux (max 5 Mo).")
    name = file.filename or "document.md"
    if not re.search(r"\.(md|markdown|mdown|mkd|txt)$", name, re.I):
        return _err("Format non supporté : choisissez un fichier .md ou .txt.")
    if b"\x00" in data[:4096] and not data[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return _err("Ce fichier ne semble pas être du texte.")
    text = _decode(data).replace("\r\n", "\n").replace("\r", "\n")
    title = Path(name).stem
    m = re.search(r"^#\s+(.+?)\s*#*\s*$", text, re.M)
    if m:
        title = m.group(1)
    return await asyncio.to_thread(_create, title, text)


_EXPORT_CSS = """
:root { color-scheme: light dark; --bg:#ffffff; --fg:#1f2328; --muted:#59636e; --border:#d1d9e0; --code:#f6f8fa; --accent:#4f46e5; }
@media (prefers-color-scheme: dark) { :root { --bg:#0f0f12; --fg:#e2e2e8; --muted:#a1a1aa; --border:#2e2e33; --code:#18181b; --accent:#818cf8; } }
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--fg); font: 16px/1.65 system-ui, -apple-system, "Segoe UI", Roboto, sans-serif; }
main { max-width: 820px; margin: 0 auto; padding: 48px 24px 80px; }
h1, h2, h3, h4, h5, h6 { line-height: 1.25; margin: 1.6em 0 .6em; font-weight: 650; }
h1 { font-size: 2em; border-bottom: 1px solid var(--border); padding-bottom: .3em; }
h2 { font-size: 1.5em; border-bottom: 1px solid var(--border); padding-bottom: .3em; }
h3 { font-size: 1.25em; }
p, ul, ol, blockquote, table, pre, dl { margin: 0 0 1em; }
a { color: var(--accent); }
img { max-width: 100%; }
code { font-family: ui-monospace, "Cascadia Code", Consolas, monospace; font-size: .88em; background: var(--code); padding: .15em .35em; border-radius: 4px; }
pre { background: var(--code); border: 1px solid var(--border); border-radius: 8px; padding: 14px 16px; overflow: auto; }
pre code { background: none; padding: 0; font-size: .85em; }
blockquote { border-left: 4px solid var(--border); color: var(--muted); padding: 0 1em; margin-left: 0; }
table { border-collapse: collapse; display: block; overflow: auto; }
th, td { border: 1px solid var(--border); padding: 6px 13px; }
th { background: var(--code); }
hr { border: none; border-top: 1px solid var(--border); margin: 2em 0; }
li.task { list-style: none; margin-left: -1.3em; }
li.task input { margin-right: .5em; }
del { color: var(--muted); }
"""


def _export(title: str, content: str, fmt: str) -> str:
    stem = re.sub(r"[^\w\-]+", "_", title, flags=re.UNICODE).strip("_")[:60] or "document"
    now = time.time()
    for p in TEMP_DIR.iterdir():
        try:
            if now - p.stat().st_mtime > 3600:
                shutil.rmtree(p, ignore_errors=True) if p.is_dir() else p.unlink()
        except OSError:
            pass
    sub = TEMP_DIR / uuid.uuid4().hex[:8]
    sub.mkdir()
    if fmt == "md":
        path = sub / f"{stem}.md"
        path.write_text(content, encoding="utf-8")
    else:
        body = _render(content)
        doc = (f'<!DOCTYPE html>\n<html lang="fr">\n<head>\n<meta charset="UTF-8">\n'
               f'<meta name="viewport" content="width=device-width, initial-scale=1">\n'
               f'<title>{html_lib.escape(title)}</title>\n<style>{_EXPORT_CSS}</style>\n</head>\n'
               f'<body>\n<main>\n{body}\n</main>\n</body>\n</html>\n')
        path = sub / f"{stem}.html"
        path.write_text(doc, encoding="utf-8")
    return f"{sub.name}__{path.name}"


class ExportIn(BaseModel):
    title: str = "document"
    content: str = ""
    fmt: str = "md"


@router.post("/export")
async def export(body: ExportIn):
    if body.fmt not in ("md", "html"):
        return _err("Format invalide (md ou html).")
    if body.fmt == "html":
        try:
            import markdown  # noqa: F401
        except ImportError:
            return _missing()
    name = await asyncio.to_thread(_export, _clean_title(body.title), body.content, body.fmt)
    return {"filename": name}


@router.get("/file/{filename}")
async def get_file(filename: str):
    if not filename or "/" in filename or "\\" in filename or ".." in filename or "__" not in filename:
        return _err("Nom de fichier invalide")
    sub, real = filename.split("__", 1)
    path = TEMP_DIR / sub / real
    if not re.fullmatch(r"[0-9a-f]{8}", sub) or not path.is_file():
        return _err("Fichier introuvable", 404)
    mt = "text/markdown; charset=utf-8" if real.endswith(".md") else "text/html; charset=utf-8"
    return FileResponse(str(path), media_type=mt, filename=real, content_disposition_type="attachment")
