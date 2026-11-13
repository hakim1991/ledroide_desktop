import ipaddress
import os
import re
import socket
import struct
import subprocess
import sys
import time

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
MIN_PREFIX = 22
IS_WINDOWS = sys.platform == "win32"

OUI = {
    "00:50:56": "VMware", "00:0C:29": "VMware", "00:05:69": "VMware", "08:00:27": "VirtualBox",
    "52:54:00": "QEMU/KVM", "00:15:5D": "Microsoft Hyper-V", "00:50:F2": "Microsoft", "28:18:78": "Microsoft",
    "7C:1E:52": "Microsoft", "C8:3F:26": "Microsoft",
    "B8:27:EB": "Raspberry Pi", "DC:A6:32": "Raspberry Pi", "E4:5F:01": "Raspberry Pi", "D8:3A:DD": "Raspberry Pi",
    "2C:CF:67": "Raspberry Pi", "28:CD:C1": "Raspberry Pi",
    "00:03:93": "Apple", "00:0A:95": "Apple", "00:1B:63": "Apple", "00:1E:C2": "Apple", "00:25:00": "Apple",
    "28:CF:E9": "Apple", "3C:07:54": "Apple", "40:6C:8F": "Apple", "70:56:81": "Apple", "78:31:C1": "Apple",
    "A4:5E:60": "Apple", "AC:BC:32": "Apple", "F0:18:98": "Apple", "BC:D0:74": "Apple", "F4:0F:24": "Apple",
    "D0:81:7A": "Apple", "8C:85:90": "Apple", "A8:66:7F": "Apple", "3C:22:FB": "Apple", "14:7D:DA": "Apple",
    "00:12:47": "Samsung", "00:16:32": "Samsung", "00:1D:25": "Samsung", "5C:0A:5B": "Samsung", "8C:77:12": "Samsung",
    "F0:25:B7": "Samsung", "BC:72:B1": "Samsung", "A0:CB:FD": "Samsung", "64:1C:AE": "Samsung", "E8:50:8B": "Samsung",
    "00:1B:21": "Intel", "00:13:E8": "Intel", "3C:97:0E": "Intel", "A0:36:9F": "Intel", "8C:8D:28": "Intel",
    "F8:94:C2": "Intel", "DC:21:5C": "Intel", "E8:94:F6": "TP-Link", "48:51:C5": "Intel", "A4:C3:F0": "Intel",
    "00:00:0C": "Cisco", "00:1B:54": "Cisco", "00:40:96": "Cisco",
    "50:C7:BF": "TP-Link", "14:CC:20": "TP-Link", "F4:F2:6D": "TP-Link", "C0:4A:00": "TP-Link", "EC:08:6B": "TP-Link",
    "98:DA:C4": "TP-Link", "60:32:B1": "TP-Link", "B0:95:75": "TP-Link",
    "00:09:5B": "Netgear", "00:14:6C": "Netgear", "00:24:B2": "Netgear", "A0:40:A0": "Netgear",
    "00:05:5D": "D-Link", "00:1B:11": "D-Link", "1C:7E:E5": "D-Link",
    "00:15:6D": "Ubiquiti", "00:27:22": "Ubiquiti", "24:A4:3C": "Ubiquiti", "44:D9:E7": "Ubiquiti",
    "78:8A:20": "Ubiquiti", "F0:9F:C2": "Ubiquiti", "FC:EC:DA": "Ubiquiti",
    "24:0A:C4": "Espressif", "30:AE:A4": "Espressif", "84:F3:EB": "Espressif", "A4:CF:12": "Espressif",
    "EC:FA:BC": "Espressif", "5C:CF:7F": "Espressif", "24:6F:28": "Espressif", "08:3A:F2": "Espressif",
    "48:3F:DA": "Espressif", "C4:5B:BE": "Espressif",
    "00:11:32": "Synology", "90:09:D0": "Synology", "00:08:9B": "QNAP", "24:5E:BE": "QNAP",
    "00:1E:0B": "HP", "3C:D9:2B": "HP", "9C:8E:99": "HP", "A0:D3:C1": "HP", "64:51:06": "HP",
    "00:00:48": "Epson", "64:EB:8C": "Epson", "00:1B:A9": "Brother", "00:80:77": "Brother", "00:00:85": "Canon",
    "00:1E:8F": "Canon",
    "00:14:22": "Dell", "18:03:73": "Dell", "B8:AC:6F": "Dell", "F8:B1:56": "Dell",
    "00:23:24": "Lenovo", "54:EE:75": "Lenovo", "E8:6A:64": "Lenovo",
    "00:E0:4C": "Realtek", "52:54:AB": "Realtek", "00:1A:11": "Google", "F4:F5:D8": "Google", "3C:5A:B4": "Google",
    "54:60:09": "Google", "D8:6C:63": "Google", "48:D6:D5": "Google",
    "64:09:80": "Xiaomi", "28:6C:07": "Xiaomi", "78:11:DC": "Xiaomi", "50:EC:50": "Xiaomi",
    "00:E0:FC": "Huawei", "00:18:82": "Huawei", "28:6E:D4": "Huawei",
    "F0:27:2D": "Amazon", "74:C2:46": "Amazon", "44:65:0D": "Amazon", "FC:65:DE": "Amazon", "68:54:FD": "Amazon",
    "00:0E:58": "Sonos", "5C:AA:FD": "Sonos", "94:9F:3E": "Sonos", "48:A6:B8": "Sonos",
    "00:04:0E": "AVM (Fritz!Box)", "3C:A6:2F": "AVM (Fritz!Box)", "C8:0E:14": "AVM (Fritz!Box)",
    "00:07:CB": "Freebox", "F4:CA:E5": "Freebox", "68:A3:78": "Freebox", "14:0C:76": "Freebox",
    "64:9C:F3": "Sagemcom", "00:24:D4": "Sagemcom", "E8:BE:81": "Sagemcom", "30:93:BC": "Sagemcom",
    "7C:03:4C": "Sagemcom", "34:8A:AE": "Sagemcom", "A0:1B:29": "Sagemcom", "00:1F:9F": "Thomson",
    "00:09:BF": "Nintendo", "00:17:AB": "Nintendo", "98:B6:E9": "Nintendo", "00:D9:D1": "Sony", "FC:0F:E6": "Sony",
    "28:3F:69": "Sony", "00:1D:BA": "Sony", "00:17:88": "Philips Hue", "EC:B5:FA": "Philips Hue",
    "D0:73:D5": "LIFX", "18:B4:30": "Nest", "64:16:66": "Nest", "B0:C5:54": "D-Link", "9C:53:22": "TP-Link",
    "4C:11:AE": "Espressif", "BC:DD:C2": "Espressif", "00:62:6E": "Shenzhen", "38:2C:4A": "ASUS", "04:D9:F5": "ASUS",
    "2C:56:DC": "ASUS", "AC:22:0B": "ASUS", "00:1A:79": "Telefunken", "00:0D:4B": "Roku", "B0:A7:37": "Roku",
    "00:04:20": "Logitech", "00:1F:20": "Logitech", "AC:84:C6": "TP-Link", "C4:E9:84": "TP-Link",
}

COMMON_PORTS = {
    21: "ftp", 22: "ssh", 23: "telnet", 53: "dns", 80: "http", 139: "netbios", 443: "https", 445: "smb",
    515: "lpd", 548: "afp", 554: "rtsp", 631: "ipp", 1883: "mqtt", 3389: "rdp", 5000: "upnp/dev", 5900: "vnc",
    8008: "chromecast", 8080: "http-alt", 8123: "home-assistant", 8443: "https-alt", 9100: "imprimante", 32400: "plex",
}


def vendor_for(mac: str) -> str:
    mac = (mac or "").upper().replace("-", ":")
    if len(mac) < 8:
        return ""
    try:
        if int(mac[:2], 16) & 0x02:
            return "Adresse aléatoire (privée)"
    except ValueError:
        return ""
    return OUI.get(mac[:8], "")


def _run(cmd: list[str], timeout: float = 10) -> str:
    try:
        out = subprocess.run(cmd, capture_output=True, timeout=timeout, creationflags=NO_WINDOW).stdout
    except (OSError, subprocess.SubprocessError):
        return ""
    return out.decode("cp850" if IS_WINDOWS else "utf-8", errors="replace")


def local_ip() -> str | None:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("192.0.2.1", 9))
        return s.getsockname()[0]
    except OSError:
        return None
    finally:
        s.close()


def parse_ipconfig(text: str, ip: str) -> dict:
    """Mask, gateway and MAC of the adapter block holding `ip` (ipconfig /all, any language)."""
    blocks = re.split(r"\r?\n(?=\S)", text)
    for b in blocks:
        if not re.search(rf"(?<![\d.]){re.escape(ip)}(?![\d.])", b):
            continue
        mask = re.search(r"(255\.\d{1,3}\.\d{1,3}\.\d{1,3})", b)
        mac = re.search(r":\s*([0-9A-Fa-f]{2}(?:-[0-9A-Fa-f]{2}){5})\s*$", b, re.M)
        gw = None
        lines = b.splitlines()
        for i, line in enumerate(lines):
            if re.search(r"(passerelle|gateway)", line, re.I):
                m = re.search(r"(\d{1,3}(?:\.\d{1,3}){3})", line)
                if not m:
                    for nxt in lines[i + 1:i + 3]:
                        m = re.search(r"^\s+(\d{1,3}(?:\.\d{1,3}){3})\s*$", nxt)
                        if m:
                            break
                if m:
                    gw = m.group(1)
                break
        name = b.splitlines()[0].strip().rstrip(":").strip() if b.strip() else ""
        return {"mask": mask.group(1) if mask else None, "gateway": gw,
                "mac": mac.group(1).upper().replace("-", ":") if mac else "", "adapter": name}
    return {"mask": None, "gateway": None, "mac": "", "adapter": ""}


def detect_network() -> dict:
    ip = local_ip()
    if not ip:
        return {"ip": None, "cidr": "192.168.1.0/24", "prefix": None, "gateway": None, "mac": "", "adapter": ""}
    info = parse_ipconfig(_run(["ipconfig", "/all"]), ip) if IS_WINDOWS else {"mask": None, "gateway": None, "mac": "", "adapter": ""}
    prefix = None
    if info["mask"]:
        try:
            prefix = ipaddress.IPv4Network(f"0.0.0.0/{info['mask']}").prefixlen
        except ValueError:
            prefix = None
    use = prefix if prefix and prefix >= MIN_PREFIX else 24
    net = ipaddress.ip_network(f"{ip}/{use}", strict=False)
    return {"ip": ip, "cidr": str(net), "prefix": prefix, "gateway": info["gateway"], "mac": info["mac"],
            "adapter": info["adapter"]}


def parse_cidr(cidr: str) -> list[str]:
    try:
        net = ipaddress.ip_network((cidr or "").strip(), strict=False)
    except ValueError:
        raise ValueError(f"Plage CIDR invalide : « {cidr} »")
    if net.version != 4:
        raise ValueError("Seules les plages IPv4 sont prises en charge")
    if not net.is_private:
        raise ValueError("Seuls les réseaux privés (192.168.x, 10.x, 172.16-31.x) peuvent être balayés")
    if net.prefixlen < MIN_PREFIX:
        raise ValueError(f"Plage trop grande (/{net.prefixlen}) : maximum /{MIN_PREFIX} (1024 adresses)")
    return [str(h) for h in (list(net.hosts()) or [net.network_address])]


def ping(ip: str, timeout_ms: int) -> float | None:
    if IS_WINDOWS:
        cmd = ["ping", "-n", "1", "-w", str(timeout_ms), ip]
    else:
        cmd = ["ping", "-c", "1", "-W", str(max(1, timeout_ms // 1000)), ip]
    started = time.perf_counter()
    text = _run(cmd, timeout_ms / 1000 + 3)
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


def arp_table() -> dict[str, str]:
    return parse_arp(_run(["arp", "-a"]))


def reverse_dns(ip: str) -> str:
    try:
        name = socket.gethostbyaddr(ip)[0]
    except (OSError, UnicodeError):
        return ""
    return "" if name == ip else name


def _nb_encode(name: bytes) -> bytes:
    name = name.ljust(16, b"\x00") if name == b"*" else name.ljust(15, b" ") + b"\x00"
    return b"\x20" + bytes(c for b in name for c in ((b >> 4) + 0x41, (b & 0x0F) + 0x41)) + b"\x00"


def netbios_name(ip: str, timeout: float = 1.0) -> str:
    """NBSTAT query (UDP 137): returns the machine's NetBIOS name, or ''."""
    tid = os.urandom(2)
    pkt = tid + b"\x00\x00\x00\x01\x00\x00\x00\x00\x00\x00" + _nb_encode(b"*") + b"\x00\x21\x00\x01"
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(timeout)
    try:
        s.sendto(pkt, (ip, 137))
        data, _ = s.recvfrom(2048)
    except OSError:
        return ""
    finally:
        s.close()
    return parse_nbstat(data, tid)


def parse_nbstat(data: bytes, tid: bytes | None = None) -> str:
    if len(data) < 57 or (tid and data[:2] != tid):
        return ""
    off = 12
    while off < len(data) and data[off] != 0:
        off += data[off] + 1
    off += 1 + 10
    if off + 2 > len(data):
        return ""
    off += 2
    if off >= len(data):
        return ""
    count = data[off]
    off += 1
    for i in range(count):
        e = data[off + i * 18: off + i * 18 + 18]
        if len(e) < 18:
            break
        name, suffix, flags = e[:15].decode("latin-1").strip(), e[15], struct.unpack(">H", e[16:18])[0]
        if suffix == 0 and not flags & 0x8000 and name:
            return "".join(c for c in name if c.isprintable())
    return ""


def _dns_name(data: bytes, off: int, depth: int = 0) -> tuple[str, int]:
    labels, jumped, end = [], False, off
    while off < len(data) and depth < 20:
        n = data[off]
        if n == 0:
            off += 1
            break
        if n & 0xC0 == 0xC0:
            if off + 1 >= len(data):
                return "", len(data)
            ptr = ((n & 0x3F) << 8) | data[off + 1]
            if not jumped:
                end = off + 2
            jumped, off, depth = True, ptr, depth + 1
            continue
        labels.append(data[off + 1: off + 1 + n].decode("utf-8", errors="replace"))
        off += n + 1
    return ".".join(labels), (end if jumped else off)


def mdns_name(ip: str, timeout: float = 1.0) -> str:
    """Unicast PTR query to <ip>:5353 (answered by Apple/Avahi/Android devices)."""
    rev = ".".join(reversed(ip.split("."))) + ".in-addr.arpa"
    q = b"".join(bytes([len(p)]) + p.encode() for p in rev.split(".")) + b"\x00"
    tid = os.urandom(2)
    pkt = tid + b"\x00\x00\x00\x01\x00\x00\x00\x00\x00\x00" + q + b"\x00\x0c\x00\x01"
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(timeout)
    try:
        s.sendto(pkt, (ip, 5353))
        data, _ = s.recvfrom(4096)
    except OSError:
        return ""
    finally:
        s.close()
    return parse_mdns_ptr(data)


def parse_mdns_ptr(data: bytes) -> str:
    if len(data) < 12:
        return ""
    qd, an = struct.unpack(">HH", data[4:8])
    off = 12
    for _ in range(qd):
        _, off = _dns_name(data, off)
        off += 4
    for _ in range(an):
        _, off = _dns_name(data, off)
        if off + 10 > len(data):
            return ""
        rtype, _cls, _ttl, rdlen = struct.unpack(">HHIH", data[off:off + 10])
        off += 10
        if rtype == 12:
            name, _ = _dns_name(data, off)
            return name.rstrip(".")
        off += rdlen
    return ""


def guess_type(dev: dict) -> str:
    ports = set(dev.get("ports") or [])
    vendor = (dev.get("vendor") or "").lower()
    host = " ".join([dev.get("hostname") or "", dev.get("netbios") or "", dev.get("mdns") or ""]).lower()
    if dev.get("is_gateway"):
        return "routeur"
    if dev.get("is_self"):
        return "ordinateur"
    if ports & {9100, 631, 515} or any(v in vendor for v in ("epson", "brother", "canon")) or "printer" in host:
        return "imprimante"
    if 554 in ports or "cam" in host:
        return "camera"
    if any(v in vendor for v in ("synology", "qnap")) or "nas" in host:
        return "nas"
    if any(v in vendor for v in ("espressif", "philips hue", "lifx", "nest", "shenzhen")) or 1883 in ports:
        return "objet"
    if any(v in vendor for v in ("sonos", "roku", "google", "amazon")) or 8008 in ports or 32400 in ports:
        return "multimedia"
    if any(v in vendor for v in ("nintendo", "sony")):
        return "console"
    if ports & {3389} or (ports & {445, 139} and dev.get("netbios")):
        return "ordinateur"
    if any(v in vendor for v in ("tp-link", "netgear", "d-link", "ubiquiti", "cisco", "avm", "freebox", "sagemcom", "thomson")):
        return "reseau"
    if any(k in host for k in ("iphone", "android", "galaxy", "pixel", "phone", "ipad")):
        return "mobile"
    if ports & {3389, 445, 139} or any(v in vendor for v in ("dell", "lenovo", "intel", "hp", "asus")) or dev.get("netbios"):
        return "ordinateur"
    if "raspberry" in vendor or 22 in ports:
        return "serveur"
    if "apple" in vendor:
        return "apple"
    return "inconnu"
