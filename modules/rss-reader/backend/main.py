import asyncio
import email.utils
import hashlib
import html
import html.entities
import logging
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from html.parser import HTMLParser

from ledroide_sdk import Module

from sanitize import sanitize_html, strip_tags

log = logging.getLogger("ledroide.rss-reader")

module = Module(__file__)
router = module.router

USER_AGENT = "Mozilla/5.0 (compatible; LedroideRSS/1.0)"
MAX_BYTES = 10 * 1024 * 1024
FEED_TYPES = ("application/rss+xml", "application/atom+xml", "application/rdf+xml", "application/xml", "text/xml")
XML_ENTITIES = {"amp", "lt", "gt", "quot", "apos"}

_schema_ready = False
_auto_task: asyncio.Task | None = None
_refresh_lock: asyncio.Lock | None = None


class FeedError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _err(message: str, code: str) -> dict:
    return {"error": message, "code": code}


# ── HTTP ─────────────────────────────────────────────────────────────────────

class _SafeRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if urllib.parse.urlsplit(newurl).scheme.lower() not in ("http", "https"):
            raise FeedError("BAD_REDIRECT", "Redirection refusée (schéma non http/https)")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def normalize_url(raw: str) -> str:
    url = (raw or "").strip()
    if not url:
        raise FeedError("BAD_URL", "Adresse vide")
    if "://" not in url:
        url = "https://" + url
    parts = urllib.parse.urlsplit(url)
    if parts.scheme.lower() not in ("http", "https"):
        raise FeedError("BAD_URL", "Seules les adresses http:// et https:// sont acceptées")
    if not parts.hostname:
        raise FeedError("BAD_URL", "Adresse invalide : hôte manquant")
    return urllib.parse.quote(url, safe=":/?#[]@!$&'()*+,;=%~")


def fetch(url: str, timeout: float, etag: str | None = None, modified: str | None = None) -> dict:
    req = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT,
        "Accept": "application/rss+xml, application/atom+xml, application/xml;q=0.9, text/xml;q=0.9, text/html;q=0.8, */*;q=0.5",
    })
    if etag:
        req.add_header("If-None-Match", etag)
    if modified:
        req.add_header("If-Modified-Since", modified)
    opener = urllib.request.build_opener(_SafeRedirects())
    try:
        with opener.open(req, timeout=timeout) as resp:
            data = resp.read(MAX_BYTES + 1)
            if len(data) > MAX_BYTES:
                raise FeedError("TOO_LARGE", "Flux trop volumineux (10 Mo maximum)")
            return {"status": resp.status, "data": data, "url": resp.geturl(),
                    "content_type": resp.headers.get("Content-Type", ""),
                    "etag": resp.headers.get("ETag"), "modified": resp.headers.get("Last-Modified")}
    except urllib.error.HTTPError as exc:
        if exc.code == 304:
            return {"status": 304, "data": b"", "url": url, "content_type": "", "etag": etag, "modified": modified}
        raise FeedError("HTTP_ERROR", f"Le serveur a répondu {exc.code} {exc.reason}")
    except FeedError:
        raise
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, TimeoutError):
            raise FeedError("TIMEOUT", f"Délai dépassé ({timeout:g} s)")
        raise FeedError("NETWORK_ERROR", f"Connexion impossible : {exc.reason}")
    except TimeoutError:
        raise FeedError("TIMEOUT", f"Délai dépassé ({timeout:g} s)")
    except (OSError, ValueError) as exc:
        raise FeedError("NETWORK_ERROR", f"Erreur réseau : {exc}")


# ── Parsing ──────────────────────────────────────────────────────────────────

def _local(tag) -> str:
    return tag.rsplit("}", 1)[-1].lower() if isinstance(tag, str) else ""


def _child(el, *names):
    for c in el:
        if _local(c.tag) in names:
            return c
    return None


def _text(el, *names) -> str:
    c = _child(el, *names) if names else el
    if c is None:
        return ""
    return "".join(c.itertext()).strip()


def _fix_entities(data: bytes) -> bytes:
    def repl(m):
        name = m.group(1).decode()
        if name in XML_ENTITIES:
            return m.group(0)
        cp = html.entities.name2codepoint.get(name)
        return f"&#{cp};".encode() if cp else b"&amp;" + m.group(1) + b";"
    return re.sub(rb"&([A-Za-z][A-Za-z0-9]{1,31});", repl, data)


def _parse_xml(data: bytes):
    if re.search(rb"<!ENTITY", data[:4096], re.I):
        raise FeedError("NOT_A_FEED", "Document XML refusé (déclarations d'entités)")
    data = re.sub(rb"<!DOCTYPE[^>\[]*(\[[^\]]*\])?\s*>", b"", data, count=1, flags=re.I)
    try:
        return ET.fromstring(data)
    except ET.ParseError:
        try:
            return ET.fromstring(_fix_entities(data))
        except ET.ParseError as exc:
            raise FeedError("NOT_A_FEED", f"XML invalide : {exc}")


def _date(value: str) -> float | None:
    value = (value or "").strip()
    if not value:
        return None
    try:
        dt = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError):
        dt = None
    if dt is None:
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def _atom_content(el) -> str:
    if el is None:
        return ""
    kind = (el.get("type") or "text").lower()
    if kind in ("xhtml",) or (len(el) and kind not in ("text", "html")):
        inner = (el.text or "") + "".join(ET.tostring(c, encoding="unicode") for c in el)
        return re.sub(r"\s?xmlns(:\w+)?=\"[^\"]*\"", "", re.sub(r"<(/?)\w+:", r"<\1", inner))
    text = el.text or ""
    return text if kind in ("html", "text/html") else html.escape(text).replace("\n", "<br>")


def _atom_link(el, rel=("alternate", "")) -> str:
    best = ""
    for c in el:
        if _local(c.tag) == "link":
            r = (c.get("rel") or "").lower()
            if r in rel and c.get("href"):
                if r == "alternate" or not best:
                    best = c.get("href")
    return best


def _make_guid(*parts) -> str:
    return hashlib.sha1("|".join(p or "" for p in parts).encode("utf-8")).hexdigest()


def parse_feed(data: bytes, base_url: str) -> dict:
    root = _parse_xml(data)
    kind = _local(root.tag)
    items = []
    if kind == "feed":
        title = _text(root, "title")
        site = urllib.parse.urljoin(base_url, _atom_link(root))
        for e in root:
            if _local(e.tag) != "entry":
                continue
            link = _atom_link(e)
            link = urllib.parse.urljoin(base_url, link) if link else ""
            content = _atom_content(_child(e, "content")) or _atom_content(_child(e, "summary"))
            author = _child(e, "author")
            title_el = _child(e, "title")
            t = strip_tags(_atom_content(title_el)) if title_el is not None else ""
            pub = _date(_text(e, "published")) or _date(_text(e, "updated"))
            items.append({"guid": _text(e, "id") or link or _make_guid(t, content[:200]), "title": t, "link": link,
                          "author": _text(author, "name") if author is not None else "",
                          "published": pub, "content": content})
    elif kind in ("rss", "rdf"):
        channel = _child(root, "channel")
        if channel is None:
            raise FeedError("NOT_A_FEED", "Flux RSS sans élément <channel>")
        title = _text(channel, "title")
        site = _text(channel, "link")
        entries = [c for c in (channel if kind == "rss" else root) if _local(c.tag) == "item"]
        for e in entries:
            link = _text(e, "link")
            guid_el = _child(e, "guid")
            if not link and guid_el is not None and guid_el.get("isPermaLink", "true") != "false":
                candidate = (guid_el.text or "").strip()
                link = candidate if candidate.startswith(("http://", "https://")) else ""
            link = urllib.parse.urljoin(base_url, link) if link else ""
            content = _text(e, "encoded") or _text(e, "description")
            enclosure = _child(e, "enclosure")
            if enclosure is not None and enclosure.get("url"):
                etype = enclosure.get("type") or "fichier"
                content += f'<p><a href="{html.escape(enclosure.get("url"))}">Pièce jointe ({html.escape(etype)})</a></p>'
            t = strip_tags(_text(e, "title"))
            pub = _date(_text(e, "pubdate")) or _date(_text(e, "date"))
            items.append({"guid": _text(e, "guid") or link or _make_guid(t, content[:200]), "title": t, "link": link,
                          "author": _text(e, "creator") or _text(e, "author"), "published": pub, "content": content})
    else:
        raise FeedError("NOT_A_FEED", "Ce document n'est ni un flux RSS ni un flux Atom")
    return {"title": strip_tags(title) or urllib.parse.urlsplit(base_url).hostname or base_url,
            "site_url": site if site.startswith(("http://", "https://")) else "", "items": items}


class _LinkFinder(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.links: list[tuple[str, str, str]] = []

    def handle_starttag(self, tag, attrs):
        if tag != "link":
            return
        a = {k.lower(): (v or "") for k, v in attrs}
        rels = a.get("rel", "").lower().split()
        t = a.get("type", "").lower()
        if "alternate" in rels and t in ("application/rss+xml", "application/atom+xml", "application/rdf+xml") and a.get("href"):
            self.links.append((a["href"], t, a.get("title", "")))


def discover_links(page: bytes, base: str) -> list[str]:
    finder = _LinkFinder()
    try:
        finder.feed(page[:2 * 1024 * 1024].decode("utf-8", "replace"))
    except Exception:
        pass
    seen, out = set(), []
    for href, _t, _title in finder.links:
        url = urllib.parse.urljoin(base, href)
        if urllib.parse.urlsplit(url).scheme in ("http", "https") and url not in seen:
            seen.add(url)
            out.append(url)
    return out


def _looks_like_html(resp: dict) -> bool:
    ct = resp["content_type"].lower()
    head = resp["data"][:1024].lstrip().lower()
    return "html" in ct or head.startswith((b"<!doctype html", b"<html"))


def resolve_feed(url: str, timeout: float) -> tuple[str, dict, dict]:
    resp = fetch(url, timeout)
    if _looks_like_html(resp):
        links = discover_links(resp["data"], resp["url"])
        if not links:
            raise FeedError("NO_FEED_FOUND", "Aucun flux RSS/Atom trouvé sur cette page")
        url = links[0]
        resp = fetch(url, timeout)
    feed = parse_feed(resp["data"], resp["url"])
    return resp["url"], resp, feed


# ── Storage ──────────────────────────────────────────────────────────────────

async def _ensure_schema():
    global _schema_ready
    if _schema_ready:
        return
    await module.db.executescript("""
        CREATE TABLE IF NOT EXISTS feeds (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            url TEXT NOT NULL UNIQUE,
            title TEXT, site_url TEXT, folder TEXT NOT NULL DEFAULT '',
            last_fetch REAL, last_error TEXT, etag TEXT, last_modified TEXT, created REAL
        );
        CREATE TABLE IF NOT EXISTS items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            feed_id INTEGER NOT NULL REFERENCES feeds(id) ON DELETE CASCADE,
            guid TEXT NOT NULL, title TEXT, link TEXT, author TEXT,
            published REAL, content TEXT, summary TEXT,
            read INTEGER NOT NULL DEFAULT 0, starred INTEGER NOT NULL DEFAULT 0, fetched_at REAL,
            UNIQUE(feed_id, guid)
        );
        CREATE INDEX IF NOT EXISTS items_feed ON items(feed_id, published);
        CREATE INDEX IF NOT EXISTS items_read ON items(read);
    """)
    _schema_ready = True
    _ensure_auto()


async def _store_items(feed_id: int, base: str, items: list[dict]) -> int:
    now = time.time()
    new = 0
    for it in items:
        content = sanitize_html(it["content"], it["link"] or base)
        summary = strip_tags(content)[:400]
        n = await module.db.execute(
            "INSERT OR IGNORE INTO items(feed_id, guid, title, link, author, published, content, summary, fetched_at) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (feed_id, it["guid"][:500], it["title"][:500] or "(sans titre)", it["link"][:2000], (it["author"] or "")[:200],
             it["published"] or now, content, summary, now))
        new += max(n, 0)
    keep = int(module.config.get("max_items_per_feed", 500))
    await module.db.execute(
        "DELETE FROM items WHERE feed_id=? AND starred=0 AND id NOT IN "
        "(SELECT id FROM items WHERE feed_id=? ORDER BY published DESC, id DESC LIMIT ?)", (feed_id, feed_id, keep))
    return new


def _timeout() -> float:
    return float(module.config.get("fetch_timeout", 20))


async def refresh_feed(feed: dict) -> dict:
    try:
        resp = await asyncio.to_thread(fetch, feed["url"], _timeout(), feed.get("etag"), feed.get("last_modified"))
        if resp["status"] == 304:
            await module.db.execute("UPDATE feeds SET last_fetch=?, last_error=NULL WHERE id=?", (time.time(), feed["id"]))
            return {"id": feed["id"], "new": 0, "not_modified": True}
        parsed = await asyncio.to_thread(parse_feed, resp["data"], resp["url"])
    except FeedError as exc:
        await module.db.execute("UPDATE feeds SET last_fetch=?, last_error=? WHERE id=?", (time.time(), exc.message, feed["id"]))
        return {"id": feed["id"], "new": 0, "error": exc.message}
    new = await _store_items(feed["id"], resp["url"], parsed["items"])
    await module.db.execute(
        "UPDATE feeds SET last_fetch=?, last_error=NULL, etag=?, last_modified=?, "
        "title=COALESCE(NULLIF(title, ''), ?), site_url=COALESCE(NULLIF(?, ''), site_url) WHERE id=?",
        (time.time(), resp.get("etag"), resp.get("modified"), parsed["title"], parsed["site_url"], feed["id"]))
    return {"id": feed["id"], "new": new}


async def refresh_many(feeds: list[dict], progress=None) -> dict:
    global _refresh_lock
    if _refresh_lock is None:
        _refresh_lock = asyncio.Lock()
    async with _refresh_lock:
        sem = asyncio.Semaphore(4)
        done, total_new, errors = 0, 0, []

        async def one(f):
            nonlocal done, total_new
            async with sem:
                r = await refresh_feed(f)
            done += 1
            total_new += r["new"]
            if r.get("error"):
                errors.append({"id": f["id"], "title": f.get("title") or f["url"], "error": r["error"]})
            if progress:
                await progress(done, len(feeds), f.get("title") or f["url"])
        await asyncio.gather(*(one(f) for f in feeds))
    if feeds:
        await module.events.emit("refreshed", {"new": total_new, "errors": len(errors)})
    return {"feeds": len(feeds), "new": total_new, "errors": errors}


# ── Auto-refresh ─────────────────────────────────────────────────────────────

async def _auto_loop():
    await asyncio.sleep(20)
    while True:
        minutes = int(module.config.get("refresh_minutes", 30) or 0)
        if minutes > 0:
            try:
                await _ensure_schema()
                now = time.time()
                feeds = await module.db.fetch_all("SELECT * FROM feeds")
                due = [f for f in feeds if not f["last_fetch"] or now - f["last_fetch"] >= minutes * 60 - 5]
                if due:
                    r = await refresh_many(due)
                    log.info("auto-refresh: %d flux, %d nouveaux articles", r["feeds"], r["new"])
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("auto-refresh failed")
        await asyncio.sleep(60)


def _ensure_auto():
    global _auto_task
    if _auto_task is not None and not _auto_task.done():
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    _auto_task = loop.create_task(_auto_loop())


_ensure_auto()


# ── Actions : flux ───────────────────────────────────────────────────────────

FEED_COLS = ("f.id, f.url, f.title, f.site_url, f.folder, f.last_fetch, f.last_error, "
             "(SELECT COUNT(*) FROM items i WHERE i.feed_id=f.id AND i.read=0) AS unread, "
             "(SELECT COUNT(*) FROM items i WHERE i.feed_id=f.id) AS total")


@module.on("feeds_list")
async def feeds_list(payload: dict):
    await _ensure_schema()
    feeds = await module.db.fetch_all(f"SELECT {FEED_COLS} FROM feeds f ORDER BY LOWER(f.folder), LOWER(f.title)")
    totals = await module.db.fetch_one(
        "SELECT (SELECT COUNT(*) FROM items WHERE read=0) AS unread, (SELECT COUNT(*) FROM items WHERE starred=1) AS starred, "
        "(SELECT COUNT(*) FROM items) AS total")
    return {"feeds": feeds, "totals": totals,
            "refresh_minutes": int(module.config.get("refresh_minutes", 30) or 0)}


@module.on("feed_add")
async def feed_add(payload: dict):
    await _ensure_schema()
    try:
        url = normalize_url(str(payload.get("url", "")))
        existing = await module.db.fetch_one("SELECT id FROM feeds WHERE url=?", (url,))
        if existing:
            return _err("Ce flux est déjà abonné", "DUPLICATE")
        final_url, resp, parsed = await asyncio.to_thread(resolve_feed, url, _timeout())
    except FeedError as exc:
        return _err(exc.message, exc.code)
    if await module.db.fetch_one("SELECT id FROM feeds WHERE url=?", (final_url,)):
        return _err("Ce flux est déjà abonné", "DUPLICATE")
    folder = str(payload.get("folder") or "").strip()[:80]
    title = str(payload.get("title") or "").strip()[:200] or parsed["title"]
    await module.db.execute(
        "INSERT INTO feeds(url, title, site_url, folder, last_fetch, etag, last_modified, created) VALUES (?,?,?,?,?,?,?,?)",
        (final_url, title, parsed["site_url"], folder, time.time(), resp.get("etag"), resp.get("modified"), time.time()))
    feed = await module.db.fetch_one("SELECT id FROM feeds WHERE url=?", (final_url,))
    new = await _store_items(feed["id"], final_url, parsed["items"])
    log.info("feed added (%d articles)", new)
    row = await module.db.fetch_one(f"SELECT {FEED_COLS} FROM feeds f WHERE f.id=?", (feed["id"],))
    return {"feed": row, "new": new, "discovered": final_url != url}


@module.on("feed_update")
async def feed_update(payload: dict):
    await _ensure_schema()
    try:
        feed_id = int(payload.get("id"))
    except (TypeError, ValueError):
        return _err("Identifiant invalide", "BAD_REQUEST")
    feed = await module.db.fetch_one("SELECT * FROM feeds WHERE id=?", (feed_id,))
    if not feed:
        return _err("Flux introuvable", "NOT_FOUND")
    title = str(payload.get("title", feed["title"]) or "").strip()[:200] or feed["title"]
    folder = str(payload.get("folder", feed["folder"]) or "").strip()[:80]
    await module.db.execute("UPDATE feeds SET title=?, folder=? WHERE id=?", (title, folder, feed_id))
    return await module.db.fetch_one(f"SELECT {FEED_COLS} FROM feeds f WHERE f.id=?", (feed_id,))


@module.on("folder_rename")
async def folder_rename(payload: dict):
    await _ensure_schema()
    old = str(payload.get("old") or "")
    new = str(payload.get("new") or "").strip()[:80]
    n = await module.db.execute("UPDATE feeds SET folder=? WHERE folder=?", (new, old))
    return {"ok": True, "updated": n}


@module.on("feed_delete")
async def feed_delete(payload: dict):
    await _ensure_schema()
    try:
        feed_id = int(payload.get("id"))
    except (TypeError, ValueError):
        return _err("Identifiant invalide", "BAD_REQUEST")
    await module.db.execute("DELETE FROM items WHERE feed_id=?", (feed_id,))
    n = await module.db.execute("DELETE FROM feeds WHERE id=?", (feed_id,))
    return {"ok": bool(n)}


@module.on("feed_refresh")
async def feed_refresh(payload: dict):
    await _ensure_schema()
    try:
        feed_id = int(payload.get("id"))
    except (TypeError, ValueError):
        return _err("Identifiant invalide", "BAD_REQUEST")
    feed = await module.db.fetch_one("SELECT * FROM feeds WHERE id=?", (feed_id,))
    if not feed:
        return _err("Flux introuvable", "NOT_FOUND")
    r = await refresh_many([feed])
    if r["errors"]:
        return _err(r["errors"][0]["error"], "FETCH_FAILED")
    return r


@module.job("refresh_all")
async def refresh_all(payload: dict, job):
    await _ensure_schema()
    folder = payload.get("folder")
    if folder is not None:
        feeds = await module.db.fetch_all("SELECT * FROM feeds WHERE folder=?", (str(folder),))
    else:
        feeds = await module.db.fetch_all("SELECT * FROM feeds")

    async def progress(done, total, title):
        await job.progress(round(done * 100 / max(total, 1)), f"{done}/{total} — {title}")
    await job.progress(0, f"0/{len(feeds)}")
    return await refresh_many(feeds, progress)


# ── Actions : articles ───────────────────────────────────────────────────────

def _scope(payload: dict) -> tuple[str, list]:
    where, params = [], []
    if payload.get("feed_id") is not None:
        where.append("i.feed_id=?")
        params.append(int(payload["feed_id"]))
    elif payload.get("folder") is not None:
        where.append("f.folder=?")
        params.append(str(payload["folder"]))
    return where, params


@module.on("items_list")
async def items_list(payload: dict):
    await _ensure_schema()
    try:
        where, params = _scope(payload)
    except (TypeError, ValueError):
        return _err("Filtre invalide", "BAD_REQUEST")
    flt = payload.get("filter", "all")
    if flt == "unread":
        where.append("i.read=0")
    elif flt == "starred":
        where.append("i.starred=1")
    q = str(payload.get("q") or "").strip()
    if q:
        where.append("(i.title LIKE ? OR i.summary LIKE ? OR i.author LIKE ?)")
        params += [f"%{q}%"] * 3
    limit = max(1, min(500, int(payload.get("limit") or 100)))
    offset = max(0, int(payload.get("offset") or 0))
    sql = ("SELECT i.id, i.feed_id, i.title, i.link, i.author, i.published, substr(i.summary, 1, 220) AS summary, i.read, i.starred, "
           "f.title AS feed_title FROM items i JOIN feeds f ON f.id=i.feed_id"
           + (" WHERE " + " AND ".join(where) if where else "")
           + " ORDER BY i.published DESC, i.id DESC LIMIT ? OFFSET ?")
    rows = await module.db.fetch_all(sql, (*params, limit + 1, offset))
    return {"items": rows[:limit], "more": len(rows) > limit}


@module.on("item_get")
async def item_get(payload: dict):
    await _ensure_schema()
    try:
        item_id = int(payload.get("id"))
    except (TypeError, ValueError):
        return _err("Identifiant invalide", "BAD_REQUEST")
    row = await module.db.fetch_one(
        "SELECT i.*, f.title AS feed_title, f.site_url FROM items i JOIN feeds f ON f.id=i.feed_id WHERE i.id=?", (item_id,))
    if not row:
        return _err("Article introuvable", "NOT_FOUND")
    if not row["read"] and payload.get("mark_read", True):
        await module.db.execute("UPDATE items SET read=1 WHERE id=?", (item_id,))
        row["read"] = 1
    return row


@module.on("item_mark")
async def item_mark(payload: dict):
    await _ensure_schema()
    try:
        item_id = int(payload.get("id"))
    except (TypeError, ValueError):
        return _err("Identifiant invalide", "BAD_REQUEST")
    sets, params = [], []
    if "read" in payload:
        sets.append("read=?")
        params.append(1 if payload["read"] else 0)
    if "starred" in payload:
        sets.append("starred=?")
        params.append(1 if payload["starred"] else 0)
    if not sets:
        return _err("Rien à modifier", "BAD_REQUEST")
    n = await module.db.execute(f"UPDATE items SET {', '.join(sets)} WHERE id=?", (*params, item_id))
    if not n:
        return _err("Article introuvable", "NOT_FOUND")
    return {"ok": True}


@module.on("mark_all_read")
async def mark_all_read(payload: dict):
    await _ensure_schema()
    try:
        where, params = _scope(payload)
    except (TypeError, ValueError):
        return _err("Filtre invalide", "BAD_REQUEST")
    cond = " AND ".join(["read=0"] + [w.replace("i.", "").replace("f.folder=?", "feed_id IN (SELECT id FROM feeds WHERE folder=?)") for w in where])
    n = await module.db.execute(f"UPDATE items SET read=1 WHERE {cond}", tuple(params))
    return {"ok": True, "updated": n}


@module.on("open_url")
async def open_url(payload: dict):
    url = str(payload.get("url") or "").strip()
    if urllib.parse.urlsplit(url).scheme.lower() not in ("http", "https"):
        return _err("Seuls les liens http:// et https:// peuvent être ouverts", "BAD_URL")
    ok = await asyncio.to_thread(webbrowser.open, url)
    return {"ok": bool(ok)}


# ── OPML ─────────────────────────────────────────────────────────────────────

@module.on("opml_export")
async def opml_export(payload: dict):
    await _ensure_schema()
    feeds = await module.db.fetch_all("SELECT * FROM feeds ORDER BY LOWER(folder), LOWER(title)")
    root = ET.Element("opml", version="2.0")
    head = ET.SubElement(root, "head")
    ET.SubElement(head, "title").text = "Abonnements LedroideDesktop"
    ET.SubElement(head, "dateCreated").text = email.utils.formatdate(usegmt=True)
    body = ET.SubElement(root, "body")
    groups: dict[str, ET.Element] = {}
    for f in feeds:
        parent = body
        if f["folder"]:
            if f["folder"] not in groups:
                groups[f["folder"]] = ET.SubElement(body, "outline", text=f["folder"], title=f["folder"])
            parent = groups[f["folder"]]
        attrs = {"type": "rss", "text": f["title"] or f["url"], "title": f["title"] or f["url"], "xmlUrl": f["url"]}
        if f["site_url"]:
            attrs["htmlUrl"] = f["site_url"]
        ET.SubElement(parent, "outline", attrs)
    ET.indent(root)
    return {"opml": '<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(root, encoding="unicode"), "count": len(feeds)}


def parse_opml(text: str) -> list[dict]:
    data = text.encode("utf-8") if isinstance(text, str) else text
    if re.search(rb"<!ENTITY", data[:4096], re.I):
        raise FeedError("BAD_OPML", "Fichier OPML refusé (déclarations d'entités)")
    try:
        root = ET.fromstring(data)
    except ET.ParseError as exc:
        raise FeedError("BAD_OPML", f"Fichier OPML invalide : {exc}")
    if _local(root.tag) != "opml":
        raise FeedError("BAD_OPML", "Ce fichier n'est pas un document OPML")
    body = _child(root, "body")
    out = []

    def walk(el, folder):
        for o in el:
            if _local(o.tag) != "outline":
                continue
            xml_url = o.get("xmlUrl") or o.get("xmlurl")
            if xml_url:
                out.append({"url": xml_url.strip(), "title": (o.get("title") or o.get("text") or "").strip(),
                            "site_url": (o.get("htmlUrl") or "").strip(), "folder": folder})
            else:
                walk(o, (o.get("title") or o.get("text") or "").strip() or folder)
    if body is not None:
        walk(body, "")
    return out


@module.on("opml_import")
async def opml_import(payload: dict):
    await _ensure_schema()
    try:
        entries = parse_opml(str(payload.get("text") or ""))
    except FeedError as exc:
        return _err(exc.message, exc.code)
    added, skipped = 0, 0
    for e in entries:
        try:
            url = normalize_url(e["url"])
        except FeedError:
            skipped += 1
            continue
        n = await module.db.execute(
            "INSERT OR IGNORE INTO feeds(url, title, site_url, folder, created) VALUES (?,?,?,?,?)",
            (url, e["title"][:200] or url, e["site_url"] if e["site_url"].startswith(("http://", "https://")) else "",
             e["folder"][:80], time.time()))
        if n > 0:
            added += 1
        else:
            skipped += 1
    return {"ok": True, "added": added, "skipped": skipped, "found": len(entries)}


@module.on_shutdown
async def shutdown():
    global _auto_task
    if _auto_task is not None:
        _auto_task.cancel()
        try:
            await _auto_task
        except (asyncio.CancelledError, Exception):
            pass
        _auto_task = None
