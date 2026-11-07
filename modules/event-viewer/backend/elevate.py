"""Exécution de scripts PowerShell, éventuellement élevés (UAC).

Module autonome copié dans chaque module LedroideDesktop qui en a besoin
(chargé via importlib sous un nom unique pour éviter les collisions).

Principe : le script est enveloppé dans un fichier .ps1 temporaire qui écrit
sa sortie (texte) dans un fichier temporaire, puis il est lancé :
  - directement (subprocess) si l'on est déjà administrateur ou si l'élévation
    n'est pas demandée ;
  - via ShellExecuteExW(verb="runas") sinon (invite UAC), en attendant la fin
    du processus pour récupérer son code de sortie.
"""
import ctypes
import os
import subprocess
import tempfile
import uuid
from ctypes import wintypes
from pathlib import Path

ERROR_CANCELLED = 1223
SEE_MASK_NOCLOSEPROCESS = 0x00000040
SEE_MASK_NOASYNC = 0x00000100
SEE_MASK_FLAG_NO_UI = 0x00000400
SW_HIDE = 0
INFINITE = 0xFFFFFFFF
WAIT_TIMEOUT = 0x00000102

TEMP_DIR = Path(tempfile.gettempdir()) / "ledroide_elevate"


def is_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def ps_quote(value: str) -> str:
    """Chaîne PowerShell entre apostrophes (les apostrophes sont doublées)."""
    return "'" + str(value).replace("'", "''") + "'"


class SHELLEXECUTEINFOW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("fMask", wintypes.ULONG),
        ("hwnd", wintypes.HWND),
        ("lpVerb", wintypes.LPCWSTR),
        ("lpFile", wintypes.LPCWSTR),
        ("lpParameters", wintypes.LPCWSTR),
        ("lpDirectory", wintypes.LPCWSTR),
        ("nShow", ctypes.c_int),
        ("hInstApp", wintypes.HINSTANCE),
        ("lpIDList", ctypes.c_void_p),
        ("lpClass", wintypes.LPCWSTR),
        ("hkeyClass", wintypes.HKEY),
        ("dwHotKey", wintypes.DWORD),
        ("hIconOrMonitor", wintypes.HANDLE),
        ("hProcess", wintypes.HANDLE),
    ]


def _wrap_script(script: str, out_path: Path) -> str:
    """Enveloppe : toute la sortie (y compris erreurs) va dans out_path, code 0/1."""
    return (
        "$ErrorActionPreference = 'Stop'\r\n"
        "$ProgressPreference = 'SilentlyContinue'\r\n"
        f"$__out = {ps_quote(str(out_path))}\r\n"
        "try {\r\n"
        "  $__r = & {\r\n"
        f"{script}\r\n"
        "  } 2>&1 | ForEach-Object { if ($_ -is [System.Management.Automation.ErrorRecord]) { 'ERREUR: ' + $_.ToString() } else { [string]$_ } }\r\n"
        "  [IO.File]::WriteAllText($__out, (@($__r) -join \"`n\"), (New-Object Text.UTF8Encoding($false)))\r\n"
        "  exit 0\r\n"
        "} catch {\r\n"
        "  $__m = $_.Exception.Message\r\n"
        "  if ($_.FullyQualifiedErrorId -and $_.FullyQualifiedErrorId -ne $__m) { $__m = $__m + \"`n[\" + $_.FullyQualifiedErrorId + \"]\" }\r\n"
        "  [IO.File]::WriteAllText($__out, $__m, (New-Object Text.UTF8Encoding($false)))\r\n"
        "  exit 1\r\n"
        "}\r\n"
    )


def build_command(script: str):
    """Crée les fichiers temporaires ; renvoie (ps1_path, out_path, params)."""
    TEMP_DIR.mkdir(parents=True, exist_ok=True)
    token = uuid.uuid4().hex
    ps1 = TEMP_DIR / f"run_{token}.ps1"
    out = TEMP_DIR / f"out_{token}.txt"
    # BOM UTF-8 : indispensable pour que Windows PowerShell 5.1 lise les accents.
    ps1.write_text(_wrap_script(script, out), encoding="utf-8-sig")
    params = f'-NoProfile -NonInteractive -ExecutionPolicy Bypass -WindowStyle Hidden -File "{ps1}"'
    return ps1, out, params


def _shell_execute_runas(params: str, timeout: float):
    """Lance powershell.exe élevé. Renvoie (exit_code | None, erreur_win32 | 0)."""
    sei = SHELLEXECUTEINFOW()
    sei.cbSize = ctypes.sizeof(sei)
    sei.fMask = SEE_MASK_NOCLOSEPROCESS | SEE_MASK_NOASYNC
    sei.hwnd = None
    sei.lpVerb = "runas"
    sei.lpFile = "powershell.exe"
    sei.lpParameters = params
    sei.lpDirectory = None
    sei.nShow = SW_HIDE
    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    shell32.ShellExecuteExW.argtypes = [ctypes.POINTER(SHELLEXECUTEINFOW)]
    shell32.ShellExecuteExW.restype = wintypes.BOOL
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    if not shell32.ShellExecuteExW(ctypes.byref(sei)):
        return None, ctypes.get_last_error() or ERROR_CANCELLED
    if not sei.hProcess:
        return None, 0
    try:
        ms = INFINITE if timeout is None else int(timeout * 1000)
        if kernel32.WaitForSingleObject(sei.hProcess, ms) == WAIT_TIMEOUT:
            return None, WAIT_TIMEOUT
        code = wintypes.DWORD()
        kernel32.GetExitCodeProcess(sei.hProcess, ctypes.byref(code))
        return int(code.value), 0
    finally:
        kernel32.CloseHandle(sei.hProcess)


def _run_direct(params_list, timeout: float) -> int:
    r = subprocess.run(params_list, capture_output=True, timeout=timeout,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return r.returncode


def run_powershell(script: str, elevate: bool = False, timeout: float = 120) -> dict:
    """Exécute `script`. elevate=True → invite UAC si l'on n'est pas déjà admin.

    Renvoie {"ok", "code", "output", "cancelled", "elevated", "timeout"}.
    Le script ne doit pas appeler `exit` ; il produit des chaînes en sortie.
    """
    ps1, out, params = build_command(script)
    res = {"ok": False, "code": None, "output": "", "cancelled": False,
           "elevated": False, "timeout": False}
    try:
        if elevate and not is_admin():
            res["elevated"] = True
            code, err = _shell_execute_runas(params, timeout)
            if code is None:
                if err == ERROR_CANCELLED:
                    res["cancelled"] = True
                    res["output"] = "Élévation refusée (invite UAC annulée)."
                elif err == WAIT_TIMEOUT:
                    res["timeout"] = True
                    res["output"] = "Délai dépassé pendant l'exécution élevée."
                else:
                    res["output"] = f"Impossible de lancer PowerShell en administrateur (erreur {err})."
                return res
        else:
            try:
                code = _run_direct(["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy",
                                    "Bypass", "-File", str(ps1)], timeout)
            except subprocess.TimeoutExpired:
                res["timeout"] = True
                res["output"] = "Délai dépassé."
                return res
        res["code"] = code
        try:
            res["output"] = out.read_text(encoding="utf-8", errors="replace").strip()
        except FileNotFoundError:
            res["output"] = ""
        res["ok"] = code == 0
        return res
    finally:
        for p in (ps1, out):
            try:
                os.remove(p)
            except OSError:
                pass
