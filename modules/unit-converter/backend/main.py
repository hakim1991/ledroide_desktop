import asyncio
import json
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

from ledroide_sdk.http import JSONResponse
from pydantic import BaseModel
from ledroide_sdk import Module, data_path

module = Module(__file__)
router = module.router

RATES_URL = "https://open.er-api.com/v6/latest/EUR"
CACHE_PATH = data_path(__file__, "rates_cache.json")
CACHE_TTL = 6 * 3600

MISSING_TZ = {"error": "Base des fuseaux horaires absente (paquet tzdata). Installez les dépendances du module.",
              "code": "MISSING_DEP"}


# ── Currency rates ────────────────────────────────────────────────────────────

def _fetch_remote() -> dict:
    req = urllib.request.Request(RATES_URL, headers={"User-Agent": "LedroideDesktop/1.0"})
    with urllib.request.urlopen(req, timeout=8) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    if data.get("result") != "success" or not isinstance(data.get("rates"), dict):
        raise ValueError("Réponse inattendue du service de taux")
    return {
        "base": data.get("base_code", "EUR"),
        "rates": data["rates"],
        "updated": data.get("time_last_update_unix"),
        "next_update": data.get("time_next_update_unix"),
        "fetched_at": int(time.time()),
    }


def _read_cache() -> Optional[dict]:
    try:
        return json.loads(CACHE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _write_cache(data: dict):
    tmp = CACHE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(data), encoding="utf-8")
    tmp.replace(CACHE_PATH)


def _iso(ts) -> Optional[str]:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat() if ts else None


def _get_rates(force: bool) -> dict:
    cache = _read_cache()
    now = time.time()
    if cache and not force and now - cache.get("fetched_at", 0) < CACHE_TTL:
        return {**cache, "source": "cache", "offline": False}
    try:
        fresh = _fetch_remote()
        _write_cache(fresh)
        return {**fresh, "source": "network", "offline": False}
    except Exception as e:
        if cache:
            return {**cache, "source": "cache", "offline": True, "warning": f"Hors ligne : taux en cache utilisés ({e.__class__.__name__})"}
        raise


@router.get("/health")
async def health():
    return {"ok": True}


@router.get("/rates")
async def rates(force: bool = False):
    try:
        data = await asyncio.to_thread(_get_rates, force)
    except Exception as e:
        return JSONResponse(status_code=503, content={
            "error": f"Impossible de récupérer les taux de change et aucun cache disponible ({e.__class__.__name__}).",
            "code": "OFFLINE"})
    data["updated_iso"] = _iso(data.get("updated"))
    data["fetched_iso"] = _iso(data.get("fetched_at"))
    return data


# ── Time zones ────────────────────────────────────────────────────────────────

def _zoneinfo():
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError, available_timezones
    try:
        ZoneInfo("Europe/Paris")
    except ZoneInfoNotFoundError:
        return None
    return ZoneInfo, ZoneInfoNotFoundError, available_timezones


@router.get("/tz/zones")
async def tz_zones():
    zi = await asyncio.to_thread(_zoneinfo)
    if zi is None:
        return JSONResponse(status_code=400, content=MISSING_TZ)
    zones = await asyncio.to_thread(lambda: sorted(z for z in zi[2]() if "/" in z or z == "UTC"))
    return {"zones": zones}


class TzIn(BaseModel):
    datetime: Optional[str] = None   # "YYYY-MM-DDTHH:MM[:SS]" naive, in from_tz
    from_tz: str = "UTC"
    to_tz: List[str] = []


def _describe(dt) -> dict:
    off = dt.utcoffset()
    total = int(off.total_seconds()) if off is not None else 0
    sign = "+" if total >= 0 else "-"
    h, rem = divmod(abs(total), 3600)
    offset = f"UTC{sign}{h:02d}:{rem // 60:02d}"
    dst = dt.dst()
    return {
        "iso": dt.replace(microsecond=0).isoformat(),
        "date": dt.strftime("%Y-%m-%d"),
        "time": dt.strftime("%H:%M:%S"),
        "weekday": dt.weekday(),
        "offset": offset,
        "offset_minutes": total // 60,
        "abbr": dt.tzname(),
        "dst": bool(dst and dst.total_seconds()),
    }


@router.post("/tz/convert")
async def tz_convert(body: TzIn):
    zi = await asyncio.to_thread(_zoneinfo)
    if zi is None:
        return JSONResponse(status_code=400, content=MISSING_TZ)
    ZoneInfo, NotFound, _ = zi
    try:
        src = ZoneInfo(body.from_tz)
        targets = [(name, ZoneInfo(name)) for name in body.to_tz]
    except (NotFound, ValueError) as e:
        return JSONResponse(status_code=400, content={"error": f"Fuseau horaire inconnu : {e}"})
    if body.datetime:
        try:
            naive = datetime.fromisoformat(body.datetime)
        except ValueError:
            return JSONResponse(status_code=400, content={"error": "Date/heure invalide (format AAAA-MM-JJTHH:MM)."})
        if naive.tzinfo is not None:
            naive = naive.replace(tzinfo=None)
        dt = naive.replace(tzinfo=src)
        # detect non-existent (spring forward) / ambiguous (fall back) local times
        roundtrip = dt.astimezone(timezone.utc).astimezone(src).replace(tzinfo=None)
        nonexistent = roundtrip != naive
        ambiguous = (naive.replace(tzinfo=src, fold=1).utcoffset() != dt.utcoffset()) and not nonexistent
    else:
        dt = datetime.now(src)
        nonexistent = ambiguous = False
    utc = dt.astimezone(timezone.utc)
    return {
        "source": {"tz": body.from_tz, **_describe(dt)},
        "utc": utc.replace(microsecond=0).isoformat(),
        "nonexistent": nonexistent,
        "ambiguous": ambiguous,
        "results": [{"tz": name, **_describe(utc.astimezone(z))} for name, z in targets],
    }
