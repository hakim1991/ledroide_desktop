import html
import posixpath
import re
import urllib.parse
import xml.etree.ElementTree as ET
import zipfile
from html.parser import HTMLParser

MAX_MEMBER = 40 * 1024 * 1024
IMAGE_TYPES = {
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".gif": "image/gif",
    ".webp": "image/webp", ".svg": "image/svg+xml", ".bmp": "image/bmp",
}

ALLOWED_TAGS = {
    "a", "abbr", "article", "aside", "b", "big", "blockquote", "br", "caption", "center", "cite", "code", "col",
    "colgroup", "dd", "del", "dfn", "div", "dl", "dt", "em", "figcaption", "figure", "footer", "h1", "h2", "h3",
    "h4", "h5", "h6", "header", "hr", "i", "img", "ins", "kbd", "li", "main", "mark", "nav", "ol", "p", "pre",
    "q", "rp", "rt", "ruby", "s", "samp", "section", "small", "span", "strike", "strong", "sub", "sup", "table",
    "tbody", "td", "tfoot", "th", "thead", "time", "tr", "tt", "u", "ul", "var", "wbr",
}
VOID_TAGS = {"br", "hr", "img", "col", "wbr"}
DROP_WITH_CONTENT = {"script", "style", "iframe", "object", "embed", "noscript", "template", "math", "form",
                     "button", "select", "textarea", "head", "title", "frameset", "frame", "applet", "audio",
                     "video", "canvas"}
ALLOWED_ATTRS = {
    "img": {"alt", "title", "width", "height"},
    "td": {"colspan", "rowspan"},
    "th": {"colspan", "rowspan"},
    "abbr": {"title"},
    "ol": {"start"},
}


class EpubError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _local(tag) -> str:
    return tag.rsplit("}", 1)[-1].lower() if isinstance(tag, str) else ""


def _parse_xml(data: bytes):
    if re.search(rb"<!ENTITY", data[:8192], re.I):
        raise EpubError("BAD_EPUB", "EPUB refusé (déclarations d'entités XML)")
    data = re.sub(rb"<!DOCTYPE[^>\[]*(\[[^\]]*\])?\s*>", b"", data, count=1, flags=re.I)
    try:
        return ET.fromstring(data)
    except ET.ParseError as exc:
        raise EpubError("BAD_EPUB", f"XML invalide dans l'EPUB : {exc}")


def _join(base_dir: str, href: str) -> str:
    href = urllib.parse.unquote(href.split("#", 1)[0])
    if not href:
        return ""
    path = posixpath.normpath(posixpath.join(base_dir, href))
    return "" if path.startswith("..") or path.startswith("/") else path


class Epub:
    """Read-only view of an EPUB zip: metadata, spine, TOC, members (bounded size)."""

    def __init__(self, path: str):
        try:
            self.zf = zipfile.ZipFile(path)
        except (zipfile.BadZipFile, OSError) as exc:
            raise EpubError("BAD_EPUB", f"Archive EPUB illisible : {exc}")
        self.names = set(self.zf.namelist())
        try:
            container = _parse_xml(self.read("META-INF/container.xml"))
        except KeyError:
            raise EpubError("BAD_EPUB", "EPUB invalide : META-INF/container.xml manquant")
        rootfile = next((el.get("full-path") for el in container.iter() if _local(el.tag) == "rootfile"), None)
        if not rootfile or rootfile not in self.names:
            raise EpubError("BAD_EPUB", "EPUB invalide : fichier OPF introuvable")
        self.opf_dir = posixpath.dirname(rootfile)
        opf = _parse_xml(self.read(rootfile))
        self.title, self.author, self.cover_meta = "", "", ""
        self.items: dict[str, dict] = {}
        self.spine: list[str] = []
        toc_id = ""
        for el in opf.iter():
            name = _local(el.tag)
            if name == "title" and not self.title:
                self.title = "".join(el.itertext()).strip()
            elif name == "creator" and not self.author:
                self.author = "".join(el.itertext()).strip()
            elif name == "meta" and (el.get("name") or "").lower() == "cover":
                self.cover_meta = el.get("content") or ""
            elif name == "item" and el.get("id") and el.get("href"):
                self.items[el.get("id")] = {
                    "path": _join(self.opf_dir, el.get("href")),
                    "type": (el.get("media-type") or "").lower(),
                    "props": (el.get("properties") or "").split(),
                }
            elif name == "spine":
                toc_id = el.get("toc") or ""
            elif name == "itemref" and el.get("idref") and (el.get("linear") or "yes") != "no":
                self.spine.append(el.get("idref"))
        self.chapters = [self.items[i]["path"] for i in self.spine
                         if i in self.items and self.items[i]["path"] in self.names]
        if not self.chapters:
            raise EpubError("BAD_EPUB", "EPUB sans chapitre lisible")
        self.toc = self._nav_toc() or self._ncx_toc(toc_id)

    def close(self):
        self.zf.close()

    def read(self, name: str) -> bytes:
        info = self.zf.getinfo(name)
        if info.file_size > MAX_MEMBER:
            raise EpubError("TOO_LARGE", "Élément de l'EPUB trop volumineux")
        return self.zf.read(info)

    def chapter_index(self, path: str) -> int:
        try:
            return self.chapters.index(path)
        except ValueError:
            return -1

    def _entry(self, title: str, base_dir: str, href: str, level: int) -> dict | None:
        target = _join(base_dir, href)
        idx = self.chapter_index(target)
        if idx < 0 or not title:
            return None
        frag = href.split("#", 1)[1] if "#" in href else ""
        return {"title": re.sub(r"\s+", " ", title).strip()[:300], "chapter": idx, "anchor": frag, "level": level}

    def _nav_toc(self) -> list[dict]:
        nav = next((it for it in self.items.values() if "nav" in it["props"]), None)
        if not nav or nav["path"] not in self.names:
            return []
        try:
            root = _parse_xml(self.read(nav["path"]))
        except EpubError:
            return []
        base = posixpath.dirname(nav["path"])
        navs = [n for n in root.iter() if _local(n.tag) == "nav"]
        toc_nav = next((n for n in navs if any(v == "toc" for k, v in n.attrib.items() if _local(k) == "type")),
                       navs[0] if navs else None)
        out: list[dict] = []
        if toc_nav is None:
            return out

        def walk(el, level):
            for child in el:
                name = _local(child.tag)
                if name == "li":
                    a = next((c for c in child if _local(c.tag) in ("a", "span")), None)
                    if a is not None and a.get("href"):
                        e = self._entry("".join(a.itertext()), base, a.get("href"), level)
                        if e:
                            out.append(e)
                    for sub in child:
                        if _local(sub.tag) == "ol":
                            walk(sub, level + 1)
                elif name == "ol":
                    walk(child, level)
        for c in toc_nav:
            if _local(c.tag) == "ol":
                walk(c, 0)
        return out

    def _ncx_toc(self, toc_id: str) -> list[dict]:
        ncx = self.items.get(toc_id) or next((it for it in self.items.values()
                                              if it["type"] == "application/x-dtbncx+xml"), None)
        if not ncx or ncx["path"] not in self.names:
            return []
        try:
            root = _parse_xml(self.read(ncx["path"]))
        except EpubError:
            return []
        base = posixpath.dirname(ncx["path"])
        out: list[dict] = []

        def walk(el, level):
            for p in el:
                if _local(p.tag) != "navpoint":
                    continue
                label = next((c for c in p if _local(c.tag) == "navlabel"), None)
                content = next((c for c in p if _local(c.tag) == "content"), None)
                if label is not None and content is not None and content.get("src"):
                    e = self._entry("".join(label.itertext()), base, content.get("src"), level)
                    if e:
                        out.append(e)
                walk(p, level + 1)
        navmap = next((c for c in root.iter() if _local(c.tag) == "navmap"), None)
        if navmap is not None:
            walk(navmap, 0)
        return out

    def chapter_titles(self) -> list[str]:
        titles: dict[int, str] = {}
        for e in self.toc:
            titles.setdefault(e["chapter"], e["title"])
        return [titles.get(i, f"Section {i + 1}") for i in range(len(self.chapters))]

    def cover_path(self) -> str | None:
        for it in self.items.values():
            if "cover-image" in it["props"] and it["path"] in self.names:
                return it["path"]
        if self.cover_meta in self.items and self.items[self.cover_meta]["path"] in self.names:
            return self.items[self.cover_meta]["path"]
        for it in self.items.values():
            if it["type"].startswith("image/") and "cover" in it["path"].lower() and it["path"] in self.names:
                return it["path"]
        return None

    def resource(self, path: str) -> tuple[bytes, str]:
        path = posixpath.normpath(path or "")
        ext = posixpath.splitext(path)[1].lower()
        if ext not in IMAGE_TYPES or ext == ".svg" or path not in self.names:
            raise EpubError("NOT_FOUND", "Ressource introuvable")
        return self.read(path), IMAGE_TYPES[ext]

    def chapter_html(self, index: int, res_url) -> str:
        path = self.chapters[index]
        raw = self.read(path)
        text = raw.decode("utf-8", errors="replace")
        m = re.search(r"<body[^>]*>(.*)</body>", text, re.S | re.I)
        body = m.group(1) if m else text
        return sanitize(body, posixpath.dirname(path), self, res_url)


class _Sanitizer(HTMLParser):
    def __init__(self, base_dir: str, book: Epub, res_url):
        super().__init__(convert_charrefs=True)
        self.base_dir, self.book, self.res_url = base_dir, book, res_url
        self.out: list[str] = []
        self.stack: list[str] = []
        self.drop_depth = 0
        self.drop_tag: str | None = None

    def _img(self, src: str) -> str | None:
        if not src or re.match(r"^[a-z][a-z0-9+.-]*:", src, re.I):
            return None
        target = _join(self.base_dir, src)
        if not target or target not in self.book.names:
            return None
        if posixpath.splitext(target)[1].lower() not in IMAGE_TYPES or target.lower().endswith(".svg"):
            return None
        return self.res_url(target)

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if self.drop_depth:
            if tag == self.drop_tag:
                self.drop_depth += 1
            return
        if tag in DROP_WITH_CONTENT:
            self.drop_tag, self.drop_depth = tag, 1
            return
        a = {k.lower(): (v or "") for k, v in attrs}
        if tag == "image":
            src = self._img(a.get("xlink:href") or a.get("href") or "")
            if src:
                self.out.append(f'<img src="{html.escape(src, quote=True)}" alt="">')
            return
        if tag not in ALLOWED_TAGS:
            return
        parts = []
        if a.get("id") and re.fullmatch(r"[\w.:-]{1,120}", a["id"]):
            parts.append(f' data-anchor="{html.escape(a["id"], quote=True)}"')
        for name in ALLOWED_ATTRS.get(tag, ()):
            value = a.get(name, "").strip()
            if not value:
                continue
            if name in ("width", "height", "colspan", "rowspan", "start") and not re.fullmatch(r"\d{1,4}%?", value):
                continue
            parts.append(f' {name}="{html.escape(value[:300], quote=True)}"')
        if tag == "img":
            src = self._img(a.get("src", ""))
            if not src:
                return
            parts.insert(0, f' src="{html.escape(src, quote=True)}"')
            parts.append(' loading="lazy"')
        if tag == "a":
            href = a.get("href", "").strip()
            if re.match(r"^https?://", href, re.I):
                parts.append(f' data-ext="{html.escape(href[:2000], quote=True)}"')
            elif href.startswith("#"):
                parts.append(f' data-chapter="-1" data-frag="{html.escape(href[1:121], quote=True)}"')
            elif href and not re.match(r"^[a-z][a-z0-9+.-]*:", href, re.I):
                idx = self.book.chapter_index(_join(self.base_dir, href))
                if idx >= 0:
                    frag = href.split("#", 1)[1][:120] if "#" in href else ""
                    parts.append(f' data-chapter="{idx}" data-frag="{html.escape(frag, quote=True)}"')
        self.out.append(f"<{tag}{''.join(parts)}>")
        if tag not in VOID_TAGS:
            self.stack.append(tag)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        t = tag.lower()
        if t not in VOID_TAGS and not self.drop_depth and self.stack and self.stack[-1] == t:
            self.handle_endtag(tag)
        elif t in DROP_WITH_CONTENT and self.drop_tag == t and self.drop_depth == 1:
            self.drop_tag, self.drop_depth = None, 0

    def handle_endtag(self, tag):
        tag = tag.lower()
        if self.drop_depth:
            if tag == self.drop_tag:
                self.drop_depth -= 1
                if not self.drop_depth:
                    self.drop_tag = None
            return
        if tag not in self.stack:
            return
        while self.stack:
            t = self.stack.pop()
            self.out.append(f"</{t}>")
            if t == tag:
                break

    def handle_data(self, data):
        if not self.drop_depth:
            self.out.append(html.escape(data, quote=False))

    def close(self):
        super().close()
        while self.stack:
            self.out.append(f"</{self.stack.pop()}>")


def sanitize(fragment: str, base_dir: str, book: Epub, res_url) -> str:
    p = _Sanitizer(base_dir, book, res_url)
    p.feed(fragment or "")
    p.close()
    return "".join(p.out).strip()
