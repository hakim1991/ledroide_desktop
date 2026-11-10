import asyncio
import base64
import io
import logging
import os
import re
import shutil
import struct
import uuid
from datetime import datetime
from fractions import Fraction

from ledroide_sdk import Module

log = logging.getLogger("ledroide.exif-tool")

module = Module(__file__)
router = module.router

SUPPORTED = {".jpg": "JPEG", ".jpeg": "JPEG", ".jpe": "JPEG", ".png": "PNG", ".webp": "WEBP",
             ".tif": "TIFF", ".tiff": "TIFF"}
HEIC = {".heic", ".heif", ".avif"}
IFD_EXIF, IFD_GPS, IFD_INTEROP = 0x8769, 0x8825, 0xA005
TAG_ORIENTATION, TAG_DATETIME, TAG_DT_ORIGINAL, TAG_DT_DIGITIZED = 0x0112, 0x0132, 0x9003, 0x9004
STRUCTURAL = {0x0111, 0x0117, 0x0116, 0x0144, 0x0145, 0x0201, 0x0202, 0x0103, 0x0106, 0x0115, 0x011C,
              0x0102, 0x0100, 0x0101, 0x0153, 0x0152, 0x013D, 0x0140, 0x02BC, 0x8773, 0x83BB, 0x8649,
              IFD_EXIF, IFD_GPS, IFD_INTEROP, 0x927C}
MAX_FOLDER_LIST = 500

GROUPS = {
    "Appareil": {0x010F: "Fabricant", 0x0110: "Modèle", 0xA431: "N° de série du boîtier", 0xA430: "Propriétaire"},
    "Objectif": {0xA433: "Fabricant de l'objectif", 0xA434: "Modèle d'objectif", 0xA432: "Caractéristiques",
                 0x920A: "Focale", 0xA405: "Focale équiv. 24×36", 0xA435: "N° de série de l'objectif",
                 0x9205: "Ouverture max."},
    "Exposition": {0x829A: "Temps de pose", 0x829D: "Ouverture", 0x8827: "Sensibilité ISO",
                   0x9204: "Correction d'exposition", 0x8822: "Programme", 0x9207: "Mesure",
                   0x9209: "Flash", 0xA403: "Balance des blancs", 0xA402: "Mode d'exposition",
                   0xA406: "Type de scène", 0x9201: "Vitesse (APEX)", 0x9202: "Ouverture (APEX)"},
    "Date": {TAG_DT_ORIGINAL: "Prise de vue", TAG_DT_DIGITIZED: "Numérisation", TAG_DATETIME: "Modification",
             0x9011: "Fuseau (prise de vue)", 0x9010: "Fuseau (modification)", 0x9291: "Fraction de seconde"},
    "Logiciel & auteur": {0x0131: "Logiciel", 0x013B: "Auteur", 0x8298: "Copyright",
                          0x010E: "Description", 0x9286: "Commentaire", 0xA420: "Identifiant unique"},
    "Image": {TAG_ORIENTATION: "Orientation", 0xA002: "Largeur (EXIF)", 0xA003: "Hauteur (EXIF)",
              0x011A: "Résolution X", 0x011B: "Résolution Y", 0x0128: "Unité de résolution",
              0xA001: "Espace colorimétrique", 0x9000: "Version EXIF"},
}
TAG_GROUP = {tag: (g, label) for g, tags in GROUPS.items() for tag, label in tags.items()}
ENUMS = {
    TAG_ORIENTATION: {1: "Normale", 2: "Miroir horizontal", 3: "Rotation 180°", 4: "Miroir vertical",
                      5: "Miroir + rotation 90° anti-horaire", 6: "Rotation 90° horaire",
                      7: "Miroir + rotation 90° horaire", 8: "Rotation 90° anti-horaire"},
    0x8822: {0: "Non défini", 1: "Manuel", 2: "Programme normal", 3: "Priorité ouverture",
             4: "Priorité vitesse", 5: "Créatif", 6: "Action", 7: "Portrait", 8: "Paysage"},
    0x9207: {0: "Inconnue", 1: "Moyenne", 2: "Pondérée centrale", 3: "Spot", 4: "Multi-spot",
             5: "Matricielle", 6: "Partielle", 255: "Autre"},
    0xA403: {0: "Automatique", 1: "Manuelle"},
    0xA402: {0: "Automatique", 1: "Manuelle", 2: "Bracketing auto"},
    0xA406: {0: "Standard", 1: "Paysage", 2: "Portrait", 3: "Nuit"},
    0xA001: {1: "sRGB", 0xFFFF: "Non calibré"},
    0x0128: {1: "Aucune", 2: "pouce", 3: "cm"},
}


class ExifError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _pil():
    try:
        from PIL import Image, ImageOps
    except ImportError:
        raise ExifError("MISSING_DEP", "Pillow n'est pas installé : installez les dépendances du module")
    return Image, ImageOps


def _uploads_dir() -> str:
    path = module.data_path("uploads")
    path.mkdir(parents=True, exist_ok=True)
    return str(path)


def _is_upload(path: str) -> bool:
    up = os.path.normcase(os.path.realpath(_uploads_dir()))
    return os.path.normcase(os.path.realpath(path)).startswith(up + os.sep)


def _check_file(path: str) -> tuple[str, str]:
    path = (path or "").strip().strip('"')
    if not path:
        raise ExifError("PATH_REQUIRED", "Indiquez le chemin d'une image")
    if not os.path.isabs(path):
        raise ExifError("BAD_PATH", "Le chemin doit être absolu")
    ext = os.path.splitext(path)[1].lower()
    if ext in HEIC:
        raise ExifError("UNSUPPORTED_HEIC", "Les fichiers HEIC/HEIF/AVIF ne sont pas pris en charge. "
                                            "Convertissez-les d'abord en JPEG (module Convertisseur d'images).")
    if ext not in SUPPORTED:
        raise ExifError("UNSUPPORTED_FORMAT", f"Format non pris en charge ({ext or 'sans extension'}) : JPEG, PNG, WebP ou TIFF")
    if not os.path.isfile(path):
        raise ExifError("NOT_FOUND", f"Fichier introuvable : {path}")
    return path, SUPPORTED[ext]


# ── Formatting ───────────────────────────────────────────────────────────────

def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError, ZeroDivisionError):
        return None


def _fmt_value(tag: int, v) -> str:
    if tag in ENUMS and isinstance(v, int):
        return ENUMS[tag].get(v, str(v))
    if tag == 0x829A:
        f = _num(v)
        if f is None or f <= 0:
            return str(v)
        return f"1/{round(1 / f)} s" if f < 1 else f"{f:g} s"
    if tag in (0x829D, 0x9205):
        f = _num(v)
        return f"f/{f:.1f}".replace(".0", "") if f else str(v)
    if tag in (0x920A, 0xA405):
        f = _num(v)
        return f"{f:g} mm" if f is not None else str(v)
    if tag == 0x9204:
        f = _num(v)
        return f"{f:+.2g} IL" if f is not None else str(v)
    if tag == 0x9209 and isinstance(v, int):
        return "Déclenché" if v & 1 else "Non déclenché"
    if tag == 0xA432 and isinstance(v, tuple):
        vals = [_num(x) for x in v]
        if len(vals) == 4 and None not in vals:
            fl = f"{vals[0]:g}" if vals[0] == vals[1] else f"{vals[0]:g}-{vals[1]:g}"
            ap = f"{vals[2]:g}" if vals[2] == vals[3] else f"{vals[2]:g}-{vals[3]:g}"
            return f"{fl} mm f/{ap}"
    if tag == 0x8827 and isinstance(v, tuple):
        return ", ".join(map(str, v))
    if isinstance(v, bytes):
        if tag in (0x9000, 0xA000):
            return v.decode("ascii", "replace")
        if tag == 0x9286 and len(v) > 8:
            return v[8:].decode("utf-8", "replace").strip("\x00 ")
        text = v.rstrip(b"\x00")
        if text and all(32 <= b < 127 for b in text):
            return text.decode("ascii")
        if len(v) <= 4:
            return " ".join(str(b) for b in v)
        return f"<{len(v)} octets>"
    if isinstance(v, str):
        return v.strip("\x00 ").strip()
    if isinstance(v, tuple):
        return ", ".join(_fmt_value(-1, x) for x in v[:16]) + ("…" if len(v) > 16 else "")
    f = _num(v) if not isinstance(v, int) else None
    if f is not None:
        return f"{f:.6g}"
    return str(v)


def _dms(values, ref) -> float | None:
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


def _gps(gps: dict) -> dict | None:
    if not gps:
        return None
    lat, lon = _dms(gps.get(2), gps.get(1, "N")), _dms(gps.get(4), gps.get(3, "E"))
    out: dict = {"raw": {}}
    from PIL.ExifTags import GPSTAGS
    for k, v in gps.items():
        out["raw"][GPSTAGS.get(k, f"0x{k:04X}")] = _fmt_value(-1, v)
    if lat is None or lon is None or (lat == 0 and lon == 0 and not gps.get(2)):
        out.update(lat=None, lon=None)
        return out
    alt = _num(gps.get(6))
    if alt is not None and gps.get(5) in (b"\x01", 1):
        alt = -alt
    out.update(lat=lat, lon=lon, alt=round(alt, 1) if alt is not None else None,
               text=f"{lat:.6f}, {lon:.6f}",
               osm=f"https://www.openstreetmap.org/?mlat={lat:.6f}&mlon={lon:.6f}#map=16/{lat:.6f}/{lon:.6f}")
    return out


def _collect(exif) -> tuple[list[dict], dict | None, int]:
    from PIL.ExifTags import TAGS
    merged: dict[int, object] = {}
    for k, v in exif.items():
        merged[k] = v
    for ifd in (IFD_EXIF, IFD_INTEROP):
        try:
            for k, v in exif.get_ifd(ifd).items():
                merged.setdefault(k, v)
        except Exception:
            pass
    try:
        gps_ifd = dict(exif.get_ifd(IFD_GPS))
    except Exception:
        gps_ifd = {}
    groups: dict[str, list] = {g: [] for g in [*GROUPS, "Autres"]}
    count = 0
    for tag, v in merged.items():
        if tag in STRUCTURAL:
            if tag == 0x927C and isinstance(v, bytes):
                groups["Autres"].append({"tag": f"0x{tag:04X}", "label": "MakerNote", "value": f"<{len(v)} octets>"})
                count += 1
            continue
        count += 1
        g, label = TAG_GROUP.get(tag, ("Autres", TAGS.get(tag, f"Tag 0x{tag:04X}")))
        groups[g].append({"tag": f"0x{tag:04X}", "label": label, "value": _fmt_value(tag, v)})
    order = {g: list(tags) for g, tags in GROUPS.items()}
    result = []
    for g, items in groups.items():
        if not items:
            continue
        if g in order:
            items.sort(key=lambda it: order[g].index(int(it["tag"], 16)) if int(it["tag"], 16) in order[g] else 999)
        else:
            items.sort(key=lambda it: it["label"].lower())
        result.append({"title": g, "items": items})
    return result, _gps(gps_ifd), count + len(gps_ifd)


def _thumbnail(img) -> str:
    Image, ImageOps = _pil()
    try:
        img.seek(0)
    except EOFError:
        pass
    t = ImageOps.exif_transpose(img.copy()) if hasattr(img, "getexif") else img.copy()
    t.thumbnail((360, 360))
    if t.mode not in ("RGB", "RGBA"):
        t = t.convert("RGBA" if "A" in t.getbands() or t.mode == "P" else "RGB")
    buf = io.BytesIO()
    t.save(buf, "PNG" if t.mode == "RGBA" else "JPEG", quality=85)
    mime = "image/png" if t.mode == "RGBA" else "image/jpeg"
    return f"data:{mime};base64,{base64.b64encode(buf.getvalue()).decode()}"


def _read_info(path: str) -> dict:
    path, fmt = _check_file(path)
    Image, _ = _pil()
    try:
        with Image.open(path) as img:
            exif = img.getexif()
            groups, gps, count = _collect(exif)
            other = []
            for k, v in (img.info or {}).items():
                if k in ("exif", "icc_profile", "dpi", "transparency", "duration", "loop", "background",
                         "compression", "progressive", "progression", "jfif", "jfif_version", "jfif_unit",
                         "jfif_density", "adobe", "adobe_transform", "interlace", "gamma", "aspect"):
                    continue
                if isinstance(v, (str, bytes)):
                    text = v if isinstance(v, str) else v.decode("utf-8", "replace")
                    label = "XMP" if "xmp" in k.lower() else k
                    other.append({"tag": k, "label": label,
                                  "value": text[:300] + ("…" if len(text) > 300 else "")})
            if other:
                groups.append({"title": "Autres métadonnées (texte, XMP)", "items": other})
            info = {
                "path": path, "name": os.path.basename(path), "format": img.format or fmt,
                "width": img.width, "height": img.height, "mode": img.mode,
                "frames": getattr(img, "n_frames", 1),
                "size_bytes": os.path.getsize(path), "icc": bool(img.info.get("icc_profile")),
                "groups": groups, "gps": gps, "count": count + len(other),
                "has_xmp": any(it["label"] == "XMP" for it in other) or "xmp" in img.info,
                "date": _exif_date(exif), "thumbnail": _thumbnail(img),
                "uploaded": _is_upload(path),
            }
    except ExifError:
        raise
    except Exception as exc:
        raise ExifError("BAD_IMAGE", f"Image illisible : {exc}")
    return info


def _exif_date(exif) -> str | None:
    try:
        raw = exif.get_ifd(IFD_EXIF).get(TAG_DT_ORIGINAL) or exif.get(TAG_DATETIME)
    except Exception:
        raw = exif.get(TAG_DATETIME)
    if not raw:
        return None
    m = re.match(r"(\d{4}):(\d{2}):(\d{2})[ T](\d{2}):(\d{2}):(\d{2})", str(raw).strip())
    return f"{m[1]}-{m[2]}-{m[3]}T{m[4]}:{m[5]}:{m[6]}" if m else None


# ── Writing ──────────────────────────────────────────────────────────────────

def _unique(path: str) -> str:
    if not os.path.exists(path):
        return path
    stem, ext = os.path.splitext(path)
    i = 2
    while os.path.exists(f"{stem}_{i}{ext}"):
        i += 1
    return f"{stem}_{i}{ext}"


def _out_path(path: str, suffix: str, out_dir: str | None = None) -> str:
    stem, ext = os.path.splitext(os.path.basename(path))
    folder = out_dir or os.path.dirname(path)
    return _unique(os.path.join(folder, f"{stem}{suffix}{ext}"))


def _jpeg_segments(data: bytes) -> tuple[list[tuple[int, bytes]], bytes]:
    if data[:2] != b"\xff\xd8":
        raise ExifError("BAD_IMAGE", "Fichier JPEG invalide")
    pos, segs = 2, []
    while pos < len(data):
        if data[pos] != 0xFF:
            raise ExifError("BAD_IMAGE", "Structure JPEG inattendue")
        marker = data[pos + 1]
        if marker == 0xFF:
            pos += 1
            continue
        if marker == 0xDA:
            return segs, data[pos:]
        if 0xD0 <= marker <= 0xD7 or marker == 0x01:
            segs.append((marker, data[pos:pos + 2]))
            pos += 2
            continue
        (length,) = struct.unpack(">H", data[pos + 2:pos + 4])
        segs.append((marker, data[pos:pos + 2 + length]))
        pos += 2 + length
    raise ExifError("BAD_IMAGE", "JPEG tronqué (pas de données d'image)")


def _jpeg_rewrite(data: bytes, exif_bytes: bytes | None, strip_all: bool) -> bytes:
    """Lossless: only the metadata segments change, the compressed image is untouched."""
    segs, rest = _jpeg_segments(data)
    kept = []
    for marker, raw in segs:
        body = raw[4:]
        if marker == 0xE1 and body.startswith(b"Exif\x00\x00"):
            continue
        if strip_all and (marker == 0xE1 or marker == 0xED or marker == 0xFE):
            continue
        kept.append((marker, raw))
    insert = 0
    while insert < len(kept) and kept[insert][0] == 0xE0:
        insert += 1
    if exif_bytes:
        if len(exif_bytes) + 2 > 0xFFFF:
            raise ExifError("EXIF_TOO_LARGE", "Bloc EXIF trop volumineux")
        kept.insert(insert, (0xE1, b"\xff\xe1" + struct.pack(">H", len(exif_bytes) + 2) + exif_bytes))
    return b"\xff\xd8" + b"".join(raw for _, raw in kept) + rest


def _minimal_exif(exif):
    Image, _ = _pil()
    new = Image.Exif()
    orient = exif.get(TAG_ORIENTATION)
    if orient and orient != 1:
        new[TAG_ORIENTATION] = orient
    return new


def _write_with_exif(src: str, fmt: str, dst: str, build, strip_all: bool):
    """build(exif) -> Exif to write (or None). Writes dst atomically."""
    Image, _ = _pil()
    with open(src, "rb") as f:
        data = f.read()
    try:
        img = Image.open(io.BytesIO(data))
    except Exception as exc:
        raise ExifError("BAD_IMAGE", f"Image illisible ({type(exc).__name__})")
    with img:
        exif = img.getexif()
        new_exif = build(exif)
        exif_bytes = new_exif.tobytes() if new_exif is not None and len(new_exif) else None
        if fmt == "JPEG":
            out = _jpeg_rewrite(data, exif_bytes, strip_all)
        else:
            if getattr(img, "n_frames", 1) > 1:
                raise ExifError("MULTI_FRAME", "Images animées ou multipages non prises en charge")
            img.load()
            clean = img.copy()
            clean.info = {}
            kwargs = {}
            if img.info.get("icc_profile"):
                kwargs["icc_profile"] = img.info["icc_profile"]
            if exif_bytes:
                kwargs["exif"] = exif_bytes
            if fmt == "PNG":
                if not strip_all:
                    from PIL.PngImagePlugin import PngInfo
                    meta = PngInfo()
                    for k, v in (img.info or {}).items():
                        if isinstance(v, str) and "xmp" not in k.lower():
                            meta.add_text(k, v)
                    kwargs["pnginfo"] = meta
                if "transparency" in img.info:
                    kwargs["transparency"] = img.info["transparency"]
                kwargs["optimize"] = False
            elif fmt == "WEBP":
                lossless = b"VP8L" in data[:64]
                kwargs.update(lossless=True) if lossless else kwargs.update(quality=95, method=4)
            elif fmt == "TIFF":
                kwargs["compression"] = img.info.get("compression", "raw")
                if img.info.get("dpi"):
                    kwargs["dpi"] = img.info["dpi"]
            buf = io.BytesIO()
            clean.save(buf, fmt, **kwargs)
            out = buf.getvalue()
    tmp = dst + ".exif-part"
    try:
        with open(tmp, "wb") as f:
            f.write(out)
        os.replace(tmp, dst)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def _strip(path: str, mode: str, overwrite: bool, out_dir: str | None = None) -> dict:
    path, fmt = _check_file(path)
    if mode not in ("all", "gps"):
        raise ExifError("BAD_MODE", "Mode inconnu (all ou gps)")

    def build(exif):
        if mode == "all":
            return _minimal_exif(exif)
        if IFD_GPS in exif:
            del exif[IFD_GPS]
        return exif

    dst = path if overwrite else _out_path(path, "_clean", out_dir)
    before = os.path.getsize(path)
    _write_with_exif(path, fmt, dst, build, strip_all=mode == "all")
    return {"path": dst, "name": os.path.basename(dst), "overwritten": overwrite,
            "size_before": before, "size_after": os.path.getsize(dst),
            "lossless": fmt in ("JPEG", "PNG", "TIFF")}


def _parse_dt(value: str) -> str:
    value = (value or "").strip()
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M", "%Y:%m:%d %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            dt = datetime.strptime(value, fmt)
            break
        except ValueError:
            continue
    else:
        raise ExifError("BAD_DATE", "Date invalide (attendu AAAA-MM-JJ HH:MM[:SS])")
    if not 1900 <= dt.year <= 2100:
        raise ExifError("BAD_DATE", "Année hors limites (1900-2100)")
    return dt.strftime("%Y:%m:%d %H:%M:%S")


def _set_date(path: str, value: str, overwrite: bool) -> dict:
    path, fmt = _check_file(path)
    stamp = _parse_dt(value)

    def build(exif):
        exif[TAG_DATETIME] = stamp
        sub = exif.get_ifd(IFD_EXIF)
        sub[TAG_DT_ORIGINAL] = stamp
        sub[TAG_DT_DIGITIZED] = stamp
        return exif

    dst = path if overwrite else _out_path(path, "_date")
    _write_with_exif(path, fmt, dst, build, strip_all=False)
    return {"path": dst, "name": os.path.basename(dst), "date": stamp, "overwritten": overwrite}


def _download(path: str) -> dict | None:
    if not _is_upload(path):
        return None
    with open(path, "rb") as f:
        return {"name": os.path.basename(path), "data": base64.b64encode(f.read()).decode()}


async def _guard(fn, *args):
    try:
        return await asyncio.to_thread(fn, *args)
    except ExifError as exc:
        return {"error": exc.message, "code": exc.code}
    except PermissionError:
        return {"error": "Accès refusé (fichier ouvert ailleurs ou dossier protégé)", "code": "ACCESS_DENIED"}
    except OSError as exc:
        return {"error": f"Erreur disque : {exc.strerror or exc}", "code": "IO_ERROR"}


# ── Handlers ─────────────────────────────────────────────────────────────────

def _list_folder(folder: str, recursive: bool) -> list[str]:
    out = []
    for root, dirs, files in os.walk(folder):
        dirs[:] = sorted(d for d in dirs if not d.startswith("."))
        for name in sorted(files, key=str.lower):
            if os.path.splitext(name)[1].lower() in SUPPORTED:
                out.append(os.path.join(root, name))
        if not recursive:
            break
    return out


def _open(path: str) -> dict:
    path = (path or "").strip().strip('"')
    if path and os.path.isabs(path) and os.path.isdir(path):
        files = _list_folder(path, False)
        return {"folder": path, "files": files[:MAX_FOLDER_LIST], "total": len(files)}
    return _read_info(path)


@module.on("open")
async def open_path(payload: dict):
    return await _guard(_open, str(payload.get("path") or ""))


@module.on("open_upload")
async def open_upload(payload: dict):
    name = os.path.basename(str(payload.get("name") or "image")).strip() or "image"
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name)[:120]
    ext = os.path.splitext(name)[1].lower()
    if ext in HEIC:
        return {"error": "Les fichiers HEIC/HEIF/AVIF ne sont pas pris en charge", "code": "UNSUPPORTED_HEIC"}
    if ext not in SUPPORTED:
        return {"error": f"Format non pris en charge ({ext or 'sans extension'})", "code": "UNSUPPORTED_FORMAT"}
    try:
        data = base64.b64decode(str(payload.get("data") or ""), validate=True)
    except ValueError:
        return {"error": "Données invalides", "code": "BAD_DATA"}
    limit = int(module.config.get("max_upload_mb", 40)) * 1024 * 1024
    if not data:
        return {"error": "Fichier vide", "code": "BAD_DATA"}
    if len(data) > limit:
        return {"error": f"Fichier trop volumineux (max. {limit // 1024 // 1024} Mo) : ouvrez-le par son chemin",
                "code": "TOO_LARGE"}
    folder = os.path.join(_uploads_dir(), uuid.uuid4().hex[:12])
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, name)

    def write():
        with open(path, "wb") as f:
            f.write(data)
    await asyncio.to_thread(write)
    res = await _guard(_read_info, path)
    if "error" in res:
        shutil.rmtree(folder, ignore_errors=True)
    return res


@module.on("strip")
async def strip(payload: dict):
    path = str(payload.get("path") or "")
    overwrite = bool(payload.get("overwrite")) and payload.get("confirm_overwrite") is True
    res = await _guard(_strip, path, str(payload.get("mode") or "all"), overwrite)
    if "error" not in res:
        log.info("strip %s (%s)", os.path.splitext(path)[1], payload.get("mode"))
        res["download"] = await asyncio.to_thread(_download, res["path"])
    return res


@module.on("set_date")
async def set_date(payload: dict):
    overwrite = bool(payload.get("overwrite")) and payload.get("confirm_overwrite") is True
    res = await _guard(_set_date, str(payload.get("path") or ""), str(payload.get("date") or ""), overwrite)
    if "error" not in res:
        res["download"] = await asyncio.to_thread(_download, res["path"])
    return res


@module.job("batch_clean")
async def batch_clean(payload: dict, job):
    folder = str(payload.get("folder") or "").strip().strip('"')
    mode = str(payload.get("mode") or "all")
    if mode not in ("all", "gps"):
        raise ValueError("Mode inconnu (all ou gps)")
    if not folder or not os.path.isabs(folder) or not os.path.isdir(folder):
        raise ValueError(f"Dossier introuvable : {folder or '(vide)'}")
    out_dir = str(payload.get("out_dir") or "").strip().strip('"') or None
    if out_dir:
        if not os.path.isabs(out_dir):
            raise ValueError("Le dossier de sortie doit être un chemin absolu")
        os.makedirs(out_dir, exist_ok=True)
    files = await asyncio.to_thread(_list_folder, folder, bool(payload.get("recursive")))
    files = [f for f in files if not re.search(r"_clean(_\d+)?$", os.path.splitext(os.path.basename(f))[0])]
    if out_dir:
        out_norm = os.path.normcase(os.path.realpath(out_dir))
        files = [f for f in files if os.path.normcase(os.path.realpath(os.path.dirname(f))) != out_norm]
    results, ok, failed = [], 0, 0
    for i, f in enumerate(files):
        await job.progress(100 * i / max(1, len(files)), f"{i + 1}/{len(files)} : {os.path.basename(f)}")
        try:
            r = await asyncio.to_thread(_strip, f, mode, False, out_dir)
            results.append({"src": f, "dst": r["path"], "ok": True})
            ok += 1
        except ExifError as exc:
            results.append({"src": f, "ok": False, "error": exc.message})
            failed += 1
        except OSError as exc:
            results.append({"src": f, "ok": False, "error": exc.strerror or str(exc)})
            failed += 1
    await job.progress(100, "Terminé")
    log.info("batch clean: %d ok, %d failed", ok, failed)
    return {"total": len(files), "ok": ok, "failed": failed, "results": results}


@module.on("forget_upload")
async def forget_upload(payload: dict):
    path = str(payload.get("path") or "")
    if path and _is_upload(path):
        shutil.rmtree(os.path.dirname(os.path.realpath(path)), ignore_errors=True)
    return {"ok": True}


@module.on_shutdown
async def cleanup():
    shutil.rmtree(_uploads_dir(), ignore_errors=True)
