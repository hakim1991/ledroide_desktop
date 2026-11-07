"""Wi-Fi — connexion courante, réseaux à proximité, profils enregistrés (netsh wlan)."""
import asyncio
import csv
import ctypes
import importlib.util
import os
import re
import subprocess
import sys
import tempfile
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Optional

from ledroide_sdk.http import FileResponse, JSONResponse
from pydantic import BaseModel
from ledroide_sdk import Module

module = Module(__file__)
router = module.router

IS_WINDOWS = sys.platform == "win32"
CREATE_NO_WINDOW = 0x08000000 if IS_WINDOWS else 0
TEMP_DIR = Path(tempfile.gettempdir()) / "ledroide_wifi-tools"
PROFILE_RE = re.compile(r'^[^\x00-\x1f\x7f"]{1,255}$')


def _load_elevate():
    spec = importlib.util.spec_from_file_location("wifi_tools_elevate", Path(__file__).parent / "elevate.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


elevate = _load_elevate()


def _err(msg: str, status: int = 400, code: Optional[str] = None, **extra):
    c = {"error": msg}
    if code:
        c["code"] = code
    c.update(extra)
    return JSONResponse(status_code=status, content=c)


# ── netsh helpers ────────────────────────────────────────────────────────────

def _decode(b: bytes) -> str:
    try:
        return b.decode("utf-8")
    except UnicodeDecodeError:
        return b.decode("cp850", "replace")


def _run(args: list, timeout: int = 15):
    """Single entry point for system commands (monkeypatched in tests)."""
    if not IS_WINDOWS:
        raise RuntimeError("Fonction disponible uniquement sous Windows")
    r = subprocess.run(args, capture_output=True, timeout=timeout, creationflags=CREATE_NO_WINDOW)
    return r.returncode, _decode(r.stdout) + ("\n" + _decode(r.stderr) if r.stderr else "")


def _norm_label(s: str) -> str:
    s = s.replace("\xa0", " ").replace("’", "'").replace(" ", " ")
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", s).strip().lower()


def _lines(text: str):
    """Yield (indent, normalized_label, raw_label, value) for 'label : value' lines."""
    for raw in text.splitlines():
        line = raw.replace("\xa0", " ").rstrip()
        m = re.match(r"^(\s*)([^:]+?)\s*:\s?(.*)$", line)
        if not m:
            continue
        yield len(m.group(1)), _norm_label(m.group(2)), m.group(2).strip(), m.group(3).strip()


IFACE_KEYS = {
    "nom": "name", "name": "name", "description": "description", "guid": "guid",
    "adresse physique": "mac", "physical address": "mac", "etat": "state", "state": "state",
    "ssid": "ssid", "bssid": "bssid", "ap bssid": "bssid",
    "bande": "band", "band": "band", "canal": "channel", "channel": "channel",
    "type de reseau": "network_type", "network type": "network_type",
    "type de radio": "radio", "radio type": "radio",
    "authentification": "auth", "authentication": "auth", "chiffrement": "cipher", "cipher": "cipher",
    "mode de connexion": "connection_mode", "connection mode": "connection_mode",
    "signal": "signal", "rssi": "rssi", "profil": "profile", "profile": "profile",
}


def _iface_key(label: str) -> Optional[str]:
    if label in IFACE_KEYS:
        return IFACE_KEYS[label]
    if label.startswith("point d'acces") or label.endswith("bssid"):
        return "bssid"
    if label.startswith("reception") or label.startswith("receive rate"):
        return "rx_mbps"
    if label.startswith("transmission") or label.startswith("transmit rate"):
        return "tx_mbps"
    return None


def _num(s) -> Optional[float]:
    m = re.search(r"-?\d+(?:[.,]\d+)?", str(s or ""))
    return float(m.group(0).replace(",", ".")) if m else None


def _band_of(band: str, channel) -> Optional[str]:
    n = _num(band)
    if n:
        if n < 3:
            return "2.4"
        if n < 5.9:
            return "5"
        return "6"
    ch = _num(channel)
    if ch is None:
        return None
    return "2.4" if ch <= 14 else "5"


def _rssi_estimate(signal) -> Optional[int]:
    if signal is None:
        return None
    return int(round(signal / 2 - 100))


def parse_interfaces(text: str) -> list:
    ifaces, cur = [], None
    for indent, label, _raw, value in _lines(text):
        key = _iface_key(label)
        if not key:
            continue
        if key == "name":
            cur = {"name": value}
            ifaces.append(cur)
            continue
        if cur is None:
            continue
        if key not in cur:
            cur[key] = value
    for i in ifaces:
        st = _norm_label(i.get("state", ""))
        i["connected"] = st in ("connecte", "connected")
        i["signal"] = int(_num(i["signal"])) if i.get("signal") and _num(i["signal"]) is not None else None
        rssi = _num(i.get("rssi"))
        i["rssi"] = int(rssi) if rssi is not None else None
        i["rssi_estimated"] = i["rssi"] is None and i["signal"] is not None
        if i["rssi"] is None:
            i["rssi"] = _rssi_estimate(i["signal"])
        ch = _num(i.get("channel"))
        i["channel"] = int(ch) if ch is not None else None
        i["band_key"] = _band_of(i.get("band", ""), i["channel"])
        for k in ("rx_mbps", "tx_mbps"):
            n = _num(i.get(k))
            i[k] = n if n is not None else None
    return ifaces


def parse_networks(text: str) -> list:
    nets, ssid, bss = [], None, None
    for indent, label, raw, value in _lines(text):
        if re.fullmatch(r"ssid \d+", label):
            ssid = {"ssid": value, "auth": None, "cipher": None, "network_type": None}
            bss = None
            continue
        if ssid is None:
            continue
        if re.fullmatch(r"bssid \d+", label):
            bss = {"ssid": ssid["ssid"], "bssid": value.lower(), "signal": None, "radio": None, "band": None, "channel": None,
                   "stations": None, "utilization": None, "auth": ssid["auth"], "cipher": ssid["cipher"]}
            nets.append(bss)
            continue
        if bss is None:
            k = {"type de reseau": "network_type", "network type": "network_type", "authentification": "auth",
                 "authentication": "auth", "chiffrement": "cipher", "encryption": "cipher", "cipher": "cipher"}.get(label)
            if k and not ssid.get(k):
                ssid[k] = value
            continue
        if label == "signal":
            n = _num(value)
            bss["signal"] = int(n) if n is not None else None
        elif label in ("type de radio", "radio type"):
            bss["radio"] = value
        elif label in ("bande", "band"):
            bss["band"] = value
        elif label in ("canal", "channel"):
            n = _num(value)
            bss["channel"] = int(n) if n is not None else None
        elif label in ("stations connectees", "connected stations"):
            n = _num(value)
            bss["stations"] = int(n) if n is not None else None
        elif label in ("utilisation du canal", "channel utilization"):
            m = re.search(r"\((\d+)\s*%\)", value)
            bss["utilization"] = int(m.group(1)) if m else None
    for b in nets:
        b["band_key"] = _band_of(b.get("band") or "", b["channel"])
        b["rssi"] = _rssi_estimate(b["signal"])
        if not b["ssid"]:
            b["hidden"] = True
    return nets


def parse_profiles(text: str) -> list:
    profiles, section, seen = [], "user", set()
    for raw in text.splitlines():
        n = _norm_label(raw)
        if not n or set(n) <= {"-", "="}:
            continue
        if ":" not in raw:
            if "strat" in n or "group policy" in n:
                section = "gp"
            elif "utilisateur" in n or "user profile" in n:
                section = "user"
            continue
        label, _, value = raw.partition(":")
        value = value.strip()
        nl = _norm_label(label)
        if "profil" not in nl or not value or value.startswith("<"):
            continue
        if value in seen:
            continue
        seen.add(value)
        scope = "Tous les utilisateurs" if ("tous" in nl or "all user" in nl) else ("Utilisateur actuel" if ("actuel" in nl or "current" in nl) else "")
        profiles.append({"name": value, "scope": scope, "group_policy": section == "gp"})
    return profiles


def parse_profile_detail(text: str) -> dict:
    d = {"ssid": None, "connection_mode": None, "auto_switch": None, "mac_randomization": None, "auth": [], "cipher": [],
         "security_key": None, "cost": None, "radio": None}
    for indent, label, raw, value in _lines(text):
        if label in ("nom du ssid", "ssid name") and not d["ssid"]:
            d["ssid"] = value.strip('"')
        elif label in ("mode de connexion", "connection mode") and not d["connection_mode"]:
            d["connection_mode"] = value
        elif label in ("commutation auto", "autoswitch"):
            d["auto_switch"] = value
        elif label in ("randomisation mac", "mac randomization"):
            d["mac_randomization"] = value
        elif label in ("authentification", "authentication") and value not in d["auth"]:
            d["auth"].append(value)
        elif label in ("chiffrement", "cipher") and value not in d["cipher"]:
            d["cipher"].append(value)
        elif label in ("cle de securite", "security key"):
            d["security_key"] = value
        elif label in ("cout", "cost"):
            d["cost"] = value
        elif label in ("type de radio", "radio type") and not d["radio"]:
            d["radio"] = value
    return d


def parse_key(text: str) -> str:
    for indent, label, raw, value in _lines(text):
        if label in ("contenu de la cle", "key content"):
            return value
    return ""


def _denied_message(text: str) -> bool:
    n = _norm_label(text)
    return any(w in n for w in ("autorisation", "emplacement", "localisation", "location", "permission", "position"))


def _access_denied(text: str) -> bool:
    n = _norm_label(text)
    return any(w in n for w in ("acces refuse", "access is denied", "access denied", "privilege", "eleve", "elevation", "administrat"))


# ── Profiles helpers ─────────────────────────────────────────────────────────

def _profiles() -> list:
    code, out = _run(["netsh", "wlan", "show", "profiles"])
    return parse_profiles(out)


def _check_profile(name: str) -> Optional[str]:
    """Return an error message if the name is invalid or unknown, else None."""
    if not isinstance(name, str) or not PROFILE_RE.match(name):
        return "Nom de profil invalide"
    if name not in {p["name"] for p in _profiles()}:
        return "Profil introuvable"
    return None


def _profile_detail(name: str) -> dict:
    code, out = _run(["netsh", "wlan", "show", "profile", f"name={name}"])
    d = parse_profile_detail(out)
    d["name"] = name
    return d


def _ps_quote(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"


# ── WlanScan (fresh scan) ────────────────────────────────────────────────────

def _wlan_scan() -> int:
    """Ask Windows to rescan on every WLAN interface. Returns number of interfaces scanned."""
    if not IS_WINDOWS:
        return 0
    from ctypes import wintypes

    class GUID(ctypes.Structure):
        _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD), ("Data3", wintypes.WORD), ("Data4", ctypes.c_ubyte * 8)]

    class WLAN_INTERFACE_INFO(ctypes.Structure):
        _fields_ = [("InterfaceGuid", GUID), ("strInterfaceDescription", ctypes.c_wchar * 256), ("isState", ctypes.c_int)]

    class WLAN_INTERFACE_INFO_LIST(ctypes.Structure):
        _fields_ = [("dwNumberOfItems", wintypes.DWORD), ("dwIndex", wintypes.DWORD), ("InterfaceInfo", WLAN_INTERFACE_INFO * 1)]

    wlan = ctypes.windll.wlanapi
    handle, ver = wintypes.HANDLE(), wintypes.DWORD()
    if wlan.WlanOpenHandle(2, None, ctypes.byref(ver), ctypes.byref(handle)) != 0:
        return 0
    try:
        plist = ctypes.POINTER(WLAN_INTERFACE_INFO_LIST)()
        if wlan.WlanEnumInterfaces(handle, None, ctypes.byref(plist)) != 0:
            return 0
        try:
            n = plist.contents.dwNumberOfItems
            arr = ctypes.cast(ctypes.byref(plist.contents.InterfaceInfo), ctypes.POINTER(WLAN_INTERFACE_INFO * n)).contents if n else []
            done = 0
            for info in arr:
                if wlan.WlanScan(handle, ctypes.byref(info.InterfaceGuid), None, None, None) == 0:
                    done += 1
            return done
        finally:
            wlan.WlanFreeMemory(plist)
    finally:
        wlan.WlanCloseHandle(handle, None)


# ── Routes ───────────────────────────────────────────────────────────────────

class ProfileReq(BaseModel):
    name: str
    elevate: bool = False


@router.get("/status")
async def status():
    available = True
    try:
        code, out = await asyncio.to_thread(_run, ["netsh", "wlan", "show", "interfaces"])
        available = bool(parse_interfaces(out))
    except Exception:
        available = False
    return {"admin": elevate.is_admin(), "windows": IS_WINDOWS, "wlan_available": available}


@router.get("/interfaces")
async def interfaces():
    try:
        code, out = await asyncio.to_thread(_run, ["netsh", "wlan", "show", "interfaces"])
    except subprocess.TimeoutExpired:
        return _err("netsh ne répond pas", 504)
    except Exception as e:
        return _err(f"Erreur : {e}", 500)
    ifaces = parse_interfaces(out)
    msg = None
    if not ifaces:
        msg = "Aucune interface Wi-Fi détectée (service WLAN AutoConfig arrêté ou pas de carte Wi-Fi)."
    elif any(i["connected"] and not i.get("ssid") for i in ifaces) and _denied_message(out):
        msg = "Windows masque certaines informations : autorisez l'accès à la localisation pour les applications de bureau."
    return {"interfaces": ifaces, "message": msg, "ts": time.time()}


@router.get("/signal")
async def signal():
    try:
        code, out = await asyncio.to_thread(_run, ["netsh", "wlan", "show", "interfaces"], 8)
    except Exception as e:
        return _err(f"Erreur : {e}", 500)
    ifaces = [i for i in parse_interfaces(out) if i["connected"]]
    if not ifaces:
        return {"connected": False, "ts": time.time()}
    i = ifaces[0]
    return {"connected": True, "ssid": i.get("ssid"), "bssid": i.get("bssid"), "signal": i["signal"], "rssi": i["rssi"],
            "rssi_estimated": i["rssi_estimated"], "rx_mbps": i["rx_mbps"], "tx_mbps": i["tx_mbps"],
            "channel": i["channel"], "band": i.get("band"), "ts": time.time()}


@router.post("/scan")
async def scan():
    try:
        n = await asyncio.to_thread(_wlan_scan)
    except Exception as e:
        return _err(f"Échec du balayage : {e}", 500)
    return {"ok": True, "interfaces": n}


@router.get("/networks")
async def networks():
    try:
        code, out = await asyncio.to_thread(_run, ["netsh", "wlan", "show", "networks", "mode=bssid"])
    except subprocess.TimeoutExpired:
        return _err("netsh ne répond pas", 504)
    except Exception as e:
        return _err(f"Erreur : {e}", 500)
    nets = parse_networks(out)
    denied = False
    message = None
    if not nets:
        if code != 0 or _denied_message(out):
            denied = _denied_message(out)
            message = ("Windows refuse la liste des réseaux : l'accès à la localisation est requis. Ouvrez Paramètres › "
                       "Confidentialité et sécurité › Localisation, activez « Services de localisation » et "
                       "« Autoriser les applications de bureau à accéder à votre localisation ».") if denied else (out.strip()[:400] or "Erreur netsh")
        else:
            message = "Aucun réseau visible. Lancez un nouveau balayage."
    channels = {}
    for n in nets:
        if n["channel"] is not None:
            k = f"{n['band_key']}:{n['channel']}"
            channels[k] = channels.get(k, 0) + 1
    return {"networks": nets, "denied": denied, "message": message, "raw_hint": out.strip()[:400] if denied else None,
            "channel_counts": channels, "ts": time.time()}


@router.post("/open-location-settings")
async def open_location_settings():
    try:
        await asyncio.to_thread(os.startfile, "ms-settings:privacy-location")
    except Exception as e:
        return _err(f"Impossible d'ouvrir les paramètres : {e}", 500)
    return {"ok": True}


@router.get("/profiles")
async def profiles():
    try:
        p = await asyncio.to_thread(_profiles)
    except Exception as e:
        return _err(f"Erreur : {e}", 500)
    return {"profiles": p}


def _all_details() -> list:
    plist = _profiles()
    with ThreadPoolExecutor(max_workers=6) as ex:
        details = list(ex.map(lambda p: _profile_detail(p["name"]), plist))
    for p, d in zip(plist, details):
        d["scope"] = p["scope"]
        d["group_policy"] = p["group_policy"]
    return details


@router.get("/profiles/details")
async def profiles_details():
    try:
        return {"profiles": await asyncio.to_thread(_all_details)}
    except Exception as e:
        return _err(f"Erreur : {e}", 500)


@router.post("/profiles/password")
async def profile_password(req: ProfileReq):
    err = await asyncio.to_thread(_check_profile, req.name)
    if err:
        return _err(err, 404 if err == "Profil introuvable" else 400)

    def _get():
        if req.elevate and not elevate.is_admin():
            script = f"$n = 'name=' + {_ps_quote(req.name)}; & netsh.exe wlan show profile $n key=clear"
            code, out = elevate.run_elevated(script, 30)
        else:
            code, out = _run(["netsh", "wlan", "show", "profile", f"name={req.name}", "key=clear"])
        d = parse_profile_detail(out)
        return parse_key(out), d.get("security_key")

    try:
        pwd, sec = await asyncio.to_thread(_get)
    except elevate.ElevationCancelled:
        return _err("Élévation refusée (UAC annulé)", 403, "UAC_CANCELLED")
    except Exception as e:
        return _err(f"Erreur : {e}", 500)
    open_net = sec is not None and _norm_label(sec) in ("absent", "absente")
    needs_admin = not pwd and not open_net and not elevate.is_admin()
    return {"name": req.name, "password": pwd, "open_network": open_net, "needs_admin": needs_admin}


@router.post("/profiles/forget")
async def profile_forget(req: ProfileReq):
    err = await asyncio.to_thread(_check_profile, req.name)
    if err:
        return _err(err, 404 if err == "Profil introuvable" else 400)

    def _del():
        if req.elevate and not elevate.is_admin():
            script = f"$n = 'name=' + {_ps_quote(req.name)}; & netsh.exe wlan delete profile $n"
            return elevate.run_elevated(script, 30)
        return _run(["netsh", "wlan", "delete", "profile", f"name={req.name}"])

    try:
        code, out = await asyncio.to_thread(_del)
    except elevate.ElevationCancelled:
        return _err("Élévation refusée (UAC annulé)", 403, "UAC_CANCELLED")
    except Exception as e:
        return _err(f"Erreur : {e}", 500)
    if code != 0:
        if _access_denied(out) and not elevate.is_admin():
            return _err("Droits administrateur requis pour supprimer ce profil.", 403, "NEEDS_ADMIN")
        return _err(out.strip()[:300] or f"netsh a échoué (code {code})", 500)
    return {"ok": True, "message": out.strip()[:300]}


@router.post("/export/profiles")
async def export_profiles():
    try:
        details = await asyncio.to_thread(_all_details)
    except Exception as e:
        return _err(f"Erreur : {e}", 500)
    TEMP_DIR.mkdir(parents=True, exist_ok=True)
    fn = f"profils_wifi_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"

    def _write():
        with open(TEMP_DIR / fn, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f, delimiter=";")
            w.writerow(["Profil", "SSID", "Portée", "Authentification", "Chiffrement", "Mode de connexion",
                        "Commutation auto", "Randomisation MAC", "Clé de sécurité", "Coût"])
            for d in details:
                w.writerow([d["name"], d["ssid"] or "", d["scope"], " / ".join(d["auth"]), " / ".join(d["cipher"]),
                            d["connection_mode"] or "", d["auto_switch"] or "", d["mac_randomization"] or "",
                            d["security_key"] or "", d["cost"] or ""])

    await asyncio.to_thread(_write)
    return {"filename": fn, "count": len(details)}


@router.get("/file/{filename}")
async def get_file(filename: str):
    if "/" in filename or "\\" in filename or ".." in filename:
        return _err("Nom de fichier invalide")
    path = TEMP_DIR / filename
    if not path.is_file():
        return _err("Fichier introuvable", 404)
    return FileResponse(path, media_type="text/csv", headers={"Content-Disposition": f'attachment; filename="{filename}"'})
