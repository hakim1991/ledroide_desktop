import email.utils
import hashlib
import html
import html.entities
import re
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from html.parser import HTMLParser

USER_AGENT = "Mozilla/5.0 (compatible; LedroidePodcasts/1.0)"
MAX_FEED = 15 * 1024 * 1024
XML_ENTITIES = {"amp", "lt", "gt", "quot", "apos"}


class FeedError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class SafeRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if urllib.parse.urlsplit(newurl).scheme.lower() not in ("http", "https"):
            raise FeedError("BAD_REDIRECT", "Redirection refusée (schéma non http/https)")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def opener():
    return urllib.request.build_opener(SafeRedirects())


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
    if len(url) > 2000:
        raise FeedError("BAD_URL", "Adresse trop longue")
    return urllib.parse.quote(url, safe=":/?#[]@!$&'()*+,;=%~")


def fetch(url: str, timeout: float = 20) -> tuple[bytes, str]:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT,
                                               "Accept": "application/rss+xml, application/xml;q=0.9, */*;q=0.5"})
    try:
        with opener().open(req, timeout=timeout) as resp:
            data = resp.read(MAX_FEED + 1)
            if len(data) > MAX_FEED:
                raise FeedError("TOO_LARGE", "Flux trop volumineux (15 Mo maximum)")
            return data, resp.geturl()
    except urllib.error.HTTPError as exc:
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


def _local(tag) -> str:
    return tag.rsplit("}", 1)[-1].lower() if isinstance(tag, str) else ""


def _ns(tag) -> str:
    return tag[1:].split("}", 1)[0] if isinstance(tag, str) and tag.startswith("{") else ""


def _child(el, name, itunes=None):
    for c in el:
        if _local(c.tag) == name and (itunes is None or ("itunes" in _ns(c.tag)) == itunes):
            return c
    return None


def _text(el, name, itunes=None) -> str:
    c = _child(el, name, itunes)
    return "".join(c.itertext()).strip() if c is not None else ""


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
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def _duration(value: str) -> float | None:
    value = (value or "").strip()
    if not value:
        return None
    if re.fullmatch(r"\d+(\.\d+)?", value):
        return float(value)
    m = re.fullmatch(r"(?:(\d+):)?(\d{1,2}):(\d{1,2})", value)
    if m:
        return int(m[1] or 0) * 3600 + int(m[2]) * 60 + int(m[3])
    return None


class _Text(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self.skip += 1
        elif tag in ("br", "p", "div", "li", "h1", "h2", "h3", "tr"):
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style") and self.skip:
            self.skip -= 1

    def handle_data(self, data):
        if not self.skip:
            self.parts.append(data)


def strip_tags(fragment: str) -> str:
    p = _Text()
    try:
        p.feed(fragment or "")
        p.close()
        text = "".join(p.parts)
    except Exception:
        text = re.sub(r"<[^>]*>", " ", fragment or "")
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    return re.sub(r"\n\s*\n+", "\n\n", text).strip()


def _http(url: str, base: str) -> str:
    if not url:
        return ""
    try:
        url = urllib.parse.urljoin(base, url.strip())
    except ValueError:
        return ""
    return url if urllib.parse.urlsplit(url).scheme.lower() in ("http", "https") else ""


def parse_podcast(data: bytes, base_url: str) -> dict:
    root = _parse_xml(data)
    if _local(root.tag) != "rss":
        raise FeedError("NOT_A_FEED", "Ce document n'est pas un flux RSS de podcast")
    ch = _child(root, "channel")
    if ch is None:
        raise FeedError("NOT_A_FEED", "Flux RSS sans élément <channel>")
    img = _child(ch, "image", True)
    image = img.get("href", "") if img is not None else ""
    if not image:
        rss_img = _child(ch, "image", False)
        image = _text(rss_img, "url") if rss_img is not None else ""
    episodes = []
    for it in ch:
        if _local(it.tag) != "item":
            continue
        enc = _child(it, "enclosure")
        url = _http(enc.get("url", ""), base_url) if enc is not None else ""
        if not url:
            continue
        mime = (enc.get("type") or "").lower()
        if mime and not (mime.startswith("audio/") or mime.startswith("video/") or mime == "application/octet-stream"):
            continue
        title = strip_tags(_text(it, "title", False) or _text(it, "title"))[:500]
        desc = _text(it, "encoded") or _text(it, "description", False) or _text(it, "summary", True)
        guid = _text(it, "guid") or url
        try:
            size = int(enc.get("length") or 0)
        except ValueError:
            size = 0
        eimg = _child(it, "image", True)
        episodes.append({
            "guid": guid[:500] or hashlib.sha1(url.encode()).hexdigest(),
            "title": title or "Sans titre",
            "description": strip_tags(desc)[:20000],
            "published": _date(_text(it, "pubdate")),
            "duration": _duration(_text(it, "duration", True)),
            "audio_url": url, "mime": mime[:100], "size": max(0, size),
            "image": _http(eimg.get("href", ""), base_url) if eimg is not None else "",
            "link": _http(_text(it, "link"), base_url),
        })
    return {
        "title": strip_tags(_text(ch, "title", False) or _text(ch, "title"))[:300] or urllib.parse.urlsplit(base_url).hostname,
        "author": strip_tags(_text(ch, "author", True) or _text(ch, "managingeditor"))[:300],
        "description": strip_tags(_text(ch, "description", False) or _text(ch, "summary", True))[:5000],
        "image": _http(image, base_url), "site_url": _http(_text(ch, "link", False), base_url), "episodes": episodes,
    }
