"""Outils réseau — infos, ping, DNS, scan de ports, traceroute, test de débit."""
import asyncio
import ipaddress
import json
import re
import socket
import ssl
import subprocess
import sys
import threading
import time
import urllib.request
import uuid
from typing import Optional

from ledroide_sdk.http import JSONResponse
from pydantic import BaseModel
from ledroide_sdk import Module

module = Module(__file__)
router = module.router

IS_WINDOWS = sys.platform == "win32"
CREATE_NO_WINDOW = 0x08000000 if IS_WINDOWS else 0
UA = {"User-Agent": "LedroideDesktop-network-tools/1.0"}
MAX_SCAN_PORTS = 1024
HOST_RE = re.compile(r"^[A-Za-z0-9_]([A-Za-z0-9_.\-]{0,252})$")

COMMON_PORTS = {
    20: "ftp-data", 21: "ftp", 22: "ssh", 23: "telnet", 25: "smtp", 53: "dns", 67: "dhcp", 69: "tftp",
    80: "http", 110: "pop3", 123: "ntp", 135: "msrpc", 137: "netbios-ns", 139: "netbios-ssn", 143: "imap",
    161: "snmp", 389: "ldap", 443: "https", 445: "smb", 465: "smtps", 514: "syslog", 554: "rtsp",
    587: "submission", 631: "ipp", 636: "ldaps", 853: "dns-over-tls", 993: "imaps", 995: "pop3s",
    1080: "socks", 1433: "mssql", 1521: "oracle", 1723: "pptp", 1883: "mqtt", 1900: "upnp",
    2049: "nfs", 2375: "docker", 3000: "dev-http", 3306: "mysql", 3389: "rdp", 5000: "upnp/dev",
    5353: "mdns", 5432: "postgresql", 5900: "vnc", 5985: "winrm", 6379: "redis", 6881: "bittorrent",
    8000: "http-alt", 8008: "http-alt", 8080: "http-proxy", 8081: "http-alt", 8443: "https-alt",
    8765: "ledroide", 8888: "http-alt", 9000: "http-alt", 9090: "http-alt", 9100: "printer",
    9200: "elasticsearch", 11211: "memcached", 25565: "minecraft", 27017: "mongodb", 32400: "plex",
}


def _err(msg: str, status: int = 400, code: Optional[str] = None):
    c = {"error": msg}
    if code:
        c["code"] = code
    return JSONResponse(status_code=status, content=c)


def _valid_host(host: str) -> Optional[str]:
    host = (host or "").strip()
    if not host or len(host) > 253:
        return None
    try:
        return str(ipaddress.ip_address(host.strip("[]")))
    except ValueError:
        pass
    if HOST_RE.match(host) and ".." not in host and not host.startswith("-"):
        return host
    return None


def _decode(b: bytes) -> str:
    for enc in (("oem",) if IS_WINDOWS else ()) + ("utf-8", "cp850", "latin-1"):
        try:
            return b.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return b.decode("utf-8", "replace")


# ── Jobs (exécutions longues interrogées par le frontend) ─────────────────────

_jobs: dict = {}
_jobs_lock = threading.Lock()
JOB_TTL = 900
MAX_RUNNING = 6


class Job:
    def __init__(self, kind: str, params: dict):
        self.id = uuid.uuid4().hex[:12]
        self.kind = kind
        self.params = params
        self.status = "running"     # running | done | error | cancelled
        self.lines: list = []
        self.items: list = []        # résultats structurés (réponses ping, sauts, ports…)
        self.summary: dict = {}
        self.progress: float = 0.0
        self.error: Optional[str] = None
        self.started = time.time()
        self.finished: Optional[float] = None
        self.cancel = threading.Event()
        self.proc: Optional[subprocess.Popen] = None

    def finish(self, status="done", error=None):
        if self.status == "running":
            self.status = "cancelled" if self.cancel.is_set() else status
        if self.status == "cancelled":
            error = None
        self.error = error
        self.finished = time.time()
        self.progress = 1.0 if self.status == "done" else self.progress

    def view(self, since_line: int = 0, since_item: int = 0) -> dict:
        return {
            "id": self.id, "kind": self.kind, "params": self.params, "status": self.status,
            "lines": self.lines[since_line:], "line_count": len(self.lines),
            "items": self.items[since_item:], "item_count": len(self.items),
            "summary": self.summary, "progress": round(self.progress, 4), "error": self.error,
            "elapsed": round((self.finished or time.time()) - self.started, 2),
        }


def _new_job(kind: str, params: dict, target) -> Job:
    now = time.time()
    with _jobs_lock:
        for jid in [j for j, v in _jobs.items() if v.finished and now - v.finished > JOB_TTL]:
            del _jobs[jid]
        running = sum(1 for v in _jobs.values() if v.status == "running")
        if running >= MAX_RUNNING:
            raise RuntimeError("Trop de tâches en cours, patientez ou annulez-en une.")
        job = Job(kind, params)
        _jobs[job.id] = job

    def _run():
        try:
            target(job)
            job.finish("done")
        except Exception as e:  # noqa: BLE001
            job.finish("error", str(e) or e.__class__.__name__)
        finally:
            if job.proc and job.proc.poll() is None:
                try:
                    job.proc.kill()
                except Exception:
                    pass
    threading.Thread(target=_run, name=f"nettools-{kind}-{job.id}", daemon=True).start()
    return job


def _run_process(job: Job, args: list, on_line):
    job.proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                stdin=subprocess.DEVNULL, creationflags=CREATE_NO_WINDOW)
    for raw in iter(job.proc.stdout.readline, b""):
        if job.cancel.is_set():
            job.proc.kill()
            break
        line = _decode(raw).rstrip("\r\n")
        if not line.strip():
            continue
        job.lines.append(line)
        on_line(line)
    job.proc.wait(timeout=5)


@router.get("/jobs/{job_id}")
async def get_job(job_id: str, since_line: int = 0, since_item: int = 0):
    job = _jobs.get(job_id)
    if not job:
        return _err("Tâche introuvable", 404)
    return job.view(max(0, since_line), max(0, since_item))


@router.post("/jobs/{job_id}/cancel")
async def cancel_job(job_id: str):
    job = _jobs.get(job_id)
    if not job:
        return _err("Tâche introuvable", 404)
    job.cancel.set()
    if job.proc and job.proc.poll() is None:
        try:
            job.proc.kill()
        except Exception:
            pass
    return {"ok": True}


def _start(kind, params, target):
    try:
        job = _new_job(kind, params, target)
    except RuntimeError as e:
        return _err(str(e), 429)
    return {"job_id": job.id}


# ── Infos ─────────────────────────────────────────────────────────────────────

def _primary_ip() -> Optional[str]:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))  # UDP : aucun paquet envoyé
        return s.getsockname()[0]
    except OSError:
        return None
    finally:
        s.close()


@router.get("/info")
async def info():
    try:
        import psutil
    except ImportError:
        return _err("Le paquet psutil n'est pas installé.", 400, "MISSING_DEP")

    def _q():
        addrs = psutil.net_if_addrs()
        stats = psutil.net_if_stats()
        try:
            io = psutil.net_io_counters(pernic=True)
        except Exception:
            io = {}
        af_link = getattr(psutil, "AF_LINK", -1)
        ifaces = []
        for name, lst in addrs.items():
            st = stats.get(name)
            d = {"name": name, "up": bool(st and st.isup), "speed": st.speed if st else 0,
                 "mtu": st.mtu if st else 0, "mac": None, "ipv4": [], "ipv6": [],
                 "sent": io[name].bytes_sent if name in io else 0,
                 "recv": io[name].bytes_recv if name in io else 0}
            for a in lst:
                if a.family == socket.AF_INET:
                    d["ipv4"].append({"address": a.address, "netmask": a.netmask})
                elif a.family == socket.AF_INET6:
                    d["ipv6"].append({"address": a.address.split("%")[0], "netmask": a.netmask})
                elif a.family == af_link:
                    d["mac"] = a.address
            ifaces.append(d)
        ifaces.sort(key=lambda x: (not x["up"], not x["ipv4"], x["name"].lower()))
        hostname = socket.gethostname()
        try:
            fqdn = socket.getfqdn()
        except Exception:
            fqdn = hostname
        return {"hostname": hostname, "fqdn": fqdn, "primary_ip": _primary_ip(), "interfaces": ifaces}
    return await asyncio.to_thread(_q)


@router.get("/public-ip")
async def public_ip():
    def _q():
        req = urllib.request.Request("https://api.ipify.org?format=json", headers=UA)
        with urllib.request.urlopen(req, timeout=8) as r:
            return json.loads(r.read().decode())["ip"]
    try:
        ip = await asyncio.to_thread(_q)
    except Exception as e:  # noqa: BLE001
        return _err(f"Impossible d'obtenir l'IP publique : {e}", 502)
    rev = None
    try:
        rev = (await asyncio.wait_for(asyncio.to_thread(socket.gethostbyaddr, ip), 4))[0]
    except Exception:
        pass
    return {"ip": ip, "reverse": rev}


# ── Ping ──────────────────────────────────────────────────────────────────────

class PingIn(BaseModel):
    host: str
    count: int = 4
    timeout_ms: int = 2000


PING_RE = re.compile(r"[=<]\s*(\d+)\s*ms\s+TTL\s*=\s*(\d+)", re.I)
PING_RE_UNIX = re.compile(r"ttl=(\d+).*?time[=<]\s*([\d.]+)\s*ms", re.I)


def _ping_job(job: Job):
    p = job.params
    if IS_WINDOWS:
        args = ["ping", "-n", str(p["count"]), "-w", str(p["timeout_ms"]), p["host"]]
    else:
        args = ["ping", "-c", str(p["count"]), "-W", str(max(1, p["timeout_ms"] // 1000)), p["host"]]

    def on_line(line):
        m = PING_RE.search(line)
        if m:
            job.items.append({"seq": len(job.items) + 1, "time": int(m.group(1)), "ttl": int(m.group(2)),
                              "raw": line})
        else:
            m2 = PING_RE_UNIX.search(line)
            if m2:
                job.items.append({"seq": len(job.items) + 1, "time": float(m2.group(2)),
                                  "ttl": int(m2.group(1)), "raw": line})
        job.progress = min(0.99, len(job.lines) / (p["count"] + 2))
        _ping_summary(job)

    _run_process(job, args, on_line)
    _ping_summary(job, final=True)


def _ping_summary(job: Job, final: bool = False):
    times = [x["time"] for x in job.items]
    sent = job.params["count"]
    recv = len(times)
    job.summary = {
        "sent": sent, "received": recv, "lost": max(0, sent - recv) if final else None,
        "loss_percent": round(100 * (sent - recv) / sent, 1) if (sent and final) else None,
        "min": min(times) if times else None, "max": max(times) if times else None,
        "avg": round(sum(times) / recv, 1) if recv else None,
    }


@router.post("/ping")
async def ping(payload: PingIn):
    host = _valid_host(payload.host)
    if not host:
        return _err("Nom d'hôte ou adresse IP invalide")
    if not (1 <= payload.count <= 100):
        return _err("Le nombre de requêtes doit être entre 1 et 100")
    to = max(200, min(payload.timeout_ms, 10000))
    return _start("ping", {"host": host, "count": payload.count, "timeout_ms": to}, _ping_job)


# ── Traceroute ────────────────────────────────────────────────────────────────

class TraceIn(BaseModel):
    host: str
    max_hops: int = 30


HOP_RE = re.compile(r"^\s*(\d{1,2})\s+(.*)$")
RTT_RE = re.compile(r"(<?\s*\d+)\s*ms|(\*)")


def _trace_job(job: Job):
    p = job.params
    args = (["tracert", "-d", "-h", str(p["max_hops"]), "-w", "1500", p["host"]] if IS_WINDOWS
            else ["traceroute", "-n", "-m", str(p["max_hops"]), p["host"]])

    def on_line(line):
        m = HOP_RE.match(line)
        if not m:
            return
        rest = m.group(2)
        rtts = []
        for a, star in RTT_RE.findall(rest):
            if star:
                rtts.append(None)
            else:
                rtts.append(a.replace(" ", ""))
        if not rtts:
            return
        ip = None
        tail = RTT_RE.sub(" ", rest).split()
        for tok in reversed(tail):
            tok = tok.strip("[]")
            try:
                ipaddress.ip_address(tok)
                ip = tok
                break
            except ValueError:
                continue
        job.items.append({"hop": int(m.group(1)), "rtts": rtts[:3], "ip": ip,
                          "timeout": all(r is None for r in rtts)})
        job.progress = min(0.99, int(m.group(1)) / p["max_hops"])

    _run_process(job, args, on_line)
    job.summary = {"hops": len(job.items),
                   "reached": bool(job.items and job.items[-1]["ip"] and not job.items[-1]["timeout"])}


@router.post("/traceroute")
async def traceroute(payload: TraceIn):
    host = _valid_host(payload.host)
    if not host:
        return _err("Nom d'hôte ou adresse IP invalide")
    hops = max(1, min(payload.max_hops, 64))
    return _start("traceroute", {"host": host, "max_hops": hops}, _trace_job)


# ── DNS ───────────────────────────────────────────────────────────────────────

@router.get("/dns")
async def dns(host: str, reverse_each: bool = True):
    h = _valid_host(host)
    if not h:
        return _err("Nom d'hôte ou adresse IP invalide")

    def _q():
        t0 = time.perf_counter()
        is_ip = True
        try:
            ipaddress.ip_address(h)
        except ValueError:
            is_ip = False
        result = {"query": h, "is_ip": is_ip, "addresses": [], "reverse": None, "aliases": [],
                  "canonical": None}
        if is_ip:
            try:
                name, aliases, _ = socket.gethostbyaddr(h)
                result["reverse"] = name
                result["aliases"] = aliases
            except (socket.herror, socket.gaierror, OSError):
                result["reverse"] = None
            result["addresses"] = [{"address": h, "family": "IPv6" if ":" in h else "IPv4", "reverse": result["reverse"]}]
        else:
            try:
                infos = socket.getaddrinfo(h, None, 0, socket.SOCK_STREAM, 0, socket.AI_CANONNAME)
            except socket.gaierror as e:
                raise LookupError(f"Résolution impossible : {e.strerror or e}")
            seen = set()
            for fam, _, _, canon, sa in infos:
                if canon and not result["canonical"]:
                    result["canonical"] = canon
                ip = sa[0]
                if ip in seen:
                    continue
                seen.add(ip)
                result["addresses"].append({"address": ip, "family": "IPv6" if fam == socket.AF_INET6 else "IPv4",
                                            "reverse": None})
            if reverse_each:
                for a in result["addresses"][:8]:
                    try:
                        a["reverse"] = socket.gethostbyaddr(a["address"])[0]
                    except (socket.herror, socket.gaierror, OSError):
                        pass
        result["elapsed_ms"] = round((time.perf_counter() - t0) * 1000, 1)
        return result
    try:
        return await asyncio.wait_for(asyncio.to_thread(_q), 20)
    except LookupError as e:
        return _err(str(e), 404)
    except asyncio.TimeoutError:
        return _err("Délai de résolution DNS dépassé", 504)


# ── Scan de ports ─────────────────────────────────────────────────────────────

class ScanIn(BaseModel):
    host: str
    ports: str = ""              # "1-1024", "22,80,443", "20-25,80"
    common: bool = False
    timeout_ms: int = 600
    concurrency: int = 200


def _parse_ports(spec: str) -> list:
    out = set()
    for part in re.split(r"[,\s;]+", spec.strip()):
        if not part:
            continue
        m = re.fullmatch(r"(\d{1,5})(?:-(\d{1,5}))?", part)
        if not m:
            raise ValueError(f"Port invalide : « {part} »")
        a = int(m.group(1))
        b = int(m.group(2) or a)
        if a > b:
            a, b = b, a
        if a < 1 or b > 65535:
            raise ValueError("Les ports doivent être entre 1 et 65535")
        if b - a + 1 > MAX_SCAN_PORTS:
            raise ValueError(f"Maximum {MAX_SCAN_PORTS} ports par scan")
        out.update(range(a, b + 1))
        if len(out) > MAX_SCAN_PORTS:
            raise ValueError(f"Maximum {MAX_SCAN_PORTS} ports par scan")
    if not out:
        raise ValueError("Aucun port indiqué")
    return sorted(out)


def _service(port: int) -> str:
    if port in COMMON_PORTS:
        return COMMON_PORTS[port]
    try:
        return socket.getservbyport(port, "tcp")
    except OSError:
        return ""


def _scan_job(job: Job):
    p = job.params
    infos = socket.getaddrinfo(p["host"], None, 0, socket.SOCK_STREAM)
    if not infos:
        raise RuntimeError("Hôte introuvable")
    infos.sort(key=lambda i: i[0] != socket.AF_INET)  # IPv4 d'abord
    ip = infos[0][4][0]
    job.summary = {"ip": ip, "total": len(p["ports"]), "scanned": 0, "open": 0}
    timeout = p["timeout_ms"] / 1000

    async def main():
        sem = asyncio.Semaphore(p["concurrency"])

        async def probe(port):
            if job.cancel.is_set():
                return
            async with sem:
                if job.cancel.is_set():
                    return
                state = "closed"
                t0 = time.perf_counter()
                try:
                    _, w = await asyncio.wait_for(asyncio.open_connection(ip, port), timeout)
                    state = "open"
                    w.close()
                    try:
                        await asyncio.wait_for(w.wait_closed(), 0.5)
                    except Exception:
                        pass
                except asyncio.TimeoutError:
                    state = "filtered"
                except (ConnectionRefusedError, OSError):
                    state = "closed"
                job.summary["scanned"] += 1
                job.progress = job.summary["scanned"] / job.summary["total"]
                if state == "open":
                    job.summary["open"] += 1
                    job.items.append({"port": port, "state": "open", "service": _service(port),
                                      "time_ms": round((time.perf_counter() - t0) * 1000, 1)})
        await asyncio.gather(*(probe(pt) for pt in p["ports"]))

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(main())
    finally:
        loop.close()
    job.items.sort(key=lambda x: x["port"])


@router.post("/portscan")
async def portscan(payload: ScanIn):
    host = _valid_host(payload.host)
    if not host:
        return _err("Nom d'hôte ou adresse IP invalide")
    try:
        ports = sorted(COMMON_PORTS) if payload.common else _parse_ports(payload.ports)
    except ValueError as e:
        return _err(str(e))
    to = max(100, min(payload.timeout_ms, 5000))
    conc = max(1, min(payload.concurrency, 500))
    return _start("portscan", {"host": host, "ports": ports, "timeout_ms": to, "concurrency": conc,
                               "label": "ports courants" if payload.common else payload.ports}, _scan_job)


@router.get("/common-ports")
async def common_ports():
    return {"ports": [{"port": k, "service": v} for k, v in sorted(COMMON_PORTS.items())], "max": MAX_SCAN_PORTS}


# ── Test de débit ─────────────────────────────────────────────────────────────

class SpeedIn(BaseModel):
    download_mb: int = 25
    upload_mb: int = 10


SPEED_DOWN = "https://speed.cloudflare.com/__down?bytes={n}"
SPEED_UP = "https://speed.cloudflare.com/__up"


class _UploadBody:
    """Corps d'upload lu par morceaux pour mesurer la progression."""

    def __init__(self, job: Job, total: int):
        self.job, self.total, self.sent = job, total, 0
        self.chunk = b"\x00" * 65536
        self.t0 = None

    def __iter__(self):
        return self

    def __next__(self):
        if self.job.cancel.is_set():
            raise RuntimeError("Annulé")
        if self.t0 is None:
            self.t0 = time.perf_counter()
        if self.sent >= self.total:
            raise StopIteration
        n = min(len(self.chunk), self.total - self.sent)
        self.sent += n
        dt = time.perf_counter() - self.t0
        if dt > 0:
            self.job.summary["upload_mbps"] = round(self.sent * 8 / dt / 1e6, 2)
        self.job.summary["upload_progress"] = round(self.sent / self.total, 3)
        self.job.progress = 0.65 + 0.34 * self.sent / self.total
        return self.chunk[:n]


def _speed_job(job: Job):
    p = job.params
    ctx = ssl.create_default_context()
    s = job.summary
    s.update({"phase": "latence", "ping_ms": None, "jitter_ms": None, "download_mbps": None,
              "upload_mbps": None, "download_progress": 0, "upload_progress": 0})

    # Latence (petites requêtes)
    lat = []
    for _ in range(5):
        if job.cancel.is_set():
            return
        t0 = time.perf_counter()
        req = urllib.request.Request(SPEED_DOWN.format(n=0), headers=UA)
        with urllib.request.urlopen(req, timeout=10, context=ctx) as r:
            r.read()
        lat.append((time.perf_counter() - t0) * 1000)
    lat.sort()
    s["ping_ms"] = round(lat[len(lat) // 2], 1)
    s["jitter_ms"] = round(sum(abs(lat[i] - lat[i - 1]) for i in range(1, len(lat))) / (len(lat) - 1), 1)
    job.progress = 0.05
    job.lines.append(f"Latence : {s['ping_ms']} ms (gigue {s['jitter_ms']} ms)")

    # Téléchargement
    s["phase"] = "téléchargement"
    total = p["download_mb"] * 1_000_000
    req = urllib.request.Request(SPEED_DOWN.format(n=total), headers=UA)
    got = 0
    t0 = time.perf_counter()
    with urllib.request.urlopen(req, timeout=30, context=ctx) as r:
        while True:
            if job.cancel.is_set():
                return
            chunk = r.read(131072)
            if not chunk:
                break
            got += len(chunk)
            dt = time.perf_counter() - t0
            if dt > 0:
                s["download_mbps"] = round(got * 8 / dt / 1e6, 2)
            s["download_progress"] = round(got / total, 3)
            job.progress = 0.05 + 0.6 * got / total
    dt = time.perf_counter() - t0
    s["download_mbps"] = round(got * 8 / dt / 1e6, 2) if dt > 0 else None
    s["download_bytes"] = got
    job.lines.append(f"Téléchargement : {s['download_mbps']} Mbit/s ({got / 1e6:.1f} Mo en {dt:.1f} s)")

    # Envoi
    s["phase"] = "envoi"
    up_total = p["upload_mb"] * 1_000_000
    body = _UploadBody(job, up_total)
    req = urllib.request.Request(SPEED_UP, data=body, method="POST",
                                 headers={**UA, "Content-Type": "application/octet-stream",
                                          "Content-Length": str(up_total)})
    t0 = time.perf_counter()
    with urllib.request.urlopen(req, timeout=60, context=ctx) as r:
        r.read()
    if job.cancel.is_set():
        return
    dt = time.perf_counter() - t0
    s["upload_mbps"] = round(body.sent * 8 / dt / 1e6, 2) if dt > 0 else None
    s["upload_progress"] = 1
    s["upload_bytes"] = body.sent
    job.lines.append(f"Envoi : {s['upload_mbps']} Mbit/s ({body.sent / 1e6:.1f} Mo en {dt:.1f} s)")
    s["phase"] = "terminé"


@router.post("/speedtest")
async def speedtest(payload: SpeedIn = SpeedIn()):
    dl = max(1, min(payload.download_mb, 200))
    ul = max(1, min(payload.upload_mb, 100))
    return _start("speedtest", {"download_mb": dl, "upload_mb": ul}, _speed_job)


@router.get("/health")
async def health():
    return {"ok": True}
