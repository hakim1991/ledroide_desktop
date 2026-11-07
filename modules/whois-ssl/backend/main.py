"""WHOIS & SSL — WHOIS domaine/IP, certificats TLS, en-têtes HTTP de sécurité, surveillance des expirations."""
import asyncio
import ctypes
import hashlib
import http.client
import ipaddress
import os
import re
import socket
import sqlite3
import ssl
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import urljoin, urlsplit

from ledroide_sdk.http import JSONResponse
from pydantic import BaseModel
from ledroide_sdk import Module, data_path

module = Module(__file__)
router = module.router

DB_PATH = Path(os.environ.get("LEDROIDE_WHOIS_SSL_DB") or (data_path(__file__, "whois-ssl.db")))
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) LedroideDesktop-whois-ssl/1.0"
WHOIS_TIMEOUT = 10
TLS_TIMEOUT = 8
HTTP_TIMEOUT = 10
MAX_WHOIS_BYTES = 1_000_000
HOST_RE = re.compile(r"^(?=.{1,253}$)(?!-)[A-Za-z0-9-]{1,63}(?<!-)(\.(?!-)[A-Za-z0-9-]{1,63}(?<!-))*\.?$")
# Common multi-label public suffixes (enough to find the registrable domain in most cases)
MULTI_SUFFIXES = {
    "co.uk", "org.uk", "me.uk", "ac.uk", "gov.uk", "ltd.uk", "plc.uk", "net.uk", "com.au", "net.au", "org.au", "edu.au",
    "co.nz", "org.nz", "co.jp", "ne.jp", "or.jp", "ac.jp", "co.kr", "or.kr", "com.br", "net.br", "org.br", "com.cn", "net.cn",
    "org.cn", "com.mx", "com.ar", "com.tr", "com.tw", "com.hk", "com.sg", "com.my", "co.in", "net.in", "org.in", "co.za",
    "org.za", "com.dz", "net.dz", "org.dz", "gov.dz", "edu.dz", "co.ma", "net.ma", "org.ma", "gov.ma", "com.tn", "com.eg",
    "com.sa", "com.pl", "com.es", "com.pt", "co.il", "co.id", "com.ua", "com.ru", "co.th", "gouv.fr", "asso.fr", "com.fr",
}


def _err(msg: str, status: int = 400, code: Optional[str] = None):
    c = {"error": msg}
    if code:
        c["code"] = code
    return JSONResponse(status_code=status, content=c)


def _is_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def _has(mod: str) -> bool:
    try:
        __import__(mod)
        return True
    except ImportError:
        return False


# ── Input validation ─────────────────────────────────────────────────────────

def _clean_host(raw: str) -> Optional[str]:
    """Accept a hostname, URL or IP; return an ASCII (IDNA) host or IP string, or None."""
    s = (raw or "").strip()
    if not s:
        return None
    if "://" in s:
        s = urlsplit(s).hostname or ""
    else:
        s = s.split("/")[0]
        if s.count(":") == 1:
            s = s.split(":")[0]
    s = s.strip("[]").strip().rstrip(".").lower()
    try:
        return str(ipaddress.ip_address(s))
    except ValueError:
        pass
    try:
        s = s.encode("idna").decode("ascii")
    except UnicodeError:
        return None
    return s if HOST_RE.match(s) and "." in s else None


def _is_ip(s: str) -> bool:
    try:
        ipaddress.ip_address(s)
        return True
    except ValueError:
        return False


def registrable_domain(host: str) -> str:
    labels = host.split(".")
    if len(labels) <= 2:
        return host
    if ".".join(labels[-2:]) in MULTI_SUFFIXES:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


# ── WHOIS ────────────────────────────────────────────────────────────────────

def whois_query(server: str, query: str, timeout: int = WHOIS_TIMEOUT) -> str:
    deadline = time.time() + timeout + 5
    with socket.create_connection((server, 43), timeout=timeout) as s:
        s.sendall((query + "\r\n").encode("utf-8"))
        chunks, total = [], 0
        while time.time() < deadline:
            b = s.recv(65536)
            if not b:
                break
            chunks.append(b)
            total += len(b)
            if total > MAX_WHOIS_BYTES:
                break
    data = b"".join(chunks)
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("latin-1")


def _kv_lines(text: str):
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith(("%", "#", ">>>")):
            continue
        m = re.match(r"^([A-Za-z][A-Za-z0-9 /()._-]{0,60}?)\s*:\s*(.*)$", line)
        if m:
            yield m.group(1).strip().lower(), m.group(2).strip()


def _first(text: str, keys) -> Optional[str]:
    for k, v in _kv_lines(text):
        if k in keys and v:
            return v
    return None


def _all(text: str, keys) -> list:
    out = []
    for k, v in _kv_lines(text):
        if k in keys and v and v not in out:
            out.append(v)
    return out


def _server_from(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    v = value.strip()
    v = re.sub(r"^(r?whois|https?)://", "", v, flags=re.I).split("/")[0].split(":")[0].strip().lower()
    return v if HOST_RE.match(v) else None


_MONTHS = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}


def parse_date(s: Optional[str]) -> Optional[str]:
    """Best-effort WHOIS date parsing → ISO date (YYYY-MM-DD) or None."""
    if not s:
        return None
    s = s.strip()
    m = re.search(r"(\d{4})[-./](\d{1,2})[-./](\d{1,2})", s)
    if m:
        y, mo, d = map(int, m.groups())
    else:
        m = re.search(r"(\d{1,2})[-\s./]([A-Za-z]{3})[A-Za-z]*[-\s./,]+(\d{4})", s)
        if m and m.group(2).lower() in _MONTHS:
            d, mo, y = int(m.group(1)), _MONTHS[m.group(2).lower()], int(m.group(3))
        else:
            m = re.search(r"([A-Za-z]{3})[A-Za-z]*\s+(\d{1,2}),?\s+(\d{4})", s)
            if m and m.group(1).lower() in _MONTHS:
                mo, d, y = _MONTHS[m.group(1).lower()], int(m.group(2)), int(m.group(3))
            else:
                m = re.search(r"(\d{1,2})[./](\d{1,2})[./](\d{4})", s)
                if not m:
                    return None
                d, mo, y = map(int, m.groups())
    try:
        return datetime(y, mo, d).strftime("%Y-%m-%d")
    except ValueError:
        return None


def _days_until(iso: Optional[str]) -> Optional[int]:
    if not iso:
        return None
    try:
        dt = datetime.strptime(iso[:10], "%Y-%m-%d").date()
    except ValueError:
        return None
    return (dt - datetime.now().date()).days


K_REGISTRAR = {"registrar", "sponsoring registrar", "registrar name", "registrar organization", "registrar-name"}
K_CREATED = {"creation date", "created", "created on", "registered on", "registration time", "domain registration date",
             "registered", "domain record activated", "created date", "registration date", "domain created", "commencement date"}
K_EXPIRY = {"registry expiry date", "registrar registration expiration date", "expiry date", "expiration date", "expires",
            "expires on", "paid-till", "renewal date", "expiration time", "domain expiration date", "domain expires",
            "record expires on", "expire date", "valid until", "registry expiration date"}
K_UPDATED = {"updated date", "last updated", "last-update", "changed", "last modified", "modified", "last update",
             "domain record last updated", "updated", "last updated on"}
K_STATUS = {"domain status", "status", "state", "eppstatus"}
K_NS = {"name server", "nserver", "nameserver", "name servers", "nameservers", "dns", "host name"}
K_DNSSEC = {"dnssec", "dnssec status", "signed"}


def parse_domain_whois(texts: list) -> dict:
    """texts: most specific first (registrar, then registry)."""
    joined = "\n".join(texts)

    def first(keys):
        for t in texts:
            v = _first(t, keys)
            if v:
                return v
        return None

    created, expiry, updated = first(K_CREATED), first(K_EXPIRY), first(K_UPDATED)
    statuses = []
    for v in _all(joined, K_STATUS):
        code = v.split()[0] if v.split() else v
        if code not in statuses:
            statuses.append(code)
    ns = []
    for v in _all(joined, K_NS):
        h = v.split()[0].rstrip(".").lower()
        if HOST_RE.match(h) and "." in h and h not in ns:
            ns.append(h)
    exp_iso = parse_date(expiry)
    return {
        "registrar": first(K_REGISTRAR),
        "created": parse_date(created), "created_raw": created,
        "expires": exp_iso, "expires_raw": expiry, "days_left": _days_until(exp_iso),
        "updated": parse_date(updated), "updated_raw": updated,
        "status": statuses[:12], "name_servers": ns[:13], "dnssec": first(K_DNSSEC),
        "registrant_country": first({"registrant country", "country"}),
        "abuse_email": first({"registrar abuse contact email", "abuse-mailbox"}),
    }


def _not_found(text: str) -> bool:
    t = text.lower()
    return any(p in t for p in ("no match for", "not found", "no data found", "no entries found", "status: free",
                                "domain not found", "is available for registration", "no object found"))


def _tld_query(server: str, domain: str) -> str:
    if server == "whois.denic.de":
        return f"-T dn,ace {domain}"
    if server == "whois.jprs.jp":
        return f"{domain}/e"
    if server == "whois.verisign-grs.com":
        return f"domain {domain}"
    return domain


def whois_domain(domain: str) -> dict:
    chain = []
    iana = whois_query("whois.iana.org", domain)
    chain.append({"server": "whois.iana.org", "query": domain, "text": iana})
    tld_server = _server_from(_first(iana, {"refer", "whois"}))
    if not tld_server and "." in domain:
        # IANA may answer for the domain itself (e.g. reserved names): ask for the TLD instead
        try:
            t = whois_query("whois.iana.org", domain.rsplit(".", 1)[1])
            tld_server = _server_from(_first(t, {"refer", "whois"}))
        except OSError:
            pass
    registrar_server = None
    if tld_server:
        q = _tld_query(tld_server, domain)
        try:
            t = whois_query(tld_server, q)
            chain.append({"server": tld_server, "query": q, "text": t})
            registrar_server = _server_from(_first(t, {"registrar whois server", "whois server", "whois"}))
            if registrar_server and registrar_server != tld_server and not _not_found(t):
                try:
                    t2 = whois_query(registrar_server, domain)
                    chain.append({"server": registrar_server, "query": domain, "text": t2})
                except OSError as e:
                    chain.append({"server": registrar_server, "query": domain, "text": "", "error": f"{e}"})
        except OSError as e:
            chain.append({"server": tld_server, "query": q, "text": "", "error": f"{e}"})
    texts = [c["text"] for c in reversed(chain[1:]) if c.get("text")]
    parsed = parse_domain_whois(texts or [""])
    found = bool(texts) and not _not_found(chain[1]["text"])
    source = "WHOIS" if texts else None
    note = None
    if not parsed["expires"] and (found or not texts):
        # Registry without port-43 WHOIS or without dates (e.g. .uk): fall back to RDAP
        try:
            r = rdap_domain(domain)
        except Exception as e:
            r = None
            note = f"RDAP indisponible : {e}"
        if r:
            raw = r.pop("_raw", "")
            for k, v in r.items():
                if v and not parsed.get(k):
                    parsed[k] = v
            parsed["days_left"] = _days_until(parsed["expires"])
            found, source = True, ("WHOIS + RDAP" if texts else "RDAP")
            chain.append({"server": "rdap.org", "query": domain, "text": raw})
    if not found and not tld_server:
        note = note or "Aucun serveur WHOIS connu pour cette extension."
    return {"kind": "domain", "query": domain, "found": found, "tld_server": tld_server, "registrar_server": registrar_server,
            "parsed": parsed, "chain": chain, "source": source, "lookup_note": note}


def rdap_domain(domain: str) -> Optional[dict]:
    """Query rdap.org (redirects to the authoritative RDAP server). Returns parsed fields, or None if not found."""
    import json
    import urllib.error
    import urllib.request
    req = urllib.request.Request(f"https://rdap.org/domain/{domain}", headers={"User-Agent": UA, "Accept": "application/rdap+json"})
    try:
        with urllib.request.urlopen(req, timeout=WHOIS_TIMEOUT, context=_verify_context()[0]) as resp:
            raw = resp.read(MAX_WHOIS_BYTES).decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise
    d = json.loads(raw)
    ev = {e.get("eventAction"): e.get("eventDate") for e in d.get("events", []) or [] if isinstance(e, dict)}
    registrar = None
    for ent in d.get("entities", []) or []:
        if "registrar" in (ent.get("roles") or []):
            vcard = ent.get("vcardArray") or [None, []]
            for item in (vcard[1] if len(vcard) > 1 else []):
                if isinstance(item, list) and len(item) > 3 and item[0] == "fn":
                    registrar = item[3]
            registrar = registrar or ent.get("handle")
    sec = d.get("secureDNS") or {}
    return {
        "registrar": registrar,
        "created": parse_date(ev.get("registration")), "created_raw": ev.get("registration"),
        "expires": parse_date(ev.get("expiration")), "expires_raw": ev.get("expiration"),
        "updated": parse_date(ev.get("last changed")), "updated_raw": ev.get("last changed"),
        "status": d.get("status") or [],
        "name_servers": [str(n.get("ldhName")).lower().rstrip(".") for n in d.get("nameservers", []) or [] if n.get("ldhName")],
        "dnssec": ("signé" if sec.get("delegationSigned") else "non signé") if "delegationSigned" in sec else None,
        "_raw": json.dumps(d, ensure_ascii=False, indent=1)[:200000],
    }


def parse_ip_whois(text: str) -> dict:
    return {
        "range": _first(text, {"netrange", "inetnum", "inet6num"}),
        "cidr": _first(text, {"cidr", "route", "route6"}),
        "name": _first(text, {"netname", "net-name"}),
        "org": _first(text, {"orgname", "org-name", "organization", "owner", "descr"}),
        "country": _first(text, {"country"}),
        "origin_as": _first(text, {"originas", "origin", "aut-num"}),
        "created": parse_date(_first(text, {"regdate", "created"})),
        "updated": parse_date(_first(text, {"updated", "last-modified", "changed"})),
        "abuse": _first(text, {"orgabuseemail", "abuse-mailbox", "abuse-c"}),
    }


def whois_ip(ip: str) -> dict:
    chain = []
    iana = whois_query("whois.iana.org", ip)
    chain.append({"server": "whois.iana.org", "query": ip, "text": iana})
    server = _server_from(_first(iana, {"refer", "whois"})) or "whois.arin.net"
    seen = {"whois.iana.org"}
    for _ in range(3):
        if server in seen:
            break
        seen.add(server)
        q = f"n + {ip}" if server == "whois.arin.net" else ip
        try:
            t = whois_query(server, q)
        except OSError as e:
            chain.append({"server": server, "query": q, "text": "", "error": str(e)})
            break
        chain.append({"server": server, "query": q, "text": t})
        nxt = _server_from(_first(t, {"referralserver", "rwhois"}))
        if not nxt or nxt.startswith("rwhois"):
            break
        server = nxt
    best = chain[-1]["text"] or iana
    return {"kind": "ip", "query": ip, "found": True, "parsed": parse_ip_whois(best), "chain": chain}


# ── TLS / certificates ───────────────────────────────────────────────────────

def _verify_context():
    try:
        import truststore
        return truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT), "Magasin Windows (truststore)"
    except ImportError:
        pass
    ctx = ssl.create_default_context()
    src = "Magasin Windows (Python)"
    try:
        import certifi
        ctx.load_verify_locations(certifi.where())
        src += " + certifi"
    except ImportError:
        pass
    return ctx, src


def _noverify_context():
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def _chain_der(s: ssl.SSLSocket) -> list:
    out = []
    try:
        enc = getattr(ssl._ssl, "ENCODING_DER", 2)
        for c in s._sslobj.get_unverified_chain() or []:
            out.append(c.public_bytes(enc))
    except Exception:
        pass
    if not out:
        der = s.getpeercert(binary_form=True)
        if der:
            out = [der]
    return out


def _match_hostname(host: str, dns_names: list, ip_names: list) -> bool:
    host = host.lower().rstrip(".")
    if _is_ip(host):
        return any(str(ipaddress.ip_address(host)) == str(i) for i in ip_names)
    for pat in dns_names:
        pat = pat.lower().rstrip(".")
        if pat == host:
            return True
        if pat.startswith("*.") and "." in host:
            left, rest = host.split(".", 1)
            if rest == pat[2:] and left:
                return True
    return False


def _parse_cert_crypto(der: bytes) -> dict:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import dsa, ec, ed448, ed25519, rsa
    from cryptography.x509.oid import NameOID

    c = x509.load_der_x509_certificate(der)

    def cn(name):
        a = name.get_attributes_for_oid(NameOID.COMMON_NAME)
        return a[0].value if a else None

    def org(name):
        a = name.get_attributes_for_oid(NameOID.ORGANIZATION_NAME)
        return a[0].value if a else None

    dns, ips = [], []
    try:
        san = c.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
        dns = san.get_values_for_type(x509.DNSName)
        ips = [str(i) for i in san.get_values_for_type(x509.IPAddress)]
    except x509.ExtensionNotFound:
        pass
    is_ca = False
    try:
        is_ca = c.extensions.get_extension_for_class(x509.BasicConstraints).value.ca
    except x509.ExtensionNotFound:
        pass
    k = c.public_key()
    if isinstance(k, rsa.RSAPublicKey):
        key = {"type": "RSA", "size": k.key_size}
    elif isinstance(k, ec.EllipticCurvePublicKey):
        key = {"type": f"ECDSA ({k.curve.name})", "size": k.key_size}
    elif isinstance(k, ed25519.Ed25519PublicKey):
        key = {"type": "Ed25519", "size": 256}
    elif isinstance(k, ed448.Ed448PublicKey):
        key = {"type": "Ed448", "size": 456}
    elif isinstance(k, dsa.DSAPublicKey):
        key = {"type": "DSA", "size": k.key_size}
    else:
        key = {"type": type(k).__name__, "size": None}
    nb = getattr(c, "not_valid_before_utc", None) or c.not_valid_before.replace(tzinfo=timezone.utc)
    na = getattr(c, "not_valid_after_utc", None) or c.not_valid_after.replace(tzinfo=timezone.utc)
    sig = getattr(c.signature_algorithm_oid, "_name", None) or c.signature_algorithm_oid.dotted_string
    return {
        "subject": c.subject.rfc4514_string(), "subject_cn": cn(c.subject), "subject_org": org(c.subject),
        "issuer": c.issuer.rfc4514_string(), "issuer_cn": cn(c.issuer), "issuer_org": org(c.issuer),
        "san_dns": dns, "san_ip": ips,
        "not_before": nb.isoformat(), "not_after": na.isoformat(),
        "days_left": int((na - datetime.now(timezone.utc)).total_seconds() // 86400),
        "key": key, "signature_algorithm": sig, "serial": format(c.serial_number, "X"),
        "sha256": c.fingerprint(hashes.SHA256()).hex(":").upper(), "sha1": c.fingerprint(hashes.SHA1()).hex(":").upper(),
        "is_ca": is_ca, "self_signed": c.subject == c.issuer, "version": c.version.name,
    }


def _parse_cert_basic(der: bytes) -> dict:
    """Fallback without `cryptography`: uses the stdlib private decoder."""
    info = {}
    fd, path = tempfile.mkstemp(suffix=".pem")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(ssl.DER_cert_to_PEM_cert(der))
        info = ssl._ssl._test_decode_cert(path)
    except Exception:
        info = {}
    finally:
        try:
            os.remove(path)
        except OSError:
            pass

    def name(tup):
        return ", ".join(f"{k}={v}" for rdn in (tup or ()) for k, v in rdn)

    def get(tup, key):
        for rdn in tup or ():
            for k, v in rdn:
                if k == key:
                    return v
        return None

    na = info.get("notAfter")
    nb = info.get("notBefore")
    na_dt = datetime.fromtimestamp(ssl.cert_time_to_seconds(na), timezone.utc) if na else None
    nb_dt = datetime.fromtimestamp(ssl.cert_time_to_seconds(nb), timezone.utc) if nb else None
    sans = info.get("subjectAltName", ())
    return {
        "subject": name(info.get("subject")), "subject_cn": get(info.get("subject"), "commonName"),
        "subject_org": get(info.get("subject"), "organizationName"),
        "issuer": name(info.get("issuer")), "issuer_cn": get(info.get("issuer"), "commonName"),
        "issuer_org": get(info.get("issuer"), "organizationName"),
        "san_dns": [v for k, v in sans if k == "DNS"], "san_ip": [v for k, v in sans if k == "IP Address"],
        "not_before": nb_dt.isoformat() if nb_dt else None, "not_after": na_dt.isoformat() if na_dt else None,
        "days_left": int((na_dt - datetime.now(timezone.utc)).total_seconds() // 86400) if na_dt else None,
        "key": {"type": None, "size": None}, "signature_algorithm": None, "serial": info.get("serialNumber"),
        "sha256": hashlib.sha256(der).hexdigest().upper(), "sha1": hashlib.sha1(der).hexdigest().upper(),
        "is_ca": None, "self_signed": info.get("subject") == info.get("issuer") if info else None, "version": None,
    }


def parse_cert(der: bytes) -> dict:
    try:
        return _parse_cert_crypto(der)
    except ImportError:
        d = _parse_cert_basic(der)
        d["basic"] = True
        return d


def fetch_cert(host: str, port: int = 443, timeout: int = TLS_TIMEOUT) -> dict:
    t0 = time.time()
    sni = None if _is_ip(host) else host
    ctx, trust_src = _verify_context()
    verified, verify_error = False, None
    s = None
    try:
        sock = socket.create_connection((host, port), timeout=timeout)
        try:
            s = ctx.wrap_socket(sock, server_hostname=sni or host)
            verified = True
        except ssl.SSLCertVerificationError as e:
            verify_error = getattr(e, "verify_message", None) or str(e)
            sock.close()
        except ssl.SSLError as e:
            sock.close()
            if "CERTIFICATE_VERIFY_FAILED" in str(e) or "verify" in str(e).lower():
                verify_error = str(e)
            else:
                raise
        except Exception:
            sock.close()
            raise
        if s is None:
            sock = socket.create_connection((host, port), timeout=timeout)
            try:
                s = _noverify_context().wrap_socket(sock, server_hostname=sni)
            except Exception:
                sock.close()
                raise
        with s:
            version = s.version()
            cipher = s.cipher()
            chain = _chain_der(s)
            peer_ip = s.getpeername()[0]
    finally:
        pass
    if not chain:
        raise RuntimeError("Aucun certificat reçu")
    certs = [parse_cert(d) for d in chain]
    leaf = certs[0]
    hostname_ok = _match_hostname(host, leaf.get("san_dns") or ([leaf["subject_cn"]] if leaf.get("subject_cn") else []), leaf.get("san_ip") or [])
    return {
        "host": host, "port": port, "ip": peer_ip, "tls_version": version,
        "cipher": {"name": cipher[0], "protocol": cipher[1], "bits": cipher[2]} if cipher else None,
        "verified": verified, "verify_error": verify_error, "trust_store": trust_src, "hostname_match": hostname_ok,
        "leaf": leaf, "chain": certs, "basic": bool(leaf.get("basic")), "elapsed_ms": int((time.time() - t0) * 1000),
    }


# ── HTTP headers ─────────────────────────────────────────────────────────────

SEC_HEADERS = [
    ("strict-transport-security", "HSTS (Strict-Transport-Security)",
     "Force le navigateur à n'utiliser que HTTPS pour ce site, empêchant les attaques de rétrogradation et le vol de cookies sur HTTP."),
    ("content-security-policy", "Content-Security-Policy",
     "Liste les sources autorisées de scripts, styles, images… : principale protection contre les injections XSS."),
    ("x-frame-options", "X-Frame-Options",
     "Empêche l'intégration du site dans une iframe tierce (protection contre le clickjacking). Alternative moderne : CSP frame-ancestors."),
    ("x-content-type-options", "X-Content-Type-Options",
     "Avec « nosniff », interdit au navigateur de deviner le type MIME (évite l'exécution de fichiers déguisés)."),
    ("referrer-policy", "Referrer-Policy",
     "Contrôle l'URL d'origine transmise aux autres sites ; limite la fuite d'URL contenant des données sensibles."),
    ("permissions-policy", "Permissions-Policy",
     "Restreint l'accès aux fonctions du navigateur (caméra, micro, géolocalisation…) pour le site et ses iframes."),
]


def _hget(headers: list, name: str) -> Optional[str]:
    vals = [v for k, v in headers if k.lower() == name]
    return ", ".join(vals) if vals else None


def security_checks(headers: list, https: bool) -> list:
    out = []
    csp = _hget(headers, "content-security-policy")
    for key, label, expl in SEC_HEADERS:
        val = _hget(headers, key)
        status, note = ("fail", "En-tête absent")
        if key == "strict-transport-security":
            if not https:
                status, note = "fail", "Site servi en HTTP : HSTS ne s'applique qu'en HTTPS"
            elif val:
                m = re.search(r"max-age\s*=\s*\"?(\d+)", val, re.I)
                age = int(m.group(1)) if m else 0
                if age >= 15552000:
                    status, note = "pass", f"max-age = {age // 86400} jours" + (" · includeSubDomains" if "includesubdomains" in val.lower() else "") + (" · preload" if "preload" in val.lower() else "")
                else:
                    status, note = "warn", f"max-age trop court ({age // 86400} jours, 180 jours minimum recommandés)"
        elif key == "content-security-policy" and val:
            low = val.lower()
            if "'unsafe-inline'" in low and "nonce-" not in low and "strict-dynamic" not in low or "'unsafe-eval'" in low:
                status, note = "warn", "Présent mais autorise 'unsafe-inline' ou 'unsafe-eval'"
            else:
                status, note = "pass", "Présent"
        elif key == "x-frame-options":
            if val and val.strip().upper() in ("DENY", "SAMEORIGIN"):
                status, note = "pass", val.strip().upper()
            elif csp and "frame-ancestors" in csp.lower():
                status, note = "pass", "Absent, mais CSP frame-ancestors est défini"
            elif val:
                status, note = "warn", f"Valeur non standard : {val}"
        elif key == "x-content-type-options" and val:
            status, note = ("pass", "nosniff") if val.strip().lower() == "nosniff" else ("warn", f"Valeur inattendue : {val}")
        elif key == "referrer-policy" and val:
            status, note = ("warn", "« unsafe-url » transmet l'URL complète") if "unsafe-url" in val.lower() else ("pass", val)
        elif key == "permissions-policy" and val:
            status, note = "pass", "Présent"
        out.append({"header": label, "key": key, "status": status, "value": val, "note": note, "explanation": expl})
    leaks = [h for h in ("server", "x-powered-by", "x-aspnet-version", "x-aspnetmvc-version") if _hget(headers, h)]
    if leaks:
        out.append({"header": "Divulgation de version", "key": "info-leak", "status": "info",
                    "value": "; ".join(f"{h}: {_hget(headers, h)}" for h in leaks),
                    "note": "Informations sur le serveur exposées",
                    "explanation": "Les en-têtes Server / X-Powered-By révèlent la technologie et parfois la version utilisée ; à masquer si possible."})
    return out


def _clean_url(raw: str) -> Optional[str]:
    u = (raw or "").strip()
    if not u:
        return None
    if "://" not in u:
        u = "https://" + u
    p = urlsplit(u)
    if p.scheme not in ("http", "https") or not p.hostname or not _clean_host(p.hostname):
        return None
    if len(u) > 2048:
        return None
    return u


def _one_request(url: str, verify: bool):
    p = urlsplit(url)
    host = p.hostname.encode("idna").decode("ascii") if not _is_ip(p.hostname) else p.hostname
    port = p.port
    path = (p.path or "/") + (f"?{p.query}" if p.query else "")
    if p.scheme == "https":
        ctx = _verify_context()[0] if verify else _noverify_context()
        conn = http.client.HTTPSConnection(host, port, timeout=HTTP_TIMEOUT, context=ctx)
    else:
        conn = http.client.HTTPConnection(host, port, timeout=HTTP_TIMEOUT)
    t0 = time.time()
    try:
        conn.request("GET", path, headers={"User-Agent": UA, "Accept": "text/html,*/*;q=0.8", "Accept-Encoding": "identity"})
        r = conn.getresponse()
        elapsed = int((time.time() - t0) * 1000)
        headers = list(r.getheaders())
        ver = {10: "HTTP/1.0", 11: "HTTP/1.1"}.get(r.version, str(r.version))
        return r.status, r.reason, headers, elapsed, ver
    finally:
        conn.close()


def fetch_headers(url: str, max_hops: int = 10) -> dict:
    hops, cur, seen = [], url, set()
    final = None
    for _ in range(max_hops):
        hop = {"url": cur}
        try:
            try:
                status, reason, headers, ms, ver = _one_request(cur, True)
            except ssl.SSLCertVerificationError as e:
                hop["cert_error"] = getattr(e, "verify_message", None) or str(e)
                status, reason, headers, ms, ver = _one_request(cur, False)
        except (OSError, http.client.HTTPException) as e:
            hop["error"] = f"{type(e).__name__}: {e}"
            hops.append(hop)
            break
        loc = _hget(headers, "location")
        hop.update({"status": status, "reason": reason, "time_ms": ms, "http_version": ver, "location": loc})
        hops.append(hop)
        final = {"url": cur, "status": status, "headers": headers}
        if 300 <= status < 400 and loc:
            nxt = urljoin(cur, loc.split(",")[0].strip())
            if not _clean_url(nxt) or nxt in seen:
                hop["error"] = "Boucle ou redirection invalide"
                break
            seen.add(cur)
            cur = nxt
            continue
        break
    else:
        hops[-1]["error"] = f"Trop de redirections (> {max_hops})"
    res = {"url": url, "hops": hops, "final": None, "checks": [], "score": None}
    if final:
        https = final["url"].lower().startswith("https://")
        checks = security_checks(final["headers"], https)
        scored = [c for c in checks if c["status"] in ("pass", "warn", "fail")]
        score = sum(1 if c["status"] == "pass" else 0.5 if c["status"] == "warn" else 0 for c in scored)
        res.update({"final": {"url": final["url"], "status": final["status"], "https": https,
                              "headers": [{"name": k, "value": v} for k, v in final["headers"]]},
                    "checks": checks, "score": score, "score_max": len(scored)})
    return res


# ── SQLite (surveillance) ────────────────────────────────────────────────────

@contextmanager
def _db():
    conn = sqlite3.connect(str(DB_PATH), timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _init():
    with _db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS watch (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                host          TEXT    NOT NULL,
                port          INTEGER NOT NULL DEFAULT 443,
                note          TEXT    NOT NULL DEFAULT '',
                added_at      TEXT    NOT NULL,
                last_check    TEXT,
                cert_expiry   TEXT,
                cert_days     INTEGER,
                cert_issuer   TEXT,
                cert_ok       INTEGER,
                cert_error    TEXT,
                domain_expiry TEXT,
                domain_days   INTEGER,
                registrar     TEXT,
                domain_error  TEXT,
                UNIQUE(host, port)
            )
        """)


_init()


def _rows():
    with _db() as conn:
        rows = [dict(r) for r in conn.execute("SELECT * FROM watch ORDER BY host, port").fetchall()]
    today = datetime.now().date()
    for r in rows:  # recompute remaining days from stored dates
        for exp, days in (("cert_expiry", "cert_days"), ("domain_expiry", "domain_days")):
            if r[exp]:
                try:
                    r[days] = (datetime.fromisoformat(r[exp][:10]).date() - today).days
                except ValueError:
                    pass
    return rows


def _check_one(row: dict, whois_cache: Optional[dict] = None) -> dict:
    host, port = row["host"], row["port"]
    upd = {"last_check": datetime.now().isoformat(timespec="seconds")}
    try:
        c = fetch_cert(host, port)
        upd.update({"cert_expiry": c["leaf"]["not_after"], "cert_days": c["leaf"]["days_left"],
                    "cert_issuer": c["leaf"].get("issuer_org") or c["leaf"].get("issuer_cn"),
                    "cert_ok": 1 if (c["verified"] and c["hostname_match"]) else 0,
                    "cert_error": c["verify_error"] or (None if c["hostname_match"] else "Nom d'hôte non couvert par le certificat")})
    except Exception as e:
        upd.update({"cert_expiry": None, "cert_days": None, "cert_issuer": None, "cert_ok": 0, "cert_error": f"{type(e).__name__}: {e}"[:300]})
    if _is_ip(host):
        upd.update({"domain_expiry": None, "domain_days": None, "registrar": None, "domain_error": "Adresse IP"})
    else:
        dom = registrable_domain(host)
        try:
            if whois_cache is not None and dom in whois_cache:
                w = whois_cache[dom]
            else:
                w = whois_domain(dom)
                if whois_cache is not None:
                    whois_cache[dom] = w
            p = w["parsed"]
            upd.update({"domain_expiry": p["expires"], "domain_days": p["days_left"], "registrar": p["registrar"],
                        "domain_error": None if p["expires"] else "Date d'expiration introuvable dans le WHOIS"})
        except Exception as e:
            upd.update({"domain_expiry": None, "domain_days": None, "registrar": None, "domain_error": f"WHOIS : {e}"[:300]})
    with _db() as conn:
        sets = ", ".join(f"{k}=?" for k in upd)
        conn.execute(f"UPDATE watch SET {sets} WHERE id=?", list(upd.values()) + [row["id"]])
    return upd


# ── Routes ───────────────────────────────────────────────────────────────────

@router.get("/status")
async def status():
    return {"admin": _is_admin(), "cryptography": _has("cryptography"), "truststore": _has("truststore")}


@router.get("/whois")
async def whois(q: str):
    host = _clean_host(q)
    if not host:
        return _err("Nom de domaine ou adresse IP invalide")
    try:
        if _is_ip(host):
            res = await asyncio.to_thread(whois_ip, host)
        else:
            dom = registrable_domain(host)
            res = await asyncio.to_thread(whois_domain, dom)
            if dom != host:
                res["note"] = f"Sous-domaine : recherche effectuée sur le domaine enregistré « {dom} »."
    except socket.timeout:
        return _err("Le serveur WHOIS ne répond pas (délai dépassé)", 504)
    except OSError as e:
        return _err(f"Connexion WHOIS impossible : {e}", 502)
    return res


@router.get("/ssl")
async def ssl_info(host: str, port: int = 443):
    h = _clean_host(host)
    if not h:
        return _err("Hôte invalide")
    if not (1 <= port <= 65535):
        return _err("Port invalide")
    try:
        return await asyncio.to_thread(fetch_cert, h, port)
    except socket.timeout:
        return _err("Délai dépassé lors de la connexion TLS", 504)
    except socket.gaierror:
        return _err("Nom d'hôte introuvable (DNS)", 502)
    except ConnectionRefusedError:
        return _err(f"Connexion refusée sur le port {port}", 502)
    except ssl.SSLError as e:
        return _err(f"Échec de la négociation TLS : {e.reason or e}", 502)
    except Exception as e:
        return _err(f"Erreur : {e}", 502)


@router.get("/headers")
async def headers(url: str):
    u = _clean_url(url)
    if not u:
        return _err("URL invalide (http ou https uniquement)")
    try:
        return await asyncio.to_thread(fetch_headers, u)
    except Exception as e:
        return _err(f"Erreur : {e}", 502)


class WatchReq(BaseModel):
    host: str
    port: int = 443
    note: str = ""


@router.get("/watch")
async def watch_list():
    return {"items": await asyncio.to_thread(_rows)}


@router.post("/watch")
async def watch_add(req: WatchReq):
    h = _clean_host(req.host)
    if not h:
        return _err("Hôte invalide")
    if not (1 <= req.port <= 65535):
        return _err("Port invalide")

    def _ins():
        with _db() as conn:
            try:
                cur = conn.execute("INSERT INTO watch (host, port, note, added_at) VALUES (?, ?, ?, ?)",
                                   (h, req.port, req.note[:200], datetime.now().isoformat(timespec="seconds")))
            except sqlite3.IntegrityError:
                return None
            return dict(conn.execute("SELECT * FROM watch WHERE id=?", (cur.lastrowid,)).fetchone())

    row = await asyncio.to_thread(_ins)
    if row is None:
        return _err("Ce domaine est déjà surveillé", 409)
    return row


@router.delete("/watch/{wid}")
async def watch_delete(wid: int):
    def _d():
        with _db() as conn:
            return conn.execute("DELETE FROM watch WHERE id=?", (wid,)).rowcount
    if not await asyncio.to_thread(_d):
        return _err("Entrée introuvable", 404)
    return {"ok": True}


@router.post("/watch/{wid}/refresh")
async def watch_refresh(wid: int):
    def _r():
        with _db() as conn:
            row = conn.execute("SELECT * FROM watch WHERE id=?", (wid,)).fetchone()
        if not row:
            return None
        _check_one(dict(row))
        return next((r for r in _rows() if r["id"] == wid), None)
    row = await asyncio.to_thread(_r)
    if row is None:
        return _err("Entrée introuvable", 404)
    return row


@router.post("/watch/refresh-all")
async def watch_refresh_all():
    def _r():
        rows = _rows()
        cache = {}
        with ThreadPoolExecutor(max_workers=4) as ex:
            list(ex.map(lambda r: _check_one(r, cache), rows))
        return _rows()
    return {"items": await asyncio.to_thread(_r)}
