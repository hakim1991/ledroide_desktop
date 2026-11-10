"""Démarrage Windows — entrées Run/RunOnce, dossiers Démarrage, tâches d'ouverture de session."""
import asyncio
import ctypes
import json
import logging
import os
import shutil
import struct
import subprocess
import winreg
from ctypes import wintypes
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ledroide_sdk import Module

log = logging.getLogger("ledroide.startup-manager")

module = Module(__file__)
router = module.router

HKCU, HKLM = winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE
HIVE_NAMES = {HKCU: "HKCU", HKLM: "HKLM"}
CV = r"Software\Microsoft\Windows\CurrentVersion"
CV32 = r"Software\WOW6432Node\Microsoft\Windows\CurrentVersion"
APPROVED = CV + r"\Explorer\StartupApproved"


def _known_folder(env: str, *rest: str) -> str:
    base = os.environ.get(env, "")
    return str(Path(base, *rest)) if base else ""


# Each source: registry key (kind "reg") or folder (kind "folder"); `approved` is the
# StartupApproved key whose binary values carry the enabled/disabled flag (None = not toggleable).
SOURCES: list[dict] = [
    {"key": "hkcu_run", "kind": "reg", "hive": HKCU, "path": CV + r"\Run", "approved": (HKCU, APPROVED + r"\Run"),
     "scope": "user", "label": "Registre utilisateur (Run)"},
    {"key": "hkcu_runonce", "kind": "reg", "hive": HKCU, "path": CV + r"\RunOnce", "approved": None,
     "scope": "user", "label": "Registre utilisateur (RunOnce)"},
    {"key": "hklm_run", "kind": "reg", "hive": HKLM, "path": CV + r"\Run", "approved": (HKLM, APPROVED + r"\Run"),
     "scope": "machine", "label": "Registre machine (Run)"},
    {"key": "hklm_run32", "kind": "reg", "hive": HKLM, "path": CV32 + r"\Run", "approved": (HKLM, APPROVED + r"\Run32"),
     "scope": "machine", "label": "Registre machine 32 bits (Run)"},
    {"key": "hklm_runonce", "kind": "reg", "hive": HKLM, "path": CV + r"\RunOnce", "approved": None,
     "scope": "machine", "label": "Registre machine (RunOnce)"},
    {"key": "hklm_runonce32", "kind": "reg", "hive": HKLM, "path": CV32 + r"\RunOnce", "approved": None,
     "scope": "machine", "label": "Registre machine 32 bits (RunOnce)"},
    {"key": "user_folder", "kind": "folder",
     "path": _known_folder("APPDATA", "Microsoft", "Windows", "Start Menu", "Programs", "Startup"),
     "approved": (HKCU, APPROVED + r"\StartupFolder"), "scope": "user", "label": "Dossier Démarrage (utilisateur)"},
    {"key": "common_folder", "kind": "folder",
     "path": _known_folder("PROGRAMDATA", "Microsoft", "Windows", "Start Menu", "Programs", "StartUp"),
     "approved": (HKLM, APPROVED + r"\StartupFolder"), "scope": "machine", "label": "Dossier Démarrage (tous les utilisateurs)"},
]
ADD_SOURCE = "hkcu_run"
KEY64 = winreg.KEY_WOW64_64KEY


class StartupError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _err(exc: StartupError) -> dict:
    return {"error": exc.message, "code": exc.code}


def _source(key: str) -> dict:
    for s in SOURCES:
        if s["key"] == key:
            return s
    raise StartupError("BAD_ID", "Emplacement inconnu")


def _is_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def _denied(src_scope: str) -> StartupError:
    if src_scope == "machine":
        return StartupError("NEED_ADMIN", "Accès refusé : cette entrée concerne tous les utilisateurs et nécessite "
                                          "les droits administrateur. Relancez LedroideDesktop en tant qu'administrateur.")
    return StartupError("ACCESS_DENIED", "Accès refusé par Windows.")


# ── File metadata (publisher / description) ─────────────────────────────────

_version = ctypes.WinDLL("version")
_version.GetFileVersionInfoSizeW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(wintypes.DWORD)]
_version.GetFileVersionInfoSizeW.restype = wintypes.DWORD
_version.GetFileVersionInfoW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p]
_version.GetFileVersionInfoW.restype = wintypes.BOOL
_version.VerQueryValueW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_void_p),
                                    ctypes.POINTER(wintypes.UINT)]
_version.VerQueryValueW.restype = wintypes.BOOL

_meta_cache: dict[tuple, dict] = {}


def _file_meta(path: str) -> dict:
    try:
        st = os.stat(path)
    except OSError:
        return {}
    ck = (path.lower(), st.st_mtime_ns)
    if ck in _meta_cache:
        return _meta_cache[ck]
    out: dict = {}
    try:
        size = _version.GetFileVersionInfoSizeW(path, None)
        if size:
            buf = ctypes.create_string_buffer(size)
            if _version.GetFileVersionInfoW(path, 0, size, buf):
                ptr, ln = ctypes.c_void_p(), wintypes.UINT()
                langs = []
                if _version.VerQueryValueW(buf, "\\VarFileInfo\\Translation", ctypes.byref(ptr), ctypes.byref(ln)) \
                        and ln.value >= 4:
                    arr = ctypes.cast(ptr, ctypes.POINTER(ctypes.c_uint16 * (ln.value // 2))).contents
                    langs = [f"{arr[i]:04x}{arr[i + 1]:04x}" for i in range(0, len(arr) - 1, 2)]
                langs += ["040904b0", "040904e4", "040c04b0", "000004b0"]
                for field, key in (("CompanyName", "publisher"), ("FileDescription", "description"),
                                   ("ProductName", "product"), ("FileVersion", "version")):
                    for lang in langs:
                        if _version.VerQueryValueW(buf, f"\\StringFileInfo\\{lang}\\{field}", ctypes.byref(ptr),
                                                   ctypes.byref(ln)) and ln.value and ptr.value:
                            val = ctypes.wstring_at(ptr.value).strip()
                            if val:
                                out[key] = val
                                break
    except Exception:
        log.debug("version info failed for %s", path, exc_info=True)
    _meta_cache[ck] = out
    return out


def _exe_from_command(cmd: str) -> str:
    c = os.path.expandvars(cmd.strip())
    if not c:
        return ""
    if c.startswith('"'):
        end = c.find('"', 1)
        return c[1:end] if end > 0 else c[1:]
    parts = c.split(" ")
    for i in range(1, len(parts) + 1):
        cand = " ".join(parts[:i])
        for ext in ("", ".exe"):
            if os.path.isfile(cand + ext):
                return cand + ext
    return shutil.which(parts[0]) or parts[0]


# ── StartupApproved flags ──────────────────────────────────────────────────

def _filetime_now() -> bytes:
    ft = int((datetime.now(timezone.utc) - datetime(1601, 1, 1, tzinfo=timezone.utc)).total_seconds() * 10_000_000)
    return struct.pack("<Q", ft)


def _decode_flag(raw) -> tuple[bool, str | None]:
    if not isinstance(raw, (bytes, bytearray)) or not raw:
        return True, None
    enabled = raw[0] & 1 == 0
    disabled_at = None
    if not enabled and len(raw) >= 12:
        ft = struct.unpack("<Q", bytes(raw[4:12]))[0]
        if ft:
            try:
                dt = datetime(1601, 1, 1, tzinfo=timezone.utc) + timedelta(microseconds=ft // 10)
                disabled_at = dt.isoformat()
            except OverflowError:
                pass
    return enabled, disabled_at


def _read_values(hive, path: str) -> list[tuple[str, object, int]]:
    try:
        with winreg.OpenKey(hive, path, 0, winreg.KEY_READ | KEY64) as k:
            out, i = [], 0
            while True:
                try:
                    out.append(winreg.EnumValue(k, i))
                except OSError:
                    return out
                i += 1
    except FileNotFoundError:
        return []
    except PermissionError:
        log.info("read denied: %s\\%s", HIVE_NAMES.get(hive), path)
        return []


def _approved_map(approved) -> dict[str, object]:
    if not approved:
        return {}
    return {name.lower(): data for name, data, _ in _read_values(*approved)}


# ── Shortcuts (.lnk) ───────────────────────────────────────────────────────

_lnk_cache: dict[tuple, dict] = {}
_LNK_PS = (
    "[Console]::OutputEncoding=[Text.Encoding]::UTF8;"
    "$sh=New-Object -ComObject WScript.Shell;"
    "$p=$env:LD_LNK_PATHS | ConvertFrom-Json;"
    "@($p | ForEach-Object { $l=$sh.CreateShortcut($_); "
    "[pscustomobject]@{p=$_;t=$l.TargetPath;a=$l.Arguments;d=$l.Description} }) | ConvertTo-Json -Compress"
)


def _powershell(script: str, env: dict | None = None, timeout: float = 30) -> str:
    r = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", script],
        capture_output=True, timeout=timeout, creationflags=subprocess.CREATE_NO_WINDOW,
        env={**os.environ, **(env or {})},
    )
    return r.stdout.decode("utf-8", "replace").strip()


def _resolve_shortcuts(paths: list[str]) -> dict[str, dict]:
    todo, out = [], {}
    for p in paths:
        try:
            ck = (p.lower(), os.stat(p).st_mtime_ns)
        except OSError:
            continue
        if ck in _lnk_cache:
            out[p] = _lnk_cache[ck]
        else:
            todo.append((p, ck))
    if todo:
        try:
            raw = _powershell(_LNK_PS, {"LD_LNK_PATHS": json.dumps([p for p, _ in todo])})
            data = json.loads(raw) if raw else []
            if isinstance(data, dict):
                data = [data]
            by_path = {d.get("p", "").lower(): d for d in data if isinstance(d, dict)}
            for p, ck in todo:
                d = by_path.get(p.lower()) or {}
                info = {"target": d.get("t") or "", "args": d.get("a") or "", "description": d.get("d") or ""}
                _lnk_cache[ck] = info
                out[p] = info
        except (subprocess.SubprocessError, OSError, ValueError):
            log.warning("shortcut resolution failed", exc_info=True)
    return out


# ── Listing ────────────────────────────────────────────────────────────────

def _entry(src: dict, name: str, command: str, approved: dict, extra: dict | None = None) -> dict:
    toggle = src.get("approved") is not None
    enabled, disabled_at = _decode_flag(approved.get(name.lower())) if toggle else (True, None)
    exe = (extra or {}).get("exe") or _exe_from_command(command)
    exists = bool(exe) and os.path.isfile(exe)
    meta = _file_meta(exe) if exists else {}
    return {
        "id": f"{src['key']}|{name}",
        "name": name,
        "command": command,
        "source": src["key"],
        "kind": src["kind"],
        "location": src["label"],
        "location_path": (f"{HIVE_NAMES.get(src.get('hive'), '')}\\{src['path']}" if src["kind"] == "reg"
                          else src["path"]),
        "scope": src["scope"],
        "enabled": enabled,
        "disabled_at": disabled_at,
        "can_toggle": toggle,
        "exe": exe,
        "exists": exists,
        "publisher": meta.get("publisher", ""),
        "description": meta.get("description") or meta.get("product") or (extra or {}).get("description", ""),
        "version": meta.get("version", ""),
        **{k: v for k, v in (extra or {}).items() if k not in ("exe", "description")},
    }


def _list_sync() -> list[dict]:
    items: list[dict] = []
    for src in SOURCES:
        approved = _approved_map(src.get("approved"))
        if src["kind"] == "reg":
            for name, data, vtype in _read_values(src["hive"], src["path"]):
                if not name:
                    continue
                command = str(data) if vtype in (winreg.REG_SZ, winreg.REG_EXPAND_SZ) else repr(data)
                items.append(_entry(src, name, command, approved))
        else:
            folder = src["path"]
            if not folder or not os.path.isdir(folder):
                continue
            try:
                files = [f for f in os.scandir(folder) if f.is_file() and f.name.lower() != "desktop.ini"]
            except OSError:
                continue
            shortcuts = _resolve_shortcuts([f.path for f in files if f.name.lower().endswith(".lnk")])
            for f in files:
                info = shortcuts.get(f.path)
                if info:
                    command = f'"{info["target"]}" {info["args"]}'.strip() if info["target"] else f.path
                    extra = {"exe": info["target"], "file": f.path, "description": info["description"]}
                else:
                    command, extra = f.path, {"exe": f.path, "file": f.path}
                items.append(_entry(src, f.name, command, approved, extra))
    return items


@module.on("list")
async def list_entries(payload: dict):
    items = await asyncio.to_thread(_list_sync)
    return {"items": items, "is_admin": _is_admin(),
            "sources": [{"key": s["key"], "label": s["label"], "scope": s["scope"]} for s in SOURCES]}


_TASKS_PS = r"""
[Console]::OutputEncoding=[Text.Encoding]::UTF8
$ErrorActionPreference='SilentlyContinue'
@(Get-ScheduledTask | Where-Object { $_.Triggers | Where-Object {
    $_.CimClass.CimClassName -in @('MSFT_TaskLogonTrigger','MSFT_TaskBootTrigger') } } | ForEach-Object {
  [pscustomobject]@{
    name=$_.TaskName; path=$_.TaskPath; state=[string]$_.State; author=$_.Author; description=$_.Description
    actions=@($_.Actions | ForEach-Object { ((@($_.Execute, $_.Arguments) | Where-Object { $_ }) -join ' ').Trim() })
    triggers=@($_.Triggers | ForEach-Object { $_.CimClass.CimClassName -replace '^MSFT_Task','' -replace 'Trigger$','' })
  } }) | ConvertTo-Json -Depth 4 -Compress
"""


def _tasks_sync() -> list[dict]:
    raw = _powershell(_TASKS_PS, timeout=60)
    data = json.loads(raw) if raw else []
    if isinstance(data, dict):
        data = [data]
    out = []
    for t in data:
        if not isinstance(t, dict):
            continue
        actions = t.get("actions") or []
        actions = [actions] if isinstance(actions, str) else actions
        triggers = t.get("triggers") or []
        triggers = [triggers] if isinstance(triggers, str) else triggers
        exe = _exe_from_command(actions[0]) if actions else ""
        meta = _file_meta(exe) if exe and os.path.isfile(exe) else {}
        out.append({
            "name": t.get("name") or "", "path": t.get("path") or "\\",
            "state": t.get("state") or "", "author": t.get("author") or "",
            "description": t.get("description") or meta.get("description", ""),
            "publisher": meta.get("publisher", ""),
            "actions": actions,
            "triggers": ["ouverture de session" if x == "Logon" else "démarrage" if x == "Boot" else x for x in triggers],
            "microsoft": (t.get("path") or "").lower().startswith("\\microsoft\\"),
        })
    out.sort(key=lambda x: (x["microsoft"], x["path"].lower(), x["name"].lower()))
    return out


@module.on("tasks")
async def tasks(payload: dict):
    try:
        return {"items": await asyncio.to_thread(_tasks_sync)}
    except subprocess.TimeoutExpired:
        return {"error": "Le Planificateur de tâches ne répond pas (délai dépassé)", "code": "TIMEOUT"}
    except (OSError, ValueError) as exc:
        log.warning("scheduled tasks listing failed: %s", exc)
        return {"error": "Impossible de lire les tâches planifiées", "code": "TASKS_FAILED"}


# ── Mutations ──────────────────────────────────────────────────────────────

def _split_id(entry_id: str) -> tuple[dict, str]:
    key, sep, name = str(entry_id or "").partition("|")
    if not sep or not name:
        raise StartupError("BAD_ID", "Identifiant d'entrée invalide")
    return _source(key), name


def _entry_exists(src: dict, name: str) -> bool:
    if src["kind"] == "reg":
        return any(n.lower() == name.lower() for n, _, _ in _read_values(src["hive"], src["path"]))
    return os.path.isfile(os.path.join(src["path"], name))


def _set_enabled_sync(entry_id: str, enabled: bool) -> dict:
    src, name = _split_id(entry_id)
    if not src.get("approved"):
        raise StartupError("NOT_TOGGLEABLE", "Les entrées RunOnce ne peuvent pas être désactivées "
                                             "(elles ne s'exécutent qu'une fois) — supprimez-les si besoin.")
    if not _entry_exists(src, name):
        raise StartupError("NOT_FOUND", "Entrée introuvable (déjà supprimée ?). Actualisez la liste.")
    hive, path = src["approved"]
    data = (b"\x02" + b"\x00" * 11) if enabled else (b"\x03\x00\x00\x00" + _filetime_now())
    try:
        with winreg.CreateKeyEx(hive, path, 0, winreg.KEY_SET_VALUE | KEY64) as k:
            winreg.SetValueEx(k, name, 0, winreg.REG_BINARY, data)
    except PermissionError:
        raise _denied(src["scope"])
    log.info("startup entry %s -> %s", src["key"], "enabled" if enabled else "disabled")
    return {"ok": True, "enabled": enabled}


def _delete_sync(entry_id: str) -> dict:
    src, name = _split_id(entry_id)
    try:
        if src["kind"] == "reg":
            try:
                with winreg.OpenKey(src["hive"], src["path"], 0, winreg.KEY_SET_VALUE | KEY64) as k:
                    winreg.DeleteValue(k, name)
            except FileNotFoundError:
                raise StartupError("NOT_FOUND", "Entrée introuvable (déjà supprimée ?)")
        else:
            folder = Path(src["path"]).resolve()
            target = (folder / name).resolve()
            if target.parent != folder or not target.is_file():
                raise StartupError("NOT_FOUND", "Fichier introuvable dans le dossier Démarrage")
            target.unlink()
        if src.get("approved"):
            try:
                with winreg.OpenKey(src["approved"][0], src["approved"][1], 0, winreg.KEY_SET_VALUE | KEY64) as k:
                    winreg.DeleteValue(k, name)
            except OSError:
                pass
    except PermissionError:
        raise _denied(src["scope"])
    log.info("startup entry deleted from %s", src["key"])
    return {"ok": True}


def _validate_name(name: str) -> str:
    name = name.strip()
    if not name or len(name) > 200 or any(ord(c) < 32 for c in name) or "|" in name:
        raise StartupError("BAD_NAME", "Nom invalide (1 à 200 caractères, sans « | » ni caractère de contrôle)")
    return name


def _add_sync(name: str, command: str, overwrite: bool) -> dict:
    src = _source(ADD_SOURCE)
    name = _validate_name(name)
    command = command.strip()
    if not command or len(command) > 2000 or "\n" in command:
        raise StartupError("BAD_COMMAND", "Commande invalide (une ligne, 2000 caractères au plus)")
    if not overwrite and _entry_exists(src, name):
        raise StartupError("EXISTS", f"Une entrée « {name} » existe déjà")
    vtype = winreg.REG_EXPAND_SZ if "%" in command else winreg.REG_SZ
    try:
        with winreg.CreateKeyEx(src["hive"], src["path"], 0, winreg.KEY_SET_VALUE | KEY64) as k:
            winreg.SetValueEx(k, name, 0, vtype, command)
        if src.get("approved"):
            try:
                with winreg.OpenKey(src["approved"][0], src["approved"][1], 0, winreg.KEY_SET_VALUE | KEY64) as k:
                    winreg.DeleteValue(k, name)
            except OSError:
                pass
    except PermissionError:
        raise _denied(src["scope"])
    log.info("startup entry added to %s", src["key"])
    exe = _exe_from_command(command)
    return {"ok": True, "id": f"{src['key']}|{name}", "exe_exists": bool(exe) and os.path.isfile(exe)}


@module.on("set_enabled")
async def set_enabled(payload: dict):
    try:
        return await asyncio.to_thread(_set_enabled_sync, str(payload.get("id", "")), bool(payload.get("enabled")))
    except StartupError as exc:
        return _err(exc)


@module.on("delete")
async def delete(payload: dict):
    try:
        return await asyncio.to_thread(_delete_sync, str(payload.get("id", "")))
    except StartupError as exc:
        return _err(exc)


@module.on("add")
async def add(payload: dict):
    try:
        return await asyncio.to_thread(_add_sync, str(payload.get("name") or ""), str(payload.get("command") or ""),
                                       bool(payload.get("overwrite")))
    except StartupError as exc:
        return _err(exc)


@module.on("reveal")
async def reveal(payload: dict):
    path = os.path.expandvars(str(payload.get("path") or "")).strip().strip('"')
    if not path or not os.path.exists(path):
        return {"error": "Fichier introuvable", "code": "NOT_FOUND"}
    args = ["explorer.exe", f"/select,{os.path.normpath(path)}"] if os.path.isfile(path) else ["explorer.exe", path]
    await asyncio.to_thread(subprocess.Popen, args, creationflags=subprocess.CREATE_NO_WINDOW)
    return {"ok": True}


@module.on("open_folder")
async def open_folder(payload: dict):
    src = next((s for s in SOURCES if s["key"] == payload.get("source") and s["kind"] == "folder"), None)
    if not src or not os.path.isdir(src["path"]):
        return {"error": "Dossier introuvable", "code": "NOT_FOUND"}
    await asyncio.to_thread(subprocess.Popen, ["explorer.exe", src["path"]], creationflags=subprocess.CREATE_NO_WINDOW)
    return {"ok": True}
