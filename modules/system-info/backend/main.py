"""Inventaire système — équivalent msinfo32/systeminfo (lecture seule)."""
import asyncio
import csv
import ctypes
import html
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

from ledroide_sdk.http import FileResponse, JSONResponse
from ledroide_sdk import Module

module = Module(__file__)
router = module.router

IS_WINDOWS = sys.platform == "win32"
CREATE_NO_WINDOW = 0x08000000 if IS_WINDOWS else 0
TEMP_DIR = Path(tempfile.gettempdir()) / "ledroide_system-info"
SECTIONS = ["system", "hardware", "storage", "software", "updates", "startup", "env"]
SECTION_LABELS = {
    "system": "Système", "hardware": "Matériel", "storage": "Stockage", "software": "Logiciels installés",
    "updates": "Mises à jour", "startup": "Démarrage", "env": "Variables d'environnement",
}

_cache: dict = {}
_locks: dict = {}


def _err(msg: str, status: int = 400, code: Optional[str] = None):
    c = {"error": msg}
    if code:
        c["code"] = code
    return JSONResponse(status_code=status, content=c)


def _is_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


# ── PowerShell helper ────────────────────────────────────────────────────────

PS_PRELUDE = r"""
[Console]::OutputEncoding=[Text.Encoding]::UTF8
$ErrorActionPreference='SilentlyContinue'
function D($d){ if($d){ try { ([datetime]$d).ToString('yyyy-MM-ddTHH:mm:ss') } catch { "$d" } } else { $null } }
function T([scriptblock]$sb){ try { & $sb } catch { $null } }
"""

_DATE_RE = re.compile(r"^/Date\((-?\d+)\)/$")


def _norm(v):
    """Normalize PowerShell JSON quirks: /Date(ms)/ strings, trailing spaces."""
    if isinstance(v, dict):
        return {k: _norm(x) for k, x in v.items()}
    if isinstance(v, list):
        return [_norm(x) for x in v]
    if isinstance(v, str):
        m = _DATE_RE.match(v)
        if m:
            try:
                return datetime.fromtimestamp(int(m.group(1)) / 1000).strftime("%Y-%m-%dT%H:%M:%S")
            except (OverflowError, OSError, ValueError):
                return v
        return v.strip()
    return v


def _as_list(v) -> list:
    if v is None:
        return []
    if isinstance(v, list):
        return [x for x in v if x is not None]
    return [v]


def run_ps(script: str, timeout: int = 60):
    if not IS_WINDOWS:
        raise RuntimeError("Fonction disponible uniquement sous Windows")
    r = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", PS_PRELUDE + script],
        capture_output=True, timeout=timeout, creationflags=CREATE_NO_WINDOW,
    )
    out = r.stdout.decode("utf-8", "replace").strip()
    if not out:
        err = r.stderr.decode("utf-8", "replace").strip()
        raise RuntimeError("PowerShell n'a renvoyé aucune donnée" + (f" : {err[:300]}" if err else ""))
    # Keep the last JSON line (ignore any stray output)
    line = out.splitlines()[-1]
    return _norm(json.loads(line))


# ── Sections ─────────────────────────────────────────────────────────────────

PS_SYSTEM = r"""
$cs = Get-CimInstance Win32_ComputerSystem
$os = Get-CimInstance Win32_OperatingSystem
$cv = Get-ItemProperty 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion'
$tz = Get-TimeZone
$act = T { $lp = Get-CimInstance SoftwareLicensingProduct -Filter "PartialProductKey IS NOT NULL AND ApplicationID='55c92734-d682-4d71-983e-d6ec3f16059f'" -ErrorAction Stop | Select-Object -First 1
  if ($lp) { [ordered]@{ status = [int]$lp.LicenseStatus; name = $lp.Name; channel = $lp.ProductKeyChannel } } }
$up = $null; if ($os.LastBootUpTime) { $up = [int64]((Get-Date) - $os.LastBootUpTime).TotalSeconds }
[ordered]@{
  hostname = $env:COMPUTERNAME; dns_hostname = $cs.DNSHostName; domain = $cs.Domain; part_of_domain = $cs.PartOfDomain;
  workgroup = $cs.Workgroup; os_caption = $os.Caption; os_version = $os.Version; build = $os.BuildNumber; ubr = $cv.UBR;
  display_version = $cv.DisplayVersion; edition_id = $cv.EditionID; arch = $os.OSArchitecture;
  install_date = D $os.InstallDate; last_boot = D $os.LastBootUpTime; uptime_s = $up;
  registered_user = $os.RegisteredUser; organization = $os.Organization;
  current_user = "$env:USERDOMAIN\$env:USERNAME"; session_user = $cs.UserName;
  timezone = $tz.DisplayName; timezone_id = $tz.Id; locale = (Get-Culture).Name; ui_language = (Get-UICulture).Name;
  windows_dir = $os.WindowsDirectory; activation = $act
} | ConvertTo-Json -Depth 4 -Compress
"""

PS_HARDWARE = r"""
$cs = Get-CimInstance Win32_ComputerSystem
$bios = Get-CimInstance Win32_BIOS
$prod = Get-CimInstance Win32_ComputerSystemProduct
$bb = Get-CimInstance Win32_BaseBoard
$sb = T { (Get-ItemProperty 'HKLM:\SYSTEM\CurrentControlSet\Control\SecureBoot\State' -ErrorAction Stop).UEFISecureBootEnabled }
$tpm = T { $t = Get-CimInstance -Namespace root/cimv2/security/microsofttpm -ClassName Win32_Tpm -ErrorAction Stop
  if ($t) { [ordered]@{ present = $true; spec_version = $t.SpecVersion; enabled = $t.IsEnabled_InitialValue; activated = $t.IsActivated_InitialValue; manufacturer = $t.ManufacturerIdTxt; source = 'Win32_Tpm' } } }
if (-not $tpm) { $tpm = T { $p = @(Get-CimInstance Win32_PnPEntity -Filter "PNPClass='SecurityDevices'" | Where-Object { $_.Name -match 'TPM|Trusted|plateforme' })
  if ($p.Count) { [ordered]@{ present = $true; name = $p[0].Name; status = $p[0].Status; source = 'PnP' } } else { [ordered]@{ present = $false; source = 'PnP' } } } }
$cpus = @(Get-CimInstance Win32_Processor | ForEach-Object { [ordered]@{ name = $_.Name; manufacturer = $_.Manufacturer; cores = $_.NumberOfCores; threads = $_.NumberOfLogicalProcessors;
  max_mhz = $_.MaxClockSpeed; current_mhz = $_.CurrentClockSpeed; socket = $_.SocketDesignation; l2_kb = $_.L2CacheSize; l3_kb = $_.L3CacheSize; virtualization = $_.VirtualizationFirmwareEnabled } })
$ram = @(Get-CimInstance Win32_PhysicalMemory | ForEach-Object { [ordered]@{ bank = $_.BankLabel; slot = $_.DeviceLocator; size = $_.Capacity; speed = $_.Speed;
  configured_speed = $_.ConfiguredClockSpeed; manufacturer = $_.Manufacturer; part_number = $_.PartNumber; serial = $_.SerialNumber } })
$slots = T { (Get-CimInstance Win32_PhysicalMemoryArray | Measure-Object -Property MemoryDevices -Sum).Sum }
$gpus = @(Get-CimInstance Win32_VideoController | ForEach-Object { [ordered]@{ name = $_.Name; driver_version = $_.DriverVersion; driver_date = D $_.DriverDate;
  vram = $_.AdapterRAM; resolution = $(if ($_.CurrentHorizontalResolution) { "$($_.CurrentHorizontalResolution) x $($_.CurrentVerticalResolution)" } else { $null }); refresh = $_.CurrentRefreshRate; status = $_.Status } })
$sizes = @{}
T { Get-CimInstance -Namespace root/wmi -ClassName WmiMonitorBasicDisplayParams -ErrorAction Stop | ForEach-Object { $sizes[$_.InstanceName] = @($_.MaxHorizontalImageSize, $_.MaxVerticalImageSize) } } | Out-Null
function S($a){ if ($a) { (($a | Where-Object { $_ -ne 0 } | ForEach-Object { [char]$_ }) -join '').Trim() } else { $null } }
$mons = @(T { Get-CimInstance -Namespace root/wmi -ClassName WmiMonitorID -ErrorAction Stop | ForEach-Object {
  $sz = $sizes[$_.InstanceName]; $diag = $null
  if ($sz -and $sz[0] -gt 0) { $diag = [math]::Round([math]::Sqrt($sz[0]*$sz[0] + $sz[1]*$sz[1]) / 2.54, 1) }
  [ordered]@{ name = S $_.UserFriendlyName; manufacturer = S $_.ManufacturerName; serial = S $_.SerialNumberID; year = $_.YearOfManufacture; active = $_.Active; diagonal_in = $diag } } })
[ordered]@{
  manufacturer = $cs.Manufacturer; model = $cs.Model; system_family = $cs.SystemFamily; system_type = $cs.SystemType;
  serial = $(if ($bios.SerialNumber) { $bios.SerialNumber } else { $prod.IdentifyingNumber }); uuid = $prod.UUID;
  baseboard = "$($bb.Manufacturer) $($bb.Product)".Trim();
  bios_vendor = $bios.Manufacturer; bios_version = $bios.SMBIOSBIOSVersion; bios_date = D $bios.ReleaseDate;
  firmware_type = $env:firmware_type; secure_boot = $sb; tpm = $tpm;
  cpus = $cpus; ram_total = $cs.TotalPhysicalMemory; ram_slots = $slots; ram_modules = $ram; gpus = $gpus; monitors = $mons
} | ConvertTo-Json -Depth 5 -Compress
"""

PS_STORAGE = r"""
$pd = @(T { Get-PhysicalDisk -ErrorAction Stop | Sort-Object DeviceId | ForEach-Object { [ordered]@{ id = $_.DeviceId; model = $_.FriendlyName; size = $_.Size;
  media = "$($_.MediaType)"; bus = "$($_.BusType)"; health = "$($_.HealthStatus)"; status = "$($_.OperationalStatus)"; serial = $_.SerialNumber; firmware = $_.FirmwareVersion } } })
if (-not $pd.Count) { $pd = @(Get-CimInstance Win32_DiskDrive | ForEach-Object { [ordered]@{ id = $_.Index; model = $_.Model; size = $_.Size; media = $_.MediaType; bus = $_.InterfaceType; health = $_.Status; serial = $_.SerialNumber; firmware = $_.FirmwareRevision } }) }
$sh = T { New-Object -ComObject Shell.Application }
$vols = @(Get-CimInstance Win32_LogicalDisk | ForEach-Object {
  $bl = $null; if ($sh) { $bl = T { $sh.NameSpace($_.DeviceID + '\').Self.ExtendedProperty('System.Volume.BitLockerProtection') } }
  [ordered]@{ letter = $_.DeviceID; label = $_.VolumeName; fs = $_.FileSystem; size = $_.Size; free = $_.FreeSpace; drive_type = $_.DriveType; bitlocker = $bl } })
[ordered]@{ disks = $pd; volumes = $vols } | ConvertTo-Json -Depth 4 -Compress
"""

PS_UPDATES = r"""
$h = @(Get-HotFix | ForEach-Object { $d = $null; try { $d = D $_.InstalledOn } catch {}
  [ordered]@{ id = $_.HotFixID; description = $_.Description; installed_on = $d; installed_by = $_.InstalledBy } })
[ordered]@{ hotfixes = $h } | ConvertTo-Json -Depth 4 -Compress
"""

PS_STARTUP = r"""
$s = @(Get-CimInstance Win32_StartupCommand | ForEach-Object { [ordered]@{ name = $_.Name; command = $_.Command; location = $_.Location; user = $_.User } })
[ordered]@{ items = $s } | ConvertTo-Json -Depth 4 -Compress
"""

MEDIA = {"0": "Non spécifié", "3": "HDD", "4": "SSD", "5": "SCM", "Unspecified": "Non spécifié"}
BUS = {"1": "SCSI", "2": "ATAPI", "3": "ATA", "4": "IEEE 1394", "5": "SSA", "6": "Fibre Channel", "7": "USB", "8": "RAID",
       "9": "iSCSI", "10": "SAS", "11": "SATA", "12": "SD", "13": "MMC", "15": "File Backed Virtual", "16": "Storage Spaces", "17": "NVMe"}
HEALTH = {"0": "Healthy", "1": "Warning", "2": "Unhealthy", "5": "Unknown"}
HEALTH_FR = {"Healthy": "Sain", "Warning": "Avertissement", "Unhealthy": "Défaillant", "Unknown": "Inconnu", "OK": "Sain"}
DRIVE_TYPES = {0: "Inconnu", 1: "Sans racine", 2: "Amovible", 3: "Local", 4: "Réseau", 5: "CD/DVD", 6: "RAM disk"}
BITLOCKER = {0: None, 1: "Activé", 2: "Désactivé", 3: "Chiffrement en cours", 4: "Déchiffrement en cours",
             5: "Suspendu", 6: "Activé (verrouillé)", 8: "En attente d'activation"}
LICENSE = {0: "Sans licence", 1: "Activé", 2: "Période de grâce initiale", 3: "Grâce (hors tolérance)",
           4: "Grâce (non authentique)", 5: "Notification", 6: "Grâce étendue"}


def _system():
    d = run_ps(PS_SYSTEM)
    act = d.get("activation")
    if isinstance(act, dict):
        act["label"] = LICENSE.get(act.get("status"), str(act.get("status")))
    return d


def _hardware():
    d = run_ps(PS_HARDWARE)
    for k in ("cpus", "ram_modules", "gpus", "monitors"):
        d[k] = _as_list(d.get(k))
    sb = d.get("secure_boot")
    d["secure_boot"] = None if sb is None else bool(sb)
    if isinstance(d.get("tpm"), list):
        d["tpm"] = d["tpm"][0] if d["tpm"] else None
    return d


def _storage():
    d = run_ps(PS_STORAGE)
    disks = _as_list(d.get("disks"))
    for x in disks:
        m, b, h = str(x.get("media") or ""), str(x.get("bus") or ""), str(x.get("health") or "")
        x["media"] = MEDIA.get(m, m) or "–"
        x["bus"] = BUS.get(b, b) or "–"
        h = HEALTH.get(h, h)
        x["health"] = HEALTH_FR.get(h, h) or "–"
        x["health_ok"] = h in ("Healthy", "OK")
    vols = _as_list(d.get("volumes"))
    for v in vols:
        v["drive_type_label"] = DRIVE_TYPES.get(v.get("drive_type"), "–")
        bl = v.get("bitlocker")
        try:
            bl = int(bl) if bl is not None and bl != "" else None
        except (TypeError, ValueError):
            bl = None
        v["bitlocker"] = BITLOCKER.get(bl) if bl is not None else None
        size, free = v.get("size") or 0, v.get("free") or 0
        v["used_pct"] = round((size - free) * 100 / size, 1) if size else None
    return {"disks": disks, "volumes": vols}


def _updates():
    d = run_ps(PS_UPDATES)
    hf = _as_list(d.get("hotfixes"))
    hf.sort(key=lambda x: x.get("installed_on") or "", reverse=True)
    return {"hotfixes": hf}


# ── Registry-based sections ──────────────────────────────────────────────────

def _reg_values(root, path, view=0):
    import winreg
    out = {}
    try:
        with winreg.OpenKey(root, path, 0, winreg.KEY_READ | view) as k:
            i = 0
            while True:
                try:
                    name, val, typ = winreg.EnumValue(k, i)
                except OSError:
                    break
                out[name] = (val, typ)
                i += 1
    except OSError:
        pass
    return out


def _software():
    import winreg
    UNINST = r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"
    sources = [
        (winreg.HKEY_LOCAL_MACHINE, winreg.KEY_WOW64_64KEY, "HKLM 64 bits"),
        (winreg.HKEY_LOCAL_MACHINE, winreg.KEY_WOW64_32KEY, "HKLM 32 bits"),
        (winreg.HKEY_CURRENT_USER, 0, "HKCU"),
    ]
    items, seen = [], set()
    for root, view, label in sources:
        try:
            base = winreg.OpenKey(root, UNINST, 0, winreg.KEY_READ | view)
        except OSError:
            continue
        with base:
            i = 0
            while True:
                try:
                    sub = winreg.EnumKey(base, i)
                except OSError:
                    break
                i += 1
                vals = _reg_values(root, UNINST + "\\" + sub, view)
                name = str(vals.get("DisplayName", ("",))[0] or "").strip()
                if not name:
                    continue
                if vals.get("SystemComponent", (0,))[0] == 1 or vals.get("ParentKeyName") or vals.get("ReleaseType", ("",))[0] in ("Update", "Hotfix", "Security Update"):
                    continue
                version = str(vals.get("DisplayVersion", ("",))[0] or "").strip()
                key = (name.lower(), version.lower())
                if key in seen:
                    continue
                seen.add(key)
                idate = str(vals.get("InstallDate", ("",))[0] or "").strip()
                if re.fullmatch(r"\d{8}", idate):
                    idate = f"{idate[:4]}-{idate[4:6]}-{idate[6:]}"
                size = vals.get("EstimatedSize", (None,))[0]
                items.append({
                    "name": name, "version": version,
                    "publisher": str(vals.get("Publisher", ("",))[0] or "").strip(),
                    "install_date": idate,
                    "size": int(size) * 1024 if isinstance(size, int) and size > 0 else None,
                    "location": str(vals.get("InstallLocation", ("",))[0] or "").strip(),
                    "uninstall": str(vals.get("UninstallString", ("",))[0] or "").strip(),
                    "source": label,
                })
    items.sort(key=lambda x: x["name"].lower())
    return {"items": items}


def _startup_approved():
    """Return {name.lower(): enabled_bool} from Explorer\\StartupApproved."""
    import winreg
    res = {}
    base = r"Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved"
    for root in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        for sub in ("Run", "Run32", "StartupFolder"):
            for name, (val, typ) in _reg_values(root, base + "\\" + sub).items():
                if isinstance(val, (bytes, bytearray)) and val:
                    res[name.lower()] = val[0] in (0, 2, 6)
    return res


def _startup():
    import winreg
    items = []
    RUN = r"SOFTWARE\Microsoft\Windows\CurrentVersion"
    keys = [
        (winreg.HKEY_LOCAL_MACHINE, RUN + r"\Run", winreg.KEY_WOW64_64KEY, "HKLM\\Run"),
        (winreg.HKEY_LOCAL_MACHINE, RUN + r"\RunOnce", winreg.KEY_WOW64_64KEY, "HKLM\\RunOnce"),
        (winreg.HKEY_LOCAL_MACHINE, RUN + r"\Run", winreg.KEY_WOW64_32KEY, "HKLM\\Run (32 bits)"),
        (winreg.HKEY_LOCAL_MACHINE, RUN + r"\RunOnce", winreg.KEY_WOW64_32KEY, "HKLM\\RunOnce (32 bits)"),
        (winreg.HKEY_CURRENT_USER, RUN + r"\Run", 0, "HKCU\\Run"),
        (winreg.HKEY_CURRENT_USER, RUN + r"\RunOnce", 0, "HKCU\\RunOnce"),
    ]
    seen = set()
    for root, path, view, label in keys:
        for name, (val, typ) in _reg_values(root, path, view).items():
            k = (name.lower(), str(val).lower())
            if not name or k in seen:
                continue
            seen.add(k)
            items.append({"name": name, "command": str(val), "location": label,
                          "user": "Tous les utilisateurs" if root == winreg.HKEY_LOCAL_MACHINE else os.environ.get("USERNAME", "")})
    folders = [
        (Path(os.environ.get("APPDATA", "")) / r"Microsoft\Windows\Start Menu\Programs\Startup", "Dossier Démarrage (utilisateur)"),
        (Path(os.environ.get("PROGRAMDATA", r"C:\ProgramData")) / r"Microsoft\Windows\Start Menu\Programs\StartUp", "Dossier Démarrage (commun)"),
    ]
    for folder, label in folders:
        try:
            for f in folder.iterdir():
                if f.name.lower() == "desktop.ini" or not f.is_file():
                    continue
                k = (f.name.lower(), str(f).lower())
                if k in seen:
                    continue
                seen.add(k)
                items.append({"name": f.name, "command": str(f), "location": label, "user": ""})
        except OSError:
            pass
    # Complete with Win32_StartupCommand (other users' hives, etc.)
    try:
        d = run_ps(PS_STARTUP, timeout=30)
        names = {i["name"].lower() for i in items}
        for x in _as_list(d.get("items")):
            n = str(x.get("name") or "")
            if n and n.lower() not in names and Path(n).stem.lower() not in {Path(i).stem for i in names}:
                names.add(n.lower())
                items.append({"name": n, "command": x.get("command") or "", "location": x.get("location") or "WMI", "user": x.get("user") or ""})
    except Exception:
        pass
    approved = _startup_approved()
    for i in items:
        en = approved.get(i["name"].lower())
        i["enabled"] = True if en is None else en
    items.sort(key=lambda x: x["name"].lower())
    return {"items": items}


def _env():
    import winreg
    sys_vars = _reg_values(winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment")
    usr_vars = _reg_values(winreg.HKEY_CURRENT_USER, "Environment")
    items, known = [], set()
    for scope, src in (("Système", sys_vars), ("Utilisateur", usr_vars)):
        for name, (val, typ) in src.items():
            known.add(name.upper())
            items.append({"name": name, "value": str(val), "scope": scope, "expand": typ == winreg.REG_EXPAND_SZ})
    for name, val in os.environ.items():
        if name.upper() not in known:
            items.append({"name": name, "value": val, "scope": "Processus", "expand": False})
    items.sort(key=lambda x: (x["name"].upper(), x["scope"]))
    return {"items": items}


COLLECTORS = {
    "system": _system, "hardware": _hardware, "storage": _storage, "software": _software,
    "updates": _updates, "startup": _startup, "env": _env,
}


async def _get_section(name: str, refresh: bool = False) -> dict:
    lock = _locks.setdefault(name, asyncio.Lock())
    async with lock:
        if not refresh and name in _cache:
            return _cache[name]
        t0 = time.time()
        data = await asyncio.to_thread(COLLECTORS[name])
        entry = {"section": name, "data": data, "collected_at": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
                 "duration_ms": int((time.time() - t0) * 1000)}
        _cache[name] = entry
        return entry


# ── Routes ───────────────────────────────────────────────────────────────────

@router.get("/status")
async def status():
    return {"admin": _is_admin(), "windows": IS_WINDOWS, "sections": SECTIONS, "cached": sorted(_cache)}


@router.get("/section/{name}")
async def section(name: str, refresh: bool = False):
    if name not in COLLECTORS:
        return _err("Section inconnue", 404)
    try:
        return await _get_section(name, refresh)
    except subprocess.TimeoutExpired:
        return _err("Délai dépassé lors de la collecte des informations", 504)
    except Exception as e:
        return _err(f"Erreur de collecte : {e}", 500)


async def _all_sections(refresh: bool = False) -> dict:
    res = await asyncio.gather(*[_get_section(s, refresh) for s in SECTIONS], return_exceptions=True)
    out = {}
    for s, r in zip(SECTIONS, res):
        out[s] = {"error": str(r)} if isinstance(r, Exception) else r
    return out


@router.post("/export/{fmt}")
async def export(fmt: str):
    if fmt not in ("html", "json", "software-csv"):
        return _err("Format inconnu", 404)
    TEMP_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    host = re.sub(r"[^A-Za-z0-9_-]", "_", os.environ.get("COMPUTERNAME", "pc"))
    if fmt == "software-csv":
        sw = await _get_section("software")
        fn = f"logiciels_{host}_{ts}.csv"
        await asyncio.to_thread(_write_csv, TEMP_DIR / fn, sw["data"]["items"])
        return {"filename": fn}
    data = await _all_sections()
    if fmt == "json":
        fn = f"inventaire_{host}_{ts}.json"
        payload = {"generated_at": datetime.now().isoformat(timespec="seconds"), "hostname": os.environ.get("COMPUTERNAME"),
                   "sections": {k: (v.get("data") if "data" in v else v) for k, v in data.items()}}
        (TEMP_DIR / fn).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    else:
        fn = f"inventaire_{host}_{ts}.html"
        (TEMP_DIR / fn).write_text(_render_html(data), encoding="utf-8")
    return {"filename": fn}


def _write_csv(path: Path, items: list):
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(["Nom", "Version", "Éditeur", "Date d'installation", "Taille (octets)", "Emplacement", "Source"])
        for i in items:
            w.writerow([i["name"], i["version"], i["publisher"], i["install_date"], i["size"] or "", i["location"], i["source"]])


@router.get("/file/{filename}")
async def get_file(filename: str):
    if "/" in filename or "\\" in filename or ".." in filename:
        return _err("Nom de fichier invalide")
    path = TEMP_DIR / filename
    if not path.is_file():
        return _err("Fichier introuvable", 404)
    mt = {"html": "text/html", "json": "application/json", "csv": "text/csv"}.get(filename.rsplit(".", 1)[-1], "application/octet-stream")
    return FileResponse(path, media_type=mt, headers={"Content-Disposition": f'attachment; filename="{filename}"'})


# ── HTML report ──────────────────────────────────────────────────────────────

def _fmt_bytes(b) -> str:
    try:
        b = float(b)
    except (TypeError, ValueError):
        return "–"
    for u in ("o", "Ko", "Mo", "Go", "To"):
        if b < 1024 or u == "To":
            return f"{b:.0f} {u}" if u == "o" else f"{b:.1f} {u}".replace(".", ",")
        b /= 1024
    return "–"


def _fmt_dur(s) -> str:
    try:
        s = int(s)
    except (TypeError, ValueError):
        return "–"
    d, s = divmod(s, 86400)
    h, s = divmod(s, 3600)
    m = s // 60
    return f"{d} j {h} h {m} min" if d else f"{h} h {m} min"


def _e(v) -> str:
    if v is None or v == "":
        return "–"
    if isinstance(v, bool):
        return "Oui" if v else "Non"
    return html.escape(str(v))


def _kv(rows) -> str:
    return "<table class='kv'>" + "".join(f"<tr><th>{html.escape(k)}</th><td>{_e(v)}</td></tr>" for k, v in rows) + "</table>"


def _tbl(headers, rows) -> str:
    h = "".join(f"<th>{html.escape(x)}</th>" for x in headers)
    b = "".join("<tr>" + "".join(f"<td>{_e(c)}</td>" for c in r) + "</tr>" for r in rows)
    return f"<table><thead><tr>{h}</tr></thead><tbody>{b or '<tr><td colspan=99>Aucune donnée</td></tr>'}</tbody></table>"


def _render_html(data: dict) -> str:
    parts = []

    def sec(key, body):
        parts.append(f"<section id='{key}'><h2>{SECTION_LABELS[key]}</h2>{body}</section>")

    def d(key):
        v = data.get(key) or {}
        return v.get("data") if "data" in v else None

    s = d("system")
    if s:
        act = s.get("activation") or {}
        sec("system", _kv([
            ("Nom d'hôte", s.get("hostname")), ("Domaine / groupe de travail", s.get("domain") if s.get("part_of_domain") else s.get("workgroup") or s.get("domain")),
            ("Système", s.get("os_caption")), ("Version", f"{s.get('display_version') or ''} ({s.get('os_version')}.{s.get('ubr') or 0})"),
            ("Build", s.get("build")), ("Architecture", s.get("arch")), ("Installé le", s.get("install_date")),
            ("Dernier démarrage", s.get("last_boot")), ("Temps de fonctionnement", _fmt_dur(s.get("uptime_s"))),
            ("Utilisateur courant", s.get("current_user")), ("Fuseau horaire", s.get("timezone")), ("Langue", s.get("ui_language")),
            ("Activation", act.get("label")),
        ]))
    h = d("hardware")
    if h:
        tpm = h.get("tpm") or {}
        body = _kv([
            ("Fabricant", h.get("manufacturer")), ("Modèle", h.get("model")), ("Numéro de série", h.get("serial")),
            ("Carte mère", h.get("baseboard")), ("BIOS", f"{h.get('bios_vendor') or ''} {h.get('bios_version') or ''}"),
            ("Date du BIOS", h.get("bios_date")), ("Micrologiciel", h.get("firmware_type")), ("Secure Boot", h.get("secure_boot")),
            ("TPM", (f"Présent {tpm.get('spec_version') or tpm.get('name') or ''}" if tpm.get("present") else "Non détecté") if tpm else None),
            ("Mémoire totale", _fmt_bytes(h.get("ram_total"))),
        ])
        body += "<h3>Processeur(s)</h3>" + _tbl(["Modèle", "Cœurs", "Threads", "Fréquence max (MHz)"],
                                                [[c.get("name"), c.get("cores"), c.get("threads"), c.get("max_mhz")] for c in h.get("cpus", [])])
        body += "<h3>Barrettes mémoire</h3>" + _tbl(["Emplacement", "Taille", "Vitesse (MT/s)", "Fabricant", "Référence"],
                                                    [[m.get("slot"), _fmt_bytes(m.get("size")), m.get("configured_speed") or m.get("speed"), m.get("manufacturer"), m.get("part_number")] for m in h.get("ram_modules", [])])
        body += "<h3>Cartes graphiques</h3>" + _tbl(["Nom", "Pilote", "Date du pilote", "Résolution"],
                                                    [[g.get("name"), g.get("driver_version"), g.get("driver_date"), g.get("resolution")] for g in h.get("gpus", [])])
        body += "<h3>Moniteurs</h3>" + _tbl(["Nom", "Fabricant", "Diagonale (pouces)", "Année"],
                                            [[m.get("name"), m.get("manufacturer"), m.get("diagonal_in"), m.get("year")] for m in h.get("monitors", [])])
        sec("hardware", body)
    st = d("storage")
    if st:
        body = "<h3>Disques physiques</h3>" + _tbl(["Modèle", "Taille", "Type", "Bus", "Santé"],
                                                   [[x.get("model"), _fmt_bytes(x.get("size")), x.get("media"), x.get("bus"), x.get("health")] for x in st["disks"]])
        rows = []
        for v in st["volumes"]:
            pct = v.get("used_pct")
            bar = f"<div class='bar'><i style='width:{pct or 0}%;background:{'#ef4444' if (pct or 0) > 90 else '#6366f1'}'></i></div>{pct} %" if pct is not None else "–"
            rows.append(f"<tr><td>{_e(v.get('letter'))}</td><td>{_e(v.get('label'))}</td><td>{_e(v.get('fs'))}</td><td>{_e(v.get('drive_type_label'))}</td>"
                        f"<td>{_fmt_bytes(v.get('size'))}</td><td>{_fmt_bytes(v.get('free'))}</td><td>{bar}</td><td>{_e(v.get('bitlocker'))}</td></tr>")
        body += ("<h3>Volumes</h3><table><thead><tr><th>Lecteur</th><th>Nom</th><th>FS</th><th>Type</th><th>Taille</th><th>Libre</th><th>Utilisation</th><th>BitLocker</th></tr></thead><tbody>"
                 + "".join(rows) + "</tbody></table>")
        sec("storage", body)
    sw = d("software")
    if sw:
        sec("software", f"<p>{len(sw['items'])} logiciels</p>" + _tbl(["Nom", "Version", "Éditeur", "Installé le", "Taille"],
                                                                      [[i["name"], i["version"], i["publisher"], i["install_date"], _fmt_bytes(i["size"]) if i["size"] else None] for i in sw["items"]]))
    up = d("updates")
    if up:
        sec("updates", _tbl(["KB", "Description", "Installé le", "Par"],
                            [[x.get("id"), x.get("description"), x.get("installed_on"), x.get("installed_by")] for x in up["hotfixes"]]))
    so = d("startup")
    if so:
        sec("startup", _tbl(["Nom", "Commande", "Emplacement", "Activé"],
                            [[x["name"], x["command"], x["location"], x["enabled"]] for x in so["items"]]))
    en = d("env")
    if en:
        sec("env", _tbl(["Nom", "Valeur", "Portée"], [[x["name"], x["value"], x["scope"]] for x in en["items"]]))
    errs = [f"<li>{SECTION_LABELS[k]} : {html.escape(v['error'])}</li>" for k, v in data.items() if "error" in v]
    if errs:
        parts.append("<section><h2>Erreurs</h2><ul>" + "".join(errs) + "</ul></section>")
    host = html.escape(os.environ.get("COMPUTERNAME", ""))
    nav = "".join(f"<a href='#{k}'>{SECTION_LABELS[k]}</a>" for k in SECTIONS)
    return f"""<!DOCTYPE html><html lang="fr"><head><meta charset="utf-8"><title>Inventaire système — {host}</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
body{{background:#0f0f12;color:#e2e2e8;font:14px/1.45 system-ui,'Segoe UI',sans-serif;margin:0;padding:24px;max-width:1200px;margin:auto}}
h1{{font-size:22px}}h2{{font-size:17px;border-bottom:1px solid #27272a;padding-bottom:6px;margin-top:0}}h3{{font-size:14px;color:#a1a1aa;margin:16px 0 6px}}
section{{background:#18181b;border:1px solid #27272a;border-radius:8px;padding:16px;margin:14px 0}}
table{{width:100%;border-collapse:collapse;font-size:13px}}th,td{{text-align:left;padding:5px 8px;border-bottom:1px solid #27272a;vertical-align:top;word-break:break-word}}
th{{color:#71717a;font-weight:500}}table.kv th{{width:240px}}nav a{{color:#a5b4fc;margin-right:12px;text-decoration:none}}
.bar{{display:inline-block;width:120px;height:8px;background:#27272a;border-radius:4px;overflow:hidden;margin-right:6px;vertical-align:middle}}.bar i{{display:block;height:100%}}
@media print{{body{{background:#fff;color:#000}}section{{background:#fff;border-color:#ccc}}}}
</style></head><body>
<h1>💻 Inventaire système — {host}</h1><p style="color:#71717a">Généré le {datetime.now().strftime('%d/%m/%Y à %H:%M')} par LedroideDesktop</p>
<nav>{nav}</nav>{''.join(parts)}</body></html>"""
