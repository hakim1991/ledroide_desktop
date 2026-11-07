"""DNS / nslookup — requêtes multi-types, résolveurs au choix, propagation, diagnostic e-mail."""
import asyncio
import base64
import ipaddress
import os
import re
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from ledroide_sdk.http import JSONResponse
from pydantic import BaseModel
from ledroide_sdk import Module, data_path

module = Module(__file__)
router = module.router

DB_PATH = Path(os.environ.get("LEDROIDE_DNS_LOOKUP_DB") or (data_path(__file__, "dns-lookup.db")))

RESOLVERS = {
    "system": {"label": "Système", "ips": None},
    "cloudflare": {"label": "Cloudflare", "ips": ["1.1.1.1", "1.0.0.1"]},
    "google": {"label": "Google", "ips": ["8.8.8.8", "8.8.4.4"]},
    "quad9": {"label": "Quad9", "ips": ["9.9.9.9", "149.112.112.112"]},
    "opendns": {"label": "OpenDNS", "ips": ["208.67.222.222", "208.67.220.220"]},
}

PROPAGATION = [
    ("Cloudflare", "1.1.1.1", "🇺🇸 Anycast"),
    ("Google", "8.8.8.8", "🇺🇸 Anycast"),
    ("Quad9", "9.9.9.9", "🇨🇭 Anycast"),
    ("OpenDNS", "208.67.222.222", "🇺🇸 Anycast"),
    ("AdGuard", "94.140.14.14", "🇨🇾 Anycast"),
    ("Level3 / Lumen", "4.2.2.1", "🇺🇸"),
    ("Comodo", "8.26.56.26", "🇺🇸"),
    ("DNS.Watch", "84.200.69.80", "🇩🇪"),
    ("Yandex", "77.88.8.8", "🇷🇺"),
    ("Control D", "76.76.2.0", "🇨🇦 Anycast"),
]

RECORD_TYPES = ["A", "AAAA", "MX", "TXT", "NS", "CNAME", "SOA", "PTR", "SRV", "CAA"]
ALL_TYPES = ["A", "AAAA", "CNAME", "MX", "NS", "TXT", "SOA", "CAA"]

NAME_RE = re.compile(r"^(?=.{1,253}\.?$)([A-Za-z0-9_](?:[A-Za-z0-9_-]{0,61}[A-Za-z0-9_])?\.)*"
                     r"[A-Za-z0-9_](?:[A-Za-z0-9_-]{0,61}[A-Za-z0-9_])?\.?$")
SELECTOR_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,62}$")
DKIM_COMMON = ["default", "google", "selector1", "selector2", "k1", "k2", "s1", "s2", "mail", "dkim",
               "smtp", "mxvault", "fm1", "protonmail", "zoho", "mandrill", "everlytickey1", "sig1"]


def _err(msg: str, status: int = 400, code: Optional[str] = None):
    content = {"error": msg}
    if code:
        content["code"] = code
    return JSONResponse(status_code=status, content=content)


def _missing_dep():
    return _err("Le module Python « dnspython » n'est pas installé. Installez les dépendances du module.",
                400, "MISSING_DEP")


def _dns():
    import dns.flags  # noqa: F401
    import dns.message  # noqa: F401
    import dns.name  # noqa: F401
    import dns.query  # noqa: F401
    import dns.rcode  # noqa: F401
    import dns.rdatatype  # noqa: F401
    import dns.resolver  # noqa: F401
    import dns.reversename  # noqa: F401
    import dns
    return dns


# ── SQLite (historique) ────────────────────────────────────────────────────

@contextmanager
def _db():
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _init_db():
    with _db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS history (
                id       INTEGER PRIMARY KEY AUTOINCREMENT,
                ts       TEXT    NOT NULL,
                kind     TEXT    NOT NULL DEFAULT 'query',
                name     TEXT    NOT NULL,
                rdtype   TEXT    NOT NULL DEFAULT '',
                resolver TEXT    NOT NULL DEFAULT '',
                custom   TEXT    NOT NULL DEFAULT '',
                status   TEXT    NOT NULL DEFAULT '',
                count    INTEGER NOT NULL DEFAULT 0,
                ms       REAL
            )
        """)


_db_ready = False


def _ensure_db():
    global _db_ready
    if not _db_ready:
        _init_db()
        _db_ready = True


def _add_history(kind, name, rdtype, resolver, custom, status, count, ms):
    try:
        _ensure_db()
        with _db() as conn:
            conn.execute(
                "INSERT INTO history (ts, kind, name, rdtype, resolver, custom, status, count, ms) VALUES (?,?,?,?,?,?,?,?,?)",
                (datetime.now(timezone.utc).isoformat(), kind, name, rdtype, resolver, custom or "", status, count, ms))
            conn.execute("DELETE FROM history WHERE id NOT IN (SELECT id FROM history ORDER BY id DESC LIMIT 200)")
    except sqlite3.Error:
        pass


# ── Validation ─────────────────────────────────────────────────────────────

class InputError(Exception):
    pass


def _clean_name(raw: str) -> tuple[str, bool]:
    """Retourne (nom ASCII, est_une_ip)."""
    s = (raw or "").strip()
    s = re.sub(r"^[a-z][a-z0-9+.-]*://", "", s, flags=re.I)  # tolère une URL collée
    s = s.split("/")[0].strip() if not _is_ip(s) else s
    if s.startswith("[") and s.endswith("]"):
        s = s[1:-1]
    if not s:
        raise InputError("Saisissez un nom de domaine ou une adresse IP.")
    if _is_ip(s):
        return s, True
    try:
        ascii_name = s.encode("idna").decode("ascii") if any(ord(ch) > 127 for ch in s) else s
    except UnicodeError:
        raise InputError("Nom de domaine international invalide.")
    if not NAME_RE.match(ascii_name):
        raise InputError("Nom de domaine invalide.")
    return ascii_name.lower(), False


def _is_ip(s: str) -> bool:
    try:
        ipaddress.ip_address(s)
        return True
    except ValueError:
        return False


def _nameservers(resolver: str, custom: Optional[str]) -> tuple[list[str], str]:
    if resolver == "custom":
        c = (custom or "").strip()
        try:
            ip = ipaddress.ip_address(c)
        except ValueError:
            raise InputError("Adresse IP du serveur DNS personnalisé invalide.")
        return [str(ip)], f"Personnalisé ({ip})"
    if resolver not in RESOLVERS:
        raise InputError("Résolveur inconnu.")
    r = RESOLVERS[resolver]
    if r["ips"] is None:
        dns = _dns()
        try:
            ns = list(dns.resolver.Resolver().nameservers)
        except Exception:
            ns = []
        ns = [str(x) for x in ns if _is_ip(str(x))]
        if not ns:
            raise InputError("Aucun serveur DNS système détecté.")
        return ns, "Système (" + ", ".join(ns[:3]) + ")"
    return list(r["ips"]), f"{r['label']} ({r['ips'][0]})"


# ── Requête bas niveau ─────────────────────────────────────────────────────

def _rdata_struct(rd, rtype: str) -> dict:
    try:
        if rtype in ("A", "AAAA"):
            return {"address": rd.address}
        if rtype == "MX":
            return {"preference": rd.preference, "exchange": rd.exchange.to_text()}
        if rtype in ("NS", "CNAME", "PTR", "DNAME"):
            return {"target": rd.target.to_text()}
        if rtype == "SOA":
            return {"mname": rd.mname.to_text(), "rname": rd.rname.to_text(), "serial": rd.serial,
                    "refresh": rd.refresh, "retry": rd.retry, "expire": rd.expire, "minimum": rd.minimum}
        if rtype == "SRV":
            return {"priority": rd.priority, "weight": rd.weight, "port": rd.port, "target": rd.target.to_text()}
        if rtype == "CAA":
            return {"flags": rd.flags, "tag": rd.tag.decode(errors="replace"), "value": rd.value.decode(errors="replace")}
        if rtype in ("TXT", "SPF"):
            return {"text": "".join(s.decode("utf-8", errors="replace") for s in rd.strings)}
    except Exception:
        pass
    return {}


def _display(rd, rtype: str, struct: dict) -> str:
    if rtype in ("TXT", "SPF") and "text" in struct:
        return struct["text"]
    return rd.to_text()


def _rrsets(section) -> list[dict]:
    dns = _dns()
    out = []
    for rrset in section:
        rtype = dns.rdatatype.to_text(rrset.rdtype)
        for rd in rrset:
            st = _rdata_struct(rd, rtype)
            out.append({"name": rrset.name.to_text(), "type": rtype, "ttl": rrset.ttl,
                        "value": _display(rd, rtype, st), "data": st})
    return out


def query_one(qname: str, rtype: str, servers: list[str], timeout: float = 4.0, tcp: bool = False) -> dict:
    """Interroge les serveurs dans l'ordre jusqu'à obtenir une réponse."""
    dns = _dns()
    name = dns.name.from_text(qname)
    rdtype = dns.rdatatype.from_text(rtype)
    last_err = None
    for server in servers:
        q = dns.message.make_query(name, rdtype, use_edns=0, payload=1232)
        q.flags |= dns.flags.RD
        t0 = time.perf_counter()
        try:
            if tcp:
                r = dns.query.tcp(q, server, timeout=timeout)
            else:
                r = dns.query.udp(q, server, timeout=timeout, ignore_unexpected=True)
                if r.flags & dns.flags.TC:
                    # Réponse tronquée : TCP, sinon UDP avec un tampon EDNS plus grand
                    try:
                        r = dns.query.tcp(q, server, timeout=timeout)
                    except Exception:  # noqa: BLE001
                        q2 = dns.message.make_query(name, rdtype, use_edns=0, payload=4096)
                        q2.flags |= dns.flags.RD
                        try:
                            r2 = dns.query.udp(q2, server, timeout=timeout, ignore_unexpected=True)
                            r = r2
                        except Exception:  # noqa: BLE001
                            pass
        except dns.exception.Timeout:
            last_err = ("TIMEOUT", f"Aucune réponse de {server} (délai dépassé)")
            continue
        except OSError as e:
            last_err = ("NETERR", f"Erreur réseau vers {server} : {e}")
            continue
        except Exception as e:  # noqa: BLE001
            last_err = ("ERROR", f"Réponse invalide de {server} : {e}")
            continue
        ms = round((time.perf_counter() - t0) * 1000, 1)
        rcode = dns.rcode.to_text(r.rcode())
        answers = _rrsets(r.answer)
        matching = [a for a in answers if a["type"] == rtype]
        return {
            "type": rtype, "server": server, "ms": ms, "status": rcode,
            "ok": rcode == "NOERROR",
            "flags": {"aa": bool(r.flags & dns.flags.AA), "ra": bool(r.flags & dns.flags.RA),
                      "ad": bool(r.flags & dns.flags.AD), "tc": bool(r.flags & dns.flags.TC)},
            "answers": answers, "count": len(matching),
            "authority": _rrsets(r.authority), "additional": _rrsets(r.additional)[:20],
        }
    code, msg = last_err or ("ERROR", "Aucun serveur DNS")
    return {"type": rtype, "server": servers[0] if servers else None, "ms": None, "status": code, "ok": False,
            "error": msg, "flags": {}, "answers": [], "count": 0, "authority": [], "additional": []}


RCODE_FR = {
    "NOERROR": "Succès", "NXDOMAIN": "Domaine inexistant", "SERVFAIL": "Échec du serveur",
    "REFUSED": "Requête refusée", "FORMERR": "Requête mal formée", "NOTIMP": "Non implémenté",
    "TIMEOUT": "Délai dépassé", "NETERR": "Erreur réseau", "ERROR": "Erreur",
}


def run_query(raw_name: str, rtype: str, resolver: str, custom: Optional[str], tcp: bool) -> dict:
    name, is_ip = _clean_name(raw_name)
    servers, label = _nameservers(resolver, custom)
    dns = _dns()
    reverse = None
    if is_ip:
        reverse = name
        qname = dns.reversename.from_address(name).to_text()
        types = ["PTR"]
    else:
        qname = name if name.endswith(".") else name + "."
        if rtype == "ALL":
            types = list(ALL_TYPES)
            if qname.startswith("_"):
                types.append("SRV")
            if qname.endswith(".in-addr.arpa.") or qname.endswith(".ip6.arpa."):
                types = ["PTR", "NS", "SOA"]
        else:
            if rtype not in RECORD_TYPES:
                raise InputError("Type d'enregistrement non pris en charge.")
            types = [rtype]
    t0 = time.perf_counter()
    if len(types) == 1:
        results = [query_one(qname, types[0], servers, tcp=tcp)]
    else:
        with ThreadPoolExecutor(max_workers=len(types)) as ex:
            results = list(ex.map(lambda t: query_one(qname, t, servers, tcp=tcp), types))
    total_ms = round((time.perf_counter() - t0) * 1000, 1)
    for r in results:
        r["status_fr"] = RCODE_FR.get(r["status"], r["status"])

    # Résolution inverse automatique des adresses obtenues (A/AAAA)
    ptrs = {}
    if not is_ip:
        ips = []
        for r in results:
            for a in r["answers"]:
                if a["type"] in ("A", "AAAA") and a["value"] not in ips:
                    ips.append(a["value"])
        ips = ips[:6]
        if ips:
            def rev(ip):
                rr = query_one(dns.reversename.from_address(ip).to_text(), "PTR", servers, timeout=2.5)
                return ip, [a["value"] for a in rr["answers"] if a["type"] == "PTR"]
            with ThreadPoolExecutor(max_workers=len(ips)) as ex:
                ptrs = dict(ex.map(rev, ips))

    total = sum(r["count"] for r in results)
    st = results[0]["status"] if len(results) == 1 else ("NOERROR" if any(r["ok"] for r in results) else results[0]["status"])
    shown_type = "PTR" if is_ip else ("Tous" if rtype == "ALL" else rtype)
    _add_history("query", raw_name.strip(), shown_type, resolver, custom if resolver == "custom" else "", st, total,
                 results[0]["ms"] if len(results) == 1 else total_ms)
    return {"name": name, "qname": qname, "is_ip": is_ip, "reverse_of": reverse, "resolver": label,
            "servers": servers, "total_ms": total_ms, "results": results, "ptr": ptrs}


# ── Propagation ────────────────────────────────────────────────────────────

def _norm_value(a: dict) -> str:
    v = a["value"].strip().lower()
    if a["type"] in ("NS", "CNAME", "MX", "PTR", "SRV", "SOA"):
        v = v.rstrip(".")
    if a["type"] == "SOA":
        # Le serial peut légèrement différer ; on compare le serveur primaire + serial
        d = a.get("data") or {}
        v = f"{d.get('mname', '').rstrip('.')} {d.get('serial', '')}"
    return v


def run_propagation(raw_name: str, rtype: str, include_system: bool = True) -> dict:
    name, is_ip = _clean_name(raw_name)
    dns = _dns()
    if is_ip:
        qname, rtype = dns.reversename.from_address(name).to_text(), "PTR"
    else:
        qname = name if name.endswith(".") else name + "."
        if rtype not in RECORD_TYPES:
            raise InputError("Type d'enregistrement non pris en charge pour la propagation.")
    targets = [{"label": l, "ip": ip, "where": w} for (l, ip, w) in PROPAGATION]
    if include_system:
        try:
            sysns, _ = _nameservers("system", None)
            targets.insert(0, {"label": "Système", "ip": sysns[0], "where": "Local"})
        except Exception:
            pass

    def one(t):
        r = query_one(qname, rtype, [t["ip"]], timeout=4.0)
        vals = sorted({_norm_value(a) for a in r["answers"] if a["type"] == rtype})
        ttl = min((a["ttl"] for a in r["answers"] if a["type"] == rtype), default=None)
        return {**t, "status": r["status"], "status_fr": RCODE_FR.get(r["status"], r["status"]),
                "ok": r["ok"], "ms": r["ms"], "values": vals, "ttl": ttl,
                "display": [a["value"] for a in r["answers"] if a["type"] == rtype],
                "error": r.get("error")}

    t0 = time.perf_counter()
    with ThreadPoolExecutor(max_workers=len(targets)) as ex:
        rows = list(ex.map(one, targets))
    groups: dict[tuple, int] = {}
    for r in rows:
        if r["status"] in ("TIMEOUT", "NETERR", "ERROR"):
            continue
        key = (r["status"], tuple(r["values"]))
        groups[key] = groups.get(key, 0) + 1
    majority = max(groups.items(), key=lambda kv: kv[1])[0] if groups else None
    answered = sum(groups.values())
    agree = groups.get(majority, 0) if majority else 0
    for r in rows:
        if r["status"] in ("TIMEOUT", "NETERR", "ERROR"):
            r["match"] = None
        else:
            r["match"] = (r["status"], tuple(r["values"])) == majority
    variants = [{"status": k[0], "values": list(k[1]), "count": c}
                for k, c in sorted(groups.items(), key=lambda kv: -kv[1])]
    _add_history("propagation", raw_name.strip(), rtype, "propagation", "", "NOERROR" if answered else "TIMEOUT",
                 agree, round((time.perf_counter() - t0) * 1000, 1))
    return {"name": name, "qname": qname, "type": rtype, "rows": rows, "answered": answered, "total": len(rows),
            "agree": agree, "consistent": len(groups) <= 1 and answered > 0, "variants": variants,
            "total_ms": round((time.perf_counter() - t0) * 1000, 1)}


# ── Diagnostic e-mail ──────────────────────────────────────────────────────

def _txts(qname: str, servers) -> tuple[list[str], str]:
    r = query_one(qname, "TXT", servers)
    return [a["value"] for a in r["answers"] if a["type"] == "TXT"], r["status"]


SPF_LOOKUP_MECHS = {"include", "a", "mx", "ptr", "exists"}
QUAL = {"+": "Pass", "-": "Fail", "~": "SoftFail", "?": "Neutral"}


def _spf_terms(record: str) -> list[dict]:
    terms = []
    for tok in record.split()[1:]:
        t = {"raw": tok}
        if "=" in tok and ":" not in tok.split("=")[0]:
            k, v = tok.split("=", 1)
            t.update({"kind": "modifier", "name": k.lower(), "value": v})
        else:
            q = tok[0] if tok[0] in QUAL else "+"
            body = tok[1:] if tok[0] in QUAL else tok
            mech, _, val = body.partition(":")
            if "/" in mech and mech.split("/")[0].lower() in ("a", "mx"):
                mech, val = mech.split("/")[0], "/" + mech.split("/", 1)[1]
            t.update({"kind": "mechanism", "qualifier": q, "result": QUAL[q], "name": mech.lower(), "value": val})
        terms.append(t)
    return terms


def _explain_term(t: dict) -> str:
    n, v = t.get("name"), t.get("value") or ""
    if t["kind"] == "modifier":
        return {"redirect": f"Utilise la politique SPF de {v}", "exp": f"Message d'explication en cas d'échec : {v}"}.get(n, "Modificateur")
    q = t["result"]
    return {
        "all": {"Fail": "Rejeter tout autre expéditeur (strict, recommandé)",
                "SoftFail": "Marquer comme suspect tout autre expéditeur (souple)",
                "Neutral": "Aucun avis sur les autres expéditeurs (inefficace)",
                "Pass": "Autoriser N'IMPORTE QUEL serveur (dangereux !)"}[q],
        "include": f"Autorise les serveurs déclarés par {v}",
        "a": f"Autorise les adresses A/AAAA de {v or 'ce domaine'}",
        "mx": f"Autorise les serveurs MX de {v or 'ce domaine'}",
        "ip4": f"Autorise la plage IPv4 {v}", "ip6": f"Autorise la plage IPv6 {v}",
        "ptr": "Vérification PTR (déconseillée, lente)", "exists": f"Autorise si {v} existe",
    }.get(n, f"Mécanisme {n}") + ("" if q == "Pass" or n == "all" else f" → résultat {q}")


def _spf_count_lookups(domain: str, servers, depth=0, seen=None, budget=None) -> tuple[int, list[str]]:
    """Compte récursivement les requêtes DNS induites (limite RFC 7208 : 10)."""
    seen = seen if seen is not None else set()
    budget = budget if budget is not None else [40]
    if depth > 6 or domain in seen or budget[0] <= 0:
        return 0, []
    seen.add(domain)
    budget[0] -= 1
    txts, _ = _txts(domain.rstrip(".") + ".", servers)
    spf = [t for t in txts if t.lower().startswith("v=spf1")]
    if not spf:
        return 0, [f"{domain} : aucun SPF"] if depth else []
    count, notes = 0, []
    for t in _spf_terms(spf[0]):
        if t["kind"] == "mechanism" and t["name"] in SPF_LOOKUP_MECHS:
            count += 1
            if t["name"] == "include" and t["value"]:
                c, n = _spf_count_lookups(t["value"], servers, depth + 1, seen, budget)
                count += c
                notes += n
        elif t["kind"] == "modifier" and t["name"] == "redirect":
            count += 1
            c, n = _spf_count_lookups(t["value"], servers, depth + 1, seen, budget)
            count += c
            notes += n
    return count, notes


DMARC_TAGS = {
    "v": "Version", "p": "Politique du domaine", "sp": "Politique des sous-domaines", "pct": "Pourcentage appliqué",
    "rua": "Rapports agrégés envoyés à", "ruf": "Rapports d'échec (forensic) envoyés à",
    "adkim": "Alignement DKIM", "aspf": "Alignement SPF", "fo": "Options des rapports d'échec",
    "ri": "Intervalle des rapports (s)", "rf": "Format des rapports", "np": "Politique des sous-domaines inexistants",
    "t": "Mode test",
}
POLICY_FR = {"none": "aucune action (surveillance seule)", "quarantine": "mise en quarantaine (spam)",
             "reject": "rejet du message"}


def _parse_tags(rec: str) -> dict:
    out = {}
    for part in rec.split(";"):
        if "=" in part:
            k, v = part.split("=", 1)
            out[k.strip().lower()] = v.strip()
    return out


def _der_len(b: bytes, i: int) -> tuple[int, int]:
    l = b[i]
    i += 1
    if l & 0x80:
        n = l & 0x7F
        l = int.from_bytes(b[i:i + n], "big")
        i += n
    return l, i


def _rsa_bits(p_b64: str) -> Optional[int]:
    try:
        der = base64.b64decode(re.sub(r"\s+", "", p_b64) + "===")
    except Exception:
        return None
    try:
        # SubjectPublicKeyInfo ::= SEQ { SEQ alg, BIT STRING { SEQ { INTEGER n, INTEGER e } } }
        i = 0
        if der[i] != 0x30:
            raise ValueError
        _, i = _der_len(der, i + 1)
        if der[i] == 0x30:  # AlgorithmIdentifier
            l, j = _der_len(der, i + 1)
            i = j + l
            if der[i] != 0x03:
                raise ValueError
            _, i = _der_len(der, i + 1)
            i += 1  # bits inutilisés
            if der[i] != 0x30:
                raise ValueError
            _, i = _der_len(der, i + 1)
        if der[i] != 0x02:
            raise ValueError
        l, i = _der_len(der, i + 1)
        n = der[i:i + l].lstrip(b"\x00")
        return len(n) * 8
    except Exception:
        return None


def run_email(raw_domain: str, selectors: list[str], resolver: str, custom: Optional[str]) -> dict:
    domain, is_ip = _clean_name((raw_domain or "").strip().split("@")[-1])
    if is_ip:
        raise InputError("Saisissez un nom de domaine (pas une adresse IP).")
    domain = domain.rstrip(".")
    servers, label = _nameservers(resolver, custom)
    checks = []

    def check(level, title, detail=""):
        checks.append({"level": level, "title": title, "detail": detail})

    # MX
    mx_r = query_one(domain + ".", "MX", servers)
    mx = sorted([a["data"] for a in mx_r["answers"] if a["type"] == "MX" and a["data"]], key=lambda d: d["preference"])
    null_mx = len(mx) == 1 and mx[0]["exchange"] in (".", "")

    def resolve_host(h):
        h4 = query_one(h, "A", servers, timeout=3)
        h6 = query_one(h, "AAAA", servers, timeout=3)
        return [a["value"] for a in h4["answers"] if a["type"] == "A"] + [a["value"] for a in h6["answers"] if a["type"] == "AAAA"]

    if mx and not null_mx:
        with ThreadPoolExecutor(max_workers=min(8, len(mx))) as ex:
            addrs = list(ex.map(lambda d: resolve_host(d["exchange"]), mx))
        for d, ad in zip(mx, addrs):
            d["addresses"] = ad
    if mx_r["status"] == "NXDOMAIN":
        check("err", "Le domaine n'existe pas (NXDOMAIN)")
    elif null_mx:
        check("info", "MX nul (RFC 7505)", "Le domaine déclare explicitement ne recevoir aucun e-mail.")
    elif not mx:
        check("warn", "Aucun enregistrement MX", "Les e-mails seront remis à l'enregistrement A du domaine (comportement de repli), ou refusés.")
    else:
        bad = [d["exchange"] for d in mx if not d.get("addresses")]
        if bad:
            check("err", "Serveur(s) MX sans adresse IP", ", ".join(bad))
        else:
            check("ok", f"{len(mx)} serveur(s) MX valides", ", ".join(d["exchange"].rstrip(".") for d in mx))

    # SPF
    txts, _ = _txts(domain + ".", servers)
    spfs = [t for t in txts if t.lower().startswith("v=spf1")]
    spf = None
    if not spfs:
        check("err", "Aucun enregistrement SPF", "Ajoutez un TXT « v=spf1 … -all » pour déclarer les serveurs autorisés à envoyer pour ce domaine.")
    else:
        if len(spfs) > 1:
            check("err", "Plusieurs enregistrements SPF", "Un seul TXT « v=spf1 » est autorisé ; les vérifications retourneront PermError.")
        rec = spfs[0]
        terms = _spf_terms(rec)
        for t in terms:
            t["explain"] = _explain_term(t)
        lookups, notes = _spf_count_lookups(domain + ".", servers)
        all_t = next((t for t in terms if t.get("name") == "all"), None)
        redirect = next((t for t in terms if t.get("name") == "redirect"), None)
        spf = {"record": rec, "terms": terms, "lookups": lookups, "notes": notes, "length": len(rec),
               "all": all_t["result"] if all_t else None}
        if lookups > 10:
            check("err", f"SPF : {lookups} requêtes DNS (limite 10 dépassée)", "Les destinataires renverront PermError ; aplatissez certains include.")
        elif lookups >= 8:
            check("warn", f"SPF : {lookups} requêtes DNS sur 10 autorisées", "Proche de la limite.")
        if all_t is None and redirect is None:
            check("warn", "SPF sans mécanisme « all »", "Ajoutez « -all » ou « ~all » à la fin.")
        elif all_t is not None:
            lvl = {"Fail": "ok", "SoftFail": "ok", "Neutral": "warn", "Pass": "err"}[all_t["result"]]
            check(lvl, f"SPF présent ({all_t['raw']})", all_t["explain"])
        else:
            check("ok", "SPF présent (redirect)", redirect["explain"])
        if any(t.get("name") == "ptr" for t in terms):
            check("warn", "SPF utilise « ptr »", "Mécanisme déconseillé par la RFC 7208.")

    # DMARC
    dmarc = None
    dtxt, _ = _txts(f"_dmarc.{domain}.", servers)
    drec = [t for t in dtxt if t.lower().replace(" ", "").startswith("v=dmarc1")]
    inherited = None
    if not drec and domain.count(".") >= 2:
        parent = ".".join(domain.split(".")[-2:])
        ptxt, _ = _txts(f"_dmarc.{parent}.", servers)
        drec = [t for t in ptxt if t.lower().replace(" ", "").startswith("v=dmarc1")]
        if drec:
            inherited = parent
    if not drec:
        check("err", "Aucun enregistrement DMARC", f"Ajoutez un TXT sur _dmarc.{domain}, par ex. « v=DMARC1; p=none; rua=mailto:dmarc@{domain} » puis durcissez progressivement.")
    else:
        tags = _parse_tags(drec[0])
        items = []
        for k, v in tags.items():
            ex = ""
            if k in ("p", "sp", "np"):
                ex = POLICY_FR.get(v.lower(), "valeur inconnue")
            elif k in ("adkim", "aspf"):
                ex = {"r": "relaxé (sous-domaines acceptés)", "s": "strict (domaine identique)"}.get(v.lower(), "")
            elif k == "pct":
                ex = f"politique appliquée à {v} % des messages"
            elif k == "fo":
                ex = {"0": "rapport si SPF et DKIM échouent", "1": "rapport si SPF ou DKIM échoue",
                      "d": "rapport si DKIM échoue", "s": "rapport si SPF échoue"}.get(v, "")
            items.append({"tag": k, "label": DMARC_TAGS.get(k, k), "value": v, "explain": ex})
        dmarc = {"record": drec[0], "tags": items, "inherited_from": inherited}
        p = tags.get("p", "").lower()
        if len(drec) > 1:
            check("err", "Plusieurs enregistrements DMARC", "Un seul est autorisé.")
        if p == "reject":
            check("ok", "DMARC : p=reject", "Protection maximale contre l'usurpation.")
        elif p == "quarantine":
            check("ok", "DMARC : p=quarantine", "Les messages non conformes vont en spam.")
        elif p == "none":
            check("warn", "DMARC : p=none (surveillance seule)", "Aucune protection effective ; passez à quarantine puis reject après analyse des rapports.")
        else:
            check("err", "DMARC : politique « p » absente ou invalide")
        if not tags.get("rua"):
            check("warn", "DMARC sans adresse de rapports (rua)", "Vous ne recevrez aucun rapport agrégé.")
        if tags.get("pct") and tags["pct"] != "100":
            check("info", f"DMARC appliqué à {tags['pct']} % des messages")
        if inherited:
            check("info", f"DMARC hérité du domaine {inherited}")

    # DKIM
    sels = [s for s in selectors if s] or list(DKIM_COMMON)
    auto = not [s for s in selectors if s]

    def dkim_one(sel):
        r = query_one(f"{sel}._domainkey.{domain}.", "TXT", servers, timeout=3)
        recs = [a["value"] for a in r["answers"] if a["type"] == "TXT"]
        cname = next((a["value"] for a in r["answers"] if a["type"] == "CNAME"), None)
        rec = next((t for t in recs if "p=" in t.replace(" ", "")), recs[0] if recs else None)
        if not rec:
            return {"selector": sel, "found": False, "status": r["status"]}
        tags = _parse_tags(rec)
        k = tags.get("k", "rsa").lower()
        p = tags.get("p", "")
        bits = 256 if k == "ed25519" else (_rsa_bits(p) if p else None)
        return {"selector": sel, "found": True, "record": rec, "cname": cname, "key_type": k, "bits": bits,
                "revoked": p == "", "testing": "y" in tags.get("t", "").lower().split(":"),
                "hash": tags.get("h"), "services": tags.get("s"), "notes": tags.get("n")}

    with ThreadPoolExecutor(max_workers=min(10, len(sels))) as ex:
        dk = list(ex.map(dkim_one, sels))
    found = [d for d in dk if d["found"]]
    if auto and len(found) >= 3 and all(d["revoked"] for d in found):
        # Enregistrement générique (*._domainkey) publiant une clé vide : on le résume
        check("info", "DKIM : clé vide publiée pour tous les sélecteurs",
              "Enregistrement générique probable (*._domainkey) indiquant que le domaine n'envoie pas d'e-mails signés.")
        for d in dk:
            d["wildcard"] = True
        found = []
        dk = dk[:1]
    elif auto:
        dk = [d for d in dk if d["found"]]
    if found:
        for d in found:
            if d["revoked"]:
                check("warn", f"DKIM « {d['selector']} » : clé révoquée (p vide)")
            elif d["key_type"] == "rsa" and d["bits"] and d["bits"] < 1024:
                check("err", f"DKIM « {d['selector']} » : clé RSA trop courte ({d['bits']} bits)")
            elif d["key_type"] == "rsa" and d["bits"] and d["bits"] < 2048:
                check("warn", f"DKIM « {d['selector']} » : clé RSA {d['bits']} bits", "2048 bits recommandés.")
            else:
                check("ok", f"DKIM « {d['selector']} » trouvé", f"{d['key_type'].upper()} {d['bits'] or '?'} bits" + (" — mode test" if d["testing"] else ""))
    elif not any(c["title"].startswith("DKIM : clé vide") for c in checks):
        if auto:
            check("info", "Aucun sélecteur DKIM courant trouvé",
                  "Le sélecteur est propre à chaque expéditeur : consultez l'en-tête DKIM-Signature (s=…) d'un e-mail reçu et saisissez-le.")
        else:
            check("err", "Sélecteur(s) DKIM introuvable(s)", ", ".join(sels))

    # Extras : MTA-STS, TLS-RPT, BIMI
    extras = []
    for label_x, q, prefix in (("MTA-STS", f"_mta-sts.{domain}.", "v=stsv1"),
                               ("TLS-RPT", f"_smtp._tls.{domain}.", "v=tlsrptv1"),
                               ("BIMI", f"default._bimi.{domain}.", "v=bimi1")):
        t, _ = _txts(q, servers)
        rec = next((x for x in t if x.lower().replace(" ", "").startswith(prefix)), None)
        extras.append({"name": label_x, "qname": q, "record": rec})

    order = {"err": 0, "warn": 1, "info": 2, "ok": 3}
    score = sum({"ok": 2, "info": 1, "warn": 0, "err": -1}[c["level"]] for c in checks)
    _add_history("email", domain, "E-MAIL", resolver, custom if resolver == "custom" else "",
                 "NOERROR" if mx_r["ok"] else mx_r["status"], len([c for c in checks if c["level"] == "ok"]), None)
    return {"domain": domain, "resolver": label, "mx": mx, "null_mx": null_mx, "mx_status": mx_r["status"],
            "spf": spf, "spf_all_records": spfs, "dmarc": dmarc, "dkim": dk, "dkim_auto": auto,
            "extras": extras, "checks": sorted(checks, key=lambda c: order[c["level"]]), "score": score}


# ── Routes ─────────────────────────────────────────────────────────────────

@router.get("/status")
async def status():
    try:
        import dns.version
        return {"ok": True, "dnspython": dns.version.version}
    except ImportError:
        return {"ok": False, "dnspython": None}


@router.get("/resolvers")
async def resolvers():
    return {"resolvers": [{"id": k, "label": v["label"], "ips": v["ips"]} for k, v in RESOLVERS.items()],
            "propagation": [{"label": l, "ip": ip, "where": w} for (l, ip, w) in PROPAGATION],
            "types": RECORD_TYPES}


class QueryReq(BaseModel):
    name: str
    type: str = "A"
    resolver: str = "system"
    custom: Optional[str] = None
    tcp: bool = False


async def _guard(fn, *args):
    try:
        return await asyncio.to_thread(fn, *args)
    except ImportError:
        return _missing_dep()
    except InputError as e:
        return _err(str(e))
    except Exception as e:  # noqa: BLE001
        return _err(f"Erreur inattendue : {e}", 500)


@router.post("/query")
async def query(req: QueryReq):
    t = (req.type or "A").upper()
    if t in ("TOUS", "ANY"):
        t = "ALL"
    if t != "ALL" and t not in RECORD_TYPES:
        return _err("Type d'enregistrement non pris en charge.")
    return await _guard(run_query, req.name, t, req.resolver, req.custom, req.tcp)


class PropReq(BaseModel):
    name: str
    type: str = "A"


@router.post("/propagation")
async def propagation(req: PropReq):
    return await _guard(run_propagation, req.name, (req.type or "A").upper())


class EmailReq(BaseModel):
    domain: str
    selectors: str = ""
    resolver: str = "system"
    custom: Optional[str] = None


@router.post("/email")
async def email(req: EmailReq):
    sels = [s.strip() for s in re.split(r"[,\s;]+", req.selectors or "") if s.strip()]
    if len(sels) > 20:
        return _err("20 sélecteurs DKIM maximum.")
    for s in sels:
        if not SELECTOR_RE.match(s):
            return _err(f"Sélecteur DKIM invalide : {s}")
    return await _guard(run_email, req.domain, sels, req.resolver, req.custom)


@router.get("/history")
async def history(limit: int = 50):
    limit = max(1, min(200, limit))

    def get():
        _ensure_db()
        with _db() as conn:
            return [dict(r) for r in conn.execute("SELECT * FROM history ORDER BY id DESC LIMIT ?", (limit,))]
    return {"items": await asyncio.to_thread(get)}


@router.delete("/history")
async def clear_history():
    def clr():
        _ensure_db()
        with _db() as conn:
            conn.execute("DELETE FROM history")
    await asyncio.to_thread(clr)
    return {"ok": True}


@router.delete("/history/{item_id}")
async def delete_history(item_id: int):
    def rm():
        _ensure_db()
        with _db() as conn:
            conn.execute("DELETE FROM history WHERE id = ?", (item_id,))
    await asyncio.to_thread(rm)
    return {"ok": True}
