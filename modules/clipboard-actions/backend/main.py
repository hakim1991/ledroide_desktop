import asyncio
import base64
import binascii
import colorsys
import ctypes
import hashlib
import io
import ipaddress
import json
import logging
import math
import os
import re
import subprocess
import sys
import time
import unicodedata
import urllib.parse
import uuid as uuidlib
from datetime import datetime, timezone
from pathlib import Path

from ledroide_sdk import Module

log = logging.getLogger("ledroide.clipboard-actions")

module = Module(__file__)
router = module.router

IS_WINDOWS = sys.platform == "win32"
MAX_TEXT = 2_000_000
MAX_SHOWN = 200_000
OPEN_SCHEMES = ("http", "https", "mailto", "tel", "ftp")
TRACKING_PARAMS = re.compile(r"^(utm_\w+|fbclid|gclid|dclid|msclkid|mc_cid|mc_eid|igshid|_hsenc|_hsmi|yclid|ref_src)$", re.I)


# ── Windows clipboard (ctypes) ────────────────────────────────────────────────

CF_UNICODETEXT = 13
CF_HDROP = 15
CF_DIB = 8
CF_BITMAP = 2
GMEM_MOVEABLE = 0x0002

if IS_WINDOWS:
    from ctypes import wintypes

    _user32 = ctypes.WinDLL("user32", use_last_error=True)
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    _user32.OpenClipboard.argtypes = [wintypes.HWND]
    _user32.OpenClipboard.restype = wintypes.BOOL
    _user32.CloseClipboard.restype = wintypes.BOOL
    _user32.EmptyClipboard.restype = wintypes.BOOL
    _user32.GetClipboardData.argtypes = [wintypes.UINT]
    _user32.GetClipboardData.restype = wintypes.HANDLE
    _user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
    _user32.SetClipboardData.restype = wintypes.HANDLE
    _user32.IsClipboardFormatAvailable.argtypes = [wintypes.UINT]
    _user32.IsClipboardFormatAvailable.restype = wintypes.BOOL
    _user32.GetClipboardSequenceNumber.restype = wintypes.DWORD
    _kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
    _kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
    _kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
    _kernel32.GlobalLock.restype = wintypes.LPVOID
    _kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
    _kernel32.GlobalFree.argtypes = [wintypes.HGLOBAL]
    _kernel32.GlobalFree.restype = wintypes.HGLOBAL
    _kernel32.GlobalSize.argtypes = [wintypes.HGLOBAL]
    _kernel32.GlobalSize.restype = ctypes.c_size_t
    _shell32.DragQueryFileW.argtypes = [wintypes.HANDLE, wintypes.UINT, wintypes.LPWSTR, wintypes.UINT]
    _shell32.DragQueryFileW.restype = wintypes.UINT


def _open_clipboard(retries: int = 15) -> bool:
    for _ in range(retries):
        if _user32.OpenClipboard(None):
            return True
        time.sleep(0.03)
    return False


def clip_seq() -> int:
    return int(_user32.GetClipboardSequenceNumber()) if IS_WINDOWS else 0


def clip_read() -> dict:
    """{'kind': 'text'|'files'|'image'|'empty', ...} from the Windows clipboard."""
    if _user32.IsClipboardFormatAvailable(CF_HDROP):
        files = _read_files()
        if files:
            return {"kind": "files", "files": files}
    if _user32.IsClipboardFormatAvailable(CF_UNICODETEXT):
        text = _read_text()
        if text is not None:
            return {"kind": "text", "text": text}
    if _user32.IsClipboardFormatAvailable(CF_DIB) or _user32.IsClipboardFormatAvailable(CF_BITMAP):
        return {"kind": "image"}
    return {"kind": "empty"}


def _read_text() -> str | None:
    if not _open_clipboard():
        raise OSError("Presse-papier occupé par une autre application")
    try:
        h = _user32.GetClipboardData(CF_UNICODETEXT)
        if not h:
            return None
        p = _kernel32.GlobalLock(h)
        if not p:
            return None
        try:
            n = min(_kernel32.GlobalSize(h) // 2, MAX_TEXT)
            return (ctypes.wstring_at(p, n) if n else "").split("\x00", 1)[0]
        finally:
            _kernel32.GlobalUnlock(h)
    finally:
        _user32.CloseClipboard()


def _read_files() -> list[str]:
    if not _open_clipboard():
        raise OSError("Presse-papier occupé par une autre application")
    try:
        h = _user32.GetClipboardData(CF_HDROP)
        if not h:
            return []
        count = _shell32.DragQueryFileW(h, 0xFFFFFFFF, None, 0)
        out = []
        for i in range(min(count, 500)):
            length = _shell32.DragQueryFileW(h, i, None, 0)
            buf = ctypes.create_unicode_buffer(length + 1)
            _shell32.DragQueryFileW(h, i, buf, length + 1)
            out.append(buf.value)
        return out
    finally:
        _user32.CloseClipboard()


def clip_write(text: str) -> bool:
    data = (text + "\x00").encode("utf-16-le")
    h = _kernel32.GlobalAlloc(GMEM_MOVEABLE, len(data))
    if not h:
        return False
    p = _kernel32.GlobalLock(h)
    if not p:
        _kernel32.GlobalFree(h)
        return False
    ctypes.memmove(p, data, len(data))
    _kernel32.GlobalUnlock(h)
    if not _open_clipboard(30):
        _kernel32.GlobalFree(h)
        return False
    try:
        _user32.EmptyClipboard()
        if not _user32.SetClipboardData(CF_UNICODETEXT, h):
            _kernel32.GlobalFree(h)
            return False
        return True
    finally:
        _user32.CloseClipboard()


def _clip_image_info() -> dict | None:
    try:
        from PIL import Image, ImageGrab
        img = ImageGrab.grabclipboard()
    except Exception:
        return None
    if not isinstance(img, Image.Image):
        return None
    thumb = img.convert("RGBA") if img.mode not in ("RGB", "RGBA") else img.copy()
    thumb.thumbnail((360, 240))
    buf = io.BytesIO()
    thumb.save(buf, "PNG")
    return {"width": img.width, "height": img.height, "mode": img.mode,
            "thumb": "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()}


# ── Safe math evaluator ───────────────────────────────────────────────────────

class MathError(Exception):
    pass


_FUNCS = {
    "sqrt": (1, lambda x: math.sqrt(x)), "racine": (1, lambda x: math.sqrt(x)), "abs": (1, abs),
    "round": (-1, lambda x, n=0: round(x, int(n))), "floor": (1, math.floor), "ceil": (1, math.ceil),
    "sin": (1, math.sin), "cos": (1, math.cos), "tan": (1, math.tan), "asin": (1, math.asin),
    "acos": (1, math.acos), "atan": (1, math.atan), "ln": (1, math.log), "log": (1, math.log10),
    "log10": (1, math.log10), "log2": (1, math.log2), "exp": (1, math.exp),
    "min": (-1, min), "max": (-1, max), "fact": (1, lambda x: _factorial(x)),
}
_CONSTS = {"pi": math.pi, "π": math.pi, "e": math.e, "tau": math.tau}
_TOKEN = re.compile(r"\s*(?:(0x[0-9a-f]+|0b[01]+|0o[0-7]+)|(\d+(?:\.\d*)?(?:e[+-]?\d+)?|\.\d+(?:e[+-]?\d+)?)"
                    r"|([a-zπ_][a-z0-9_]*)|(\*\*|//|[-+*/%^(),!×÷]))", re.I)


def _factorial(x):
    if x != int(x) or x < 0:
        raise MathError("Factorielle d'un nombre non entier ou négatif")
    if x > 1000:
        raise MathError("Factorielle trop grande")
    return math.factorial(int(x))


def _tokenize(expr: str) -> list[tuple[str, object]]:
    expr = re.sub(r"(?<=\d),(?=\d)", ".", expr)
    expr = re.sub(r"(?<=\d)[\s  ](?=\d{3}\b)", "", expr)
    pos, out = 0, []
    while pos < len(expr):
        if expr[pos:].strip() == "":
            break
        m = _TOKEN.match(expr, pos)
        if not m:
            raise MathError(f"Caractère inattendu : « {expr[pos:].strip()[:1]} »")
        pos = m.end()
        if m.group(1):
            out.append(("num", int(m.group(1), 0)))
        elif m.group(2):
            s = m.group(2)
            out.append(("num", float(s) if any(c in s.lower() for c in ".e") else int(s)))
        elif m.group(3):
            out.append(("id", m.group(3).lower()))
        else:
            op = {"×": "*", "÷": "/", "**": "^"}.get(m.group(4), m.group(4))
            out.append(("op", op))
    if len(out) > 400:
        raise MathError("Expression trop longue")
    return out


class _Parser:
    def __init__(self, tokens):
        self.t = tokens
        self.i = 0

    def peek(self):
        return self.t[self.i] if self.i < len(self.t) else (None, None)

    def take(self, kind=None, val=None):
        tok = self.peek()
        if tok[0] is None or (kind and tok[0] != kind) or (val is not None and tok[1] != val):
            raise MathError("Expression incomplète" if tok[0] is None else f"« {tok[1]} » inattendu")
        self.i += 1
        return tok

    def parse(self):
        v = self.expr()
        if self.i != len(self.t):
            raise MathError(f"« {self.t[self.i][1]} » inattendu")
        return v

    def expr(self):
        v = self.term()
        while self.peek() in (("op", "+"), ("op", "-")):
            op = self.take()[1]
            r = self.term()
            v = v + r if op == "+" else v - r
        return v

    def term(self):
        v = self.unary()
        while self.peek()[0] == "op" and self.peek()[1] in ("*", "/", "//", "%"):
            op = self.take()[1]
            r = self.unary()
            if op == "*":
                v = v * r
            elif r == 0:
                raise MathError("Division par zéro")
            elif op == "/":
                v = v / r
                if isinstance(v, float) and v.is_integer() and abs(v) < 1e15:
                    v = int(v)
            elif op == "//":
                v = v // r
            else:
                v = v % r
            _check(v)
        return v

    def unary(self):
        if self.peek() == ("op", "-"):
            self.take()
            return -self.unary()
        if self.peek() == ("op", "+"):
            self.take()
            return self.unary()
        return self.power()

    def power(self):
        base = self.postfix()
        if self.peek() == ("op", "^"):
            self.take()
            exp = self.unary()
            return _pow(base, exp)
        return base

    def postfix(self):
        v = self.primary()
        while self.peek() == ("op", "!"):
            self.take()
            v = _factorial(v)
        return v

    def primary(self):
        kind, val = self.peek()
        if kind == "num":
            self.take()
            return val
        if kind == "op" and val == "(":
            self.take()
            v = self.expr()
            self.take("op", ")")
            return v
        if kind == "id":
            self.take()
            if val in _FUNCS:
                arity, fn = _FUNCS[val]
                self.take("op", "(")
                args = [self.expr()]
                while self.peek() == ("op", ","):
                    self.take()
                    args.append(self.expr())
                self.take("op", ")")
                if arity > 0 and len(args) != arity:
                    raise MathError(f"{val}() attend {arity} argument(s)")
                try:
                    return fn(*args)
                except (ValueError, TypeError, OverflowError) as exc:
                    raise MathError(f"{val}() : {exc}")
            if val in _CONSTS:
                return _CONSTS[val]
            raise MathError(f"Fonction ou constante inconnue : {val}")
        raise MathError("Expression incomplète" if kind is None else f"« {val} » inattendu")


def _check(v):
    if isinstance(v, int) and v.bit_length() > 20000:
        raise MathError("Résultat trop grand")
    if isinstance(v, float) and (math.isinf(v) or math.isnan(v)):
        raise MathError("Résultat non défini")


def _pow(b, e):
    if isinstance(b, int) and isinstance(e, int) and e >= 0:
        if b not in (0, 1, -1) and e * math.log2(abs(b)) > 20000:
            raise MathError("Résultat trop grand")
        return b ** e
    try:
        v = float(b) ** float(e)
    except (OverflowError, ZeroDivisionError):
        raise MathError("Résultat trop grand ou non défini")
    if isinstance(v, complex):
        raise MathError("Résultat complexe")
    _check(v)
    return v


def safe_eval(expr: str):
    v = _Parser(_tokenize(expr)).parse()
    _check(v)
    return v


def fmt_num(v) -> str:
    if isinstance(v, int):
        return str(v)
    if v.is_integer() and abs(v) < 1e15:
        return str(int(v))
    return format(v, ".15g")


def fmt_fr(v) -> str:
    if isinstance(v, float) and not v.is_integer():
        s = f"{v:,.6f}".rstrip("0").rstrip(".")
    else:
        s = f"{int(v):,}"
    return s.replace(",", " ").replace(".", ",")


# ── Detection helpers ─────────────────────────────────────────────────────────

def _f(label, value, mono=True):
    return {"label": label, "value": str(value), "mono": mono}


def _copy(label, value):
    return {"kind": "copy", "label": label, "value": str(value)}


def _human_size(n: int) -> str:
    for unit in ("o", "Ko", "Mo", "Go", "To"):
        if n < 1024 or unit == "To":
            return f"{n:.0f} {unit}" if unit == "o" else f"{n:.1f} {unit}"
        n /= 1024
    return str(n)


def _relative(dt: datetime) -> str:
    diff = (datetime.now(timezone.utc) - dt).total_seconds()
    future = diff < 0
    d = abs(diff)
    for secs, unit in ((31557600, "an"), (2629800, "mois"), (86400, "jour"), (3600, "heure"), (60, "minute"), (1, "seconde")):
        if d >= secs:
            n = int(d // secs)
            label = unit if n == 1 or unit == "mois" else unit + "s"
            return f"dans {n} {label}" if future else f"il y a {n} {label}"
    return "maintenant"


def _date_fields(dt: datetime) -> tuple[list, list]:
    local = dt.astimezone()
    fields = [_f("Date locale", local.strftime("%d/%m/%Y %H:%M:%S") + f" ({local.tzname() or ''})".replace(" ()", "")),
              _f("UTC (ISO 8601)", dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")),
              _f("Relatif", _relative(dt), False),
              _f("Timestamp (s)", int(dt.timestamp())), _f("Timestamp (ms)", int(dt.timestamp() * 1000))]
    actions = [_copy("ISO 8601 (UTC)", dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")),
               _copy("ISO 8601 (local)", local.isoformat()),
               _copy("Date française", local.strftime("%d/%m/%Y %H:%M:%S")),
               _copy("Timestamp Unix (s)", int(dt.timestamp())),
               _copy("Timestamp (ms)", int(dt.timestamp() * 1000))]
    return fields, actions


# ── Detectors (each returns a detection dict or None) ─────────────────────────

URL_RE = re.compile(r"^(?:(?:https?|ftp)://|www\.)[^\s<>\"]+$", re.I)
EMAIL_RE = re.compile(r"^(?:mailto:)?([A-Za-z0-9._%+\-]+)@([A-Za-z0-9.\-]+\.[A-Za-z]{2,})$")
UUID_RE = re.compile(r"^[{(]?([0-9a-f]{8})-?([0-9a-f]{4})-?([0-9a-f]{4})-?([0-9a-f]{4})-?([0-9a-f]{12})[)}]?$", re.I)
JWT_RE = re.compile(r"^(?:Bearer\s+)?(eyJ[A-Za-z0-9_\-]+)\.(eyJ[A-Za-z0-9_\-]+|[A-Za-z0-9_\-]*)\.([A-Za-z0-9_\-]*)$")
PHONE_RE = re.compile(r"^(?:tel:)?\s*(\+|00)?[\d\s.\-()]{8,22}$")
GPS_DEC_RE = re.compile(r"^\(?\s*(-?\d{1,2}\.\d{2,})\s*[,;\s]\s*(-?\d{1,3}\.\d{2,})\s*\)?$")
GPS_DMS_RE = re.compile(r"(\d{1,3})\s*°\s*(\d{1,2})\s*['′]\s*(\d{1,2}(?:[.,]\d+)?)\s*(?:\"|″|'')?\s*([NSEWO])", re.I)
WIN_PATH_RE = re.compile(r'^"?(?:[a-zA-Z]:[\\/]|\\\\[^\\/]+[\\/]|%[A-Za-z_]+%|~[\\/])[^<>"|?*\n]*"?$')
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2}(?:[.,]\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?)?$")
FR_DATE_RE = re.compile(r"^(\d{1,2})/(\d{1,2})/(\d{4})(?:\s+(\d{1,2}):(\d{2})(?::(\d{2}))?)?$")


def detect_url(t: str):
    if not URL_RE.match(t):
        return None
    url = t if "://" in t else "https://" + t
    p = urllib.parse.urlsplit(url)
    if not p.hostname:
        return None
    try:
        port = p.port
    except ValueError:
        port = None
    query = urllib.parse.parse_qsl(p.query, keep_blank_values=True)
    fields = [_f("Schéma", p.scheme), _f("Hôte", p.hostname)]
    if port:
        fields.append(_f("Port", port))
    fields.append(_f("Chemin", urllib.parse.unquote(p.path) or "/"))
    for k, v in query[:30]:
        fields.append(_f(f"?{k}", v))
    if p.fragment:
        fields.append(_f("Fragment", urllib.parse.unquote(p.fragment)))
    actions = [{"kind": "open_url", "label": "Ouvrir dans le navigateur", "value": url}]
    decoded = urllib.parse.unquote(url)
    if decoded != url:
        actions.append(_copy("URL décodée", decoded))
    kept = [(k, v) for k, v in query if not TRACKING_PARAMS.match(k)]
    if len(kept) != len(query):
        clean = urllib.parse.urlunsplit((p.scheme, p.netloc, p.path, urllib.parse.urlencode(kept), p.fragment))
        actions.append(_copy("URL sans traqueurs (utm_…, fbclid…)", clean))
    no_query = urllib.parse.urlunsplit((p.scheme, p.netloc, p.path, "", ""))
    if no_query != url:
        actions.append(_copy("URL sans paramètres", no_query))
    actions += [_copy("Hôte", p.hostname), _copy("Encodée (composant d'URL)", urllib.parse.quote(url, safe="")),
                _copy("Lien Markdown", f"[{p.hostname}]({url})")]
    if query:
        actions.append(_copy("Paramètres en JSON", json.dumps(dict(query), ensure_ascii=False, indent=2)))
    return {"type": "url", "icon": "🔗", "label": "URL", "summary": p.hostname, "fields": fields, "actions": actions}


def detect_email(t: str):
    m = EMAIL_RE.match(t)
    if not m or ".." in t:
        return None
    user, domain = m.group(1), m.group(2)
    addr = f"{user}@{domain}"
    return {"type": "email", "icon": "✉️", "label": "Adresse e-mail", "summary": addr,
            "fields": [_f("Utilisateur", user), _f("Domaine", domain)],
            "actions": [{"kind": "open_url", "label": "Écrire un message", "value": "mailto:" + addr},
                        _copy("En minuscules", addr.lower()), _copy("Domaine", domain),
                        _copy("Lien Markdown", f"[{addr}](mailto:{addr})")]}


def _json_depth(o, d=0):
    if d > 200:
        return d
    if isinstance(o, dict):
        return max([_json_depth(v, d + 1) for v in o.values()] or [d + 1])
    if isinstance(o, list):
        return max([_json_depth(v, d + 1) for v in o] or [d + 1])
    return d


def detect_json(t: str):
    if not t or t[0] not in "{[":
        return None
    try:
        data = json.loads(t)
    except (ValueError, RecursionError):
        return None
    if not isinstance(data, (dict, list)):
        return None
    pretty = json.dumps(data, ensure_ascii=False, indent=2)
    mini = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    kind = "Objet" if isinstance(data, dict) else "Tableau"
    fields = [_f("Type", kind, False), _f("Clés" if isinstance(data, dict) else "Éléments", len(data)),
              _f("Profondeur", _json_depth(data)), _f("Taille formatée / minifiée", f"{len(pretty)} / {len(mini)} car.")]
    if isinstance(data, dict):
        fields.append(_f("Clés de premier niveau", ", ".join(list(data)[:20]) + (" …" if len(data) > 20 else "")))
    actions = [_copy("JSON formaté", pretty), _copy("JSON minifié", mini),
               _copy("JSON trié par clés", json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True))]
    try:
        import yaml
        actions.append(_copy("YAML", yaml.safe_dump(data, allow_unicode=True, sort_keys=False)))
    except Exception:
        pass
    actions.append(_copy("Chaîne échappée", json.dumps(mini, ensure_ascii=False)))
    return {"type": "json", "icon": "🧾", "label": "JSON", "summary": f"{kind} · {len(data)} entrée(s)",
            "fields": fields, "actions": actions, "code": pretty[:20000]}


def _b64url_decode(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def detect_jwt(t: str):
    m = JWT_RE.match(t)
    if not m:
        return None
    try:
        header = json.loads(_b64url_decode(m.group(1)))
        payload = json.loads(_b64url_decode(m.group(2))) if m.group(2).startswith("eyJ") else None
    except (ValueError, binascii.Error):
        return None
    if not isinstance(header, dict) or "alg" not in header:
        return None
    fields = [_f("Algorithme", header.get("alg")), _f("Type", header.get("typ", "—"))]
    summary = header.get("alg")
    if isinstance(payload, dict):
        for key, label in (("sub", "Sujet (sub)"), ("iss", "Émetteur (iss)"), ("aud", "Audience (aud)"),
                           ("name", "Nom"), ("email", "E-mail")):
            if key in payload:
                fields.append(_f(label, payload[key]))
        for key, label in (("iat", "Émis le (iat)"), ("nbf", "Valide à partir de (nbf)"), ("exp", "Expire le (exp)")):
            if isinstance(payload.get(key), (int, float)):
                dt = datetime.fromtimestamp(payload[key], timezone.utc)
                fields.append(_f(label, dt.astimezone().strftime("%d/%m/%Y %H:%M:%S") + f" ({_relative(dt)})"))
        if isinstance(payload.get("exp"), (int, float)):
            expired = payload["exp"] < time.time()
            summary = "expiré" if expired else "valide (non vérifié)"
            fields.append(_f("État", "⛔ Expiré" if expired else "✅ Non expiré", False))
    actions = [_copy("En-tête (JSON)", json.dumps(header, ensure_ascii=False, indent=2))]
    if payload is not None:
        actions.append(_copy("Contenu / payload (JSON)", json.dumps(payload, ensure_ascii=False, indent=2)))
    actions.append(_copy("Jeton seul (sans « Bearer »)", f"{m.group(1)}.{m.group(2)}.{m.group(3)}"))
    return {"type": "jwt", "icon": "🔑", "label": "Jeton JWT", "summary": summary, "fields": fields, "actions": actions,
            "note": "La signature n'est pas vérifiée.",
            "code": json.dumps(payload, ensure_ascii=False, indent=2) if payload is not None else None}


def _parse_color(t: str):
    s = t.strip().lower()
    m = re.fullmatch(r"#([0-9a-f]{3,4}|[0-9a-f]{6}|[0-9a-f]{8})", s)
    if m:
        h = m.group(1)
        if len(h) in (3, 4):
            h = "".join(c * 2 for c in h)
        r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
        a = int(h[6:8], 16) / 255 if len(h) == 8 else 1.0
        return r, g, b, a
    m = re.fullmatch(r"rgba?\(\s*([\d.]+%?)\s*[, ]\s*([\d.]+%?)\s*[, ]\s*([\d.]+%?)\s*(?:[,/]\s*([\d.]+%?)\s*)?\)", s)
    if m:
        def ch(v):
            return round(float(v[:-1]) * 2.55) if v.endswith("%") else round(float(v))
        r, g, b = ch(m.group(1)), ch(m.group(2)), ch(m.group(3))
        a = m.group(4)
        a = 1.0 if a is None else (float(a[:-1]) / 100 if a.endswith("%") else float(a))
        if max(r, g, b) > 255 or a > 1:
            return None
        return r, g, b, a
    m = re.fullmatch(r"hsla?\(\s*([\d.]+)(?:deg)?\s*[, ]\s*([\d.]+)%\s*[, ]\s*([\d.]+)%\s*(?:[,/]\s*([\d.]+%?)\s*)?\)", s)
    if m:
        h, sat, li = float(m.group(1)) % 360, float(m.group(2)) / 100, float(m.group(3)) / 100
        if sat > 1 or li > 1:
            return None
        rr, gg, bb = colorsys.hls_to_rgb(h / 360, li, sat)
        a = m.group(4)
        a = 1.0 if a is None else (float(a[:-1]) / 100 if a.endswith("%") else float(a))
        return round(rr * 255), round(gg * 255), round(bb * 255), min(1.0, a)
    return None


def _luminance(r, g, b):
    def lin(c):
        c /= 255
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    return 0.2126 * lin(r) + 0.7152 * lin(g) + 0.0722 * lin(b)


def detect_color(t: str):
    c = _parse_color(t)
    if not c:
        return None
    r, g, b, a = c
    hexa = f"#{r:02x}{g:02x}{b:02x}" + (f"{round(a * 255):02x}" if a < 1 else "")
    h, l, s = colorsys.rgb_to_hls(r / 255, g / 255, b / 255)
    hv, sv, vv = colorsys.rgb_to_hsv(r / 255, g / 255, b / 255)
    k = 1 - max(r, g, b) / 255
    cmyk = (0, 0, 0, 100) if k == 1 else tuple(round((1 - x / 255 - k) / (1 - k) * 100) for x in (r, g, b)) + (round(k * 100),)
    rgb = f"rgb({r}, {g}, {b})" if a == 1 else f"rgba({r}, {g}, {b}, {a:g})"
    hsl = f"hsl({round(h * 360)}, {round(s * 100)}%, {round(l * 100)}%)" if a == 1 else \
        f"hsla({round(h * 360)}, {round(s * 100)}%, {round(l * 100)}%, {a:g})"
    lum = _luminance(r, g, b)
    on_white = (1.05) / (lum + 0.05)
    on_black = (lum + 0.05) / 0.05
    fields = [_f("HEX", hexa.upper()), _f("RGB", rgb), _f("HSL", hsl),
              _f("HSV", f"{round(hv * 360)}°, {round(sv * 100)}%, {round(vv * 100)}%"),
              _f("CMJN", "{}%, {}%, {}%, {}%".format(*cmyk)),
              _f("Contraste sur blanc / noir", f"{on_white:.2f}:1 / {on_black:.2f}:1"),
              _f("Texte lisible dessus", "noir" if on_black > on_white else "blanc", False)]
    actions = [_copy("HEX", hexa.upper()), _copy("hex (minuscules)", hexa), _copy("RGB", rgb), _copy("HSL", hsl),
               _copy("Composantes 0-1", f"{r / 255:.3f}, {g / 255:.3f}, {b / 255:.3f}"),
               _copy("Entier 0xRRGGBB", f"0x{r:02X}{g:02X}{b:02X}")]
    return {"type": "color", "icon": "🎨", "label": "Couleur", "summary": hexa.upper(), "fields": fields,
            "actions": actions, "color": rgb if a == 1 else f"rgba({r},{g},{b},{a})"}


def detect_ip(t: str):
    s = t.strip("[] ")
    port = None
    m = re.fullmatch(r"(\d{1,3}(?:\.\d{1,3}){3}):(\d{1,5})", s) or re.fullmatch(r"\[([0-9a-f:]+)\]:(\d{1,5})", t.strip(), re.I)
    if m:
        s, port = m.group(1), int(m.group(2))
    try:
        if "/" in s:
            net = ipaddress.ip_network(s, strict=False)
            iface = ipaddress.ip_interface(s)
        else:
            iface = None
            ip = ipaddress.ip_address(s)
    except ValueError:
        return None
    if "/" in s:
        hosts = net.num_addresses - (2 if net.version == 4 and net.prefixlen < 31 else 0)
        first = net.network_address + (1 if net.version == 4 and net.prefixlen < 31 else 0)
        last = net.broadcast_address - (1 if net.version == 4 and net.prefixlen < 31 else 0)
        fields = [_f("Réseau", net.with_prefixlen), _f("Masque", net.netmask), _f("Masque inverse", net.hostmask),
                  _f("Première adresse", first), _f("Dernière adresse", last), _f("Hôtes utilisables", f"{hosts:,}".replace(",", " "))]
        if net.version == 4:
            fields.insert(3, _f("Diffusion (broadcast)", net.broadcast_address))
        if iface.ip != net.network_address:
            fields.insert(0, _f("Adresse saisie", iface.ip))
        actions = [_copy("Réseau (CIDR)", net.with_prefixlen), _copy("Masque", net.netmask),
                   _copy("Plage", f"{first} - {last}"), _copy("Réseau + masque", f"{net.network_address} {net.netmask}")]
        return {"type": "ip", "icon": "🌐", "label": f"Réseau IPv{net.version}", "summary": net.with_prefixlen,
                "fields": fields, "actions": actions}
    kind = ("bouclage (loopback)" if ip.is_loopback else "privée" if ip.is_private else "lien local" if ip.is_link_local
            else "multicast" if ip.is_multicast else "réservée" if ip.is_reserved else "publique")
    fields = [_f("Version", f"IPv{ip.version}"), _f("Type", kind, False), _f("Nom DNS inverse", ip.reverse_pointer),
              _f("Entier", int(ip))]
    if port:
        fields.append(_f("Port", port))
    if ip.version == 4:
        fields.append(_f("Binaire", ".".join(f"{int(o):08b}" for o in str(ip).split("."))))
        fields.append(_f("IPv6 mappée", f"::ffff:{ip}"))
    else:
        fields.append(_f("Forme complète", ip.exploded))
    actions = [_copy("Adresse", ip), _copy("Nom DNS inverse", ip.reverse_pointer)]
    if ip.version == 6:
        actions += [_copy("Forme complète", ip.exploded), _copy("Forme compressée", ip.compressed)]
    if port:
        actions.append({"kind": "open_url", "label": "Ouvrir http://… dans le navigateur",
                        "value": f"http://{ip if ip.version == 4 else f'[{ip}]'}:{port}"})
    return {"type": "ip", "icon": "🌐", "label": f"Adresse IPv{ip.version}", "summary": f"{ip} · {kind}",
            "fields": fields, "actions": actions}


def detect_uuid(t: str):
    m = UUID_RE.match(t)
    if not m:
        return None
    try:
        u = uuidlib.UUID("".join(m.groups()))
    except ValueError:
        return None
    variant = {uuidlib.RFC_4122: "RFC 4122", uuidlib.RESERVED_NCS: "NCS", uuidlib.RESERVED_MICROSOFT: "Microsoft",
               uuidlib.RESERVED_FUTURE: "futur"}.get(u.variant, u.variant)
    fields = [_f("Version", u.version or "—"), _f("Variante", variant, False)]
    if u.version == 1:
        ts = (u.time - 0x01B21DD213814000) / 1e7
        fields.append(_f("Date de création", datetime.fromtimestamp(ts, timezone.utc).astimezone().strftime("%d/%m/%Y %H:%M:%S")))
    if u.version == 7:
        ms = int(u.hex[:12], 16)
        fields.append(_f("Date de création", datetime.fromtimestamp(ms / 1000, timezone.utc).astimezone().strftime("%d/%m/%Y %H:%M:%S")))
    s = str(u)
    return {"type": "uuid", "icon": "🆔", "label": "UUID", "summary": f"version {u.version or '?'}", "fields": fields,
            "actions": [_copy("Minuscules", s), _copy("MAJUSCULES", s.upper()), _copy("Sans tirets", u.hex),
                        _copy("Entre accolades", "{" + s.upper() + "}"), _copy("URN", u.urn),
                        _copy("Nouvel UUID v4 aléatoire", uuidlib.uuid4())]}


def detect_timestamp(t: str):
    s = t.strip()
    dt = None
    unit = None
    if re.fullmatch(r"\d{9,19}(?:\.\d+)?", s):
        n = float(s)
        digits = len(s.split(".")[0])
        for d, div, u in ((10, 1, "secondes"), (9, 1, "secondes"), (13, 1e3, "millisecondes"), (12, 1e3, "millisecondes"),
                          (16, 1e6, "microsecondes"), (19, 1e9, "nanosecondes")):
            if digits == d:
                secs = n / div
                if 3e8 <= secs <= 4.2e9:
                    dt, unit = datetime.fromtimestamp(secs, timezone.utc), u
                break
    elif DATE_RE.match(s):
        try:
            dt = datetime.fromisoformat(s.replace(" ", "T") if "T" not in s and " " in s else s)
        except ValueError:
            return None
        if dt.tzinfo is None:
            dt = dt.astimezone()
        unit = "date ISO 8601"
    else:
        m = FR_DATE_RE.match(s)
        if m:
            try:
                dt = datetime(int(m.group(3)), int(m.group(2)), int(m.group(1)), int(m.group(4) or 0),
                              int(m.group(5) or 0), int(m.group(6) or 0)).astimezone()
            except ValueError:
                return None
            unit = "date JJ/MM/AAAA"
    if not dt:
        return None
    fields, actions = _date_fields(dt)
    fields.insert(0, _f("Interprété comme", unit, False))
    return {"type": "timestamp", "icon": "🕒", "label": "Date / timestamp", "summary": dt.astimezone().strftime("%d/%m/%Y %H:%M"),
            "fields": fields, "actions": actions}


def _path_info(p: Path) -> dict | None:
    try:
        st = p.stat()
    except OSError:
        return None
    return {"is_dir": p.is_dir(), "size": st.st_size, "mtime": st.st_mtime}


def detect_path(t: str, check=True):
    s = t.strip()
    if s.lower().startswith("file:///"):
        import urllib.request
        s = urllib.request.url2pathname(urllib.parse.urlsplit(s).path)
    if not WIN_PATH_RE.match(s):
        return None
    s = s.strip('"')
    expanded = os.path.expanduser(os.path.expandvars(s))
    p = Path(expanded)
    info = _path_info(p) if check else None
    fields = []
    actions = []
    if info:
        kind = "Dossier" if info["is_dir"] else "Fichier"
        fields += [_f("Type", kind, False), _f("Nom", p.name or str(p)), _f("Dossier parent", p.parent)]
        if not info["is_dir"]:
            fields.append(_f("Taille", _human_size(info["size"]), False))
        fields.append(_f("Modifié le", datetime.fromtimestamp(info["mtime"]).strftime("%d/%m/%Y %H:%M:%S")))
        actions += [{"kind": "open_path", "label": "Ouvrir" if not info["is_dir"] else "Ouvrir le dossier", "value": str(p)},
                    {"kind": "reveal", "label": "Afficher dans l'Explorateur", "value": str(p)}]
        summary = f"{kind.lower()} existant"
    else:
        fields.append(_f("État", "introuvable sur ce PC", False))
        summary = "introuvable"
    if expanded != s:
        fields.insert(0, _f("Chemin développé", expanded))
    actions += [_copy("Barres obliques /", expanded.replace("\\", "/")), _copy("Barres échappées \\\\", expanded.replace("\\", "\\\\")),
                _copy("Entre guillemets", f'"{expanded}"'), _copy("Nom seul", p.name), _copy("Dossier parent", p.parent)]
    try:
        actions.append(_copy("URI file://", p.as_uri()))
    except ValueError:
        pass
    return {"type": "path", "icon": "📁", "label": "Chemin", "summary": summary, "fields": fields, "actions": actions}


def detect_phone(t: str):
    s = t.strip()
    if not PHONE_RE.match(s) or not re.search(r"\d", s):
        return None
    if s.lower().startswith("tel:"):
        s = s[4:].strip()
    digits = re.sub(r"\D", "", s)
    intl = s.startswith("+") or s.startswith("00")
    if s.startswith("00"):
        digits = digits[2:]
    if re.fullmatch(r"\d{4}[-.]\d{2}[-.]\d{2}", s) or re.fullmatch(r"\d+", s) and not s.startswith("0"):
        return None
    if not intl and re.fullmatch(r"0[1-9]\d{8}", digits):
        national = digits
        e164 = "+33" + digits[1:]
    elif intl and digits.startswith("33") and re.fullmatch(r"33[1-9]\d{8}", digits):
        national = "0" + digits[2:]
        e164 = "+" + digits
    elif intl and 8 <= len(digits) <= 15:
        e164 = "+" + digits
        return {"type": "phone", "icon": "📞", "label": "Numéro de téléphone", "summary": e164,
                "fields": [_f("Format international (E.164)", e164)],
                "actions": [{"kind": "open_url", "label": "Appeler (tel:)", "value": "tel:" + e164}, _copy("E.164", e164),
                            _copy("Chiffres seuls", digits)]}
    else:
        return None
    pretty = " ".join(national[i:i + 2] for i in range(0, 10, 2))
    intl_pretty = "+33 " + national[1] + " " + " ".join(national[i:i + 2] for i in range(2, 10, 2))
    kind = {"6": "mobile", "7": "mobile", "8": "numéro spécial", "9": "fixe (box / VoIP)"}.get(national[1], "fixe")
    return {"type": "phone", "icon": "📞", "label": "Numéro de téléphone (France)", "summary": f"{pretty} · {kind}",
            "fields": [_f("National", pretty), _f("International", intl_pretty), _f("E.164", e164), _f("Type", kind, False)],
            "actions": [{"kind": "open_url", "label": "Appeler (tel:)", "value": "tel:" + e164}, _copy("National", pretty),
                        _copy("International", intl_pretty), _copy("E.164", e164), _copy("Chiffres seuls", national)]}


def _to_dms(v: float, pos: str, neg: str) -> str:
    h = pos if v >= 0 else neg
    v = abs(v)
    d = int(v)
    m = int((v - d) * 60)
    s = (v - d - m / 60) * 3600
    return f"{d}°{m:02d}'{s:05.2f}\"{h}"


def detect_gps(t: str):
    s = t.strip()
    lat = lon = None
    m = GPS_DEC_RE.match(s)
    if m:
        lat, lon = float(m.group(1)), float(m.group(2))
    else:
        parts = GPS_DMS_RE.findall(s)
        if len(parts) == 2:
            vals = []
            for d, mi, se, h in parts:
                v = int(d) + int(mi) / 60 + float(se.replace(",", ".")) / 3600
                vals.append((-v if h.upper() in ("S", "W", "O") else v, h.upper()))
            if vals[0][1] in "NS" and vals[1][1] in "EWO":
                lat, lon = vals[0][0], vals[1][0]
            elif vals[1][1] in "NS" and vals[0][1] in "EWO":
                lat, lon = vals[1][0], vals[0][0]
    if lat is None or not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return None
    dec = f"{lat:.6f}, {lon:.6f}"
    dms = f"{_to_dms(lat, 'N', 'S')} {_to_dms(lon, 'E', 'O')}"
    return {"type": "gps", "icon": "📍", "label": "Coordonnées GPS", "summary": dec,
            "fields": [_f("Décimal", dec), _f("Degrés-minutes-secondes", dms), _f("Latitude", f"{lat:.6f}"),
                       _f("Longitude", f"{lon:.6f}")],
            "actions": [{"kind": "open_url", "label": "Ouvrir dans OpenStreetMap",
                         "value": f"https://www.openstreetmap.org/?mlat={lat:.6f}&mlon={lon:.6f}#map=16/{lat:.6f}/{lon:.6f}"},
                        {"kind": "open_url", "label": "Ouvrir dans Google Maps",
                         "value": f"https://www.google.com/maps?q={lat:.6f},{lon:.6f}"},
                        _copy("Décimal", dec), _copy("DMS", dms), _copy("GeoJSON", json.dumps(
                            {"type": "Point", "coordinates": [round(lon, 6), round(lat, 6)]}))]}


def detect_math(t: str):
    s = t.strip().rstrip("=").strip()
    if not s or len(s) > 500 or "\n" in s:
        return None
    if not re.fullmatch(r"[\d\s.,+\-*/×÷^()%!a-zπ_]+", s, re.I) or not re.search(r"\d|pi|π", s, re.I):
        return None
    is_number = bool(re.fullmatch(r"[+-]?(?:\d+(?:[.,]\d+)?|0x[0-9a-f]+|0b[01]+|0o[0-7]+)", s.replace(" ", ""), re.I))
    if not is_number and not re.search(r"[+\-*/×÷^%!(]", s):
        return None
    if re.fullmatch(r"[a-z_]+", s, re.I):
        return None
    try:
        v = safe_eval(s)
    except (MathError, ValueError, OverflowError, RecursionError):
        return None
    out = fmt_num(v)
    fields = []
    actions = [_copy("Résultat", out)]
    if not is_number:
        fields.append(_f("Expression", s))
    fields.append(_f("Résultat", out))
    if isinstance(v, float) and not v.is_integer():
        fields.append(_f("Format français", fmt_fr(v)))
        actions.append(_copy("Format français", fmt_fr(v)))
        actions.append(_copy("Arrondi à 2 décimales", f"{v:.2f}"))
    else:
        iv = int(v)
        if abs(iv) >= 1000:
            fields.append(_f("Format français", fmt_fr(iv)))
            actions.append(_copy("Format français", fmt_fr(iv)))
        if abs(iv) < 2 ** 64:
            hx = hex(iv) if iv >= 0 else "-" + hex(-iv)
            fields += [_f("Hexadécimal", hx), _f("Binaire", bin(iv)), _f("Octal", oct(iv))]
            actions += [_copy("Hexadécimal", hx), _copy("Binaire", bin(iv))]
        if 1024 <= iv < 2 ** 70:
            fields.append(_f("En octets", _human_size(iv), False))
        if abs(iv) >= 1e6:
            fields.append(_f("Scientifique", f"{iv:.6e}"))
    if not is_number:
        actions.append(_copy("Expression = résultat", f"{s} = {out}"))
    return {"type": "math" if not is_number else "number", "icon": "🧮",
            "label": "Calcul" if not is_number else "Nombre", "summary": f"= {out}" if not is_number else out,
            "fields": fields, "actions": actions}


MAGIC = [(b"\x89PNG\r\n\x1a\n", "image/png", "Image PNG"), (b"\xff\xd8\xff", "image/jpeg", "Image JPEG"),
         (b"GIF8", "image/gif", "Image GIF"), (b"RIFF", "image/webp", "Image WebP"), (b"%PDF", "application/pdf", "Document PDF"),
         (b"PK\x03\x04", "application/zip", "Archive ZIP / Office"), (b"\x1f\x8b", "application/gzip", "Archive GZIP")]


def detect_base64(t: str):
    s = t.strip()
    data_url = None
    m = re.match(r"^data:([\w/+.\-]+)?(;[\w=\-]+)*;base64,(.*)$", s, re.S)
    if m:
        data_url = m.group(1) or "application/octet-stream"
        s = m.group(3)
    compact = re.sub(r"\s+", "", s)
    if len(compact) < 8 or len(compact) > MAX_TEXT or compact.isdigit():
        return None
    std = re.fullmatch(r"[A-Za-z0-9+/]+={0,2}", compact)
    url = re.fullmatch(r"[A-Za-z0-9_\-]+={0,2}", compact)
    if not (std or url):
        return None
    if not data_url:
        if not re.search(r"[0-9+/=_\-]", compact) and not (re.search(r"[a-z]", compact) and re.search(r"[A-Z]", compact)):
            return None
        if len(compact) % 4 and "=" in compact:
            return None
    try:
        raw = base64.b64decode(compact + "=" * (-len(compact) % 4), altchars=None if std else b"-_", validate=True)
    except (binascii.Error, ValueError):
        return None
    if not raw:
        return None
    fields = [_f("Taille décodée", _human_size(len(raw)), False), _f("Alphabet", "standard" if std else "URL (-_)", False)]
    actions = []
    preview = None
    kind = None
    for sig, mime, label in MAGIC:
        if raw.startswith(sig) and (sig != b"RIFF" or raw[8:12] == b"WEBP"):
            kind = label
            if mime.startswith("image/") and len(raw) < 8_000_000:
                preview = f"data:{mime};base64," + base64.b64encode(raw).decode()
            break
    text = None
    if not kind:
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            text = None
        if text is not None:
            printable = sum(1 for c in text if c.isprintable() or c in "\r\n\t")
            if printable / max(1, len(text)) < 0.9:
                text = None
    if text is None and not kind and not data_url:
        strong_shape = (len(compact) >= 32 and len(compact) % 4 == 0 and re.search(r"\d", compact)
                        and re.search(r"[A-Za-z]", compact))
        if not compact.endswith("=") and not strong_shape:
            return None
    if kind:
        fields.insert(0, _f("Contenu", kind, False))
    if text is not None:
        fields.insert(0, _f("Texte décodé", text[:300] + ("…" if len(text) > 300 else "")))
        actions.append(_copy("Texte décodé", text))
        try:
            parsed = json.loads(text)
            if isinstance(parsed, (dict, list)):
                actions.append(_copy("JSON décodé formaté", json.dumps(parsed, ensure_ascii=False, indent=2)))
        except ValueError:
            pass
    elif not kind:
        fields.insert(0, _f("Contenu", "données binaires", False))
    actions.append(_copy("Hexadécimal", raw[:4096].hex(" ") + (" …" if len(raw) > 4096 else "")))
    if data_url:
        actions.append(_copy("Base64 seul (sans en-tête data:)", compact))
    elif kind and preview:
        actions.append(_copy("En data URL", preview))
    return {"type": "base64", "icon": "🔤", "label": "Base64" + (" (data URL)" if data_url else ""),
            "summary": kind or ("texte" if text is not None else "binaire"), "fields": fields, "actions": actions,
            "image": preview}


def _strip_accents(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", s) if unicodedata.category(c) != "Mn")


def detect_text(t: str):
    lines = t.splitlines()
    words = re.findall(r"\w+", t)
    fields = [_f("Caractères", len(t)), _f("Mots", len(words)), _f("Lignes", len(lines) or 1),
              _f("Octets (UTF-8)", len(t.encode("utf-8", "surrogatepass")))]
    sample = t[:MAX_SHOWN]
    slug = re.sub(r"[^a-z0-9]+", "-", _strip_accents(sample).lower()).strip("-")[:200]
    actions = [_copy("MAJUSCULES", sample.upper()), _copy("minuscules", sample.lower()),
               _copy("Première Lettre En Majuscule", sample.title()), _copy("Sans accents", _strip_accents(sample)),
               _copy("Espaces normalisés", re.sub(r"[ \t ]+", " ", "\n".join(x.strip() for x in sample.splitlines())).strip())]
    if slug:
        actions.append(_copy("Slug", slug))
    if len(lines) > 1:
        uniq = sorted(dict.fromkeys(x for x in lines if x.strip()), key=str.casefold)
        actions.append(_copy("Lignes triées sans doublons", "\n".join(uniq)))
        actions.append(_copy("Une seule ligne", " ".join(x.strip() for x in lines if x.strip())))
    actions += [_copy("Encodé URL", urllib.parse.quote(sample, safe="")),
                _copy("Encodé Base64", base64.b64encode(sample.encode("utf-8", "surrogatepass")).decode()),
                _copy("Chaîne JSON", json.dumps(sample, ensure_ascii=False)),
                _copy("SHA-256", hashlib.sha256(t.encode("utf-8", "surrogatepass")).hexdigest())]
    return {"type": "text", "icon": "📝", "label": "Texte", "summary": f"{len(t)} car. · {len(words)} mot(s)",
            "fields": fields, "actions": actions}


def detect_links(t: str):
    urls = list(dict.fromkeys(re.findall(r"https?://[^\s<>\"'()\[\]]+[^\s<>\"'()\[\].,;:!?]", t)))[:50]
    emails = list(dict.fromkeys(re.findall(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}", t)))[:50]
    if not urls and not emails:
        return None
    fields = [_f("Lien", u) for u in urls[:20]] + [_f("E-mail", e) for e in emails[:20]]
    actions = []
    if urls:
        actions.append(_copy(f"Tous les liens ({len(urls)})", "\n".join(urls)))
        actions += [{"kind": "open_url", "label": f"Ouvrir {urllib.parse.urlsplit(u).hostname}", "value": u} for u in urls[:5]]
    if emails:
        actions.append(_copy(f"Toutes les adresses e-mail ({len(emails)})", "\n".join(emails)))
    return {"type": "links", "icon": "🔎", "label": "Liens et adresses trouvés",
            "summary": f"{len(urls)} lien(s), {len(emails)} e-mail(s)", "fields": fields, "actions": actions}


STRONG = (detect_jwt, detect_json, detect_url, detect_email, detect_uuid, detect_color, detect_ip, detect_gps,
          detect_timestamp, detect_path, detect_phone)


def analyze_text(text: str, check_paths: bool = True) -> list[dict]:
    t = text.strip()
    if not t:
        return []
    found = []
    single = "\n" not in t
    for det in STRONG:
        if det in (detect_json,) or single or det is detect_path:
            try:
                r = det(t, check_paths) if det is detect_path else det(t)
            except Exception:
                log.exception("detector %s failed", det.__name__)
                r = None
            if r:
                found.append(r)
    strong_types = {d["type"] for d in found}
    if not strong_types or (strong_types == {"timestamp"} and re.fullmatch(r"\d+(?:\.\d+)?", t)):
        for det in (detect_math, detect_base64):
            try:
                r = det(t)
            except Exception:
                log.exception("detector %s failed", det.__name__)
                r = None
            if r:
                found.append(r)
    if not single or not found:
        links = detect_links(t) if not strong_types & {"url", "email", "json", "jwt"} else None
        if links:
            found.append(links)
    found.append(detect_text(text))
    return found


def analyze_files(files: list[str]) -> list[dict]:
    out = []
    for f in files[:20]:
        d = detect_path(f, True) or {"type": "path", "icon": "📁", "label": "Chemin", "summary": f, "fields": [], "actions": []}
        d["label"] = "Fichier copié" if not Path(f).is_dir() else "Dossier copié"
        d["summary"] = Path(f).name or f
        out.append(d)
    out.append({"type": "text", "icon": "📝", "label": "Liste des chemins", "summary": f"{len(files)} élément(s)",
                "fields": [], "actions": [_copy("Chemins (un par ligne)", "\n".join(files)),
                                          _copy("Chemins entre guillemets", " ".join(f'"{f}"' for f in files)),
                                          _copy("Noms seuls", "\n".join(Path(f).name for f in files))]})
    return out


# ── Actions ───────────────────────────────────────────────────────────────────

@module.on("seq")
async def seq(payload: dict):
    return {"seq": clip_seq(), "supported": IS_WINDOWS}


@module.on("analyze")
async def analyze(payload: dict):
    if payload.get("text") is not None:
        text = str(payload["text"])[:MAX_TEXT]
        dets = await asyncio.to_thread(analyze_text, text)
        return {"source": "input", "kind": "text", "text": text[:MAX_SHOWN], "truncated": len(text) > MAX_SHOWN,
                "detections": dets, "seq": clip_seq()}
    if not IS_WINDOWS:
        return {"error": "Lecture du presse-papier disponible uniquement sous Windows", "code": "UNSUPPORTED"}

    def _work():
        s = clip_seq()
        content = clip_read()
        res = {"source": "clipboard", "seq": s, "kind": content["kind"], "text": "", "detections": []}
        if content["kind"] == "text":
            text = content["text"]
            res["text"] = text[:MAX_SHOWN]
            res["truncated"] = len(text) > MAX_SHOWN
            res["detections"] = analyze_text(text)
        elif content["kind"] == "files":
            res["files"] = content["files"]
            res["text"] = "\n".join(content["files"])
            res["detections"] = analyze_files(content["files"])
        elif content["kind"] == "image":
            info = _clip_image_info()
            if info:
                res["detections"] = [{"type": "image", "icon": "🖼️", "label": "Image", "summary": f"{info['width']}×{info['height']}",
                                      "fields": [_f("Dimensions", f"{info['width']} × {info['height']} px"), _f("Mode", info["mode"])],
                                      "actions": [], "image": info["thumb"]}]
        return res
    try:
        return await asyncio.to_thread(_work)
    except OSError as exc:
        return {"error": str(exc), "code": "CLIPBOARD_BUSY"}


@module.on("copy")
async def copy(payload: dict):
    text = payload.get("text")
    if not isinstance(text, str):
        return {"error": "Texte manquant", "code": "BAD_REQUEST"}
    if not IS_WINDOWS:
        return {"error": "Écriture du presse-papier disponible uniquement sous Windows", "code": "UNSUPPORTED"}
    ok = await asyncio.to_thread(clip_write, text)
    if not ok:
        return {"error": "Impossible d'accéder au presse-papier (occupé par une autre application)", "code": "CLIPBOARD_BUSY"}
    return {"ok": True, "seq": clip_seq()}


@module.on("open_url")
async def open_url(payload: dict):
    url = str(payload.get("url") or "").strip()
    scheme = urllib.parse.urlsplit(url).scheme.lower()
    if scheme not in OPEN_SCHEMES or any(c in url for c in "\r\n\x00"):
        return {"error": "Seuls les liens http(s), mailto, tel et ftp peuvent être ouverts", "code": "BAD_URL"}
    if IS_WINDOWS:
        await asyncio.to_thread(os.startfile, url)
    else:
        import webbrowser
        await asyncio.to_thread(webbrowser.open, url)
    return {"ok": True}


def _existing(path) -> Path | None:
    p = Path(os.path.expandvars(os.path.expanduser(str(path or "").strip().strip('"'))))
    return p if str(path or "").strip() and p.exists() else None


@module.on("open_path")
async def open_path(payload: dict):
    p = await asyncio.to_thread(_existing, payload.get("path"))
    if not p:
        return {"error": "Chemin introuvable", "code": "NOT_FOUND"}
    if not IS_WINDOWS:
        return {"error": "Fonction disponible uniquement sous Windows", "code": "UNSUPPORTED"}
    await asyncio.to_thread(os.startfile, str(p))
    return {"ok": True}


@module.on("reveal")
async def reveal(payload: dict):
    p = await asyncio.to_thread(_existing, payload.get("path"))
    if not p:
        return {"error": "Chemin introuvable", "code": "NOT_FOUND"}
    if not IS_WINDOWS:
        return {"error": "Fonction disponible uniquement sous Windows", "code": "UNSUPPORTED"}
    subprocess.Popen(f'explorer.exe /select,"{p.resolve()}"', creationflags=subprocess.CREATE_NO_WINDOW)
    return {"ok": True}


@module.on("settings")
async def settings(payload: dict):
    return {"watch_default": bool(module.config.get("watch_default", True)), "supported": IS_WINDOWS}
