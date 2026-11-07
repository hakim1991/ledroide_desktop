"""Services Windows — liste (psutil) et actions (PowerShell / sc.exe, élevées si besoin)."""
import asyncio
import importlib.util
import re
import sys
import winreg
from pathlib import Path
from typing import Optional

from ledroide_sdk.http import JSONResponse
from pydantic import BaseModel
from ledroide_sdk import Module

module = Module(__file__)
router = module.router


def _load_elevate():
    name = "services_elevate"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, Path(__file__).parent / "elevate.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


elevate = _load_elevate()

# Services dont l'arrêt / la reconfiguration peut rendre Windows instable ou injoignable.
CRITICAL = {s.lower(): s for s in [
    "RpcSs", "RpcEptMapper", "DcomLaunch", "LSM", "WinDefend", "EventLog", "Dhcp", "Dnscache",
    "SamSs", "Schedule", "PlugPlay", "Power", "ProfSvc", "BFE", "mpssvc", "CryptSvc", "gpsvc",
    "Winmgmt", "nsi", "BrokerInfrastructure", "CoreMessagingRegistrar", "SystemEventsBroker",
    "UserManager", "StateRepository", "TimeBrokerSvc", "LanmanWorkstation", "NlaSvc", "netprofm",
    "SecurityHealthService", "WdNisSvc", "Sense", "AppIDSvc", "KeyIso", "VaultSvc", "Wcmsvc",
    "WinHttpAutoProxySvc", "EventSystem", "SENS", "Themes", "AudioSrv", "AudioEndpointBuilder",
    "TrustedInstaller", "wuauserv", "msiserver", "Netlogon", "W32Time", "FontCache", "Appinfo",
    "CDPSvc", "DispBrokerDesktopSvc", "DeviceInstall", "LSA", "WlanSvc", "Netman", "iphlpsvc",
]}

START_TYPES = {
    "auto": ("auto", "Automatique"),
    "delayed": ("delayed-auto", "Automatique (début différé)"),
    "manual": ("demand", "Manuel"),
    "disabled": ("disabled", "Désactivé"),
}
NAME_RE = re.compile(r"^[^\x00-\x1f'\"`<>|\\/]{1,256}$")
_REG_BASE = r"SYSTEM\CurrentControlSet\Services"


def _err(msg: str, status: int = 400, code: Optional[str] = None, **extra):
    c = {"error": msg}
    if code:
        c["code"] = code
    c.update(extra)
    return JSONResponse(status_code=status, content=c)


def _missing_dep():
    return _err("Le paquet psutil n'est pas installé. Installez les dépendances du module.", 400, "MISSING_DEP")


def _psutil():
    try:
        import psutil
        return psutil
    except ImportError:
        return None


def _reg_values(name: str) -> dict:
    out = {"delayed": False, "depends_on": [], "type": None}
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, f"{_REG_BASE}\\{name}") as k:
            try:
                out["delayed"] = bool(winreg.QueryValueEx(k, "DelayedAutostart")[0])
            except OSError:
                pass
            try:
                v = winreg.QueryValueEx(k, "DependOnService")[0]
                out["depends_on"] = [x for x in (v if isinstance(v, list) else [v]) if x]
            except OSError:
                pass
            try:
                out["type"] = int(winreg.QueryValueEx(k, "Type")[0])
            except OSError:
                pass
    except OSError:
        pass
    return out


def _service_dict(psutil, s) -> dict:
    d = {"name": s.name(), "display_name": "", "status": "", "start_type": "", "pid": None,
         "binpath": "", "description": "", "username": ""}
    for key, fn in (("display_name", s.display_name), ("status", s.status), ("start_type", s.start_type),
                    ("pid", s.pid), ("binpath", s.binpath), ("description", s.description),
                    ("username", s.username)):
        try:
            d[key] = fn()
        except Exception:
            pass
    reg = _reg_values(d["name"])
    d["delayed"] = bool(reg["delayed"] and d["start_type"] == "automatic")
    d["start_key"] = {"automatic": "delayed" if d["delayed"] else "auto", "manual": "manual",
                      "disabled": "disabled"}.get(d["start_type"], d["start_type"] or "")
    d["depends_on"] = reg["depends_on"]
    d["critical"] = d["name"].lower() in CRITICAL
    return d


def _list_sync(psutil) -> list:
    out = []
    for s in psutil.win_service_iter():
        try:
            out.append(_service_dict(psutil, s))
        except Exception:
            continue
    # Services dépendants (inverse de DependOnService)
    by_lower = {d["name"].lower(): d for d in out}
    for d in out:
        d["dependents"] = []
    for d in out:
        for dep in d["depends_on"]:
            t = by_lower.get(dep.lower())
            if t is not None:
                t["dependents"].append(d["name"])
    out.sort(key=lambda d: (d["display_name"] or d["name"]).lower())
    return out


def _resolve_name(psutil, name: str) -> Optional[str]:
    """Validation stricte : le nom doit correspondre exactement à un service existant."""
    if not name or not NAME_RE.match(name):
        return None
    low = name.lower()
    for s in psutil.win_service_iter():
        try:
            if s.name().lower() == low:
                return s.name()
        except Exception:
            continue
    return None


@router.get("/status")
async def status():
    return {"admin": elevate.is_admin(), "psutil": _psutil() is not None}


@router.get("/services")
async def services():
    psutil = _psutil()
    if psutil is None:
        return _missing_dep()
    items = await asyncio.to_thread(_list_sync, psutil)
    counts = {"total": len(items), "running": sum(d["status"] == "running" for d in items),
              "stopped": sum(d["status"] == "stopped" for d in items)}
    return {"services": items, "counts": counts, "admin": elevate.is_admin(),
            "critical": sorted(CRITICAL.values(), key=str.lower)}


def _one_sync(psutil, name: str):
    real = _resolve_name(psutil, name)
    if not real:
        return None
    items = _list_sync(psutil)
    for d in items:
        if d["name"] == real:
            return d
    return None


@router.get("/services/{name}")
async def service_detail(name: str):
    psutil = _psutil()
    if psutil is None:
        return _missing_dep()
    d = await asyncio.to_thread(_one_sync, psutil, name)
    if d is None:
        return _err("Service introuvable.", 404)
    return d


class ActionIn(BaseModel):
    action: str
    confirm_critical: bool = False
    force: bool = False           # arrêter aussi les services dépendants


class StartTypeIn(BaseModel):
    start_type: str
    confirm_critical: bool = False


def _run(script: str) -> dict:
    """Point unique d'exécution (moqué dans les tests)."""
    return elevate.run_powershell(script, elevate=True, timeout=120)


def _result(psutil, real: str, r: dict, ok_msg: str):
    if r.get("cancelled"):
        return _err("Élévation refusée : aucune modification effectuée.", 403, "UAC_CANCELLED")
    if r.get("timeout"):
        return _err("Le service ne répond pas (délai dépassé).", 504, "TIMEOUT")
    if not r.get("ok"):
        msg = (r.get("output") or "erreur inconnue").strip()
        hint = ""
        low = msg.lower()
        if "accès refusé" in low or "access is denied" in low or "access denied" in low or re.search(r"échec\(s\)\s*5\b|failed 5\b", low):
            hint = " (accès refusé — ce service est peut-être protégé par Windows)"
        return _err(f"Échec : {msg}{hint}", 400, "ACTION_FAILED")
    d = None
    try:
        d = _service_dict(psutil, psutil.win_service_get(real))
    except Exception:
        pass
    return {"ok": True, "message": ok_msg, "service": d, "elevated": bool(r.get("elevated"))}


@router.post("/services/{name}/action")
async def service_action(name: str, payload: ActionIn):
    psutil = _psutil()
    if psutil is None:
        return _missing_dep()
    if payload.action not in ("start", "stop", "restart"):
        return _err("Action inconnue.")
    real = await asyncio.to_thread(_resolve_name, psutil, name)
    if not real:
        return _err("Service introuvable (nom invalide).", 404)
    if real.lower() in CRITICAL and not payload.confirm_critical and payload.action != "start":
        return _err(f"« {real} » est un service critique : le modifier peut rendre Windows instable. Confirmation requise.",
                    409, "CRITICAL_CONFIRM")
    q = elevate.ps_quote(real)
    force = " -Force" if payload.force else ""
    script = {
        "start": f"Start-Service -Name {q} -ErrorAction Stop; 'OK'",
        "stop": f"Stop-Service -Name {q}{force} -ErrorAction Stop; 'OK'",
        "restart": f"Restart-Service -Name {q}{force} -ErrorAction Stop; 'OK'",
    }[payload.action]
    r = await asyncio.to_thread(_run, script)
    label = {"start": "démarré", "stop": "arrêté", "restart": "redémarré"}[payload.action]
    return _result(psutil, real, r, f"Service « {real} » {label}.")


@router.post("/services/{name}/start-type")
async def service_start_type(name: str, payload: StartTypeIn):
    psutil = _psutil()
    if psutil is None:
        return _missing_dep()
    if payload.start_type not in START_TYPES:
        return _err("Type de démarrage inconnu.")
    real = await asyncio.to_thread(_resolve_name, psutil, name)
    if not real:
        return _err("Service introuvable (nom invalide).", 404)
    if real.lower() in CRITICAL and not payload.confirm_critical:
        return _err(f"« {real} » est un service critique : changer son type de démarrage peut empêcher Windows de fonctionner. Confirmation requise.",
                    409, "CRITICAL_CONFIRM")
    sc_val, label = START_TYPES[payload.start_type]
    q = elevate.ps_quote(real)
    script = (f"$o = & sc.exe config {q} start= {sc_val} 2>&1; "
              "if ($LASTEXITCODE -ne 0) { throw ((@($o) | ForEach-Object { [string]$_ }) -join ' ').Trim() }; 'OK'")
    r = await asyncio.to_thread(_run, script)
    return _result(psutil, real, r, f"Type de démarrage de « {real} » : {label}.")


@router.get("/health")
async def health():
    return {"ok": True, "psutil": _psutil() is not None}
