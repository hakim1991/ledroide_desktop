"""Connexions réseau — équivalent graphique de netstat (psutil)."""
import asyncio
import csv
import importlib.util
import ipaddress
import os
import re
import socket
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import List, Optional

from ledroide_sdk.http import FileResponse, JSONResponse
from pydantic import BaseModel
from ledroide_sdk import Module

module = Module(__file__)
router = module.router

TEMP_DIR = Path(tempfile.gettempdir()) / "ledroide_netstat"


def _load_elevate():
    name = "netstat_elevate"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, Path(__file__).parent / "elevate.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


elevate = _load_elevate()

# Processus jamais terminables depuis ce module (même liste que system-monitor + quelques ajouts).
PROTECTED_NAMES = {
    "system", "system idle process", "registry", "smss.exe", "csrss.exe", "wininit.exe",
    "services.exe", "lsass.exe", "winlogon.exe", "memory compression", "svchost.exe",
    "lsaiso.exe", "dwm.exe", "fontdrvhost.exe", "secure system", "spoolsv.exe",
    "msmpeng.exe", "nissrv.exe", "securityhealthservice.exe", "sihost.exe", "explorer.exe",
}

WELL_KNOWN = {
    20: "FTP-data", 21: "FTP", 22: "SSH", 23: "Telnet", 25: "SMTP", 53: "DNS", 67: "DHCP", 68: "DHCP",
    69: "TFTP", 80: "HTTP", 88: "Kerberos", 110: "POP3", 119: "NNTP", 123: "NTP", 135: "RPC",
    137: "NetBIOS-NS", 138: "NetBIOS-DGM", 139: "NetBIOS", 143: "IMAP", 161: "SNMP", 162: "SNMP-trap",
    389: "LDAP", 443: "HTTPS", 445: "SMB", 465: "SMTPS", 500: "IKE", 514: "Syslog", 515: "LPD",
    546: "DHCPv6", 547: "DHCPv6", 587: "SMTP-sub", 631: "IPP", 636: "LDAPS", 853: "DNS-TLS",
    993: "IMAPS", 995: "POP3S", 1080: "SOCKS", 1194: "OpenVPN", 1433: "MSSQL", 1434: "MSSQL-mon",
    1521: "Oracle", 1701: "L2TP", 1723: "PPTP", 1900: "SSDP", 2049: "NFS", 3074: "Xbox Live",
    3268: "LDAP-GC", 3306: "MySQL", 3389: "RDP", 3478: "STUN", 3702: "WS-Discovery", 4500: "IPsec-NAT",
    5000: "UPnP", 5040: "CDP", 5050: "Yahoo", 5060: "SIP", 5222: "XMPP", 5353: "mDNS",
    5355: "LLMNR", 5357: "WSDAPI", 5432: "PostgreSQL", 5900: "VNC", 5985: "WinRM", 5986: "WinRM-S",
    6379: "Redis", 6881: "BitTorrent", 7680: "WUDO", 8000: "HTTP-alt", 8008: "HTTP-alt",
    8080: "HTTP-proxy", 8443: "HTTPS-alt", 8888: "HTTP-alt", 9000: "HTTP-alt", 9090: "HTTP-alt",
    9100: "Impression", 9200: "Elasticsearch", 11211: "Memcached", 27017: "MongoDB", 27036: "Steam",
}

_proc_cache = {}          # pid -> (create_time, info)
_proc_lock = threading.Lock()
_dns_cache = {}           # ip -> (timestamp, nom | None)
_dns_pending = set()
_dns_lock = threading.Lock()
_dns_pool = ThreadPoolExecutor(max_workers=8, thread_name_prefix="netstat-dns")
DNS_TTL = 3600


def _err(msg: str, status: int = 400, code: Optional[str] = None):
    c = {"error": msg}
    if code:
        c["code"] = code
    return JSONResponse(status_code=status, content=c)


def _missing_dep():
    return _err("Le paquet psutil n'est pas installé. Installez les dépendances du module.", 400, "MISSING_DEP")


def _psutil():
    try:
        import psutil
        return psutil
    except ImportError:
        return None


def _proc_info(psutil, pid: int) -> dict:
    if pid is None:
        return {"name": "", "path": "", "denied": False}
    if pid == 0:
        return {"name": "System Idle Process", "path": "", "denied": False}
    if pid == 4:
        return {"name": "System", "path": "", "denied": False}
    try:
        p = psutil.Process(pid)
        ct = p.create_time()
    except psutil.NoSuchProcess:
        return {"name": "(terminé)", "path": "", "denied": False}
    except psutil.AccessDenied:
        return {"name": "", "path": "", "denied": True}
    with _proc_lock:
        cached = _proc_cache.get(pid)
        if cached and cached[0] == ct:
            return cached[1]
    info = {"name": "", "path": "", "denied": False}
    try:
        info["name"] = p.name()
    except psutil.AccessDenied:
        info["denied"] = True
    except psutil.NoSuchProcess:
        info["name"] = "(terminé)"
    try:
        info["path"] = p.exe() or ""
    except (psutil.AccessDenied, psutil.NoSuchProcess, OSError):
        info["path"] = ""
        if not info["name"]:
            info["denied"] = True
        info["path_denied"] = True
    with _proc_lock:
        _proc_cache[pid] = (ct, info)
    return info


def _connections_sync(psutil) -> dict:
    try:
        conns = psutil.net_connections(kind="inet")
    except psutil.AccessDenied:
        raise PermissionError("Accès refusé à la table des connexions.")
    rows = []
    seen_pids = set()
    for c in conns:
        proto = "TCP" if c.type == socket.SOCK_STREAM else "UDP"
        fam = 6 if c.family == socket.AF_INET6 else 4
        lip, lport = (c.laddr.ip, c.laddr.port) if c.laddr else ("", None)
        rip, rport = (c.raddr.ip, c.raddr.port) if c.raddr else ("", None)
        status = c.status if proto == "TCP" else ""
        if status == "NONE":
            status = ""
        listening = status == "LISTEN" or (proto == "UDP" and not rip)
        pinfo = _proc_info(psutil, c.pid)
        if c.pid is not None:
            seen_pids.add(c.pid)
        rows.append({
            "proto": proto, "family": fam,
            "laddr": lip, "lport": lport, "lport_name": WELL_KNOWN.get(lport, "") if lport else "",
            "raddr": rip, "rport": rport, "rport_name": WELL_KNOWN.get(rport, "") if rport else "",
            "status": status, "listening": listening,
            "pid": c.pid, "pname": pinfo["name"], "ppath": pinfo["path"],
            "denied": bool(pinfo["denied"] or c.pid is None),
            "path_denied": bool(pinfo.get("path_denied")),
        })
    with _proc_lock:
        for pid in list(_proc_cache):
            if pid not in seen_pids:
                _proc_cache.pop(pid, None)
    counts = {"tcp": sum(r["proto"] == "TCP" for r in rows), "udp": sum(r["proto"] == "UDP" for r in rows),
              "listening": sum(r["listening"] for r in rows),
              "established": sum(r["status"] == "ESTABLISHED" for r in rows)}
    return {"connections": rows, "counts": counts, "time": time.time(),
            "self_pid": os.getpid(), "admin": elevate.is_admin()}


@router.get("/status")
async def status():
    return {"admin": elevate.is_admin(), "psutil": _psutil() is not None}


@router.get("/connections")
async def connections():
    psutil = _psutil()
    if psutil is None:
        return _missing_dep()
    try:
        return await asyncio.to_thread(_connections_sync, psutil)
    except PermissionError as e:
        return _err(str(e), 403)


# ---------------------------------------------------------------- DNS inverse

def _valid_ip(ip: str) -> bool:
    try:
        ipaddress.ip_address(ip)
        return True
    except ValueError:
        return False


def _resolve_one(ip: str):
    try:
        name = socket.gethostbyaddr(ip)[0]
    except (socket.herror, socket.gaierror, OSError, UnicodeError):
        name = None
    with _dns_lock:
        _dns_cache[ip] = (time.time(), name)
        _dns_pending.discard(ip)


class ResolveIn(BaseModel):
    ips: List[str]
    wait: float = 1.5


@router.post("/resolve")
async def resolve(payload: ResolveIn):
    ips = []
    for ip in payload.ips[:500]:
        ip = (ip or "").strip()
        if ip and _valid_ip(ip) and ip not in ips:
            ips.append(ip)
    now = time.time()
    futures = []
    with _dns_lock:
        for ip in ips:
            addr = ipaddress.ip_address(ip)
            if addr.is_unspecified:
                _dns_cache[ip] = (now, None)
                continue
            hit = _dns_cache.get(ip)
            if hit and now - hit[0] < DNS_TTL:
                continue
            if ip in _dns_pending:
                continue
            _dns_pending.add(ip)
            futures.append(_dns_pool.submit(_resolve_one, ip))
    if futures:
        deadline = time.time() + max(0.0, min(payload.wait, 5.0))

        def _wait():
            for f in futures:
                rem = deadline - time.time()
                if rem <= 0:
                    break
                try:
                    f.result(timeout=rem)
                except Exception:
                    pass
        await asyncio.to_thread(_wait)
    out, pending = {}, []
    with _dns_lock:
        for ip in ips:
            hit = _dns_cache.get(ip)
            if hit:
                out[ip] = hit[1]
            elif ip in _dns_pending:
                pending.append(ip)
    return {"results": out, "pending": pending}


# ---------------------------------------------------------------- Terminer un processus

def _ancestors(psutil) -> set:
    pids = {os.getpid()}
    try:
        p = psutil.Process(os.getpid())
        for parent in p.parents():
            pids.add(parent.pid)
    except Exception:
        try:
            pids.add(os.getppid())
        except Exception:
            pass
    return pids


def _protection_reason(psutil, pid: int, name: str) -> Optional[str]:
    if pid in (0, 4):
        return "Ce processus système est protégé et ne peut pas être terminé."
    if pid in _ancestors(psutil):
        return "Impossible de terminer LedroideDesktop lui-même (ou l'un de ses processus parents)."
    if name.lower() in PROTECTED_NAMES:
        return f"« {name} » est un processus système critique et ne peut pas être terminé."
    return None


class KillIn(BaseModel):
    name: str = ""          # nom attendu (évite de tuer un PID réutilisé)
    force: bool = False
    elevate: bool = False


def _kill_sync(psutil, pid: int, payload: KillIn):
    try:
        p = psutil.Process(pid)
        name = p.name()
    except psutil.NoSuchProcess:
        return 404, "Processus introuvable (déjà terminé ?)", None
    except psutil.AccessDenied:
        name = ""
    if payload.name and name and payload.name.lower() != name.lower():
        return 409, f"Le PID {pid} correspond maintenant à « {name} » et non à « {payload.name} ». Rafraîchissez la liste.", None
    reason = _protection_reason(psutil, pid, name or payload.name)
    if reason:
        return 400, reason, None
    if payload.elevate:
        script = f"Stop-Process -Id {int(pid)} -Force -ErrorAction Stop; 'OK'"
        r = elevate.run_powershell(script, elevate=True, timeout=60)
        if r["cancelled"]:
            return 403, "Élévation refusée : le processus n'a pas été terminé.", "UAC_CANCELLED"
        if not r["ok"]:
            return 400, "Échec : " + (r["output"] or "erreur inconnue"), None
        return 200, name or payload.name, None
    try:
        p.terminate()
        try:
            p.wait(timeout=3)
        except psutil.TimeoutExpired:
            if payload.force:
                p.kill()
                p.wait(timeout=3)
            else:
                return 409, f"« {name} » ne s'est pas arrêté dans les temps. Réessayez en forçant.", "TIMEOUT"
        return 200, name, None
    except psutil.NoSuchProcess:
        return 404, "Processus introuvable (déjà terminé ?)", None
    except psutil.AccessDenied:
        return 403, "Accès refusé : des droits administrateur sont nécessaires.", "NEED_ADMIN"
    except psutil.TimeoutExpired:
        return 409, "Le processus ne répond pas.", "TIMEOUT"


@router.post("/kill/{pid}")
async def kill(pid: int, payload: KillIn = KillIn()):
    psutil = _psutil()
    if psutil is None:
        return _missing_dep()
    if pid < 0 or pid > 0xFFFFFFFF:
        return _err("PID invalide.")
    code, msg, ecode = await asyncio.to_thread(_kill_sync, psutil, pid, payload)
    if code != 200:
        return _err(msg, code, ecode)
    return {"ok": True, "name": msg}


# ---------------------------------------------------------------- Export CSV

class ExportIn(BaseModel):
    rows: List[dict]


EXPORT_COLS = [("proto", "Protocole"), ("laddr", "Adresse locale"), ("lport", "Port local"),
               ("lport_name", "Service local"), ("raddr", "Adresse distante"), ("rport", "Port distant"),
               ("rport_name", "Service distant"), ("rhost", "Hôte distant"), ("status", "État"),
               ("pid", "PID"), ("pname", "Processus"), ("ppath", "Chemin")]


def _csv_safe(v) -> str:
    s = "" if v is None else str(v)
    if s[:1] in ("=", "+", "-", "@"):
        s = "'" + s
    return s


@router.post("/export")
async def export(payload: ExportIn):
    if not payload.rows:
        return _err("Aucune connexion à exporter.")
    TEMP_DIR.mkdir(parents=True, exist_ok=True)
    fname = time.strftime("connexions_%Y%m%d_%H%M%S.csv")

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
    return FileResponse(path, media_type="text/csv", filename=filename,
                        headers={"Content-Disposition": f'attachment; filename="{filename}"'})


@router.get("/health")
async def health():
    return {"ok": True, "psutil": _psutil() is not None}
