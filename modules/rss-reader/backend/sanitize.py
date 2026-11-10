import html
import re
import urllib.parse
from html.parser import HTMLParser

ALLOWED_TAGS = {
    "a", "abbr", "b", "blockquote", "br", "caption", "cite", "code", "dd", "del", "details", "div", "dl", "dt",
    "em", "figcaption", "figure", "h1", "h2", "h3", "h4", "h5", "h6", "hr", "i", "img", "ins", "kbd", "li",
    "mark", "ol", "p", "pre", "q", "s", "small", "span", "strong", "sub", "summary", "sup", "table", "tbody",
    "td", "tfoot", "th", "thead", "time", "tr", "u", "ul",
}
VOID_TAGS = {"br", "hr", "img"}
DROP_WITH_CONTENT = {"script", "style", "iframe", "object", "embed", "noscript", "template", "svg", "math",
                     "form", "button", "select", "textarea", "head", "title", "frameset", "frame", "applet"}
ALLOWED_ATTRS = {
    "a": {"href", "title"},
    "img": {"src", "alt", "title", "width", "height"},
    "td": {"colspan", "rowspan"},
    "th": {"colspan", "rowspan", "scope"},
    "abbr": {"title"},
    "time": {"datetime"},
    "ol": {"start"},
    "q": {"cite"},
    "blockquote": {"cite"},
}
URL_ATTRS = {"href", "src", "cite"}


def safe_url(value: str, base: str | None, allow_data_image: bool = False) -> str | None:
    value = (value or "").strip()
    if not value:
        return None
    if allow_data_image and re.match(r"^data:image/(png|gif|jpe?g|webp);base64,", value, re.I):
        return value
    if base:
        try:
            value = urllib.parse.urljoin(base, value)
        except ValueError:
            return None
    scheme = urllib.parse.urlsplit(value).scheme.lower()
    if scheme in ("http", "https") or (scheme == "mailto" and not allow_data_image):
        return value
    return None


class _Sanitizer(HTMLParser):
    def __init__(self, base: str | None):
        super().__init__(convert_charrefs=True)
        self.base = base
        self.out: list[str] = []
        self.stack: list[str] = []
        self.drop_depth = 0
        self.drop_tag: str | None = None

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if self.drop_depth:
            if tag == self.drop_tag:
                self.drop_depth += 1
            return
        if tag in DROP_WITH_CONTENT:
            self.drop_tag, self.drop_depth = tag, 1
            return
        if tag not in ALLOWED_TAGS:
            return
        allowed = ALLOWED_ATTRS.get(tag, set())
        parts = []
        for name, value in attrs:
            name = name.lower()
            if name not in allowed or value is None:
                continue
            if name in URL_ATTRS:
                value = safe_url(value, self.base, allow_data_image=(tag == "img"))
                if value is None:
                    continue
            elif name in ("width", "height", "colspan", "rowspan", "start") and not re.fullmatch(r"\d{1,4}%?", value.strip()):
                continue
            parts.append(f' {name}="{html.escape(value, quote=True)}"')
        if tag == "a":
            parts.append(' target="_blank" rel="noopener noreferrer"')
        if tag == "img":
            if not any(p.startswith(" src=") for p in parts):
                return
            parts.append(' loading="lazy" referrerpolicy="no-referrer"')
        self.out.append(f"<{tag}{''.join(parts)}>")
        if tag not in VOID_TAGS:
            self.stack.append(tag)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag.lower() not in VOID_TAGS and self.stack and self.stack[-1] == tag.lower() and not self.drop_depth:
            self.handle_endtag(tag)

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


def sanitize_html(fragment: str, base: str | None = None) -> str:
    p = _Sanitizer(base)
    try:
        p.feed(fragment or "")
        p.close()
    except Exception:
        return html.escape(strip_tags(fragment or ""))
    return "".join(p.out).strip()


class _Text(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self.skip += 1
        elif tag in ("br", "p", "div", "li", "h1", "h2", "h3", "tr"):
            self.parts.append(" ")

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
    except Exception:
        return re.sub(r"<[^>]*>", " ", fragment or "")
    return re.sub(r"\s+", " ", "".join(p.parts)).strip()
