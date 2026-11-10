"""Gestionnaire de processus — psutil + instantané Toolhelp32 (ppid/threads en un seul appel)."""
import asyncio
import ctypes
import logging
import os
import subprocess
import threading
import time
from ctypes import wintypes

from ledroide_sdk import Module

log = logging.getLogger("ledroide.process-manager")

module = Module(__file__)
router = module.router

PROTECTED_NAMES = {
    "system", "system idle process", "registry", "smss.exe", "csrss.exe", "wininit.exe",
    "services.exe", "lsass.exe", "winlogon.exe", "memory compression", "lsaiso.exe",
    "secure system", "fontdrvhost.exe", "dwm.exe",
}
PRIORITIES = {
    "idle": ("IDLE_PRIORITY_CLASS", "Basse"),
    "below_normal": ("BELOW_NORMAL_PRIORITY_CLASS", "Inférieure à la normale"),
    "normal": ("NORMAL_PRIORITY_CLASS", "Normale"),
    "above_normal": ("ABOVE_NORMAL_PRIORITY_CLASS", "Supérieure à la normale"),
    "high": ("HIGH_PRIORITY_CLASS", "Haute"),
    "realtime": ("REALTIME_PRIORITY_CLASS", "Temps réel"),
}
SETTABLE_PRIORITIES = ("idle", "below_normal", "normal", "above_normal", "high")
MAX_ITEMS = 2000


class ProcError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _psutil():
    try:
        import psutil
        return psutil
    except ImportError:
        raise ProcError("MISSING_DEP", "Le paquet psutil n'est pas installé : réactivez le module pour installer ses dépendances.")


async def _guard(fn, *args):
    try:
        return await asyncio.to_thread(fn, *args)
    except ProcError as exc:
        return {"error": exc.message, "code": exc.code}


# ── Toolhelp32 snapshot: pid, ppid, threads, name for every process in one call ──

class _PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD), ("th32ProcessID", wintypes.DWORD),
                ("th32DefaultHeapID", ctypes.c_size_t), ("th32ModuleID", wintypes.DWORD),
                ("cntThreads", wintypes.DWORD), ("th32ParentProcessID", wintypes.DWORD),
                ("pcPriClassBase", wintypes.LONG), ("dwFlags", wintypes.DWORD), ("szExeFile", wintypes.WCHAR * 260)]


_k32 = ctypes.WinDLL("kernel32", use_last_error=True)
_k32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
_k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
_k32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_PROCESSENTRY32W)]
_k32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_PROCESSENTRY32W)]
_k32.CloseHandle.argtypes = [wintypes.HANDLE]
_INVALID = wintypes.HANDLE(-1).value


def _snapshot() -> dict[int, tuple[int, int, str]]:
    h = _k32.CreateToolhelp32Snapshot(0x2, 0)
    if not h or h == _INVALID:
        raise OSError(ctypes.get_last_error(), "CreateToolhelp32Snapshot failed")
    out = {}
    try:
        e = _PROCESSENTRY32W()
        e.dwSize = ctypes.sizeof(e)
        ok = _k32.Process32FirstW(h, ctypes.byref(e))
        while ok:
            out[e.th32ProcessID] = (e.th32ParentProcessID, e.cntThreads, e.szExeFile)
            ok = _k32.Process32NextW(h, ctypes.byref(e))
    finally:
        _k32.CloseHandle(h)
    return out


# ── Process cache: static attributes are read once per (pid, create_time) ─────

_lock = threading.Lock()
_procs: dict[int, tuple] = {}       # pid -> (psutil.Process, static dict)
_protect_cache: tuple[float, dict] = (0.0, {})


def _static(psutil, p) -> dict:
    out = {"create_time": None, "exe": "", "cmdline": [], "username": ""}
    for key, fn in (("create_time", p.create_time), ("exe", p.exe), ("cmdline", p.cmdline), ("username", p.username)):
        try:
            out[key] = fn()
        except (psutil.AccessDenied, psutil.ZombieProcess, OSError):
            pass
        except psutil.NoSuchProcess:
            raise
    return out


def _priority_key(psutil, value) -> str:
    for key, (const, _) in PRIORITIES.items():
        if getattr(psutil, const, None) == value:
            return key
    return ""


def _protected(psutil) -> dict[int, str]:
    """Backend, its ancestors (Electron…) and Electron helper processes (same exe as an ancestor)."""
    global _protect_cache
    now = time.monotonic()
    if now - _protect_cache[0] < 10:
        return _protect_cache[1]
    prot = {os.getpid(): "le backend de LedroideDesktop"}
    try:
        me = psutil.Process(os.getpid())
        ancestors = me.parents()
        for a in ancestors:
            prot[a.pid] = "un processus parent de LedroideDesktop"
        exes = set()
        for a in ancestors:
            try:
                exes.add(a.exe().lower())
            except (psutil.Error, OSError):
                pass
        for a in ancestors:
            try:
                for c in a.children():
                    try:
                        if c.exe().lower() in exes:
                            prot.setdefault(c.pid, "un processus d'Electron / LedroideDesktop")
                    except (psutil.Error, OSError):
                        pass
            except psutil.Error:
                pass
    except psutil.Error:
        prot[os.getppid()] = "un processus parent de LedroideDesktop"
    _protect_cache = (now, prot)
    return prot


def _list_sync() -> dict:
    psutil = _psutil()
    snap = _snapshot()
    ncpu = psutil.cpu_count() or 1
    prot = _protected(psutil)
    items = []
    with _lock:
        for pid in list(_procs):
            if pid not in snap:
                _procs.pop(pid, None)
        for pid, (ppid, threads, name) in snap.items():
            entry = _procs.get(pid)
            try:
                if entry is None:
                    p = psutil.Process(pid)
                    static = _static(psutil, p)
                    p.cpu_percent(None)
                    entry = _procs[pid] = (p, static)
                p, static = entry
                cpu = mem = prio = None
                try:
                    cpu = round(p.cpu_percent(None) / ncpu, 1)
                except (psutil.AccessDenied, OSError):
                    pass
                try:
                    mem = p.memory_info().rss
                except (psutil.AccessDenied, OSError):
                    pass
                try:
                    prio = _priority_key(psutil, p.nice())
                except (psutil.AccessDenied, OSError):
                    pass
            except psutil.NoSuchProcess:
                _procs.pop(pid, None)
                continue
            items.append({
                "pid": pid, "ppid": ppid if ppid in snap and ppid != pid else None, "name": name,
                "threads": threads, "cpu": cpu, "mem": mem, "priority": prio,
                "user": static["username"], "exe": static["exe"],
                "cmdline": " ".join(static["cmdline"] or []), "started": static["create_time"],
                "protected": prot.get(pid) or ("processus système critique" if name.lower() in PROTECTED_NAMES or pid in (0, 4) else None),
            })
            if len(items) >= MAX_ITEMS:
                break
    vm = psutil.virtual_memory()
    return {
        "items": items, "self_pid": os.getpid(), "cpu_count": ncpu,
        "system": {"cpu": psutil.cpu_percent(None), "mem_total": vm.total, "mem_used": vm.total - vm.available,
                   "mem_percent": vm.percent, "count": len(snap)},
    }


@module.on("list")
async def list_processes(payload: dict):
    return await _guard(_list_sync)


# ── Details ──────────────────────────────────────────────────────────────────

def _fmt_addr(a) -> str:
    if not a:
        return ""
    ip, port = a[0], a[1]
    return f"[{ip}]:{port}" if ":" in ip else f"{ip}:{port}"


def _conns(psutil, p) -> list:
    getter = getattr(p, "net_connections", None) or p.connections
    out = []
    for c in getter(kind="inet"):
        out.append({"proto": "TCP" if c.type == 1 else "UDP", "local": _fmt_addr(c.laddr), "remote": _fmt_addr(c.raddr),
                    "status": c.status if c.status != "NONE" else ""})
    return out


def _get_process(psutil, pid):
    try:
        return psutil.Process(int(pid))
    except (TypeError, ValueError):
        raise ProcError("BAD_PID", "PID invalide")
    except psutil.NoSuchProcess:
        raise ProcError("NOT_FOUND", "Processus introuvable (déjà terminé ?)")


def _section(psutil, fn):
    try:
        return {"items": fn(), "denied": False}
    except psutil.AccessDenied:
        return {"items": [], "denied": True}
    except psutil.NoSuchProcess:
        raise ProcError("NOT_FOUND", "Le processus s'est terminé")
    except OSError as exc:
        return {"items": [], "denied": False, "error": str(exc)}


def _details_sync(pid, sections: list) -> dict:
    psutil = _psutil()
    p = _get_process(psutil, pid)
    out: dict = {"pid": p.pid}

    def safe(fn, default=None):
        try:
            return fn()
        except (psutil.AccessDenied, psutil.ZombieProcess, OSError):
            return default

    try:
        with p.oneshot():
            out.update({
                "name": safe(p.name, ""), "exe": safe(p.exe, ""), "cwd": safe(p.cwd, ""),
                "cmdline": safe(p.cmdline, []) or [], "user": safe(p.username, ""),
                "started": safe(p.create_time), "ppid": safe(p.ppid),
                "priority": _priority_key(psutil, safe(p.nice)),
                "threads": safe(p.num_threads), "handles": safe(p.num_handles),
            })
            mi = safe(p.memory_info)
            out["memory"] = {"rss": mi.rss, "vms": mi.vms, "private": getattr(mi, "private", None),
                             "peak": getattr(mi, "peak_wset", None)} if mi else None
            io = safe(p.io_counters)
            out["io"] = {"read": io.read_bytes, "write": io.write_bytes} if io else None
            ct = safe(p.cpu_times)
            out["cpu_time"] = (ct.user + ct.system) if ct else None
    except psutil.NoSuchProcess:
        raise ProcError("NOT_FOUND", "Le processus s'est terminé")
    out["parents"] = [{"pid": a.pid, "name": safe(a.name, "?")} for a in (safe(p.parents, []) or [])]
    out["children"] = [{"pid": c.pid, "name": safe(c.name, "?")} for c in (safe(p.children, []) or [])]
    out["protected"] = _protected(psutil).get(p.pid)
    if "connections" in sections:
        out["connections"] = _section(psutil, lambda: _conns(psutil, p))
    if "files" in sections:
        out["files"] = _section(psutil, lambda: sorted({f.path for f in p.open_files()}))
    if "modules" in sections:
        def mods():
            rows = [{"path": m.path, "rss": m.rss} for m in p.memory_maps(grouped=True)]
            rows.sort(key=lambda r: r["path"].lower())
            return rows[:2000]
        out["modules"] = _section(psutil, mods)
    return out


@module.on("details")
async def details(payload: dict):
    sections = payload.get("sections") or ["connections"]
    return await _guard(_details_sync, payload.get("pid"), list(sections))


# ── Port lookup ──────────────────────────────────────────────────────────────

def _port_sync(port, proto: str) -> dict:
    psutil = _psutil()
    try:
        port = int(port)
    except (TypeError, ValueError):
        raise ProcError("BAD_PORT", "Port invalide")
    if not 0 < port < 65536:
        raise ProcError("BAD_PORT", "Le port doit être compris entre 1 et 65535")
    try:
        conns = psutil.net_connections(kind="inet")
    except psutil.AccessDenied:
        raise ProcError("NEED_ADMIN", "Accès refusé à la table des connexions")
    rows, names = [], {}
    for c in conns:
        is_tcp = c.type == 1
        if proto == "tcp" and not is_tcp or proto == "udp" and is_tcp:
            continue
        local = c.laddr and c.laddr[1] == port
        remote = c.raddr and c.raddr[1] == port
        if not (local or remote):
            continue
        pid = c.pid
        if pid not in names:
            try:
                pp = psutil.Process(pid) if pid is not None else None
                names[pid] = (pp.name(), _safe_exe(psutil, pp)) if pp else ("?", "")
            except psutil.Error:
                names[pid] = ("?", "")
        rows.append({
            "pid": pid, "name": names[pid][0], "exe": names[pid][1], "proto": "TCP" if is_tcp else "UDP",
            "local": _fmt_addr(c.laddr), "remote": _fmt_addr(c.raddr), "status": c.status if c.status != "NONE" else "",
            "role": "local" if local else "distant",
        })
    rows.sort(key=lambda r: (r["role"] != "local", r["status"] != "LISTEN", r["pid"] or 0))
    return {"port": port, "items": rows}


def _safe_exe(psutil, p) -> str:
    try:
        return p.exe()
    except (psutil.Error, OSError):
        return ""


@module.on("port_lookup")
async def port_lookup(payload: dict):
    proto = str(payload.get("proto") or "all").lower()
    return await _guard(_port_sync, payload.get("port"), proto)


# ── Terminate / priority ────────────────────────────────────────────────────

def _refusal(psutil, pid: int, name: str) -> str | None:
    if pid in (0, 4) or name.lower() in PROTECTED_NAMES:
        return f"« {name or pid} » est un processus système critique : il ne peut pas être terminé."
    reason = _protected(psutil).get(pid)
    if reason:
        return f"Refusé : le PID {pid} est {reason}. Le terminer fermerait l'application."
    return None


def _kill_sync(pid, expected_name: str, tree: bool, force: bool) -> dict:
    psutil = _psutil()
    p = _get_process(psutil, pid)
    try:
        name = p.name()
    except psutil.NoSuchProcess:
        raise ProcError("NOT_FOUND", "Processus introuvable (déjà terminé ?)")
    except psutil.AccessDenied:
        name = expected_name
    if expected_name and name and expected_name.lower() != name.lower():
        raise ProcError("PID_REUSED", f"Le PID {p.pid} correspond maintenant à « {name} » et non à « {expected_name} ». Actualisez la liste.")
    targets = [p]
    if tree:
        try:
            targets = p.children(recursive=True)[::-1] + [p]
        except psutil.Error:
            pass
    for t in targets:
        try:
            tname = t.name()
        except psutil.Error:
            tname = ""
        reason = _refusal(psutil, t.pid, tname)
        if reason:
            raise ProcError("PROTECTED", reason if t.pid == p.pid else f"Arbre refusé : il contient « {tname} » (PID {t.pid}). {reason}")
    denied, gone = [], []
    for t in targets:
        try:
            t.kill() if force else t.terminate()
        except psutil.NoSuchProcess:
            gone.append(t.pid)
        except psutil.AccessDenied:
            denied.append(t.pid)
    alive_targets = [t for t in targets if t.pid not in denied and t.pid not in gone]
    _, alive = psutil.wait_procs(alive_targets, timeout=3)
    if alive and not force:
        return {"ok": False, "code": "STILL_ALIVE", "alive": [a.pid for a in alive], "denied": denied,
                "error": "Le processus ne s'est pas arrêté dans les temps. Réessayez en forçant (tuer)."}
    if denied and p.pid in denied:
        raise ProcError("NEED_ADMIN", "Accès refusé : terminer ce processus nécessite les droits administrateur.")
    log.info("terminated pid %s (tree=%s, force=%s)", p.pid, tree, force)
    with _lock:
        for t in targets:
            _procs.pop(t.pid, None)
    return {"ok": True, "name": name, "count": len(targets) - len(denied), "denied": denied,
            "alive": [a.pid for a in alive]}


@module.on("kill")
async def kill(payload: dict):
    r = await _guard(_kill_sync, payload.get("pid"), str(payload.get("name") or ""), bool(payload.get("tree")),
                     bool(payload.get("force")))
    if r.get("ok") is False:
        return {"error": r["error"], "code": r["code"], "alive": r.get("alive", [])}
    return r


def _priority_sync(pid, priority: str) -> dict:
    psutil = _psutil()
    if priority not in SETTABLE_PRIORITIES:
        raise ProcError("BAD_PRIORITY", "Priorité invalide (le temps réel n'est pas proposé : il peut bloquer le système)")
    p = _get_process(psutil, pid)
    try:
        name = p.name()
    except psutil.Error:
        name = ""
    if p.pid in (0, 4) or name.lower() in PROTECTED_NAMES:
        raise ProcError("PROTECTED", f"« {name or p.pid} » est un processus système critique : sa priorité n'est pas modifiable ici.")
    try:
        p.nice(getattr(psutil, PRIORITIES[priority][0]))
    except psutil.AccessDenied:
        raise ProcError("NEED_ADMIN", "Accès refusé : modifier la priorité de ce processus nécessite les droits administrateur.")
    except psutil.NoSuchProcess:
        raise ProcError("NOT_FOUND", "Processus introuvable (déjà terminé ?)")
    return {"ok": True, "priority": _priority_key(psutil, p.nice())}


@module.on("set_priority")
async def set_priority(payload: dict):
    return await _guard(_priority_sync, payload.get("pid"), str(payload.get("priority") or ""))


@module.on("reveal")
async def reveal(payload: dict):
    path = str(payload.get("path") or "")
    if not path or not os.path.isfile(path):
        return {"error": "Fichier introuvable", "code": "NOT_FOUND"}
    await asyncio.to_thread(subprocess.Popen, ["explorer.exe", f"/select,{os.path.normpath(path)}"],
                            creationflags=subprocess.CREATE_NO_WINDOW)
    return {"ok": True}


@module.on("priorities")
async def priorities(payload: dict):
    return {"items": [{"key": k, "label": PRIORITIES[k][1]} for k in SETTABLE_PRIORITIES]}
