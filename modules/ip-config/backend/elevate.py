"""Exécution élevée (UAC) d'un script PowerShell, sortie récupérée via fichier temporaire.

Chargé par main.py via importlib sous le nom unique « ip_config_elevate ».
"""
import base64
import ctypes
import os
import tempfile
import uuid
from ctypes import wintypes
from pathlib import Path

SEE_MASK_NOCLOSEPROCESS = 0x00000040
SEE_MASK_NOASYNC = 0x00000100
SW_HIDE = 0
INFINITE = 0xFFFFFFFF
WAIT_TIMEOUT = 0x00000102
ERROR_CANCELLED = 1223

TMP_DIR = Path(tempfile.gettempdir()) / "ledroide_ip-config"


class ElevationCancelled(Exception):
    pass


class ElevationError(Exception):
    pass


def is_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


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


def build_command(script: str, out_path: Path) -> tuple[str, str]:
    """Construit (exécutable, paramètres) pour lancer `script` élevé.

    La sortie (texte) du script est écrite en UTF-8 dans out_path ; le code de sortie
    du processus est celui de la dernière commande native ($LASTEXITCODE).
    """
    out = str(out_path).replace("'", "''")
    wrapper = (
        "$ErrorActionPreference='Continue';"
        "$global:LASTEXITCODE=0;"
        f"$r = & {{ {script} }} 2>&1 | Out-String;"
        f"[IO.File]::WriteAllText('{out}', $r, [Text.Encoding]::UTF8);"
        "exit $LASTEXITCODE"
    )
    encoded = base64.b64encode(wrapper.encode("utf-16-le")).decode("ascii")
    exe = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"),
                       "System32", "WindowsPowerShell", "v1.0", "powershell.exe")
    params = f"-NoProfile -NonInteractive -ExecutionPolicy Bypass -WindowStyle Hidden -EncodedCommand {encoded}"
    return exe, params


def _shell_execute(exe: str, params: str, timeout: float) -> int:
    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    shell32.ShellExecuteExW.argtypes = [ctypes.POINTER(SHELLEXECUTEINFOW)]
    shell32.ShellExecuteExW.restype = wintypes.BOOL
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

    info = SHELLEXECUTEINFOW()
    info.cbSize = ctypes.sizeof(info)
    info.fMask = SEE_MASK_NOCLOSEPROCESS | SEE_MASK_NOASYNC
    info.hwnd = None
    info.lpVerb = "runas"
    info.lpFile = exe
    info.lpParameters = params
    info.lpDirectory = None
    info.nShow = SW_HIDE
    if not shell32.ShellExecuteExW(ctypes.byref(info)):
        err = ctypes.get_last_error()
        if err == ERROR_CANCELLED:
            raise ElevationCancelled("Élévation refusée par l'utilisateur (UAC annulé).")
        raise ElevationError(f"Échec de ShellExecuteEx (code {err}).")
    if not info.hProcess:
        raise ElevationError("Processus élevé introuvable.")
    try:
        ms = INFINITE if timeout is None else int(timeout * 1000)
        if kernel32.WaitForSingleObject(info.hProcess, ms) == WAIT_TIMEOUT:
            raise ElevationError("Délai dépassé pour la commande élevée.")
        code = wintypes.DWORD(0)
        kernel32.GetExitCodeProcess(info.hProcess, ctypes.byref(code))
        return int(code.value)
    finally:
        kernel32.CloseHandle(info.hProcess)


def run_elevated(script: str, timeout: float = 120) -> dict:
    """Exécute un script PowerShell avec UAC. Retourne {exit_code, output}."""
    TMP_DIR.mkdir(parents=True, exist_ok=True)
    out_path = TMP_DIR / f"elev_{uuid.uuid4().hex}.txt"
    exe, params = build_command(script, out_path)
    try:
        code = _shell_execute(exe, params, timeout)
        output = ""
        if out_path.exists():
            output = out_path.read_text(encoding="utf-8-sig", errors="replace")
        return {"exit_code": code, "output": output}
    finally:
        try:
            out_path.unlink()
        except OSError:
            pass
