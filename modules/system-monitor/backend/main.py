"""Moniteur système — statistiques psutil et gestion des processus."""
import asyncio
import os
import platform
import socket
import threading
import time
from typing import Optional

from ledroide_sdk.http import JSONResponse
from pydantic import BaseModel
from ledroide_sdk import Module

module = Module(__file__)
router = module.router

_lock = threading.Lock()
_net_last = None          # (t, sent, recv)
_disk_io_last = None      # (t, read, write)
PROTECTED_NAMES = {"system", "system idle process", "registry", "smss.exe", "csrss.exe", "wininit.exe",
                   "services.exe", "lsass.exe", "winlogon.exe", "memory compression"}


def _missing_dep():
    return JSONResponse(status_code=400, content={
        "error": "Le paquet psutil n'est pas installé.", "code": "MISSING_DEP"})


def _err(msg: str, status: int = 400):
    return JSONResponse(status_code=status, content={"error": msg})


def _psutil():
    try:
        import psutil
        return psutil
    except ImportError:
        return None


_cpu_last = None          # (total_times, [per_core_times])


def _busy_pct(prev, cur) -> float:
    """% d'occupation entre deux échantillons cpu_times (calcul identique à psutil)."""
    t1, t2 = sum(prev), sum(cur)
    idle1 = getattr(prev, "idle", 0) + getattr(prev, "iowait", 0)
    idle2 = getattr(cur, "idle", 0) + getattr(cur, "iowait", 0)
    dt = t2 - t1
    if dt <= 0:
        return 0.0
    busy = (t2 - idle2) - (t1 - idle1)
    return round(max(0.0, min(100.0, busy / dt * 100)), 1)


def _cpu_sample(psutil):
    """psutil.cpu_percent(None) garde un état par thread (psutil ≥ 6) : on calcule
    nous-mêmes le delta pour obtenir des valeurs cohérentes depuis le pool de threads."""
    global _cpu_last
    cur = (psutil.cpu_times(), psutil.cpu_times(percpu=True))
    if _cpu_last is None:
        _cpu_last = cur
        time.sleep(0.25)
        cur = (psutil.cpu_times(), psutil.cpu_times(percpu=True))
    prev, _cpu_last = _cpu_last, cur
    total = _busy_pct(prev[0], cur[0])
    per = [_busy_pct(a, b) for a, b in zip(prev[1], cur[1])]
    return total, per


def _overview_sync(psutil) -> dict:
    global _net_last, _disk_io_last
    with _lock:
        cpu_total, cpu_per = _cpu_sample(psutil)
        now = time.time()

        net = psutil.net_io_counters()
        up = down = 0.0
        if _net_last and now > _net_last[0]:
            dt = now - _net_last[0]
            up = max(0.0, (net.bytes_sent - _net_last[1]) / dt)
            down = max(0.0, (net.bytes_recv - _net_last[2]) / dt)
        _net_last = (now, net.bytes_sent, net.bytes_recv)

        dread = dwrite = 0.0
        try:
            dio = psutil.disk_io_counters()
        except Exception:
            dio = None
        if dio:
            if _disk_io_last and now > _disk_io_last[0]:
                dt = now - _disk_io_last[0]
                dread = max(0.0, (dio.read_bytes - _disk_io_last[1]) / dt)
                dwrite = max(0.0, (dio.write_bytes - _disk_io_last[2]) / dt)
            _disk_io_last = (now, dio.read_bytes, dio.write_bytes)

    try:
        freq = psutil.cpu_freq()
        freq_d = {"current": round(freq.current), "max": round(freq.max)} if freq else None
    except Exception:
        freq_d = None

    vm = psutil.virtual_memory()
    sw = psutil.swap_memory()

    disks = []
    for p in psutil.disk_partitions(all=False):
        if "cdrom" in p.opts or not p.fstype:
            continue
        try:
            u = psutil.disk_usage(p.mountpoint)
        except (PermissionError, OSError):
            continue
        disks.append({"device": p.device, "mountpoint": p.mountpoint, "fstype": p.fstype,
                      "total": u.total, "used": u.used, "free": u.free, "percent": u.percent})

    battery = None
    try:
        b = psutil.sensors_battery()
        if b is not None:
            secs = b.secsleft
            if secs in (psutil.POWER_TIME_UNLIMITED, psutil.POWER_TIME_UNKNOWN) or secs is None or secs < 0:
                secs = None
            battery = {"percent": round(b.percent, 1), "plugged": bool(b.power_plugged), "secsleft": secs}
    except Exception:
        battery = None

    boot = psutil.boot_time()
    try:
        nproc = len(psutil.pids())
    except Exception:
        nproc = None

    return {
        "time": now,
        "cpu": {"percent": cpu_total, "per_core": cpu_per,
                "logical": psutil.cpu_count(logical=True), "physical": psutil.cpu_count(logical=False),
                "freq": freq_d},
        "memory": {"total": vm.total, "used": vm.used, "available": vm.available, "percent": vm.percent},
        "swap": {"total": sw.total, "used": sw.used, "percent": sw.percent},
        "disks": disks,
        "disk_io": {"read": dread, "write": dwrite},
        "network": {"up": up, "down": down, "sent_total": net.bytes_sent, "recv_total": net.bytes_recv},
        "battery": battery,
        "uptime": now - boot,
        "boot_time": boot,
        "process_count": nproc,
        "os": {
            "system": platform.system(), "release": platform.release(), "version": platform.version(),
            "machine": platform.machine(), "hostname": socket.gethostname(),
            "processor": platform.processor() or platform.machine(),
            "python": platform.python_version(),
        },
    }


@router.get("/overview")
async def overview():
    psutil = _psutil()
    if psutil is None:
        return _missing_dep()
    return await asyncio.to_thread(_overview_sync, psutil)


def _processes_sync(psutil, sort: str, q: str, limit: int) -> dict:
    ncpu = psutil.cpu_count(logical=True) or 1
    q = q.lower().strip()
    out = []
    attrs = ["pid", "name", "username", "memory_info", "memory_percent", "status", "num_threads", "create_time"]
    # process_iter réutilise les instances Process mises en cache → cpu_percent(None) est cohérent entre appels
    for p in psutil.process_iter(attrs):
        info = p.info
        if info["pid"] == 0:  # « System Idle Process » : pas un vrai processus
            continue
        name = info.get("name") or ""
        if q and q not in name.lower() and q != str(info["pid"]):
            # recherche aussi sur l'utilisateur
            if not (info.get("username") and q in info["username"].lower()):
                continue
        try:
            cpu = p.cpu_percent(None) / ncpu
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            cpu = 0.0
        mi = info.get("memory_info")
        out.append({
            "pid": info["pid"], "name": name, "username": info.get("username") or "",
            "cpu": round(cpu, 1), "memory": mi.rss if mi else 0,
            "memory_percent": round(info.get("memory_percent") or 0.0, 2),
            "status": info.get("status") or "", "threads": info.get("num_threads") or 0,
            "create_time": info.get("create_time"),
        })
    keys = {
        "cpu": (lambda x: (x["cpu"], x["memory"]), True),
        "memory": (lambda x: x["memory"], True),
        "name": (lambda x: x["name"].lower(), False),
        "pid": (lambda x: x["pid"], False),
    }
    k, rev = keys.get(sort, keys["cpu"])
    out.sort(key=k, reverse=rev)
    return {"processes": out[:limit], "total": len(out)}


@router.get("/processes")
async def processes(sort: str = "cpu", q: str = "", limit: int = 100, desc: Optional[bool] = None):
    psutil = _psutil()
    if psutil is None:
        return _missing_dep()
    limit = max(1, min(limit, 2000))
    res = await asyncio.to_thread(_processes_sync, psutil, sort, q, 100000)
    default_desc = sort in ("cpu", "memory")
    if desc is not None and desc != default_desc:
        res["processes"].reverse()
    res["processes"] = res["processes"][:limit]
    return res


class KillIn(BaseModel):
    force: bool = False


@router.post("/processes/{pid}/kill")
async def kill_process(pid: int, payload: KillIn = KillIn()):
    psutil = _psutil()
    if psutil is None:
        return _missing_dep()

    def _k():
        if pid in (0, 4) or pid == os.getpid() or pid == os.getppid():
            return 400, "Ce processus est protégé et ne peut pas être terminé."
        try:
            p = psutil.Process(pid)
            name = p.name()
            if name.lower() in PROTECTED_NAMES:
                return 400, f"« {name} » est un processus système critique et ne peut pas être terminé."
            p.terminate()
            try:
                p.wait(timeout=3)
            except psutil.TimeoutExpired:
                if payload.force:
                    p.kill()
                    p.wait(timeout=3)
                else:
                    return 409, f"« {name} » ne s'est pas arrêté dans les temps. Réessayez en forçant."
            return 200, name
        except psutil.NoSuchProcess:
            return 404, "Processus introuvable (déjà terminé ?)"
        except psutil.AccessDenied:
            return 403, "Accès refusé : des droits administrateur sont nécessaires."
        except psutil.TimeoutExpired:
            return 409, "Le processus ne répond pas."

    code, msg = await asyncio.to_thread(_k)
    if code != 200:
        return _err(msg, code)
    return {"ok": True, "name": msg}


@router.get("/health")
async def health():
    return {"ok": True, "psutil": _psutil() is not None}
