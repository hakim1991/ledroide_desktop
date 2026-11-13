import asyncio
import base64
import io
import json
import logging
import os
import re
import threading
import urllib.parse
import uuid
import webbrowser
from collections import OrderedDict
from datetime import datetime, timezone
from typing import Optional

from pydantic import BaseModel, Field, ValidationError, field_validator

from ledroide_sdk import Module
from ledroide_sdk.http import Query, Response, error

from epub_book import Epub, EpubError

log = logging.getLogger("ledroide.doc-reader")

module = Module(__file__)
router = module.router

EXTS = {".pdf": "pdf", ".epub": "epub"}
MAX_FOLDER_FILES = 5000
MAX_UPLOAD = 300 * 1024 * 1024
MAX_SEARCH_RESULTS = 500
COVER_W = 360
COLORS = {"yellow", "green", "blue", "pink", "orange"}
KINDS = {"bookmark", "highlight", "note"}

_ready = False
_lock = asyncio.Lock()
_pdf_lock = threading.RLock()
_pdf_cache: "OrderedDict[int, tuple[str, float, object]]" = OrderedDict()
_epub_cache: "OrderedDict[int, tuple[str, float, Epub]]" = OrderedDict()

SCHEMA = """
CREATE TABLE IF NOT EXISTS docs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    path        TEXT    NOT NULL UNIQUE,
    kind        TEXT    NOT NULL,
    title       TEXT    NOT NULL DEFAULT '',
    author      TEXT    NOT NULL DEFAULT '',
    pages       INTEGER NOT NULL DEFAULT 0,
    size        INTEGER NOT NULL DEFAULT 0,
    mtime       REAL    NOT NULL DEFAULT 0,
    has_cover   INTEGER NOT NULL DEFAULT 0,
    imported    INTEGER NOT NULL DEFAULT 0,
    position    TEXT    NOT NULL DEFAULT '{}',
    progress    REAL    NOT NULL DEFAULT 0,
    added_at    TEXT    NOT NULL,
    last_opened TEXT
);
CREATE TABLE IF NOT EXISTS collections (
    id    INTEGER PRIMARY KEY AUTOINCREMENT,
    name  TEXT NOT NULL UNIQUE COLLATE NOCASE
);
CREATE TABLE IF NOT EXISTS doc_collections (
    doc_id  INTEGER NOT NULL REFERENCES docs(id) ON DELETE CASCADE,
    coll_id INTEGER NOT NULL REFERENCES collections(id) ON DELETE CASCADE,
    PRIMARY KEY (doc_id, coll_id)
);
CREATE TABLE IF NOT EXISTS annotations (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    doc_id     INTEGER NOT NULL REFERENCES docs(id) ON DELETE CASCADE,
    kind       TEXT    NOT NULL,
    loc        TEXT    NOT NULL DEFAULT '{}',
    sort_key   REAL    NOT NULL DEFAULT 0,
    text       TEXT    NOT NULL DEFAULT '',
    note       TEXT    NOT NULL DEFAULT '',
    color      TEXT    NOT NULL DEFAULT 'yellow',
    created_at TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ann_doc ON annotations(doc_id);
"""


class DocError(Exception):
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


def _validation_message(exc: ValidationError) -> str:
    first = exc.errors()[0]
    loc = ".".join(str(p) for p in first.get("loc", ()))
    msg = str(first.get("msg", "Données invalides")).removeprefix("Value error, ")
    return f"{loc} : {msg}" if loc else msg


def _int(v, default: int = 0) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _covers_dir() -> str:
    path = module.data_path("covers")
    path.mkdir(parents=True, exist_ok=True)
    return str(path)


def _imports_dir() -> str:
    path = module.data_path("imported")
    path.mkdir(parents=True, exist_ok=True)
    return str(path)


def _cover_file(doc_id: int) -> str:
    return os.path.join(_covers_dir(), f"{doc_id}.jpg")


def _pdfium():
    try:
        import pypdfium2 as pdfium
    except ImportError:
        raise DocError("MISSING_DEP", "pypdfium2 n'est pas installé : installez les dépendances du module")
    return pdfium


def _pil():
    try:
        from PIL import Image
    except ImportError:
        raise DocError("MISSING_DEP", "Pillow n'est pas installé : installez les dépendances du module")
    return Image


async def _guard(fn, *args):
    try:
        return await asyncio.to_thread(fn, *args)
    except (DocError, EpubError) as exc:
        return _err(exc.message, exc.code)
    except OSError as exc:
        return _err(f"Fichier inaccessible : {exc.strerror or exc}", "IO_ERROR")


# ── Documents (thread side) ──────────────────────────────────────────────────

def _open_pdf(doc_id: int, path: str):
    mtime = os.path.getmtime(path)
    with _pdf_lock:
        hit = _pdf_cache.get(doc_id)
        if hit and hit[0] == path and hit[1] == mtime:
            _pdf_cache.move_to_end(doc_id)
            return hit[2]
        pdfium = _pdfium()
        try:
            pdf = pdfium.PdfDocument(path)
        except pdfium.PdfiumError as exc:
            raise DocError("BAD_PDF", f"PDF illisible ou protégé par mot de passe : {exc}")
        if hit:
            hit[2].close()
        _pdf_cache[doc_id] = (path, mtime, pdf)
        while len(_pdf_cache) > 4:
            _, (_, _, old) = _pdf_cache.popitem(last=False)
            old.close()
        return pdf


def _open_epub(doc_id: int, path: str) -> Epub:
    mtime = os.path.getmtime(path)
    with _pdf_lock:
        hit = _epub_cache.get(doc_id)
        if hit and hit[0] == path and hit[1] == mtime:
            _epub_cache.move_to_end(doc_id)
            return hit[2]
        book = Epub(path)
        if hit:
            hit[2].close()
        _epub_cache[doc_id] = (path, mtime, book)
        while len(_epub_cache) > 4:
            _, (_, _, old) = _epub_cache.popitem(last=False)
            old.close()
        return book


def _forget(doc_id: int):
    with _pdf_lock:
        for cache in (_pdf_cache, _epub_cache):
            hit = cache.pop(doc_id, None)
            if hit:
                hit[2].close()


def _save_cover(img, doc_id: int) -> bool:
    img = img.convert("RGB")
    if img.width > COVER_W:
        img = img.resize((COVER_W, max(1, round(img.height * COVER_W / img.width))))
    img.save(_cover_file(doc_id), "JPEG", quality=85)
    return True


def _probe(path: str) -> dict:
    kind = EXTS[os.path.splitext(path)[1].lower()]
    st = os.stat(path)
    info = {"kind": kind, "size": st.st_size, "mtime": st.st_mtime, "title": "", "author": "", "pages": 0}
    if kind == "pdf":
        pdfium = _pdfium()
        with _pdf_lock:
            try:
                pdf = pdfium.PdfDocument(path)
            except pdfium.PdfiumError as exc:
                raise DocError("BAD_PDF", f"PDF illisible ou protégé : {exc}")
            try:
                meta = pdf.get_metadata_dict(skip_empty=True)
                info.update(title=(meta.get("Title") or "").strip(), author=(meta.get("Author") or "").strip(),
                            pages=len(pdf))
            finally:
                pdf.close()
    else:
        book = Epub(path)
        try:
            info.update(title=book.title, author=book.author, pages=len(book.chapters))
        finally:
            book.close()
    if not info["title"]:
        info["title"] = os.path.splitext(os.path.basename(path))[0]
    info["title"], info["author"] = info["title"][:300], info["author"][:200]
    return info


def _make_cover(doc_id: int, path: str, kind: str) -> bool:
    try:
        Image = _pil()
        if kind == "pdf":
            with _pdf_lock:
                pdf = _pdfium().PdfDocument(path)
                try:
                    page = pdf[0]
                    w, _ = page.get_size()
                    img = page.render(scale=COVER_W / max(w, 1)).to_pil()
                    page.close()
                finally:
                    pdf.close()
            return _save_cover(img, doc_id)
        book = Epub(path)
        try:
            cover = book.cover_path()
            if not cover:
                return False
            img = Image.open(io.BytesIO(book.read(cover)))
            img.load()
            return _save_cover(img, doc_id)
        finally:
            book.close()
    except Exception as exc:
        log.info("cover failed for doc %s: %s", doc_id, exc)
        return False


def _check_path(raw: str) -> str:
    path = (raw or "").strip().strip('"')
    if not path:
        raise DocError("PATH_REQUIRED", "Indiquez le chemin d'un fichier ou d'un dossier")
    if not os.path.isabs(path):
        raise DocError("BAD_PATH", "Le chemin doit être absolu (ex. D:\\Livres)")
    if not os.path.exists(path):
        raise DocError("NOT_FOUND", f"Introuvable : {path}")
    return os.path.normpath(path)


def _list_files(path: str, recursive: bool) -> list[str]:
    if os.path.isfile(path):
        if os.path.splitext(path)[1].lower() not in EXTS:
            raise DocError("UNSUPPORTED_FORMAT", "Seuls les fichiers PDF et EPUB sont pris en charge")
        return [path]
    out = []
    for root, dirs, files in os.walk(path):
        dirs[:] = sorted(d for d in dirs if not d.startswith(".")) if recursive else []
        for f in sorted(files):
            if os.path.splitext(f)[1].lower() in EXTS:
                out.append(os.path.join(root, f))
                if len(out) > MAX_FOLDER_FILES:
                    raise DocError("TOO_MANY", f"Plus de {MAX_FOLDER_FILES} documents : choisissez un dossier plus précis")
    return out


# ── Library ──────────────────────────────────────────────────────────────────

def _doc_out(row: dict) -> dict:
    d = dict(row)
    d["position"] = json.loads(d.get("position") or "{}")
    d["has_cover"] = bool(d["has_cover"])
    d["imported"] = bool(d["imported"])
    d["exists"] = os.path.isfile(d["path"])
    d["collections"] = [int(x) for x in (d.pop("colls", "") or "").split(",") if x]
    return d


async def _get_doc(doc_id) -> Optional[dict]:
    return await (await _db()).fetch_one("SELECT * FROM docs WHERE id=?", (_int(doc_id),))


async def _add_file(path: str, imported: bool = False) -> tuple[str, Optional[int]]:
    db = await _db()
    norm = os.path.normcase(os.path.realpath(path))
    existing = await db.fetch_one("SELECT id FROM docs WHERE path=?", (norm,))
    if existing:
        return "exists", existing["id"]
    info = await asyncio.to_thread(_probe, path)
    row = await db.fetch_one(
        "INSERT INTO docs (path, kind, title, author, pages, size, mtime, imported, added_at)"
        " VALUES (?,?,?,?,?,?,?,?,?) RETURNING id",
        (norm, info["kind"], info["title"], info["author"], info["pages"], info["size"], info["mtime"],
         int(imported), _now()))
    if await asyncio.to_thread(_make_cover, row["id"], norm, info["kind"]):
        await db.execute("UPDATE docs SET has_cover=1 WHERE id=?", (row["id"],))
    return "added", row["id"]


@module.on("library.list")
async def library_list(payload: dict):
    db = await _db()
    docs = await db.fetch_all(
        "SELECT d.*, (SELECT GROUP_CONCAT(coll_id) FROM doc_collections WHERE doc_id=d.id) AS colls,"
        " (SELECT COUNT(*) FROM annotations WHERE doc_id=d.id) AS annotations FROM docs d")
    colls = await db.fetch_all(
        "SELECT c.id, c.name, (SELECT COUNT(*) FROM doc_collections WHERE coll_id=c.id) AS count"
        " FROM collections c ORDER BY c.name COLLATE NOCASE")
    return {"docs": [_doc_out(d) for d in docs], "collections": colls}


@module.job("library.add")
async def library_add(payload: dict, job):
    path = _check_path(str(payload.get("path") or ""))
    files = await asyncio.to_thread(_list_files, path, payload.get("recursive", True) is not False)
    if not files:
        raise DocError("EMPTY", "Aucun PDF ni EPUB trouvé à cet emplacement")
    added, existing, failed, ids = 0, 0, [], []
    for i, f in enumerate(files):
        await job.progress(round(i * 100 / len(files)), f"{i + 1}/{len(files)} — {os.path.basename(f)}")
        try:
            status, doc_id = await _add_file(f)
            ids.append(doc_id)
            if status == "added":
                added += 1
            else:
                existing += 1
        except (DocError, EpubError) as exc:
            failed.append({"path": f, "error": exc.message})
        except OSError as exc:
            failed.append({"path": f, "error": str(exc.strerror or exc)})
    await job.progress(100, "Terminé")
    return {"added": added, "existing": existing, "failed": failed[:50], "failed_count": len(failed), "ids": ids}


@module.on("library.upload")
async def library_upload(payload: dict):
    name = os.path.basename(str(payload.get("name") or "")).strip()
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name)[:150]
    ext = os.path.splitext(name)[1].lower()
    if ext not in EXTS:
        return _err("Seuls les fichiers PDF et EPUB sont pris en charge", "UNSUPPORTED_FORMAT")
    try:
        data = base64.b64decode(str(payload.get("data") or ""), validate=True)
    except ValueError:
        return _err("Données invalides", "BAD_DATA")
    if not data:
        return _err("Fichier vide", "BAD_DATA")
    if len(data) > MAX_UPLOAD:
        return _err("Fichier trop volumineux (300 Mo max.) : ajoutez-le par son chemin", "TOO_LARGE")
    folder = os.path.join(_imports_dir(), uuid.uuid4().hex[:12])
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, name)

    def write():
        with open(path, "wb") as f:
            f.write(data)
    await asyncio.to_thread(write)
    try:
        status, doc_id = await _add_file(path, imported=True)
    except (DocError, EpubError) as exc:
        await asyncio.to_thread(_rmtree, folder)
        return _err(exc.message, exc.code)
    return {"status": status, "id": doc_id}


def _rmtree(folder: str):
    import shutil
    shutil.rmtree(folder, ignore_errors=True)


class DocMetaIn(BaseModel):
    title: str = Field(min_length=1, max_length=300)
    author: str = Field("", max_length=200)

    @field_validator("title", "author")
    @classmethod
    def _strip(cls, v: str) -> str:
        return v.strip()


@module.on("doc.update")
async def doc_update(payload: dict):
    doc = await _get_doc(payload.get("id"))
    if not doc:
        return _err("Document introuvable", "NOT_FOUND")
    try:
        m = DocMetaIn(**{k: payload.get(k) for k in ("title", "author") if payload.get(k) is not None})
    except ValidationError as exc:
        return _err(_validation_message(exc))
    if not m.title:
        return _err("Le titre est requis")
    await (await _db()).execute("UPDATE docs SET title=?, author=? WHERE id=?", (m.title, m.author, doc["id"]))
    return {"ok": True}


@module.on("doc.remove")
async def doc_remove(payload: dict):
    doc = await _get_doc(payload.get("id"))
    if not doc:
        return _err("Document introuvable", "NOT_FOUND")
    _forget(doc["id"])
    await (await _db()).execute("DELETE FROM docs WHERE id=?", (doc["id"],))
    try:
        os.remove(_cover_file(doc["id"]))
    except OSError:
        pass
    if doc["imported"]:
        folder = os.path.dirname(doc["path"])
        if os.path.normcase(os.path.dirname(folder)) == os.path.normcase(os.path.realpath(_imports_dir())):
            await asyncio.to_thread(_rmtree, folder)
    return {"ok": True}


@module.on("doc.reset_progress")
async def doc_reset_progress(payload: dict):
    doc = await _get_doc(payload.get("id"))
    if not doc:
        return _err("Document introuvable", "NOT_FOUND")
    await (await _db()).execute("UPDATE docs SET position='{}', progress=0, last_opened=NULL WHERE id=?", (doc["id"],))
    return {"ok": True}


@module.on("doc.set_collections")
async def doc_set_collections(payload: dict):
    doc = await _get_doc(payload.get("id"))
    if not doc:
        return _err("Document introuvable", "NOT_FOUND")
    ids = payload.get("collections")
    if not isinstance(ids, list):
        return _err("collections : liste attendue")
    db = await _db()
    valid = {r["id"] for r in await db.fetch_all("SELECT id FROM collections")}
    await db.execute("DELETE FROM doc_collections WHERE doc_id=?", (doc["id"],))
    for cid in {_int(i) for i in ids} & valid:
        await db.execute("INSERT INTO doc_collections (doc_id, coll_id) VALUES (?,?)", (doc["id"], cid))
    return {"ok": True}


class CollectionIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)

    @field_validator("name")
    @classmethod
    def _strip(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("Le nom est requis")
        return v


@module.on("collection.save")
async def collection_save(payload: dict):
    try:
        c = CollectionIn(name=payload.get("name") or "")
    except ValidationError as exc:
        return _err(_validation_message(exc))
    db = await _db()
    clash = await db.fetch_one("SELECT id FROM collections WHERE name=? COLLATE NOCASE", (c.name,))
    cid = _int(payload.get("id"))
    if clash and clash["id"] != cid:
        return _err("Une collection porte déjà ce nom", "DUPLICATE")
    if cid:
        if not await db.execute("UPDATE collections SET name=? WHERE id=?", (c.name, cid)):
            return _err("Collection introuvable", "NOT_FOUND")
        return {"id": cid}
    row = await db.fetch_one("INSERT INTO collections (name) VALUES (?) RETURNING id", (c.name,))
    doc_id = _int(payload.get("doc_id"))
    if doc_id and await _get_doc(doc_id):
        await db.execute("INSERT OR IGNORE INTO doc_collections (doc_id, coll_id) VALUES (?,?)", (doc_id, row["id"]))
    return {"id": row["id"]}


@module.on("collection.delete")
async def collection_delete(payload: dict):
    if not await (await _db()).execute("DELETE FROM collections WHERE id=?", (_int(payload.get("id")),)):
        return _err("Collection introuvable", "NOT_FOUND")
    return {"ok": True}


# ── Reading ──────────────────────────────────────────────────────────────────

def _pdf_meta(doc_id: int, path: str) -> dict:
    pdf = _open_pdf(doc_id, path)
    with _pdf_lock:
        sizes = []
        for i in range(len(pdf)):
            w, h = pdf.get_page_size(i)
            sizes.append([round(w, 2), round(h, 2)])
        toc = []
        try:
            for bm in pdf.get_toc(max_depth=8):
                dest = bm.get_dest()
                idx = dest.get_index() if dest is not None else None
                title = (bm.get_title() or "").strip()
                if title and idx is not None:
                    toc.append({"title": title[:300], "page": idx, "level": bm.level})
                if len(toc) >= 2000:
                    break
        except Exception as exc:
            log.info("toc failed for doc %s: %s", doc_id, exc)
    return {"pages": len(sizes), "sizes": sizes, "toc": toc}


def _epub_meta(doc_id: int, path: str) -> dict:
    book = _open_epub(doc_id, path)
    with _pdf_lock:
        return {"chapters": book.chapter_titles(), "toc": book.toc[:2000]}


@module.on("doc.open")
async def doc_open(payload: dict):
    doc = await _get_doc(payload.get("id"))
    if not doc:
        return _err("Document introuvable", "NOT_FOUND")
    if not os.path.isfile(doc["path"]):
        return _err(f"Fichier introuvable : {doc['path']}", "FILE_MISSING")
    meta = await _guard(_pdf_meta if doc["kind"] == "pdf" else _epub_meta, doc["id"], doc["path"])
    if "error" in meta:
        return meta
    db = await _db()
    count = meta.get("pages") or len(meta.get("chapters") or [])
    await db.execute("UPDATE docs SET last_opened=?, pages=? WHERE id=?", (_now(), count, doc["id"]))
    out = _doc_out({**doc, "colls": ""})
    out.update(meta)
    return out


@module.on("doc.position")
async def doc_position(payload: dict):
    doc = await _get_doc(payload.get("id"))
    if not doc:
        return _err("Document introuvable", "NOT_FOUND")
    pos = payload.get("position")
    if not isinstance(pos, dict):
        return _err("position : objet attendu")
    clean = {k: (float(v) if isinstance(v, float) else _int(v)) for k, v in pos.items()
             if k in ("page", "chapter", "scroll", "offset") and isinstance(v, (int, float))}
    try:
        progress = max(0.0, min(1.0, float(payload.get("progress") or 0)))
    except (TypeError, ValueError):
        progress = 0.0
    await (await _db()).execute("UPDATE docs SET position=?, progress=?, last_opened=? WHERE id=?",
                                (json.dumps(clean), progress, _now(), doc["id"]))
    return {"ok": True}


def _pdf_search(doc_id: int, path: str, query: str) -> dict:
    pdf = _open_pdf(doc_id, path)
    results, truncated = [], False
    with _pdf_lock:
        for pno in range(len(pdf)):
            page = pdf[pno]
            w, h = page.get_size()
            tp = page.get_textpage()
            try:
                searcher = tp.search(query, match_case=False)
                while True:
                    occ = searcher.get_next()
                    if not occ:
                        break
                    idx, count = occ
                    rects = []
                    for r in range(tp.count_rects(idx, count)):
                        left, bottom, right, top = tp.get_rect(r)
                        rects.append([round(left / w, 5), round(1 - top / h, 5),
                                      round(right / w, 5), round(1 - bottom / h, 5)])
                    start = max(0, idx - 50)
                    ctx = tp.get_text_range(start, min(count + 100, tp.count_chars() - start))
                    results.append({"page": pno, "rects": rects, "context": re.sub(r"\s+", " ", ctx).strip(),
                                    "offset": idx - start})
                    if len(results) >= MAX_SEARCH_RESULTS:
                        truncated = True
                        break
                searcher.close()
            finally:
                tp.close()
                page.close()
            if truncated:
                break
    return {"results": results, "truncated": truncated}


def _epub_search(doc_id: int, path: str, query: str) -> dict:
    book = _open_epub(doc_id, path)
    results, truncated = [], False
    needle = query.lower()
    with _pdf_lock:
        for ci in range(len(book.chapters)):
            html_text = book.chapter_html(ci, lambda p: "")
            text = re.sub(r"\s+", " ", _strip_tags(html_text))
            low = text.lower()
            start, n = 0, 0
            while True:
                i = low.find(needle, start)
                if i < 0:
                    break
                a = max(0, i - 50)
                results.append({"chapter": ci, "occurrence": n, "context": text[a:i + len(query) + 50],
                                "offset": i - a})
                n += 1
                start = i + max(1, len(needle))
                if len(results) >= MAX_SEARCH_RESULTS:
                    truncated = True
                    break
            if truncated:
                break
    return {"results": results, "truncated": truncated}


def _strip_tags(fragment: str) -> str:
    import html as _html
    return _html.unescape(re.sub(r"<[^>]+>", "", fragment))


@module.on("doc.search")
async def doc_search(payload: dict):
    doc = await _get_doc(payload.get("id"))
    if not doc:
        return _err("Document introuvable", "NOT_FOUND")
    query = str(payload.get("query") or "").strip()
    if len(query) < 2:
        return _err("Saisissez au moins 2 caractères", "QUERY_TOO_SHORT")
    if len(query) > 200:
        return _err("Recherche trop longue (200 caractères max.)")
    return await _guard(_pdf_search if doc["kind"] == "pdf" else _epub_search, doc["id"], doc["path"], query)


def _pdf_text_in(doc_id: int, path: str, page_no: int, rect: list[float]) -> dict:
    pdf = _open_pdf(doc_id, path)
    with _pdf_lock:
        if not 0 <= page_no < len(pdf):
            raise DocError("BAD_PAGE", "Page inexistante")
        page = pdf[page_no]
        w, h = page.get_size()
        tp = page.get_textpage()
        try:
            x0, y0, x1, y1 = rect
            text = tp.get_text_bounded(left=x0 * w, bottom=(1 - y1) * h, right=x1 * w, top=(1 - y0) * h)
        finally:
            tp.close()
            page.close()
    return {"text": re.sub(r"[ \t]+", " ", text or "").strip()[:5000]}


@module.on("pdf.text_in")
async def pdf_text_in(payload: dict):
    doc = await _get_doc(payload.get("id"))
    if not doc or doc["kind"] != "pdf":
        return _err("Document PDF introuvable", "NOT_FOUND")
    rect = payload.get("rect")
    if not (isinstance(rect, list) and len(rect) == 4 and all(isinstance(v, (int, float)) for v in rect)):
        return _err("rect : [x0, y0, x1, y1] attendu")
    rect = [max(0.0, min(1.0, float(v))) for v in rect]
    return await _guard(_pdf_text_in, doc["id"], doc["path"], _int(payload.get("page")), rect)


def _chapter(doc_id: int, path: str, index: int) -> dict:
    book = _open_epub(doc_id, path)
    with _pdf_lock:
        if not 0 <= index < len(book.chapters):
            raise DocError("BAD_CHAPTER", "Chapitre inexistant")

        def res_url(p: str) -> str:
            return f"/modules/{module.id}/api/res/{doc_id}?p={urllib.parse.quote(p)}"
        return {"index": index, "html": book.chapter_html(index, res_url), "count": len(book.chapters)}


@module.on("epub.chapter")
async def epub_chapter(payload: dict):
    doc = await _get_doc(payload.get("id"))
    if not doc or doc["kind"] != "epub":
        return _err("Document EPUB introuvable", "NOT_FOUND")
    return await _guard(_chapter, doc["id"], doc["path"], _int(payload.get("index"), -1))


@module.on("open_url")
async def open_url(payload: dict):
    url = str(payload.get("url") or "").strip()
    if not re.match(r"^https?://[^\s]+$", url, re.I) or len(url) > 2000:
        return _err("Adresse refusée (http/https uniquement)", "BAD_URL")
    await asyncio.to_thread(webbrowser.open, url)
    return {"ok": True}


# ── Annotations ──────────────────────────────────────────────────────────────

class AnnotationIn(BaseModel):
    kind: str
    loc: dict
    text: str = Field("", max_length=10000)
    note: str = Field("", max_length=20000)
    color: str = "yellow"

    @field_validator("kind")
    @classmethod
    def _kind(cls, v: str) -> str:
        if v not in KINDS:
            raise ValueError("Type inconnu (bookmark, highlight, note)")
        return v

    @field_validator("color")
    @classmethod
    def _color(cls, v: str) -> str:
        if v not in COLORS:
            raise ValueError("Couleur inconnue")
        return v


def _clean_loc(kind: str, doc_kind: str, loc: dict) -> dict:
    out: dict = {}
    if doc_kind == "pdf":
        out["page"] = max(0, _int(loc.get("page")))
        rects = loc.get("rects") or []
        if isinstance(rects, list):
            out["rects"] = [[max(0.0, min(1.0, float(v))) for v in r] for r in rects[:50]
                            if isinstance(r, list) and len(r) == 4 and all(isinstance(v, (int, float)) for v in r)]
        if isinstance(loc.get("offset"), (int, float)):
            out["offset"] = max(0.0, min(1.0, float(loc["offset"])))
    else:
        out["chapter"] = max(0, _int(loc.get("chapter")))
        for k in ("start", "end"):
            if isinstance(loc.get(k), int) and loc[k] >= 0:
                out[k] = loc[k]
        if isinstance(loc.get("scroll"), (int, float)):
            out["scroll"] = max(0.0, min(1.0, float(loc["scroll"])))
    return out


def _sort_key(doc_kind: str, loc: dict) -> float:
    if doc_kind == "pdf":
        y = loc["rects"][0][1] if loc.get("rects") else loc.get("offset", 0)
        return loc["page"] + min(0.999, y)
    frac = (loc.get("start", 0) / 1e7) if "start" in loc else loc.get("scroll", 0) * 0.999
    return loc["chapter"] + min(0.999, frac)


def _ann_out(r: dict) -> dict:
    return {**r, "loc": json.loads(r["loc"] or "{}")}


@module.on("annotations.list")
async def annotations_list(payload: dict):
    doc = await _get_doc(payload.get("doc_id"))
    if not doc:
        return _err("Document introuvable", "NOT_FOUND")
    rows = await (await _db()).fetch_all("SELECT * FROM annotations WHERE doc_id=? ORDER BY sort_key, id", (doc["id"],))
    return {"annotations": [_ann_out(r) for r in rows]}


@module.on("annotation.save")
async def annotation_save(payload: dict):
    db = await _db()
    ann_id = _int(payload.get("id"))
    if ann_id:
        old = await db.fetch_one("SELECT * FROM annotations WHERE id=?", (ann_id,))
        if not old:
            return _err("Annotation introuvable", "NOT_FOUND")
        note = payload.get("note", old["note"])
        color = payload.get("color", old["color"])
        if not isinstance(note, str) or len(note) > 20000:
            return _err("note : texte de 20 000 caractères maximum")
        if color not in COLORS:
            return _err("Couleur inconnue")
        await db.execute("UPDATE annotations SET note=?, color=? WHERE id=?", (note, color, ann_id))
        return _ann_out(await db.fetch_one("SELECT * FROM annotations WHERE id=?", (ann_id,)))
    doc = await _get_doc(payload.get("doc_id"))
    if not doc:
        return _err("Document introuvable", "NOT_FOUND")
    try:
        a = AnnotationIn(**{k: payload[k] for k in ("kind", "loc", "text", "note", "color") if k in payload})
    except ValidationError as exc:
        return _err(_validation_message(exc))
    loc = _clean_loc(a.kind, doc["kind"], a.loc)
    if a.kind == "highlight" and doc["kind"] == "pdf" and not loc.get("rects"):
        return _err("Surlignage sans zone")
    if a.kind == "highlight" and doc["kind"] == "epub" and not ("start" in loc and "end" in loc and loc["end"] > loc["start"]):
        return _err("Surlignage sans sélection")
    if a.kind == "note" and not a.note.strip():
        return _err("La note est vide")
    row = await db.fetch_one(
        "INSERT INTO annotations (doc_id, kind, loc, sort_key, text, note, color, created_at)"
        " VALUES (?,?,?,?,?,?,?,?) RETURNING *",
        (doc["id"], a.kind, json.dumps(loc), _sort_key(doc["kind"], loc), a.text.strip(), a.note.strip(), a.color, _now()))
    return _ann_out(row)


@module.on("annotation.delete")
async def annotation_delete(payload: dict):
    if not await (await _db()).execute("DELETE FROM annotations WHERE id=?", (_int(payload.get("id")),)):
        return _err("Annotation introuvable", "NOT_FOUND")
    return {"ok": True}


def _md_quote(text: str) -> str:
    return "\n".join("> " + line for line in text.strip().splitlines() if line.strip())


@module.on("annotations.export")
async def annotations_export(payload: dict):
    doc = await _get_doc(payload.get("doc_id"))
    if not doc:
        return _err("Document introuvable", "NOT_FOUND")
    rows = await (await _db()).fetch_all("SELECT * FROM annotations WHERE doc_id=? ORDER BY sort_key, id", (doc["id"],))
    chapters: list[str] = []
    if doc["kind"] == "epub" and os.path.isfile(doc["path"]):
        meta = await _guard(_epub_meta, doc["id"], doc["path"])
        chapters = meta.get("chapters") or []

    def where(loc: dict) -> str:
        if doc["kind"] == "pdf":
            return f"Page {loc.get('page', 0) + 1}"
        c = loc.get("chapter", 0)
        return chapters[c] if c < len(chapters) else f"Section {c + 1}"

    lines = [f"# {doc['title']}", ""]
    if doc["author"]:
        lines += [f"*{doc['author']}*", ""]
    lines += [f"Exporté le {datetime.now().strftime('%d/%m/%Y à %H:%M')} — {len(rows)} annotation(s)", ""]
    marks = [r for r in rows if r["kind"] == "bookmark"]
    others = [r for r in rows if r["kind"] != "bookmark"]
    if marks:
        lines += ["## Marque-pages", ""]
        for r in marks:
            loc = json.loads(r["loc"])
            label = r["note"] or r["text"]
            lines.append(f"- {where(loc)}" + (f" — {label}" if label else ""))
        lines.append("")
    if others:
        lines += ["## Surlignages et notes", ""]
        current = None
        for r in others:
            loc = json.loads(r["loc"])
            w = where(loc)
            if w != current:
                lines += [f"### {w}", ""]
                current = w
            if r["text"]:
                lines += [_md_quote(r["text"]), ""]
            if r["note"]:
                lines += [("**Note :** " if r["text"] else "") + r["note"].strip(), ""]
    if not rows:
        lines += ["*Aucune annotation.*", ""]
    safe = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", doc["title"]).strip()[:80] or "notes"
    return {"markdown": "\n".join(lines).rstrip() + "\n", "filename": f"{safe} - notes.md"}


# ── HTTP routes (images) ─────────────────────────────────────────────────────

def _render(doc_id: int, path: str, index: int, scale: float) -> bytes:
    pdf = _open_pdf(doc_id, path)
    with _pdf_lock:
        if not 0 <= index < len(pdf):
            raise DocError("BAD_PAGE", "Page inexistante")
        page = pdf[index]
        try:
            img = page.render(scale=scale, may_draw_forms=True).to_pil()
        finally:
            page.close()
    buf = io.BytesIO()
    img.convert("RGB").save(buf, "JPEG", quality=90)
    return buf.getvalue()


async def _route_doc(doc_id: int, kind: str) -> dict:
    doc = await _get_doc(doc_id)
    if not doc or doc["kind"] != kind or not os.path.isfile(doc["path"]):
        raise error(404, "Document introuvable", "NOT_FOUND")
    return doc


@router.get("/page/{doc_id}/{index}")
async def page_image(doc_id: int, index: int, scale: float = Query(1.5, ge=0.1, le=6)):
    doc = await _route_doc(doc_id, "pdf")
    scale = round(scale * 8) / 8 or 0.125
    try:
        data = await asyncio.to_thread(_render, doc["id"], doc["path"], index, scale)
    except DocError as exc:
        raise error(404 if exc.code == "BAD_PAGE" else 422, exc.message, exc.code)
    return Response(data, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=600"})


@router.get("/cover/{doc_id}")
async def cover_image(doc_id: int):
    path = _cover_file(doc_id)
    doc = await _get_doc(doc_id)
    if not doc or not os.path.isfile(path):
        raise error(404, "Couverture introuvable", "NOT_FOUND")
    data = await asyncio.to_thread(lambda: open(path, "rb").read())
    return Response(data, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=300"})


@router.get("/res/{doc_id}")
async def epub_resource(doc_id: int, p: str = Query("", max_length=1000)):
    doc = await _route_doc(doc_id, "epub")

    def read():
        book = _open_epub(doc["id"], doc["path"])
        with _pdf_lock:
            return book.resource(p)
    try:
        data, mime = await asyncio.to_thread(read)
    except EpubError as exc:
        raise error(404, exc.message, exc.code)
    return Response(data, media_type=mime, headers={"Cache-Control": "private, max-age=600",
                                                    "Content-Security-Policy": "default-src 'none'"})


@module.on_shutdown
async def shutdown():
    for doc_id in list(_pdf_cache) + list(_epub_cache):
        _forget(doc_id)
