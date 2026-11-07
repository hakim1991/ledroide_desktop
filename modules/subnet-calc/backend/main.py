"""Calculateur de sous-réseaux IPv4 / IPv6 (module ipaddress, sans dépendance)."""
import asyncio
import csv
import io
import ipaddress
import math
import re
import tempfile
import time
import uuid
from itertools import islice
from pathlib import Path
from typing import Optional

from ledroide_sdk.http import FileResponse, JSONResponse
from pydantic import BaseModel
from ledroide_sdk import Module

module = Module(__file__)
router = module.router

TMP_DIR = Path(tempfile.gettempdir()) / "ledroide_subnet-calc"
MAX_ROWS = 4096


def _err(msg: str, status: int = 400):
    return JSONResponse(status_code=status, content={"error": msg})


class CalcError(Exception):
    pass


# ── Plages spéciales ───────────────────────────────────────────────────────

SPECIAL_V4 = [
    ("0.0.0.0/8", "« Ce réseau » (adresse source uniquement)", "RFC 1122"),
    ("10.0.0.0/8", "Privée", "RFC 1918"),
    ("100.64.0.0/10", "Partagée / CGNAT (NAT opérateur)", "RFC 6598"),
    ("127.0.0.0/8", "Boucle locale (loopback)", "RFC 1122"),
    ("169.254.0.0/16", "Lien local (APIPA)", "RFC 3927"),
    ("172.16.0.0/12", "Privée", "RFC 1918"),
    ("192.0.0.0/24", "Affectations protocolaires IETF", "RFC 6890"),
    ("192.0.2.0/24", "Documentation (TEST-NET-1)", "RFC 5737"),
    ("192.88.99.0/24", "Relais 6to4 anycast (obsolète)", "RFC 7526"),
    ("192.168.0.0/16", "Privée", "RFC 1918"),
    ("198.18.0.0/15", "Tests de performance (benchmark)", "RFC 2544"),
    ("198.51.100.0/24", "Documentation (TEST-NET-2)", "RFC 5737"),
    ("203.0.113.0/24", "Documentation (TEST-NET-3)", "RFC 5737"),
    ("224.0.0.0/4", "Multicast", "RFC 5771"),
    ("240.0.0.0/4", "Réservée (classe E)", "RFC 1112"),
    ("255.255.255.255/32", "Diffusion limitée", "RFC 919"),
]
SPECIAL_V6 = [
    ("::/128", "Adresse non spécifiée", "RFC 4291"),
    ("::1/128", "Boucle locale (loopback)", "RFC 4291"),
    ("::ffff:0:0/96", "IPv4 mappée", "RFC 4291"),
    ("64:ff9b::/96", "Traduction IPv4/IPv6 (NAT64)", "RFC 6052"),
    ("100::/64", "Rejet (discard-only)", "RFC 6666"),
    ("2001::/32", "Teredo", "RFC 4380"),
    ("2001:db8::/32", "Documentation", "RFC 3849"),
    ("2002::/16", "6to4", "RFC 3056"),
    ("fc00::/7", "Adresse locale unique (ULA, privée)", "RFC 4193"),
    ("fe80::/10", "Lien local", "RFC 4291"),
    ("ff00::/8", "Multicast", "RFC 4291"),
]
_SPECIAL_V4 = [(ipaddress.ip_network(n), l, r) for n, l, r in SPECIAL_V4]
_SPECIAL_V6 = [(ipaddress.ip_network(n), l, r) for n, l, r in SPECIAL_V6]


def _special(net) -> Optional[dict]:
    table = _SPECIAL_V4 if net.version == 4 else _SPECIAL_V6
    best = None
    for n, label, rfc in table:
        if net.subnet_of(n) and (best is None or n.prefixlen > best[0].prefixlen):
            best = (n, label, rfc)
    if best:
        return {"range": str(best[0]), "label": best[1], "rfc": best[2]}
    return None


def _scope(net, special) -> str:
    if special:
        lab = special["label"]
        if "Privée" in lab or "ULA" in lab:
            return "privée"
        if "CGNAT" in lab:
            return "partagée"
        return "spéciale"
    if net.version == 6 and not net.subnet_of(ipaddress.ip_network("2000::/3")):
        return "réservée"
    return "publique"


def _v4_class(ip: ipaddress.IPv4Address) -> dict:
    first = int(ip) >> 24
    if first < 128:
        return {"cls": "A", "default_prefix": 8, "range": "0.0.0.0 – 127.255.255.255"}
    if first < 192:
        return {"cls": "B", "default_prefix": 16, "range": "128.0.0.0 – 191.255.255.255"}
    if first < 224:
        return {"cls": "C", "default_prefix": 24, "range": "192.0.0.0 – 223.255.255.255"}
    if first < 240:
        return {"cls": "D", "default_prefix": None, "range": "224.0.0.0 – 239.255.255.255 (multicast)"}
    return {"cls": "E", "default_prefix": None, "range": "240.0.0.0 – 255.255.255.255 (réservée)"}


# ── Parsing ────────────────────────────────────────────────────────────────

def parse_interface(raw: str) -> tuple[ipaddress._BaseAddress, int, list[str]]:
    """Accepte « ip/prefixe », « ip/masque », « ip masque », « ip wildcard », « ip »."""
    s = (raw or "").strip()
    if not s:
        raise CalcError("Saisissez une adresse (ex. 192.168.1.10/24 ou 2001:db8::1/64).")
    notes = []
    parts = [p for p in re.split(r"[\s/]+", s) if p]
    if len(parts) > 2:
        raise CalcError("Format non reconnu. Exemples : 192.168.1.10/24, 10.0.0.1 255.255.0.0, 2001:db8::/48.")
    try:
        ip = ipaddress.ip_address(parts[0])
    except ValueError:
        raise CalcError(f"Adresse IP invalide : {parts[0]}")
    maxlen = ip.max_prefixlen
    if len(parts) == 1:
        if ip.version == 4:
            c = _v4_class(ip)
            prefix = c["default_prefix"] or 32
            notes.append(f"Aucun masque indiqué : masque par défaut de la classe {c['cls']} (/{prefix}).")
        else:
            prefix = 64
            notes.append("Aucun préfixe indiqué : /64 supposé.")
        return ip, prefix, notes
    m = parts[1]
    if m.isdigit():
        prefix = int(m)
        if not 0 <= prefix <= maxlen:
            raise CalcError(f"Préfixe invalide : /{prefix} (0 à {maxlen}).")
        return ip, prefix, notes
    if ip.version == 6:
        raise CalcError("En IPv6, indiquez une longueur de préfixe (ex. /64).")
    try:
        mask = ipaddress.IPv4Address(m)
    except ValueError:
        raise CalcError(f"Masque invalide : {m}")
    mi = int(mask)
    bits = format(mi, "032b")
    if re.fullmatch(r"1*0*", bits):
        return ip, bits.count("1"), notes
    if re.fullmatch(r"0*1*", bits):
        notes.append(f"Masque générique (wildcard) {m} interprété comme /{bits.count('0')}.")
        return ip, bits.count("0"), notes
    raise CalcError(f"Masque non contigu : {m}")


def parse_network(raw: str, strict_note: list | None = None):
    ip, prefix, notes = parse_interface(raw)
    net = ipaddress.ip_network(f"{ip}/{prefix}", strict=False)
    if strict_note is not None:
        strict_note.extend(notes)
        if net.network_address != ip:
            strict_note.append(f"{ip}/{prefix} normalisé en {net}.")
    return net


def parse_list(raw) -> list[str]:
    if isinstance(raw, list):
        items = raw
    else:
        items = re.split(r"[\n,;]+", raw or "")
    return [x.strip() for x in items if x and x.strip() and not x.strip().startswith("#")]


# ── Calculs ────────────────────────────────────────────────────────────────

def _bin(addr) -> str:
    return format(int(addr), f"0{addr.max_prefixlen}b")


def _hosts_range(net):
    """(première, dernière, nb d'hôtes utilisables)."""
    total = net.num_addresses
    if net.version == 4:
        if net.prefixlen == 32:
            return net.network_address, net.network_address, 1
        if net.prefixlen == 31:
            return net.network_address, net.broadcast_address, 2
        return net.network_address + 1, net.broadcast_address - 1, total - 2
    # IPv6 : pas d'adresse de diffusion, toutes les adresses sont attribuables
    return net.network_address, net.broadcast_address, total


def _fmt_big(n: int) -> str:
    s = f"{n:,}".replace(",", " ")
    return s


def _pow2(n: int) -> str:
    if n > 0 and n & (n - 1) == 0:
        return f"2^{n.bit_length() - 1}"
    return ""


def calc_info(raw: str) -> dict:
    ip, prefix, notes = parse_interface(raw)
    iface = ipaddress.ip_interface(f"{ip}/{prefix}")
    net = iface.network
    first, last, usable = _hosts_range(net)
    special = _special(ipaddress.ip_network(f"{ip}/{ip.max_prefixlen}"))
    net_special = _special(net)
    out = {
        "version": ip.version, "input": raw.strip(), "notes": notes,
        "address": str(ip), "prefix": prefix, "cidr": f"{net.network_address}/{prefix}",
        "network": str(net.network_address), "first_host": str(first), "last_host": str(last),
        "total": str(net.num_addresses), "total_fmt": _fmt_big(net.num_addresses), "total_pow": _pow2(net.num_addresses),
        "usable": str(usable), "usable_fmt": _fmt_big(usable),
        "special": special, "network_special": net_special,
        "scope": _scope(ipaddress.ip_network(f"{ip}/{ip.max_prefixlen}"), special),
        "bits": ip.max_prefixlen,
        "bin_address": _bin(ip), "bin_network": _bin(net.network_address),
        "bin_mask": _bin(net.netmask), "bin_last": _bin(net.broadcast_address),
        "index_in_network": str(int(ip) - int(net.network_address)),
        "is_network_address": ip == net.network_address and net.num_addresses > 2,
        "integer": str(int(ip)), "hex": hex(int(ip)),
        "reverse_pointer": ip.reverse_pointer,
    }
    if net.prefixlen > 0:
        sup = net.supernet()
        out["supernet"] = str(sup)
    try:
        nxt = ipaddress.ip_network(f"{net.broadcast_address + 1}/{prefix}")
        out["next_network"] = str(nxt)
    except (ValueError, ipaddress.AddressValueError):
        out["next_network"] = None
    try:
        prv = ipaddress.ip_network(f"{net.network_address - net.num_addresses}/{prefix}")
        out["prev_network"] = str(prv)
    except (ValueError, ipaddress.AddressValueError):
        out["prev_network"] = None

    if ip.version == 4:
        out.update({
            "broadcast": str(net.broadcast_address) if prefix < 31 else None,
            "netmask": str(net.netmask), "wildcard": str(net.hostmask),
            "is_broadcast_address": ip == net.broadcast_address and prefix < 31,
            "bin_wildcard": _bin(net.hostmask),
            "class": _v4_class(ip),
            "reverse_zone": ".".join(reversed(str(net.network_address).split(".")[: max(1, prefix // 8)])) + ".in-addr.arpa"
            if prefix >= 8 else "in-addr.arpa",
        })
        if prefix == 31:
            out["notes"].append("/31 : liaison point à point (RFC 3021), 2 adresses utilisables, pas de diffusion.")
        if prefix == 32:
            out["notes"].append("/32 : route ou adresse d'hôte unique.")
        out["mask_hex"] = "0x%08X" % int(net.netmask)
    else:
        out.update({
            "broadcast": None, "netmask": None, "wildcard": None,
            "exploded": ip.exploded, "compressed": ip.compressed,
            "network_exploded": net.network_address.exploded,
            "last_address": str(net.broadcast_address),
            "subnets_64": str(2 ** (64 - prefix)) if prefix <= 64 else None,
            "subnets_64_fmt": _fmt_big(2 ** (64 - prefix)) if prefix <= 64 else None,
        })
        nib = prefix // 4
        rz = ".".join(reversed(net.network_address.exploded.replace(":", "")[:nib]))
        out["reverse_zone"] = (rz + "." if rz else "") + "ip6.arpa"
        if ip.ipv4_mapped:
            out["embedded_ipv4"] = str(ip.ipv4_mapped)
        elif ip.sixtofour:
            out["embedded_ipv4"] = str(ip.sixtofour)
        elif ip.teredo:
            out["embedded_ipv4"] = f"serveur {ip.teredo[0]}, client {ip.teredo[1]}"
        if prefix > 64 and not ip.is_loopback:
            out["notes"].append("Préfixe plus long que /64 : l'autoconfiguration SLAAC ne fonctionnera pas sur ce réseau.")
    return out


def calc_split(network: str, mode: str, value: int) -> dict:
    notes: list[str] = []
    net = parse_network(network, notes)
    maxp = net.max_prefixlen
    if mode == "count":
        if value < 1:
            raise CalcError("Le nombre de sous-réseaux doit être ≥ 1.")
        add = math.ceil(math.log2(value)) if value > 1 else 0
        new_prefix = net.prefixlen + add
        if 2 ** add != value:
            notes.append(f"{value} sous-réseaux demandés → arrondi à {2 ** add} (puissance de 2), préfixe /{new_prefix}.")
    elif mode == "prefix":
        new_prefix = value
    elif mode == "hosts":
        if value < 1:
            raise CalcError("Le nombre d'hôtes doit être ≥ 1.")
        need = value + 2 if net.version == 4 else value + 1
        new_prefix = maxp - max(0, math.ceil(math.log2(need)))
        notes.append(f"{value} hôte(s) par sous-réseau → /{new_prefix}.")
    else:
        raise CalcError("Mode de découpage inconnu.")
    if new_prefix < net.prefixlen:
        raise CalcError(f"Le nouveau préfixe /{new_prefix} doit être plus long que celui du réseau (/{net.prefixlen}).")
    if new_prefix > maxp:
        raise CalcError(f"Impossible : le découpage dépasse /{maxp}.")
    count = 2 ** (new_prefix - net.prefixlen)
    rows = []
    for i, sn in enumerate(islice(net.subnets(new_prefix=new_prefix), MAX_ROWS)):
        f, l, u = _hosts_range(sn)
        rows.append({
            "index": i + 1, "network": str(sn.network_address), "cidr": str(sn),
            "first": str(f), "last": str(l),
            "broadcast": str(sn.broadcast_address) if sn.version == 4 and sn.prefixlen < 31 else None,
            "usable": str(u),
        })
    sample = next(net.subnets(new_prefix=new_prefix))
    f, l, u = _hosts_range(sample)
    return {"network": str(net), "version": net.version, "new_prefix": new_prefix,
            "netmask": str(sample.netmask) if net.version == 4 else None,
            "count": str(count), "count_fmt": _fmt_big(count), "shown": len(rows), "truncated": count > len(rows),
            "usable_each": str(u), "usable_each_fmt": _fmt_big(u), "rows": rows, "notes": notes}


def calc_vlsm(network: str, reqs: list[dict]) -> dict:
    notes: list[str] = []
    net = parse_network(network, notes)
    maxp = net.max_prefixlen
    if not reqs:
        raise CalcError("Ajoutez au moins un besoin (nom + nombre d'hôtes).")
    if len(reqs) > 500:
        raise CalcError("500 besoins maximum.")
    items = []
    for i, r in enumerate(reqs):
        name = str(r.get("name") or f"Réseau {i + 1}").strip()[:60] or f"Réseau {i + 1}"
        try:
            hosts = int(r.get("hosts"))
        except (TypeError, ValueError):
            raise CalcError(f"Nombre d'hôtes invalide pour « {name} ».")
        if hosts < 1:
            raise CalcError(f"Nombre d'hôtes invalide pour « {name} » (≥ 1).")
        if net.version == 4:
            need = hosts + 2
            prefix = maxp - math.ceil(math.log2(need))
            prefix = min(prefix, 30)
        else:
            prefix = maxp - math.ceil(math.log2(hosts + 1)) if hosts > 1 else maxp
        if prefix < 0:
            raise CalcError(f"« {name} » demande trop d'hôtes.")
        items.append({"order": i, "name": name, "hosts": hosts, "prefix": prefix})
    items.sort(key=lambda x: (x["prefix"], x["order"]))
    cursor = int(net.network_address)
    end = int(net.broadcast_address)
    allocs = []
    used = 0
    for it in items:
        size = 2 ** (maxp - it["prefix"])
        start = (cursor + size - 1) // size * size  # alignement
        if it["prefix"] < net.prefixlen or start + size - 1 > end:
            allocs.append({**it, "ok": False, "reason": "Espace insuffisant dans le réseau parent."})
            continue
        sn = ipaddress.ip_network((start, it["prefix"])) if net.version == 4 else ipaddress.IPv6Network((start, it["prefix"]))
        f, l, u = _hosts_range(sn)
        allocs.append({
            **it, "ok": True, "cidr": str(sn), "network": str(sn.network_address),
            "netmask": str(sn.netmask) if net.version == 4 else None,
            "first": str(f), "last": str(l),
            "broadcast": str(sn.broadcast_address) if net.version == 4 else None,
            "usable": str(u), "waste": str(u - it["hosts"]),
            "efficiency": round(it["hosts"] / u * 100, 1) if u else 0,
        })
        cursor = start + size
        used += size
    # espace libre restant
    free = [net]
    for a in allocs:
        if not a["ok"]:
            continue
        sn = ipaddress.ip_network(a["cidr"])
        nf = []
        for blk in free:
            if sn.subnet_of(blk):
                nf.extend(blk.address_exclude(sn))
            else:
                nf.append(blk)
        free = nf
    free = sorted(ipaddress.collapse_addresses(free))
    total = net.num_addresses
    return {"network": str(net), "version": net.version, "allocations": allocs,
            "allocated": sum(1 for a in allocs if a["ok"]), "failed": sum(1 for a in allocs if not a["ok"]),
            "used": str(used), "total": str(total), "used_pct": round(used / total * 100, 2),
            "requested_hosts": sum(i["hosts"] for i in items),
            "free": [str(x) for x in free[:200]], "free_count": len(free), "notes": notes}


def _rel(a, b) -> Optional[str]:
    if a.version != b.version:
        return None
    if a == b:
        return "identiques"
    if a.subnet_of(b):
        return "A ⊂ B"
    if b.subnet_of(a):
        return "B ⊂ A"
    if a.overlaps(b):
        return "chevauchement"
    return None


def calc_membership(ips_raw, nets_raw) -> dict:
    nets, errors = [], []
    for s in parse_list(nets_raw)[:500]:
        try:
            notes: list[str] = []
            nets.append((s, parse_network(s, notes)))
        except CalcError as e:
            errors.append(f"{s} : {e}")
    if not nets:
        raise CalcError("Saisissez au moins un réseau (CIDR).")
    results = []
    for s in parse_list(ips_raw)[:1000]:
        try:
            ip = ipaddress.ip_address(s.split("/")[0])
        except ValueError:
            errors.append(f"{s} : adresse IP invalide")
            continue
        inside = []
        for label, n in nets:
            if ip.version == n.version and ip in n:
                kind = ""
                if n.version == 4 and n.prefixlen < 31:
                    if ip == n.network_address:
                        kind = "adresse réseau"
                    elif ip == n.broadcast_address:
                        kind = "adresse de diffusion"
                inside.append({"network": str(n), "input": label, "note": kind})
        # Le plus spécifique en premier (longest prefix match)
        inside.sort(key=lambda x: -ipaddress.ip_network(x["network"]).prefixlen)
        results.append({"ip": str(ip), "networks": inside, "match": bool(inside),
                        "best": inside[0]["network"] if inside else None})
    overlaps = []
    for i in range(len(nets)):
        for j in range(i + 1, len(nets)):
            r = _rel(nets[i][1], nets[j][1])
            if r:
                overlaps.append({"a": str(nets[i][1]), "b": str(nets[j][1]), "relation": r,
                                 "common": str(min(nets[i][1], nets[j][1], key=lambda n: n.num_addresses))})
    return {"networks": [{"input": l, "network": str(n), "normalized": l.split()[0] != str(n)} for l, n in nets],
            "results": results, "overlaps": overlaps, "errors": errors}


def calc_summarize(nets_raw) -> dict:
    nets, errors = [], []
    for s in parse_list(nets_raw)[:5000]:
        try:
            nets.append(parse_network(s, []))
        except CalcError as e:
            errors.append(f"{s} : {e}")
    if not nets:
        raise CalcError("Saisissez au moins un réseau.")
    out = {"input_count": len(nets), "errors": errors, "versions": []}
    for v in (4, 6):
        group = [n for n in nets if n.version == v]
        if not group:
            continue
        collapsed = list(ipaddress.collapse_addresses(group))
        lo = min(int(n.network_address) for n in group)
        hi = max(int(n.broadcast_address) for n in group)
        maxp = 32 if v == 4 else 128
        diff = lo ^ hi
        prefix = maxp - diff.bit_length()
        sup = ipaddress.ip_network((lo >> (maxp - prefix) << (maxp - prefix), prefix)) if v == 4 \
            else ipaddress.IPv6Network((lo >> (maxp - prefix) << (maxp - prefix), prefix))
        covered = sum(n.num_addresses for n in collapsed)
        out["versions"].append({
            "version": v, "input": len(group), "summarized": [str(n) for n in collapsed],
            "summary_count": len(collapsed), "addresses": str(covered), "addresses_fmt": _fmt_big(covered),
            "supernet": str(sup), "supernet_addresses_fmt": _fmt_big(sup.num_addresses),
            "supernet_extra": str(sup.num_addresses - covered),
            "supernet_extra_fmt": _fmt_big(sup.num_addresses - covered),
            "exact": len(collapsed) == 1,
        })
    return out


# ── Export CSV ─────────────────────────────────────────────────────────────

def _cleanup_tmp():
    try:
        now = time.time()
        for f in TMP_DIR.glob("*.csv"):
            if now - f.stat().st_mtime > 3600:
                f.unlink()
    except OSError:
        pass


def write_csv(name_hint: str, headers: list[str], rows: list[list]) -> str:
    TMP_DIR.mkdir(parents=True, exist_ok=True)
    _cleanup_tmp()
    base = re.sub(r"[^A-Za-z0-9_.-]+", "_", name_hint or "sous-reseaux").strip("._") or "sous-reseaux"
    fname = f"{base[:60]}_{uuid.uuid4().hex[:6]}.csv"
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";", lineterminator="\r\n")
    w.writerow(headers)
    for r in rows:
        w.writerow(["" if c is None else c for c in r])
    (TMP_DIR / fname).write_bytes(buf.getvalue().encode("utf-8-sig"))
    return fname


def export_split(network: str, mode: str, value: int) -> str:
    notes: list[str] = []
    net = parse_network(network, notes)
    res = calc_split(network, mode, value)
    if int(res["count"]) > 65536:
        raise CalcError("Export limité à 65 536 sous-réseaux.")
    rows = []
    for i, sn in enumerate(net.subnets(new_prefix=res["new_prefix"])):
        f, l, u = _hosts_range(sn)
        rows.append([i + 1, str(sn), str(sn.network_address), str(sn.netmask) if sn.version == 4 else "",
                     str(f), str(l), str(sn.broadcast_address) if sn.version == 4 and sn.prefixlen < 31 else "", u])
    return write_csv(f"decoupage_{net}",
                     ["#", "CIDR", "Réseau", "Masque", "Premier hôte", "Dernier hôte", "Diffusion", "Hôtes utilisables"], rows)


def export_vlsm(network: str, reqs: list[dict]) -> str:
    res = calc_vlsm(network, reqs)
    rows = []
    for a in res["allocations"]:
        if a["ok"]:
            rows.append([a["name"], a["hosts"], a["cidr"], a["netmask"] or "", a["first"], a["last"],
                         a["broadcast"] or "", a["usable"], a["waste"]])
        else:
            rows.append([a["name"], a["hosts"], "NON ALLOUÉ", "", "", "", "", "", ""])
    return write_csv(f"vlsm_{res['network']}",
                     ["Nom", "Hôtes demandés", "CIDR", "Masque", "Premier hôte", "Dernier hôte", "Diffusion",
                      "Hôtes utilisables", "Adresses perdues"], rows)


# ── Routes ─────────────────────────────────────────────────────────────────

async def _run(fn, *args):
    try:
        return await asyncio.to_thread(fn, *args)
    except CalcError as e:
        return _err(str(e))
    except (ValueError, TypeError) as e:
        return _err(f"Entrée invalide : {e}")


@router.get("/status")
async def status():
    return {"ok": True}


class InfoReq(BaseModel):
    input: str


@router.post("/info")
async def info(req: InfoReq):
    return await _run(calc_info, req.input)


class SplitReq(BaseModel):
    network: str
    mode: str = "count"
    value: int = 2


@router.post("/split")
async def split(req: SplitReq):
    return await _run(calc_split, req.network, req.mode, req.value)


class VlsmReq(BaseModel):
    network: str
    requirements: list[dict]


@router.post("/vlsm")
async def vlsm(req: VlsmReq):
    return await _run(calc_vlsm, req.network, req.requirements)


class MemberReq(BaseModel):
    ips: str = ""
    networks: str = ""


@router.post("/membership")
async def membership(req: MemberReq):
    return await _run(calc_membership, req.ips, req.networks)


class SummReq(BaseModel):
    networks: str


@router.post("/summarize")
async def summarize(req: SummReq):
    return await _run(calc_summarize, req.networks)


@router.post("/export/split")
async def export_split_route(req: SplitReq):
    res = await _run(export_split, req.network, req.mode, req.value)
    return res if isinstance(res, JSONResponse) else {"file": res}


@router.post("/export/vlsm")
async def export_vlsm_route(req: VlsmReq):
    res = await _run(export_vlsm, req.network, req.requirements)
    return res if isinstance(res, JSONResponse) else {"file": res}


@router.get("/file/{filename}")
async def get_file(filename: str):
    if "/" in filename or "\\" in filename or ".." in filename or not re.fullmatch(r"[A-Za-z0-9_.-]+\.csv", filename):
        return _err("Nom de fichier invalide.")
    path = TMP_DIR / filename
    if not path.is_file():
        return _err("Fichier introuvable (expiré ?).", 404)
    return FileResponse(path, media_type="text/csv; charset=utf-8", filename=filename,
                        headers={"Content-Disposition": f'attachment; filename="{filename}"'})
