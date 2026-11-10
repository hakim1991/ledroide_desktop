import calendar
import email.utils
import logging
import re
from datetime import datetime, timedelta, timezone, tzinfo
from functools import lru_cache

import cron_expr
from ledroide_sdk import Module

log = logging.getLogger("ledroide.cron-timestamp")

module = Module(__file__)
router = module.router

EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
UNIT_FACTORS = {"s": 1, "ms": 1_000, "us": 1_000_000, "ns": 1_000_000_000}
UNIT_LABELS = {"s": "secondes", "ms": "millisecondes", "us": "microsecondes", "ns": "nanosecondes"}
FR_DAYS = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"]
FR_MONTHS = cron_expr.FR_MONTHS
NUMERIC_RE = re.compile(r"^[+-]?\d+(?:\.\d+)?$")
MAX_RUNS = 100


class TsError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _err(e: TsError) -> dict:
    return {"error": e.message, "code": e.code}


# ── Time zones ──────────────────────────────────────────────────────────────

@lru_cache(maxsize=1)
def _available() -> tuple[str, ...]:
    try:
        import zoneinfo
        return tuple(sorted(zoneinfo.available_timezones()))
    except Exception:
        return ()


def get_zone(name: str | None) -> tzinfo:
    name = (name or "UTC").strip()
    if name.upper() in ("UTC", "Z", "ETC/UTC", "GMT"):
        return timezone.utc
    import zoneinfo
    try:
        return zoneinfo.ZoneInfo(name)
    except (zoneinfo.ZoneInfoNotFoundError, ValueError) as e:
        if not _available():
            raise TsError("TZDATA_MISSING", "Base des fuseaux horaires absente : le paquet « tzdata » est requis "
                                            "(réactivez le module ou installez-le via le menu Bibliothèques). Seul UTC est disponible.") from e
        raise TsError("UNKNOWN_TZ", f"Fuseau horaire inconnu : « {name} ».") from e


def _offset_str(dt: datetime) -> str:
    off = dt.utcoffset() or timedelta(0)
    total = int(off.total_seconds())
    sign = "+" if total >= 0 else "-"
    total = abs(total)
    return f"UTC{sign}{total // 3600:02d}:{total % 3600 // 60:02d}"


def _display(dt: datetime, seconds: bool = True) -> str:
    time_fmt = "%H:%M:%S" if seconds else "%H:%M"
    return f"{FR_DAYS[dt.weekday()]} {dt.day} {FR_MONTHS[dt.month - 1]} {dt.year} à {dt.strftime(time_fmt)}"


def _zone_view(dt: datetime, tz: tzinfo, name: str) -> dict:
    local = dt.astimezone(tz)
    dst = local.dst()
    return {
        "tz": name,
        "iso": local.isoformat(),
        "display": _display(local),
        "offset": _offset_str(local),
        "abbrev": local.tzname() or "",
        "dst": bool(dst and dst.total_seconds()),
    }


# ── Parsing instants ────────────────────────────────────────────────────────

def detect_unit(int_part: str) -> str:
    digits = len(int_part.lstrip("+-").lstrip("0") or "0")
    if digits <= 11:
        return "s"
    if digits <= 14:
        return "ms"
    if digits <= 17:
        return "us"
    return "ns"


def _from_epoch(text: str, unit: str) -> tuple[datetime, int]:
    """Returns (datetime, exact nanoseconds since epoch)."""
    neg = text.startswith("-")
    text = text.lstrip("+-")
    whole, _, frac = text.partition(".")
    factor = UNIT_FACTORS[unit]
    ns_per_unit = 1_000_000_000 // factor
    ns = int(whole) * ns_per_unit + (int((frac + "0" * 9)[:9]) * ns_per_unit // 1_000_000_000 if frac else 0)
    if neg:
        ns = -ns
    try:
        return EPOCH + timedelta(microseconds=ns // 1000), ns
    except OverflowError as e:
        raise TsError("OUT_OF_RANGE", "Timestamp hors de la plage représentable (années 1 à 9999).") from e


def parse_instant(value, tz_name: str | None, unit: str = "auto", now: datetime | None = None) -> tuple[datetime, dict]:
    text = str(value if value is not None else "").strip()
    if not text:
        raise TsError("EMPTY", "Saisissez un timestamp ou une date.")
    if text.lower() in ("now", "maintenant"):
        dt = now or datetime.now(timezone.utc)
        return dt, {"kind": "now"}
    if NUMERIC_RE.match(text):
        u = detect_unit(text.split(".")[0]) if unit in (None, "", "auto") else unit
        if u not in UNIT_FACTORS:
            raise TsError("BAD_UNIT", f"Unité inconnue : {u}.")
        dt, ns = _from_epoch(text, u)
        return dt, {"kind": "timestamp", "unit": u, "unit_label": UNIT_LABELS[u], "auto": unit in (None, "", "auto"), "ns": ns}
    iso = text
    if re.match(r"^\d{4}/\d{1,2}/\d{1,2}", iso):
        iso = iso.replace("/", "-", 2)
    iso = re.sub(r"^(\d{4}-)(\d)(?=-)", r"\g<1>0\2", iso)
    iso = re.sub(r"^(\d{4}-\d{2}-)(\d)(?=$|[ T])", r"\g<1>0\2", iso)
    try:
        dt = datetime.fromisoformat(iso)
        kind = "iso"
    except ValueError:
        try:
            dt = email.utils.parsedate_to_datetime(text)
            kind = "rfc2822"
        except (TypeError, ValueError, IndexError):
            m = re.match(r"^(\d{1,2})/(\d{1,2})/(\d{4})(?:[ T](\d{1,2}):(\d{2})(?::(\d{2}))?)?$", text)
            if not m:
                raise TsError("BAD_DATE", f"Format non reconnu : « {text} ». Exemples : 1700000000, 2024-05-17T14:30:00Z, "
                                          "2024-05-17 14:30, 17/05/2024 14:30, Fri, 17 May 2024 14:30:00 +0200.") from None
            d, mo, y, hh, mi, ss = m.groups()
            try:
                dt = datetime(int(y), int(mo), int(d), int(hh or 0), int(mi or 0), int(ss or 0))
            except ValueError as e:
                raise TsError("BAD_DATE", f"Date invalide : {e}.") from e
            kind = "fr"
    info = {"kind": kind, "naive": dt.tzinfo is None}
    if dt.tzinfo is None:
        tz = get_zone(tz_name)
        if isinstance(tz, timezone):
            dt = dt.replace(tzinfo=tz)
        else:
            if not cron_expr._exists(dt, tz):
                info["warning"] = "Cette heure locale n'existe pas (passage à l'heure d'été) : décalée automatiquement."
            elif dt.replace(tzinfo=tz, fold=0).utcoffset() != dt.replace(tzinfo=tz, fold=1).utcoffset():
                info["warning"] = "Heure locale ambiguë (passage à l'heure d'hiver) : première occurrence retenue."
            dt = dt.replace(tzinfo=tz, fold=0)
            dt = dt.astimezone(timezone.utc).astimezone(tz)
        info["assumed_tz"] = tz_name or "UTC"
    return dt, info


def _epochs(dt: datetime, ns: int | None = None) -> dict:
    if ns is None:
        delta = dt - EPOCH
        ns = (delta.days * 86400 + delta.seconds) * 1_000_000_000 + delta.microseconds * 1000
    sec, frac = divmod(abs(ns), 1_000_000_000)
    exact = ("-" if ns < 0 else "") + str(sec) + (f".{frac:09d}".rstrip("0") if frac else "")
    return {
        "s": exact,
        "s_int": str(ns // 1_000_000_000),
        "ms": str(ns // 1_000_000),
        "us": str(ns // 1_000),
        "ns": str(ns),
    }


# ── Actions ─────────────────────────────────────────────────────────────────

@module.on("timezones")
async def timezones(payload: dict):
    zones = list(_available())
    return {"zones": zones or ["UTC"], "tzdata": bool(zones)}


@module.on("convert")
async def convert(payload: dict):
    try:
        src_tz = payload.get("tz") or "UTC"
        dt, info = parse_instant(payload.get("value"), src_tz, payload.get("unit") or "auto")
        now = datetime.now(timezone.utc)
        utc = dt.astimezone(timezone.utc)
        iso_cal = utc.isocalendar()
        targets = []
        for name in payload.get("targets") or []:
            try:
                targets.append(_zone_view(dt, get_zone(name), name))
            except TsError as e:
                targets.append({"tz": name, "error": e.message})
        src = _zone_view(dt, get_zone(src_tz), src_tz)
        src_local = dt.astimezone(get_zone(src_tz))
        return {
            "input": info,
            "epoch": _epochs(dt, info.get("ns")),
            "utc": {"iso": utc.isoformat().replace("+00:00", "Z"), "display": _display(utc),
                    "rfc2822": email.utils.format_datetime(utc, usegmt=True)},
            "source": src,
            "targets": targets,
            "delta_s": (utc - now).total_seconds(),
            "details": {
                "weekday": FR_DAYS[src_local.weekday()],
                "iso_week": src_local.isocalendar().week,
                "iso_year": src_local.isocalendar().year,
                "day_of_year": src_local.timetuple().tm_yday,
                "leap_year": calendar.isleap(src_local.year),
                "utc_iso_week": iso_cal.week,
            },
        }
    except TsError as e:
        return _err(e)


def _add_months(d: datetime, months: int) -> datetime:
    y, m = divmod(d.month - 1 + months, 12)
    year, month = d.year + y, m + 1
    return d.replace(year=year, month=month, day=min(d.day, calendar.monthrange(year, month)[1]))


def calendar_diff(a: datetime, b: datetime) -> dict:
    """a <= b, naive wall-clock datetimes."""
    months = (b.year - a.year) * 12 + b.month - a.month
    if _add_months(a, months) > b:
        months -= 1
    rest = b - _add_months(a, months)
    return {
        "years": months // 12, "months": months % 12, "days": rest.days,
        "hours": rest.seconds // 3600, "minutes": rest.seconds % 3600 // 60, "seconds": rest.seconds % 60,
    }


def business_days(a: datetime, b: datetime) -> int:
    """Mon–Fri days in [a.date, b.date)."""
    start, end = a.date(), b.date()
    days = (end - start).days
    full, rem = divmod(days, 7)
    count = full * 5
    wd = start.weekday()
    for i in range(rem):
        if (wd + i) % 7 < 5:
            count += 1
    return count


@module.on("duration")
async def duration(payload: dict):
    try:
        tz_name = payload.get("tz") or "UTC"
        tz = get_zone(tz_name)
        a, _ = parse_instant(payload.get("start"), tz_name)
        b, _ = parse_instant(payload.get("end"), tz_name)
        a, b = a.astimezone(timezone.utc), b.astimezone(timezone.utc)
        sign = 1 if b >= a else -1
        lo, hi = (a, b) if sign > 0 else (b, a)
        total = (hi - lo).total_seconds()
        whole = int(total)
        lo_l, hi_l = lo.astimezone(tz).replace(tzinfo=None), hi.astimezone(tz).replace(tzinfo=None)
        return {
            "sign": sign,
            "total_seconds": total,
            "breakdown": {"days": whole // 86400, "hours": whole % 86400 // 3600, "minutes": whole % 3600 // 60,
                          "seconds": whole % 60, "microseconds": (hi - lo).microseconds},
            "calendar": calendar_diff(lo_l, hi_l),
            "totals": {"weeks": total / 604800, "days": total / 86400, "hours": total / 3600,
                       "minutes": total / 60, "seconds": total},
            "business_days": business_days(lo_l, hi_l),
            "start": _zone_view(a, tz, tz_name),
            "end": _zone_view(b, tz, tz_name),
        }
    except TsError as e:
        return _err(e)


@module.on("cron")
async def cron(payload: dict):
    expr = payload.get("expression") or ""
    try:
        c = cron_expr.parse(expr)
    except cron_expr.CronError as e:
        return {"valid": False, "message": e.message, "field_index": e.field_index}
    try:
        tz_name = payload.get("tz") or "UTC"
        tz = get_zone(tz_name)
        count = max(1, min(int(payload.get("count") or 10), MAX_RUNS))
        start = datetime.now(timezone.utc)
        if payload.get("start"):
            start = parse_instant(payload["start"], tz_name)[0].astimezone(timezone.utc)
        runs = cron_expr.next_runs(c, tz, start, count)
        return {
            "valid": True,
            "expression": c.expression,
            "normalized": c.normalized,
            "alias": c.alias,
            "description": cron_expr.describe(c),
            "fields": c.fields_dict(),
            "dom_dow_or": not c.dom.star and not c.dow.star,
            "runs": [{"iso": r.isoformat(), "display": _display(r, seconds=False), "offset": _offset_str(r),
                      "epoch": int(r.timestamp()), "delta_s": (r - start).total_seconds()} for r in runs],
            "never": not runs,
        }
    except TsError as e:
        return _err(e)
    except (TypeError, ValueError) as e:
        return {"error": f"Paramètre invalide : {e}", "code": "BAD_PAYLOAD"}
