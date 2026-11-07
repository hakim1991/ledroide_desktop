"""UAC elevation helper (wifi-tools). Runs a PowerShell script elevated and returns (exit_code, output)."""
import base64
import ctypes
import os
import tempfile
import time
import uuid
from ctypes import wintypes
from pathlib import Path

SEE_MASK_NOCLOSEPROCESS = 0x00000040
SW_HIDE = 0
ERROR_CANCELLED = 1223
INFINITE = 0xFFFFFFFF


class ElevationCancelled(Exception):
    pass


class SHELLEXECUTEINFOW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD), ("fMask", ctypes.c_ulong), ("hwnd", wintypes.HWND),
        ("lpVerb", wintypes.LPCWSTR), ("lpFile", wintypes.LPCWSTR), ("lpParameters", wintypes.LPCWSTR),
        ("lpDirectory", wintypes.LPCWSTR), ("nShow", ctypes.c_int), ("hInstApp", wintypes.HINSTANCE),
        ("lpIDList", ctypes.c_void_p), ("lpClass", wintypes.LPCWSTR), ("hkeyClass", wintypes.HKEY),
        ("dwHotKey", wintypes.DWORD), ("hIconOrMonitor", wintypes.HANDLE), ("hProcess", wintypes.HANDLE),
    ]


def is_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def build_command(script: str, out_file: Path) -> str:
    """Build the powershell.exe parameter string. Output of the script is redirected to out_file (UTF-8)."""
    of = str(out_file).replace("'", "''")
    wrapped = (
        "$ErrorActionPreference='Continue';"
        f"$o = & {{ {script} }} *>&1 | Out-String;"
        f"[IO.File]::WriteAllText('{of}', $o, [Text.Encoding]::UTF8);"
        "exit $LASTEXITCODE"
    )
    enc = base64.b64encode(wrapped.encode("utf-16-le")).decode("ascii")
    return f"-NoProfile -NonInteractive -ExecutionPolicy Bypass -WindowStyle Hidden -EncodedCommand {enc}"


def _shell_execute(params: str, timeout_s: int) -> int:
    sei = SHELLEXECUTEINFOW()
    sei.cbSize = ctypes.sizeof(sei)
    sei.fMask = SEE_MASK_NOCLOSEPROCESS
    sei.lpVerb = "runas"
    sei.lpFile = "powershell.exe"
    sei.lpParameters = params
    sei.nShow = SW_HIDE
    if not ctypes.windll.shell32.ShellExecuteExW(ctypes.byref(sei)):
        err = ctypes.GetLastError()
        if err == ERROR_CANCELLED:
            raise ElevationCancelled()
        raise OSError(err, f"ShellExecuteExW a échoué (code {err})")
    try:
        ctypes.windll.kernel32.WaitForSingleObject(sei.hProcess, int(timeout_s * 1000))
        code = wintypes.DWORD()
        ctypes.windll.kernel32.GetExitCodeProcess(sei.hProcess, ctypes.byref(code))
        return int(code.value)
    finally:
        ctypes.windll.kernel32.CloseHandle(sei.hProcess)


def run_elevated(script: str, timeout_s: int = 60):
    """Run `script` in an elevated hidden PowerShell. Returns (exit_code, output_text). Raises ElevationCancelled."""
    out = Path(tempfile.gettempdir()) / f"ledroide_wifi_{uuid.uuid4().hex}.txt"
    try:
        code = _shell_execute(build_command(script, out), timeout_s)
        text = ""
        for _ in range(20):
            if out.exists():
                text = out.read_text(encoding="utf-8-sig", errors="replace")
                break
            time.sleep(0.05)
        return code, text
    finally:
        try:
            os.remove(out)
        except OSError:
            pass
