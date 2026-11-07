"""Journaux d'événements Windows — Get-WinEvent (FilterHashtable) via PowerShell."""
import asyncio
import base64
import csv
import importlib.util
import json
import re
import sys
import tempfile
import time
from collections import Counter
from pathlib import Path
from typing import List, Optional

from ledroide_sdk.http import FileResponse, JSONResponse
from pydantic import BaseModel
from ledroide_sdk import Module

module = Module(__file__)
router = module.router

TEMP_DIR = Path(tempfile.gettempdir()) / "ledroide_event_viewer"


def _load_elevate():
    name = "event_viewer_elevate"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, Path(__file__).parent / "elevate.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


elev = _load_elevate()

STANDARD_LOGS = [
    {"name": "System", "label": "Système"},
    {"name": "Application", "label": "Application"},
    {"name": "Security", "label": "Sécurité", "admin": True},
    {"name": "Setup", "label": "Installation (Setup)"},
]
LEVELS = {1: "Critique", 2: "Erreur", 3: "Avertissement", 4: "Information", 0: "Information", 5: "Détaillé"}
LOG_RE = re.compile(r"^[\w .%/\-]{1,256}$", re.UNICODE)
PROVIDER_RE = re.compile(r"^[\w .:/\-()]{1,200}$", re.UNICODE)
MAX_LIMIT = 5000

_logs_cache = {"t": 0.0, "logs": None}


def _err(msg: str, status: int = 400, code: Optional[str] = None):
    c = {"error": msg}
    if code:
        c["code"] = code
    return JSONResponse(status_code=status, content=c)


def _b64(obj) -> str:
    return base64.b64encode(json.dumps(obj).encode("utf-8")).decode("ascii")


def _ps(script: str, elevated: bool = False, timeout: float = 120) -> dict:
    """Point unique d'exécution PowerShell (moquable dans les tests)."""
    return elev.run_powershell(script, elevate=elevated, timeout=timeout)


def _parse_json(out: str):
    out = (out or "").strip()
    if not out:
        return None
    # La sortie JSON est la dernière ligne non vide (des avertissements peuvent précéder).
    for line in reversed(out.splitlines()):
        line = line.strip()
        if line.startswith("{") or line.startswith("["):
            try:
                return json.loads(line)
            except ValueError:
                continue
    return None


def _as_list(v):
    if v is None:
        return []
    return v if isinstance(v, list) else [v]


# ---------------------------------------------------------------- Liste des journaux

LIST_SCRIPT = r"""
[Console]::OutputEncoding = [Text.Encoding]::UTF8
$l = Get-WinEvent -ListLog * -ErrorAction SilentlyContinue | Where-Object { $_.IsEnabled -and $_.RecordCount -gt 0 } |
  ForEach-Object { [pscustomobject]@{ n = $_.LogName; c = [long]$_.RecordCount; t = $(if ($_.LastWriteTime) { $_.LastWriteTime.ToString('yyyy-MM-ddTHH:mm:ss') } else { $null }) } }
ConvertTo-Json -InputObject @($l) -Compress
"""


def _list_logs_sync(force: bool = False) -> list:
    now = time.time()
    if not force and _logs_cache["logs"] is not None and now - _logs_cache["t"] < 300:
        return _logs_cache["logs"]
    r = _ps(LIST_SCRIPT, timeout=90)
    data = _as_list(_parse_json(r.get("output")))
    logs = []
    for d in data:
        if isinstance(d, dict) and d.get("n"):
            logs.append({"name": d["n"], "count": d.get("c"), "last_write": d.get("t")})
    logs.sort(key=lambda x: x["name"].lower())
    if logs:
        _logs_cache.update(t=now, logs=logs)
    return logs


@router.get("/status")
async def status():
    return {"admin": elev.is_admin()}


@router.get("/logs")
async def logs(refresh: bool = False):
    items = await asyncio.to_thread(_list_logs_sync, refresh)
    return {"standard": STANDARD_LOGS, "logs": items, "admin": elev.is_admin()}


def _valid_log(name: str) -> Optional[str]:
    if not name or not LOG_RE.match(name):
        return None
    for s in STANDARD_LOGS:
        if s["name"].lower() == name.lower():
            return s["name"]
    known = _list_logs_sync()
    for l in known:
        if l["name"].lower() == name.lower():
            return l["name"]
    return None


# ---------------------------------------------------------------- Requête d'événements

QUERY_SCRIPT = r"""
[Console]::OutputEncoding = [Text.Encoding]::UTF8
$a = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('__ARGS__')) | ConvertFrom-Json
$h = @{ LogName = [string]$a.log }
if ($a.levels) { $h.Level = [int[]]@($a.levels) }
if ($a.start) { $h.StartTime = [DateTimeOffset]::FromUnixTimeSeconds([long]$a.start).LocalDateTime }
if ($a.end) { $h.EndTime = [DateTimeOffset]::FromUnixTimeSeconds([long]$a.end).LocalDateTime }
if ($a.provider) { $h.ProviderName = [string]$a.provider }
if ($a.ids) { $h.Id = [int[]]@($a.ids) }
try {
  $ev = @(Get-WinEvent -FilterHashtable $h -MaxEvents ([int]$a.max) -ErrorAction Stop)
} catch {
  $id = [string]$_.FullyQualifiedErrorId
  if ($id -like 'NoMatchingEventsFound*') { $ev = @() }
  elseif ($_.Exception -is [System.UnauthorizedAccessException] -or $id -like '*UnauthorizedAccess*') {
    ConvertTo-Json -InputObject @{ ok = $false; code = 'ACCESS_DENIED'; error = $_.Exception.Message } -Compress; return
  } else {
    ConvertTo-Json -InputObject @{ ok = $false; code = 'PS_ERROR'; error = $_.Exception.Message; fqid = $id } -Compress; return
  }
}
$rows = foreach ($e in $ev) {
  $m = $null
  try { $m = $e.Message } catch { }
  if (-not $m) { $m = '(Aucune description disponible) ' + ((@($e.Properties) | ForEach-Object { [string]$_.Value }) -join ' | ') }
  [pscustomobject]@{
    t = $e.TimeCreated.ToString('yyyy-MM-ddTHH:mm:ss.fffzzz'); id = $e.Id; lv = [int]$e.Level
    src = $e.ProviderName; rid = $e.RecordId; msg = $m; log = $e.LogName; pc = $e.MachineName
  }
}
ConvertTo-Json -InputObject @{ ok = $true; events = @($rows) } -Depth 3 -Compress
"""


def _levels_arg(levels: str) -> List[int]:
    out = set()
    for p in (levels or "").split(","):
        p = p.strip()
        if not p:
            continue
        if not p.isdigit():
            raise ValueError("Niveau invalide.")
        v = int(p)
        if v not in (1, 2, 3, 4, 5):
            raise ValueError("Niveau invalide.")
        out.add(v)
        if v == 4:
            out.add(0)   # « LogAlways » est affiché comme Information
    return sorted(out)


def _ids_arg(ids: str) -> List[int]:
    out = []
    for p in re.split(r"[,\s;]+", (ids or "").strip()):
        if not p:
            continue
        if not p.isdigit() or int(p) > 65535:
            raise ValueError("ID d'événement invalide (nombres de 0 à 65535 séparés par des virgules).")
        out.append(int(p))
    if len(out) > 20:
        raise ValueError("20 ID d'événement au maximum.")
    return out


def _stats(events: list) -> dict:
    lv = Counter()
    for e in events:
        lvl = e.get("level")
        lv[{1: "critical", 2: "error", 3: "warning"}.get(lvl, "information")] += 1
    src = Counter(e.get("source") or "?" for e in events)
    ids = Counter((e.get("source") or "?", e.get("id")) for e in events)
    return {
        "levels": {k: lv.get(k, 0) for k in ("critical", "error", "warning", "information")},
        "top_sources": [{"source": s, "count": c} for s, c in src.most_common(10)],
        "top_ids": [{"source": s, "id": i, "count": c} for (s, i), c in ids.most_common(10)],
    }


def _normalize(raw: list, fallback_log: str) -> list:
    out = []
    for d in _as_list(raw):
        if not isinstance(d, dict):
            continue
        lvl = d.get("lv")
        try:
            lvl = int(lvl) if lvl is not None else 4
        except (TypeError, ValueError):
            lvl = 4
        out.append({
            "time": d.get("t"), "id": d.get("id"), "level": lvl, "level_name": LEVELS.get(lvl, str(lvl)),
            "source": d.get("src") or "", "record_id": d.get("rid"), "message": d.get("msg") or "",
            "log": d.get("log") or fallback_log, "machine": d.get("pc") or "",
        })
    return out


def _query_sync(args: dict, elevated: bool) -> dict:
    script = QUERY_SCRIPT.replace("__ARGS__", _b64(args))
    r = _ps(script, elevated=elevated, timeout=180)
    if r.get("cancelled"):
        return {"_error": ("Élévation refusée.", 403, "UAC_CANCELLED")}
    if r.get("timeout"):
        return {"_error": ("La requête a pris trop de temps. Réduisez la période ou le nombre de résultats.", 504, "TIMEOUT")}
    data = _parse_json(r.get("output"))
    if not isinstance(data, dict):
        msg = (r.get("output") or "").strip()
        if "NoMatchingEventsFound" in msg:
            return {"events": []}
        return {"_error": ("Erreur PowerShell : " + (msg[:500] or "sortie vide"), 500, "PS_ERROR")}
    if not data.get("ok"):
        if data.get("code") == "ACCESS_DENIED":
            return {"_error": ("Accès refusé : ce journal nécessite des droits administrateur.", 403, "ACCESS_DENIED")}
        return {"_error": ("Erreur : " + str(data.get("error") or "inconnue"), 400, "PS_ERROR")}
    return {"events": _normalize(data.get("events"), args["log"])}


def _period(period: str, start: Optional[float], end: Optional[float]):
    now = time.time()
    spans = {"1h": 3600, "24h": 86400, "7d": 7 * 86400, "30d": 30 * 86400}
    if period in spans:
        return int(now - spans[period]), None
    if period == "custom":
        if start is None and end is None:
            raise ValueError("Indiquez une date de début et/ou de fin.")
        if start is not None and end is not None and end <= start:
            raise ValueError("La date de fin doit être postérieure à la date de début.")
        return (int(start) if start else None), (int(end) if end else None)
    if period in ("", "all"):
        return None, None
    raise ValueError("Période invalide.")


@router.get("/events")
async def events(log: str = "System", levels: str = "", period: str = "24h", start: Optional[float] = None,
                 end: Optional[float] = None, provider: str = "", ids: str = "", q: str = "",
                 max: int = 500, elevate: bool = False):
    elevated = elevate
    real = await asyncio.to_thread(_valid_log, log)
    if not real:
        return _err("Journal inconnu ou vide.", 404)
    try:
        lv = _levels_arg(levels)
        id_list = _ids_arg(ids)
        st, en = _period(period, start, end)
    except ValueError as e:
        return _err(str(e))
    provider = provider.strip()
    if provider and not PROVIDER_RE.match(provider):
        return _err("Nom de source invalide.")
    max = 1 if max < 1 else min(max, MAX_LIMIT)
    q = q.strip().lower()
    fetch = min(MAX_LIMIT, max * 5) if q else max
    is_admin = _is_admin()
    if real == "Security" and not is_admin and not elevated:
        return _err("Le journal Sécurité nécessite des droits administrateur.", 403, "ACCESS_DENIED")
    args = {"log": real, "levels": lv, "start": st, "end": en, "provider": provider, "ids": id_list, "max": fetch}
    t0 = time.time()
    res = await asyncio.to_thread(_query_sync, args, bool(elevated and not is_admin))
    if "_error" in res:
        msg, status, code = res["_error"]
        return _err(msg, status, code)
    evs = res["events"]
    if q:
        evs = [e for e in evs if q in (e["message"] or "").lower() or q in e["source"].lower() or q == str(e["id"])]
        evs = evs[:max]
    return {"log": real, "events": evs, "count": len(evs), "truncated": len(res["events"]) >= fetch,
            "stats": _stats(evs), "duration": round(time.time() - t0, 2)}


def _is_admin() -> bool:
    return elev.is_admin()


@router.get("/recent-errors")
async def recent_errors(hours: int = 24, max: int = 300):
    hours = min(hours if hours > 0 else 24, 24 * 30)
    max = 1 if max < 1 else min(max, 2000)
    st = int(time.time() - hours * 3600)

    async def one(log):
        args = {"log": log, "levels": [1, 2], "start": st, "end": None, "provider": "", "ids": [], "max": max}
        return log, await asyncio.to_thread(_query_sync, args, False)

    results = await asyncio.gather(one("System"), one("Application"))
    evs, errors = [], []
    for log, r in results:
        if "_error" in r:
            errors.append(f"{log} : {r['_error'][0]}")
        else:
            evs.extend(r["events"])
    evs.sort(key=lambda e: e.get("time") or "", reverse=True)
    return {"events": evs, "count": len(evs), "stats": _stats(evs), "errors": errors, "hours": hours}


# ---------------------------------------------------------------- Détails d'un événement

DETAIL_SCRIPT = r"""
[Console]::OutputEncoding = [Text.Encoding]::UTF8
$a = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('__ARGS__')) | ConvertFrom-Json
try {
  $e = Get-WinEvent -LogName ([string]$a.log) -FilterXPath ("*[System[EventRecordID=" + [long]$a.rid + "]]") -MaxEvents 1 -ErrorAction Stop
} catch {
  $id = [string]$_.FullyQualifiedErrorId
  $code = 'PS_ERROR'
  if ($id -like 'NoMatchingEventsFound*') { $code = 'NOT_FOUND' }
  elseif ($_.Exception -is [System.UnauthorizedAccessException] -or $id -like '*UnauthorizedAccess*') { $code = 'ACCESS_DENIED' }
  ConvertTo-Json -InputObject @{ ok = $false; code = $code; error = $_.Exception.Message } -Compress; return
}
$m = $null; try { $m = $e.Message } catch { }
$task = $null; try { $task = $e.TaskDisplayName } catch { }
$op = $null; try { $op = $e.OpcodeDisplayName } catch { }
$kw = $null; try { $kw = @($e.KeywordsDisplayNames) -join ', ' } catch { }
$user = $null; if ($e.UserId) { try { $user = $e.UserId.Translate([System.Security.Principal.NTAccount]).Value } catch { $user = $e.UserId.Value } }
ConvertTo-Json -Depth 3 -Compress -InputObject @{
  ok = $true; t = $e.TimeCreated.ToString('yyyy-MM-ddTHH:mm:ss.fffzzz'); id = $e.Id; lv = [int]$e.Level
  src = $e.ProviderName; rid = $e.RecordId; msg = $m; log = $e.LogName; pc = $e.MachineName
  task = $task; opcode = $op; keywords = $kw; user = $user; pid = $e.ProcessId; tid = $e.ThreadId
  props = @(@($e.Properties) | ForEach-Object { [string]$_.Value }); xml = $e.ToXml()
}
"""


@router.get("/event")
async def event_detail(log: str, record_id: int, elevate: bool = False):
    real = await asyncio.to_thread(_valid_log, log)
    if not real:
        return _err("Journal inconnu.", 404)
    if record_id < 0:
        return _err("Identifiant d'enregistrement invalide.")
    is_admin = _is_admin()
    if real == "Security" and not is_admin and not elevate:
        return _err("Le journal Sécurité nécessite des droits administrateur.", 403, "ACCESS_DENIED")
    script = DETAIL_SCRIPT.replace("__ARGS__", _b64({"log": real, "rid": int(record_id)}))
    r = await asyncio.to_thread(_ps, script, bool(elevate and not is_admin), 60)
    if r.get("cancelled"):
        return _err("Élévation refusée.", 403, "UAC_CANCELLED")
    d = _parse_json(r.get("output"))
    if not isinstance(d, dict):
        return _err("Erreur PowerShell : " + ((r.get("output") or "")[:500] or "sortie vide"), 500, "PS_ERROR")
    if not d.get("ok"):
        if d.get("code") == "NOT_FOUND":
            return _err("Événement introuvable (journal effacé ou remplacé ?).", 404, "NOT_FOUND")
        if d.get("code") == "ACCESS_DENIED":
            return _err("Accès refusé : droits administrateur requis.", 403, "ACCESS_DENIED")
        return _err("Erreur : " + str(d.get("error") or "inconnue"), 400, "PS_ERROR")
    ev = _normalize([d], real)[0]
    ev.update({"task": d.get("task") or "", "opcode": d.get("opcode") or "", "keywords": d.get("keywords") or "",
               "user": d.get("user") or "", "pid": d.get("pid"), "tid": d.get("tid"),
               "properties": _as_list(d.get("props")), "xml": d.get("xml") or ""})
    return ev


# ---------------------------------------------------------------- Sources d'un journal

PROVIDERS_SCRIPT = r"""
[Console]::OutputEncoding = [Text.Encoding]::UTF8
$a = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('__ARGS__')) | ConvertFrom-Json
$p = @()
try { $p = @((Get-WinEvent -ListLog ([string]$a.log) -ErrorAction Stop).ProviderNames) } catch { }
ConvertTo-Json -InputObject @($p | Sort-Object) -Compress
"""


@router.get("/providers")
async def providers(log: str = "System"):
    real = await asyncio.to_thread(_valid_log, log)
    if not real:
        return _err("Journal inconnu.", 404)
    r = await asyncio.to_thread(_ps, PROVIDERS_SCRIPT.replace("__ARGS__", _b64({"log": real})), False, 60)
    return {"log": real, "providers": [p for p in _as_list(_parse_json(r.get("output"))) if isinstance(p, str)]}


# ---------------------------------------------------------------- Export CSV

class ExportIn(BaseModel):
    rows: List[dict]


EXPORT_COLS = [("time", "Date et heure"), ("level_name", "Niveau"), ("log", "Journal"), ("source", "Source"),
               ("id", "ID"), ("record_id", "N° d'enregistrement"), ("machine", "Ordinateur"), ("message", "Message")]


def _csv_safe(v) -> str:
    s = "" if v is None else str(v)
    if s[:1] in ("=", "+", "-", "@"):
        s = "'" + s
    return s


@router.post("/export")
async def export(payload: ExportIn):
    if not payload.rows:
        return _err("Aucun événement à exporter.")
    TEMP_DIR.mkdir(parents=True, exist_ok=True)
    fname = time.strftime("evenements_%Y%m%d_%H%M%S.csv")

    def _w():
        with open(TEMP_DIR / fname, "w", encoding="utf-8-sig", newline="") as f:
            w = csv.writer(f, delimiter=";")
            w.writerow([h for _, h in EXPORT_COLS])
            for r in payload.rows[:100000]:
                w.writerow([_csv_safe(r.get(k)) for k, _ in EXPORT_COLS])
    await asyncio.to_thread(_w)
    return {"filename": fname, "count": len(payload.rows)}


@router.get("/file/{filename}")
async def get_file(filename: str):
    if "/" in filename or "\\" in filename or ".." in filename or not re.fullmatch(r"[\w.\-]+", filename):
        return _err("Nom de fichier invalide.")
    path = TEMP_DIR / filename
    if not path.is_file():
        return _err("Fichier introuvable.", 404)
    return FileResponse(path, media_type="text/csv", filename=filename)


@router.get("/health")
async def health():
    return {"ok": True}
