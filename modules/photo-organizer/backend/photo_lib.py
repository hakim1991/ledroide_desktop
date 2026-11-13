import hashlib
import math
import os
import re
from datetime import datetime

IMAGE_EXTS = {".jpg", ".jpeg", ".jpe", ".png", ".webp", ".tif", ".tiff", ".bmp", ".gif"}
IFD_EXIF, IFD_GPS = 0x8769, 0x8825
TAG_MAKE, TAG_MODEL, TAG_DATETIME, TAG_DT_ORIGINAL = 0x010F, 0x0110, 0x0132, 0x9003
TAG_ORIENTATION = 0x0112
EXIF_LABELS = {
    0x829A: "Exposition", 0x829D: "Ouverture", 0x8827: "ISO", 0x920A: "Focale", 0xA434: "Objectif",
    0x9209: "Flash", 0xA405: "Focale (35 mm)",
}
MONTHS = ["janvier", "février", "mars", "avril", "mai", "juin", "juillet", "août", "septembre", "octobre",
          "novembre", "décembre"]
FORBIDDEN = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}


class PhotoError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def pil():
    try:
        from PIL import Image, ImageOps
    except ImportError:
        raise PhotoError("MISSING_DEP", "Pillow n'est pas installé : installez les dépendances du module")
    Image.MAX_IMAGE_PIXELS = 300_000_000
    return Image, ImageOps


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError, ZeroDivisionError):
        return None


def _dms(values, ref):
    try:
        d, m, s = (float(x) for x in values)
    except (TypeError, ValueError, ZeroDivisionError):
        return None
    dec = d + m / 60 + s / 3600
    if isinstance(ref, bytes):
        ref = ref.decode("ascii", "ignore")
    if str(ref).strip().upper() in ("S", "W"):
        dec = -dec
    return round(dec, 7)


def _clean(s) -> str:
    if isinstance(s, bytes):
        s = s.decode("utf-8", "ignore")
    return re.sub(r"\s+", " ", str(s or "").replace("\x00", "")).strip()


def camera_name(make: str, model: str) -> str:
    make, model = _clean(make), _clean(model)
    short = make.split(" ")[0] if make else ""
    if short and model.lower().startswith(short.lower()):
        return model
    return f"{make} {model}".strip()


def parse_exif_date(raw) -> str | None:
    m = re.match(r"(\d{4}):(\d{2}):(\d{2})[ T](\d{2}):(\d{2}):(\d{2})", _clean(raw))
    if not m or m[1] == "0000":
        return None
    try:
        return datetime(*(int(x) for x in m.groups())).isoformat()
    except ValueError:
        return None


def _bits_to_hex(bits: list[bool]) -> str:
    v = 0
    for b in bits:
        v = (v << 1) | int(b)
    return f"{v:016x}"


_DCT = [[math.cos((2 * x + 1) * u * math.pi / 64) for x in range(32)] for u in range(8)]


def dhash(gray) -> str:
    small = gray.resize((9, 8))
    px = list(small.getdata())
    return _bits_to_hex([px[r * 9 + c] > px[r * 9 + c + 1] for r in range(8) for c in range(8)])


def phash(gray) -> str:
    small = gray.resize((32, 32))
    px = list(small.getdata())
    rows = [px[i * 32:(i + 1) * 32] for i in range(32)]
    tmp = [[sum(_DCT[u][x] * rows[y][x] for x in range(32)) for u in range(8)] for y in range(32)]
    coeffs = [sum(_DCT[v][y] * tmp[y][u] for y in range(32)) for v in range(8) for u in range(8)]
    ac = sorted(coeffs[1:])
    med = (ac[31] + ac[32]) / 2
    return _bits_to_hex([c > med for c in coeffs])


def analyze(path: str, thumb_path: str, thumb_size: int = 320) -> dict:
    Image, ImageOps = pil()
    st = os.stat(path)
    out = {"size": st.st_size, "mtime": st.st_mtime, "width": 0, "height": 0, "taken": None, "taken_src": "mtime",
           "camera": "", "lat": None, "lon": None, "dhash": "", "phash": ""}
    with Image.open(path) as img:
        out["width"], out["height"] = img.size
        exif = img.getexif()
        if exif:
            try:
                sub = exif.get_ifd(IFD_EXIF)
            except Exception:
                sub = {}
            taken = parse_exif_date(sub.get(TAG_DT_ORIGINAL) or exif.get(TAG_DATETIME) or "")
            if taken:
                out["taken"], out["taken_src"] = taken, "exif"
            out["camera"] = camera_name(exif.get(TAG_MAKE, ""), exif.get(TAG_MODEL, ""))[:120]
            try:
                gps = exif.get_ifd(IFD_GPS)
            except Exception:
                gps = {}
            if gps:
                lat, lon = _dms(gps.get(2), gps.get(1, "N")), _dms(gps.get(4), gps.get(3, "E"))
                if lat is not None and lon is not None and not (lat == 0 and lon == 0) and abs(lat) <= 90 and abs(lon) <= 180:
                    out["lat"], out["lon"] = lat, lon
            if exif.get(TAG_ORIENTATION) in (5, 6, 7, 8):
                out["width"], out["height"] = out["height"], out["width"]
        if img.format == "JPEG":
            img.draft("RGB", (thumb_size * 2, thumb_size * 2))
        work = ImageOps.exif_transpose(img)
        if work.mode not in ("RGB", "L"):
            bg = Image.new("RGB", work.size, (24, 24, 27))
            rgba = work.convert("RGBA")
            bg.paste(rgba, mask=rgba.split()[3])
            work = bg
        work = work.convert("RGB")
        gray = work.convert("L")
        out["dhash"], out["phash"] = dhash(gray), phash(gray)
        work.thumbnail((thumb_size, thumb_size))
        work.save(thumb_path, "JPEG", quality=82)
    if not out["taken"]:
        out["taken"] = datetime.fromtimestamp(st.st_mtime).replace(microsecond=0).isoformat()
    return out


def exif_details(path: str) -> dict:
    Image, _ = pil()
    details = {}
    with Image.open(path) as img:
        details["Format"] = img.format or ""
        details["Mode"] = img.mode
        exif = img.getexif()
        try:
            sub = exif.get_ifd(IFD_EXIF)
        except Exception:
            sub = {}
        for tag, label in EXIF_LABELS.items():
            v = sub.get(tag, exif.get(tag))
            if v is None:
                continue
            f = _num(v)
            if tag == 0x829A and f:
                v = f"1/{round(1 / f)} s" if f < 1 else f"{f:g} s"
            elif tag == 0x829D and f:
                v = f"f/{f:.1f}"
            elif tag in (0x920A, 0xA405) and f:
                v = f"{f:g} mm"
            elif tag == 0x9209 and isinstance(v, int):
                v = "Déclenché" if v & 1 else "Non déclenché"
            details[label] = _clean(v)[:120]
    return details


def sha256(path: str, cancel=None) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(1024 * 1024):
            if cancel and cancel():
                raise PhotoError("CANCELLED", "Annulé")
            h.update(chunk)
    return h.hexdigest()


def safe_component(name: str) -> str:
    name = FORBIDDEN.sub("_", name).strip().rstrip(".")
    name = re.sub(r"_{2,}", "_", name)
    if name.upper().split(".")[0] in RESERVED:
        name = "_" + name
    return name[:180]


TOKEN = re.compile(r"\{(\w+)(?::([^{}]*))?\}")


def format_pattern(pattern: str, photo: dict, n: int, allow_sep: bool = False) -> str:
    try:
        dt = datetime.fromisoformat(photo["taken"]) if photo.get("taken") else datetime.fromtimestamp(photo["mtime"])
    except (TypeError, ValueError):
        dt = datetime.fromtimestamp(photo.get("mtime") or 0)
    stem = os.path.splitext(photo["name"])[0]

    def repl(m):
        key, spec = m.group(1).lower(), m.group(2)
        if key == "date":
            fmt = spec or "%Y-%m-%d_%H%M%S"
            if not re.fullmatch(r"(%[YmdHMSjbBaAyp%]|[^%])*", fmt):
                raise PhotoError("BAD_PATTERN", f"Format de date invalide : {fmt}")
            return dt.strftime(fmt)
        if key == "appareil":
            return (photo.get("camera") or spec or "inconnu").replace(" ", "-")
        if key == "n":
            width = int(spec) if spec and spec.isdigit() and int(spec) <= 9 else 0
            return str(n).zfill(width)
        if key == "nom":
            return stem
        if key in ("annee", "année"):
            return f"{dt.year:04d}"
        if key == "mois":
            return f"{dt.month:02d}"
        if key == "mois_nom":
            return MONTHS[dt.month - 1]
        if key == "jour":
            return f"{dt.day:02d}"
        if key == "dossier":
            return os.path.basename(os.path.dirname(photo["path"]))
        raise PhotoError("BAD_PATTERN", f"Jeton inconnu : {{{key}}}")
    out = TOKEN.sub(repl, pattern)
    if allow_sep:
        parts = [safe_component(p) for p in re.split(r"[\\/]+", out)]
        parts = [p for p in parts if p and p not in (".", "..")]
        if not parts:
            raise PhotoError("BAD_PATTERN", "Le modèle produit un chemin vide")
        return os.path.join(*parts)
    out = safe_component(out)
    if not out:
        raise PhotoError("BAD_PATTERN", "Le modèle produit un nom vide")
    return out
