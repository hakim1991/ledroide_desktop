import asyncio
import ipaddress
import logging
import re
import socket
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor

from ledroide_sdk import Module

log = logging.getLogger("ledroide.port-scanner")

module = Module(__file__)
router = module.router

MAX_PORTS = 65535
MIN_PREFIX = 22
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
HOST_RE = re.compile(r"^[A-Za-z0-9.\-:_\[\]%]{1,253}$")

SERVICES = {
    7: "echo", 20: "ftp-data", 21: "ftp", 22: "ssh", 23: "telnet", 25: "smtp", 37: "time", 53: "dns",
    67: "dhcp", 69: "tftp", 79: "finger", 80: "http", 81: "http-alt", 88: "kerberos", 102: "s7comm",
    110: "pop3", 111: "rpcbind", 113: "ident", 119: "nntp", 123: "ntp", 135: "msrpc", 137: "netbios-ns",
    139: "netbios-ssn", 143: "imap", 161: "snmp", 179: "bgp", 389: "ldap", 443: "https", 445: "smb",
    465: "smtps", 500: "isakmp", 502: "modbus", 515: "lpd", 548: "afp", 554: "rtsp", 587: "submission",
    631: "ipp", 636: "ldaps", 873: "rsync", 853: "dns-over-tls", 902: "vmware-auth", 993: "imaps",
    995: "pop3s", 1080: "socks", 1194: "openvpn", 1433: "mssql", 1521: "oracle", 1701: "l2tp",
    1723: "pptp", 1883: "mqtt", 1900: "upnp", 2049: "nfs", 2082: "cpanel", 2083: "cpanel-ssl",
    2181: "zookeeper", 2222: "ssh-alt", 2375: "docker", 2376: "docker-tls", 2525: "smtp-alt",
    3000: "dev-http", 3128: "squid", 3260: "iscsi", 3306: "mysql", 3389: "rdp", 3478: "stun",
    4369: "epmd", 5000: "upnp/dev", 5001: "dev-https", 5060: "sip", 5061: "sips", 5222: "xmpp",
    5353: "mdns", 5357: "wsdapi", 5432: "postgresql", 5601: "kibana", 5672: "amqp", 5683: "coap",
    5900: "vnc", 5984: "couchdb", 5985: "winrm", 5986: "winrm-https", 6379: "redis", 6443: "kubernetes",
    6667: "irc", 7000: "dev-http", 7001: "weblogic", 7547: "tr-069", 8000: "http-alt", 8008: "http-alt",
    8009: "ajp13", 8080: "http-proxy", 8081: "http-alt", 8086: "influxdb", 8123: "home-assistant",
    8200: "vault", 8443: "https-alt", 8765: "ledroide", 8883: "mqtts", 8888: "http-alt", 9000: "dev-http",
    9001: "tor/dev", 9090: "prometheus", 9092: "kafka", 9100: "jetdirect", 9200: "elasticsearch",
    9300: "elasticsearch", 9418: "git", 10000: "webmin", 11211: "memcached", 25565: "minecraft",
    27017: "mongodb", 32400: "plex", 49152: "upnp",
}
PRESETS = {
    "common": sorted(SERVICES),
    "web": [80, 81, 443, 3000, 5000, 7000, 8000, 8008, 8080, 8081, 8443, 8888, 9000],
    "well_known": list(range(1, 1025)),
    "all": list(range(1, 65536)),
}
HTTP_PORTS = {80, 81, 3000, 5000, 7000, 8000, 8008, 8080, 8081, 8086, 8123, 8888, 9000, 9090, 9200}

OUI = {
    "00:50:56": "VMware", "00:0C:29": "VMware", "00:05:69": "VMware", "08:00:27": "VirtualBox",
    "52:54:00": "QEMU/KVM", "00:15:5D": "Microsoft Hyper-V", "00:50:F2": "Microsoft",
    "B8:27:EB": "Raspberry Pi", "DC:A6:32": "Raspberry Pi", "E4:5F:01": "Raspberry Pi",
    "D8:3A:DD": "Raspberry Pi", "2C:CF:67": "Raspberry Pi",
    "00:03:93": "Apple", "00:0A:95": "Apple", "00:1B:63": "Apple", "00:1E:C2": "Apple", "00:25:00": "Apple",
    "28:CF:E9": "Apple", "3C:07:54": "Apple", "40:6C:8F": "Apple", "70:56:81": "Apple", "78:31:C1": "Apple",
    "A4:5E:60": "Apple", "AC:BC:32": "Apple", "F0:18:98": "Apple",
    "00:12:47": "Samsung", "00:16:32": "Samsung", "00:1D:25": "Samsung", "5C:0A:5B": "Samsung",
    "8C:77:12": "Samsung", "F0:25:B7": "Samsung",
    "00:1B:21": "Intel", "00:13:E8": "Intel", "3C:97:0E": "Intel", "A0:36:9F": "Intel",
    "00:00:0C": "Cisco", "00:1B:54": "Cisco", "00:40:96": "Cisco",
    "50:C7:BF": "TP-Link", "14:CC:20": "TP-Link", "F4:F2:6D": "TP-Link", "C0:4A:00": "TP-Link", "EC:08:6B": "TP-Link",
    "00:09:5B": "Netgear", "00:14:6C": "Netgear", "00:24:B2": "Netgear", "A0:40:A0": "Netgear",
    "00:05:5D": "D-Link", "00:1B:11": "D-Link", "1C:7E:E5": "D-Link",
    "00:15:6D": "Ubiquiti", "00:27:22": "Ubiquiti", "24:A4:3C": "Ubiquiti", "44:D9:E7": "Ubiquiti",
    "78:8A:20": "Ubiquiti", "F0:9F:C2": "Ubiquiti", "FC:EC:DA": "Ubiquiti",
    "24:0A:C4": "Espressif", "30:AE:A4": "Espressif", "84:F3:EB": "Espressif", "A4:CF:12": "Espressif",
    "EC:FA:BC": "Espressif", "5C:CF:7F": "Espressif", "24:6F:28": "Espressif",
    "00:11:32": "Synology", "00:1E:0B": "HP", "3C:D9:2B": "HP",
    "00:14:22": "Dell", "18:03:73": "Dell", "B8:AC:6F": "Dell", "F8:B1:56": "Dell",
    "64:09:80": "Xiaomi", "28:6C:07": "Xiaomi", "78:11:DC": "Xiaomi",
    "00:E0:FC": "Huawei", "00:18:82": "Huawei", "28:6E:D4": "Huawei",
    "F0:27:2D": "Amazon", "74:C2:46": "Amazon", "44:65:0D": "Amazon",
    "00:0E:58": "Sonos", "5C:AA:FD": "Sonos", "94:9F:3E": "Sonos",
    "00:04:0E": "AVM (Fritz!Box)", "3C:A6:2F": "AVM (Fritz!Box)", "C8:0E:14": "AVM (Fritz!Box)",
    "00:07:CB": "Freebox", "F4:CA:E5": "Freebox",
    "00:09:BF": "Nintendo", "00:17:AB": "Nintendo", "98:B6:E9": "Nintendo",
    "00:E0:4C": "Realtek", "00:1A:11": "Google", "F4:F5:D8": "Google", "3C:5A:B4": "Google",
}

_ping_pool: ThreadPoolExecutor | None = None


class ScanError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _err(exc: ScanError) -> dict:
    return {"error": exc.message, "code": exc.code}


def parse_ports(spec: str) -> list[int]:
    ports: set[int] = set()
    for part in re.split(r"[,\s;]+", (spec or "").strip()):
        if not part:
            continue
        m = re.fullmatch(r"(\d{1,5})(?:-(\d{1,5}))?", part)
        if not m:
            raise ScanError("BAD_PORTS", f"Port ou plage invalide : « {part} »")
        a = int(m.group(1))
        b = int(m.group(2) or a)
        if a > b:
            a, b = b, a
        if a < 1 or b > 65535:
            raise ScanError("BAD_PORTS", f"Les ports doivent être compris entre 1 et 65535 (« {part} »)")
        ports.update(range(a, b + 1))
    if not ports:
        raise ScanError("BAD_PORTS", "Aucun port à scanner")
    return sorted(ports)


def vendor_for(mac: str) -> str:
    mac = mac.upper().replace("-", ":")
    if len(mac) < 8:
        return ""
    if int(mac[:2], 16) & 0x02:
        return "Adresse locale / aléatoire"
    return OUI.get(mac[:8], "")


def _clamp(value, lo, hi, default):
    try:
        return max(lo, min(hi, type(default)(value)))
    except (TypeError, ValueError):
        return default


# ── Local network detection ─────────────────────────────────────────────────

def _local_ip() -> str | None:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("192.0.2.1", 9))
        return s.getsockname()[0]
    except OSError:
        return None
    finally:
        s.close()


def _mask_for(ip: str) -> str | None:
    if sys.platform != "win32":
        return None
    try:
        out = subprocess.run(["ipconfig"], capture_output=True, timeout=8, creationflags=NO_WINDOW).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    lines = out.decode("cp850", errors="replace").splitlines()
    for i, line in enumerate(lines):
        if re.search(rf"(?<![\d.]){re.escape(ip)}(?![\d.])", line):
            for nxt in lines[i + 1:i + 4]:
                m = re.search(r"(255\.\d{1,3}\.\d{1,3}\.\d{1,3})", nxt)
                if m:
                    return m.group(1)
    return None


def _detect_network() -> dict:
    ip = _local_ip()
    if not ip:
        return {"ip": None, "cidr": "192.168.1.0/24", "prefix": None}
    mask = _mask_for(ip)
    prefix = None
    if mask:
        try:
            prefix = ipaddress.IPv4Network(f"0.0.0.0/{mask}").prefixlen
        except ValueError:
            prefix = None
    use = prefix if prefix and prefix >= MIN_PREFIX else 24
    net = ipaddress.ip_network(f"{ip}/{use}", strict=False)
    return {"ip": ip, "cidr": str(net), "prefix": prefix}


@module.on("info")
async def info(payload: dict):
    net = await asyncio.to_thread(_detect_network)
    return {
        "network": net,
        "presets": {k: len(v) for k, v in PRESETS.items()},
        "default_concurrency": int(module.config.get("default_concurrency", 200)),
        "max_prefix": MIN_PREFIX,
    }


@module.on("parse_ports")
async def parse_ports_action(payload: dict):
    try:
        ports = parse_ports(str(payload.get("ports", "")))
    except ScanError as exc:
        return _err(exc)
    return {"count": len(ports)}


# ── Port scan ───────────────────────────────────────────────────────────────

async def _resolve(host: str) -> tuple[str, int]:
    loop = asyncio.get_running_loop()
    try:
        infos = await asyncio.wait_for(loop.getaddrinfo(host, None, type=socket.SOCK_STREAM), 10)
    except (socket.gaierror, OSError, asyncio.TimeoutError):
        raise ScanError("RESOLVE_FAILED", f"Impossible de résoudre l'hôte « {host} »")
    for fam in (socket.AF_INET, socket.AF_INET6):
        for info_ in infos:
            if info_[0] == fam:
                return info_[4][0], fam
    raise ScanError("RESOLVE_FAILED", f"Aucune adresse IP pour « {host} »")


def _clean_banner(raw: bytes) -> str:
    text = raw.decode("utf-8", errors="replace")
    line = text.strip().splitlines()[0] if text.strip() else ""
    line = "".join(c if c.isprintable() else "·" for c in line)
    return line[:160]


async def _probe(ip: str, port: int, timeout: float, banner: bool) -> tuple[str, float, str]:
    started = time.perf_counter()
    try:
        reader, writer = await asyncio.wait_for(asyncio.open_connection(ip, port), timeout)
    except asyncio.TimeoutError:
        return "filtered", 0.0, ""
    except ConnectionRefusedError:
        return "closed", 0.0, ""
    except OSError:
        return "filtered", 0.0, ""
    rtt = (time.perf_counter() - started) * 1000
    text = ""
    try:
        if banner:
            wait = min(max(timeout, 0.5), 2.0)
            try:
                data = await asyncio.wait_for(reader.read(256), wait)
            except asyncio.TimeoutError:
                data = b""
            if not data and port in HTTP_PORTS:
                writer.write(b"HEAD / HTTP/1.0\r\nHost: " + ip.encode() + b"\r\nUser-Agent: LedroideScanner\r\n\r\n")
                await writer.drain()
                try:
                    data = await asyncio.wait_for(reader.read(512), wait)
                except asyncio.TimeoutError:
                    data = b""
                if data:
                    lines = data.decode("latin-1", errors="replace").splitlines()
                    server = next((ln for ln in lines if ln.lower().startswith("server:")), "")
                    data = ((lines[0] if lines else "") + (" | " + server if server else "")).encode()
            text = _clean_banner(data)
    except (OSError, asyncio.TimeoutError):
        pass
    finally:
        writer.close()
        try:
            await asyncio.wait_for(writer.wait_closed(), 1)
        except (OSError, asyncio.TimeoutError):
            pass
    return "open", rtt, text


@module.job("scan_ports")
async def scan_ports(payload: dict, job):
    host = str(payload.get("host", "")).strip()
    if not host or not HOST_RE.match(host):
        raise ValueError("Hôte invalide")
    preset = payload.get("preset")
    if preset:
        if preset not in PRESETS:
            raise ValueError(f"Preset inconnu : {preset}")
        ports = PRESETS[preset]
    else:
        try:
            ports = parse_ports(str(payload.get("ports", "")))
        except ScanError as exc:
            raise ValueError(exc.message)
    concurrency = _clamp(payload.get("concurrency"), 1, 1000, int(module.config.get("default_concurrency", 200)))
    timeout = _clamp(payload.get("timeout_ms"), 50, 10000, 800) / 1000
    banner = bool(payload.get("banner", False))
    try:
        ip, _fam = await _resolve(host.strip("[]"))
    except ScanError as exc:
        raise ValueError(exc.message)

    log.info("scan ports host=%s ports=%d concurrency=%d", host, len(ports), concurrency)
    results: list[dict] = []
    counts = {"open": 0, "closed": 0, "filtered": 0}
    done = 0
    total = len(ports)
    started = time.perf_counter()
    last_emit = 0.0
    queue = iter(ports)

    async def worker():
        nonlocal done, last_emit
        for port in queue:
            state, rtt, text = await _probe(ip, port, timeout, banner)
            counts[state] += 1
            done += 1
            if state == "open":
                entry = {"port": port, "service": SERVICES.get(port, ""), "rtt_ms": round(rtt, 1), "banner": text}
                results.append(entry)
                await job.progress(round(done * 100 / total, 1), f"{done}/{total} ports", found=entry, counts=dict(counts))
                last_emit = time.perf_counter()
            elif time.perf_counter() - last_emit > 0.25 or done == total:
                last_emit = time.perf_counter()
                await job.progress(round(done * 100 / total, 1), f"{done}/{total} ports", counts=dict(counts))

    tasks = [asyncio.create_task(worker()) for _ in range(min(concurrency, total))]
    try:
        await asyncio.gather(*tasks)
    except asyncio.CancelledError:
        for t in tasks:
            t.cancel()
        raise
    results.sort(key=lambda r: r["port"])
    return {
        "host": host, "ip": ip, "scanned": total, "counts": counts, "open": results,
        "elapsed_s": round(time.perf_counter() - started, 2),
    }


# ── Host discovery ──────────────────────────────────────────────────────────

def _pool() -> ThreadPoolExecutor:
    global _ping_pool
    if _ping_pool is None:
        workers = _clamp(module.config.get("ping_workers", 64), 4, 256, 64)
        _ping_pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="ps-ping")
    return _ping_pool


def _ping(ip: str, timeout_ms: int) -> float | None:
    if sys.platform == "win32":
        cmd = ["ping", "-n", "1", "-w", str(timeout_ms), ip]
    else:
        cmd = ["ping", "-c", "1", "-W", str(max(1, timeout_ms // 1000)), ip]
    started = time.perf_counter()
    try:
        out = subprocess.run(cmd, capture_output=True, timeout=timeout_ms / 1000 + 3, creationflags=NO_WINDOW).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    text = out.decode("cp850", errors="replace")
    if not re.search(r"ttl[=\s]", text, re.I):
        return None
    m = re.search(r"[=<](\d+(?:[.,]\d+)?)\s*ms", text)
    if m:
        return float(m.group(1).replace(",", "."))
    return round((time.perf_counter() - started) * 1000, 1)


def parse_arp(text: str) -> dict[str, str]:
    table = {}
    for m in re.finditer(r"(\d{1,3}(?:\.\d{1,3}){3})\s+(?:\S+\s+)?\(?\s*([0-9a-fA-F]{2}(?:[-:][0-9a-fA-F]{2}){5})", text):
        mac = m.group(2).upper().replace("-", ":")
        if mac in ("FF:FF:FF:FF:FF:FF", "00:00:00:00:00:00") or mac.startswith("01:00:5E"):
            continue
        table[m.group(1)] = mac
    return table


def _arp_table() -> dict[str, str]:
    try:
        out = subprocess.run(["arp", "-a"], capture_output=True, timeout=10, creationflags=NO_WINDOW).stdout
    except (OSError, subprocess.SubprocessError):
        return {}
    return parse_arp(out.decode("cp850", errors="replace"))


def _reverse_dns(ip: str) -> str:
    try:
        return socket.gethostbyaddr(ip)[0]
    except (OSError, UnicodeError):
        return ""


def parse_cidr(cidr: str) -> list[str]:
    try:
        net = ipaddress.ip_network(cidr.strip(), strict=False)
    except ValueError:
        raise ScanError("BAD_CIDR", f"Plage CIDR invalide : « {cidr} »")
    if net.version != 4:
        raise ScanError("BAD_CIDR", "Seules les plages IPv4 sont prises en charge")
    if net.prefixlen < MIN_PREFIX:
        raise ScanError("RANGE_TOO_LARGE", f"Plage trop grande (/{net.prefixlen}) : maximum /{MIN_PREFIX} (1024 adresses)")
    hosts = list(net.hosts()) or [net.network_address]
    return [str(h) for h in hosts]


@module.job("discover")
async def discover(payload: dict, job):
    try:
        ips = parse_cidr(str(payload.get("cidr", "")))
    except ScanError as exc:
        raise ValueError(exc.message)
    timeout_ms = _clamp(payload.get("timeout_ms"), 100, 5000, 700)
    resolve = payload.get("resolve", True) is not False
    loop = asyncio.get_running_loop()
    pool = _pool()
    total = len(ips)
    alive: dict[str, dict] = {}
    done = 0
    last_emit = 0.0
    started = time.perf_counter()
    log.info("discover %s (%d hôtes)", payload.get("cidr"), total)

    async def one(ip: str):
        nonlocal done, last_emit
        rtt = await loop.run_in_executor(pool, _ping, ip, timeout_ms)
        done += 1
        if rtt is not None:
            alive[ip] = {"ip": ip, "rtt_ms": rtt, "via": "ping"}
            await job.progress(round(done * 90 / total, 1), f"Ping {done}/{total}", found={"ip": ip, "rtt_ms": rtt}, alive=len(alive))
            last_emit = time.perf_counter()
        elif time.perf_counter() - last_emit > 0.3:
            last_emit = time.perf_counter()
            await job.progress(round(done * 90 / total, 1), f"Ping {done}/{total}", alive=len(alive))

    tasks = [asyncio.create_task(one(ip)) for ip in ips]
    try:
        await asyncio.gather(*tasks)
    except asyncio.CancelledError:
        for t in tasks:
            t.cancel()
        raise

    await job.progress(91, "Lecture de la table ARP…", alive=len(alive))
    arp = await asyncio.to_thread(_arp_table)
    wanted = set(ips)
    for ip, mac in arp.items():
        if ip in wanted and ip not in alive:
            alive[ip] = {"ip": ip, "rtt_ms": None, "via": "arp"}
    local = _local_ip()
    for ip, entry in alive.items():
        mac = arp.get(ip, "")
        entry["mac"] = mac
        entry["vendor"] = vendor_for(mac) if mac else ""
        entry["self"] = ip == local

    if resolve and alive:
        await job.progress(94, "Résolution DNS inverse…", alive=len(alive))
        sem = asyncio.Semaphore(16)

        async def rdns(entry: dict):
            async with sem:
                try:
                    entry["hostname"] = await asyncio.wait_for(loop.run_in_executor(pool, _reverse_dns, entry["ip"]), 4)
                except asyncio.TimeoutError:
                    entry["hostname"] = ""
        await asyncio.gather(*(rdns(e) for e in alive.values()))

    hosts = sorted(alive.values(), key=lambda e: ipaddress.IPv4Address(e["ip"]))
    for e in hosts:
        e.setdefault("hostname", "")
    return {"cidr": str(payload.get("cidr")), "scanned": total, "hosts": hosts,
            "elapsed_s": round(time.perf_counter() - started, 2)}


@module.on_shutdown
async def shutdown():
    global _ping_pool
    if _ping_pool is not None:
        _ping_pool.shutdown(wait=False, cancel_futures=True)
        _ping_pool = None
