"""Configuration IP — ipconfig amélioré (cartes, ARP, routes, cache DNS, actions)."""
import asyncio
import ctypes
import importlib.util
import ipaddress
import json
import re
import socket
import subprocess
import sys
from pathlib import Path
from typing import Optional

from ledroide_sdk.http import JSONResponse
from pydantic import BaseModel
from ledroide_sdk import Module

module = Module(__file__)
router = module.router


def _load_elevate():
    name = "ip_config_elevate"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, Path(__file__).parent / "elevate.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


elevate = _load_elevate()

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _oem_codec() -> str:
    try:
        return f"cp{ctypes.windll.kernel32.GetOEMCP()}"
    except Exception:
        return "cp850"


def _err(msg: str, status: int = 400, code: Optional[str] = None):
    content = {"error": msg}
    if code:
        content["code"] = code
    return JSONResponse(status_code=status, content=content)


# ── Exécution PowerShell / natif ───────────────────────────────────────────

class PSError(Exception):
    pass


def _ps_json(script: str, timeout: float = 30):
    full = "[Console]::OutputEncoding=[Text.Encoding]::UTF8;$ErrorActionPreference='SilentlyContinue';" + script
    try:
        p = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", full],
            capture_output=True, timeout=timeout, creationflags=NO_WINDOW,
        )
    except subprocess.TimeoutExpired:
        raise PSError("Délai dépassé lors de l'interrogation de PowerShell.")
    except FileNotFoundError:
        raise PSError("PowerShell introuvable.")
    out = p.stdout.decode("utf-8", errors="replace").strip().lstrip("﻿")
    if not out:
        err = p.stderr.decode("utf-8", errors="replace").strip()
        if err:
            raise PSError(err[:400])
        return []
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        raise PSError("Réponse PowerShell illisible.")
    if data is None:
        return []
    return data if isinstance(data, list) else [data]


def _run_native(args: list[str], timeout: float = 60) -> tuple[int, str]:
    p = subprocess.run(args, capture_output=True, timeout=timeout, creationflags=NO_WINDOW)
    codec = _oem_codec()
    text = (p.stdout or b"").decode(codec, errors="replace") + (p.stderr or b"").decode(codec, errors="replace")
    return p.returncode, text.replace("\r\n", "\n").strip("\n")


# ── Tables de correspondance (enums CIM renvoyés en entiers) ───────────────

FAMILY = {2: "IPv4", 23: "IPv6"}
NEIGHBOR_STATE = {0: "Injoignable", 1: "Incomplet", 2: "Sonde", 3: "Délai", 4: "Obsolète",
                  5: "Joignable", 6: "Permanent", 7: "TBD"}
PREFIX_ORIGIN = {0: "Autre", 1: "Manuelle", 2: "Bien connue", 3: "DHCP", 4: "Annonce routeur"}
SUFFIX_ORIGIN = {0: "Autre", 1: "Manuelle", 2: "Bien connue", 3: "DHCP", 4: "Lien", 5: "Aléatoire"}
ADDR_STATE = {0: "Invalide", 1: "Provisoire", 2: "Dupliquée", 3: "Dépréciée", 4: "Préférée"}
ROUTE_PROTOCOL = {1: "Autre", 2: "Local", 3: "Statique", 4: "ICMP", 5: "EGP", 6: "GGP", 7: "Hello",
                  8: "RIP", 9: "IS-IS", 10: "ES-IS", 11: "IGRP", 12: "BBN", 13: "OSPF", 14: "BGP",
                  15: "IDPR", 16: "EIGRP", 17: "DVMRP", 18: "RPL", 19: "DHCP"}
ROUTE_STORE = {0: "Persistante", 1: "Active"}
DNS_TYPES = {1: "A", 2: "NS", 5: "CNAME", 6: "SOA", 12: "PTR", 15: "MX", 16: "TXT", 28: "AAAA",
             33: "SRV", 39: "DNAME", 43: "DS", 46: "RRSIG", 47: "NSEC", 48: "DNSKEY", 64: "SVCB",
             65: "HTTPS", 257: "CAA", 255: "ANY"}
DNS_SECTION = {1: "Réponse", 2: "Autorité", 3: "Additionnelle"}
DNS_STATUS = {0: "Succès", 9003: "Nom inexistant", 9501: "Aucun enregistrement", 9701: "Aucun enregistrement"}
STATUS_FR = {"Up": "Connecté", "Disconnected": "Déconnecté", "Disabled": "Désactivé",
             "Not Present": "Absent", "Dormant": "En veille", "Down": "Inactif"}

VIRTUAL_RE = re.compile(
    r"hyper-v|virtual|vmware|virtualbox|tap-|tap |wintun|wireguard|vpn|loopback|miniport|"
    r"wi-fi direct|teredo|isatap|docker|vethernet|npcap|zerotier|tailscale|openvpn|fortinet|cisco anyconnect",
    re.I)


def _int(v, default=0):
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _as_list(v):
    if v is None:
        return []
    return v if isinstance(v, list) else [v]


def _adapter_type(a: dict) -> str:
    desc = f"{a.get('description') or ''} {a.get('name') or ''}"
    pm = (a.get("physical_media") or "") + " " + (a.get("media") or "")
    if re.search(r"bluetooth", desc, re.I):
        return "Bluetooth"
    if re.search(r"vpn|wireguard|wintun|tap-|openvpn|tailscale|zerotier|fortinet|anyconnect", desc, re.I):
        return "VPN"
    if a.get("virtual"):
        return "Virtuel"
    if "802.11" in pm or re.search(r"wi-?fi|wireless|wlan", desc, re.I):
        return "Wi-Fi"
    if "802.3" in pm or re.search(r"ethernet|gbe", desc, re.I):
        return "Ethernet"
    if re.search(r"wwan|mobile|lte|5g", pm + desc, re.I):
        return "Mobile"
    return pm.strip() or "Autre"


ADAPTERS_PS = r"""
$ads = @(Get-NetAdapter -IncludeHidden)
$ipif = @(Get-NetIPInterface)
$bound = @{}; foreach($x in $ipif){ $bound[[int]$x.ifIndex] = $true }
$cfg=@{}; foreach($c in @(Get-CimInstance Win32_NetworkAdapterConfiguration)){ $cfg[[int]$c.InterfaceIndex]=$c }
$ips = @(Get-NetIPAddress)
$gws = @(Get-NetRoute -DestinationPrefix '0.0.0.0/0','::/0')
$dns = @(Get-DnsClientServerAddress)
$dc = @{}; foreach($d in @(Get-DnsClient)){ $dc[[int]$d.InterfaceIndex]=$d }
function iso($d){ if($d){ try { ([datetime]$d).ToString('o') } catch { $null } } else { $null } }
$res = foreach($a in $ads){
  $i=[int]$a.ifIndex
  if($a.Hidden -and -not $bound.ContainsKey($i)){ continue }
  $c=$cfg[$i]
  $v4 = $ipif | Where-Object { $_.ifIndex -eq $i -and [int]$_.AddressFamily -eq 2 } | Select-Object -First 1
  $v6 = $ipif | Where-Object { $_.ifIndex -eq $i -and [int]$_.AddressFamily -eq 23 } | Select-Object -First 1
  [pscustomobject]@{
    name=$a.Name; description=$a.InterfaceDescription; ifIndex=$i; status=[string]$a.Status
    mac=$a.MacAddress; link_speed=[string]$a.LinkSpeed; speed_bps=[int64]$a.Speed
    media=[string]$a.MediaType; physical_media=[string]$a.PhysicalMediaType
    virtual_flag=[bool]$a.Virtual; hardware=[bool]$a.HardwareInterface; hidden=[bool]$a.Hidden
    mtu=[int]$a.MtuSize; driver=[string]$a.DriverDescription; driver_version=[string]$a.DriverVersionString
    media_state=[string]$a.MediaConnectionState
    dhcp4 = if($v4){ [int]$v4.Dhcp } else { $null }
    dhcp6 = if($v6){ [int]$v6.Dhcp } else { $null }
    metric4 = if($v4){ [int]$v4.InterfaceMetric } else { $null }
    metric6 = if($v6){ [int]$v6.InterfaceMetric } else { $null }
    addresses=@($ips | Where-Object { $_.InterfaceIndex -eq $i } | ForEach-Object {
      [pscustomobject]@{ ip=$_.IPAddress; prefix=[int]$_.PrefixLength; family=[int]$_.AddressFamily
        prefix_origin=[int]$_.PrefixOrigin; suffix_origin=[int]$_.SuffixOrigin; state=[int]$_.AddressState
        valid_s=[double]$_.ValidLifetime.TotalSeconds; preferred_s=[double]$_.PreferredLifetime.TotalSeconds } })
    gateways=@($gws | Where-Object { $_.ifIndex -eq $i } | ForEach-Object {
      [pscustomobject]@{ ip=$_.NextHop; metric=[int]$_.RouteMetric; family=[int]$_.AddressFamily } })
    dns_servers=@($dns | Where-Object { $_.InterfaceIndex -eq $i } | ForEach-Object { $_.ServerAddresses } | Where-Object { $_ })
    dns_suffix = if($dc[$i]){ [string]$dc[$i].ConnectionSpecificSuffix } else { $null }
    dns_register = if($dc[$i]){ [bool]$dc[$i].RegisterThisConnectionsAddress } else { $null }
    dhcp_enabled = if($c){ [bool]$c.DHCPEnabled } else { $null }
    dhcp_server = if($c){ $c.DHCPServer } else { $null }
    lease_obtained = if($c){ iso $c.DHCPLeaseObtained } else { $null }
    lease_expires = if($c){ iso $c.DHCPLeaseExpires } else { $null }
    netbios = if($c){ [int]$c.TcpipNetbiosOptions } else { $null }
    wins = if($c){ @($c.WINSPrimaryServer, $c.WINSSecondaryServer) | Where-Object { $_ } } else { @() }
  }
}
$cs = Get-CimInstance Win32_ComputerSystem
$g = Get-DnsClientGlobalSetting
$hostinfo = [pscustomobject]@{
  hostname=$env:COMPUTERNAME; dns_hostname=[System.Net.Dns]::GetHostName(); domain=[string]$cs.Domain
  part_of_domain=[bool]$cs.PartOfDomain; suffix_search=@($g.SuffixSearchList)
}
ConvertTo-Json -InputObject @([pscustomobject]@{ host=$hostinfo; adapters=@($res) }) -Depth 6 -Compress
"""


def _fmt_lifetime(s) -> Optional[str]:
    try:
        s = float(s)
    except (TypeError, ValueError):
        return None
    if s > 10 * 365 * 86400:
        return "infinie"
    s = int(s)
    d, r = divmod(s, 86400)
    h, r = divmod(r, 3600)
    m, _ = divmod(r, 60)
    if d:
        return f"{d} j {h} h"
    if h:
        return f"{h} h {m} min"
    return f"{m} min"


def _normalize_adapter(a: dict) -> dict:
    a["status_fr"] = STATUS_FR.get(a.get("status") or "", a.get("status") or "Inconnu")
    a["up"] = a.get("status") == "Up"
    desc = f"{a.get('description') or ''} {a.get('name') or ''}"
    a["virtual"] = bool(a.get("virtual_flag") or a.get("hidden") or not a.get("hardware")
                        or VIRTUAL_RE.search(desc))
    a["type"] = _adapter_type(a)
    addrs = []
    for x in _as_list(a.get("addresses")):
        if not isinstance(x, dict):
            continue
        ip = str(x.get("ip") or "")
        fam = _int(x.get("family"))
        addrs.append({
            "ip": ip.split("%")[0], "scope": ip.split("%")[1] if "%" in ip else None,
            "prefix": _int(x.get("prefix")), "family": FAMILY.get(fam, str(fam)),
            "origin": PREFIX_ORIGIN.get(_int(x.get("prefix_origin")), "?"),
            "suffix_origin": SUFFIX_ORIGIN.get(_int(x.get("suffix_origin")), "?"),
            "state": ADDR_STATE.get(_int(x.get("state")), "?"),
            "link_local": ip.lower().startswith("fe80") or ip.startswith("169.254."),
            "temporary": _int(x.get("suffix_origin")) == 5,
            "valid": _fmt_lifetime(x.get("valid_s")), "preferred": _fmt_lifetime(x.get("preferred_s")),
        })
    addrs.sort(key=lambda x: (x["family"] != "IPv4", x["link_local"], x["ip"]))
    for x in addrs:
        if x["family"] == "IPv4":
            try:
                x["mask"] = str(ipaddress.ip_network(f"0.0.0.0/{x['prefix']}").netmask)
            except ValueError:
                x["mask"] = None
    a["addresses"] = addrs
    gws = []
    for g in _as_list(a.get("gateways")):
        if isinstance(g, dict) and g.get("ip") and g["ip"] not in ("0.0.0.0", "::"):
            gws.append({"ip": g["ip"], "metric": _int(g.get("metric")), "family": FAMILY.get(_int(g.get("family")), "?")})
    a["gateways"] = gws
    a["dns_servers"] = [str(s) for s in _as_list(a.get("dns_servers")) if s]
    a["wins"] = [str(s) for s in _as_list(a.get("wins")) if s]
    for k in ("dhcp4", "dhcp6"):
        if a.get(k) is not None:
            a[k] = bool(_int(a[k]))
    if a.get("dhcp_enabled") is None:
        a["dhcp_enabled"] = a.get("dhcp4")
    if not a["dhcp_enabled"]:
        a["dhcp_server"] = None
        a["lease_obtained"] = None
        a["lease_expires"] = None
    for k in ("virtual_flag",):
        a.pop(k, None)
    return a


def get_adapters() -> dict:
    data = _ps_json(ADAPTERS_PS, timeout=45)
    root = data[0] if data else {}
    adapters = [_normalize_adapter(a) for a in _as_list(root.get("adapters")) if isinstance(a, dict)]
    adapters.sort(key=lambda a: (not a["up"], a["virtual"], a.get("name") or ""))
    hostinfo = root.get("host") or {}
    hostinfo["suffix_search"] = [s for s in _as_list(hostinfo.get("suffix_search")) if s]
    hostinfo["is_admin"] = elevate.is_admin()
    return {"host": hostinfo, "adapters": adapters}


def get_arp() -> list:
    rows = _ps_json(
        "ConvertTo-Json -Compress -InputObject @(Get-NetNeighbor | ForEach-Object { [pscustomobject]@{"
        "ip=$_.IPAddress; mac=$_.LinkLayerAddress; state=[int]$_.State; family=[int]$_.AddressFamily;"
        "ifIndex=[int]$_.ifIndex; interface=$_.InterfaceAlias; store=[int]$_.Store } })")
    out = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        ip = str(r.get("ip") or "")
        mac = str(r.get("mac") or "")
        kind = "unicast"
        low = ip.lower()
        if low.startswith("ff") or re.match(r"^(22[4-9]|23\d)\.", ip):
            kind = "multicast"
        elif ip.endswith(".255") or ip == "255.255.255.255" or mac.upper() == "FF-FF-FF-FF-FF-FF":
            kind = "broadcast"
        out.append({
            "ip": ip, "mac": mac if mac and mac != "00-00-00-00-00-00" else "",
            "state": NEIGHBOR_STATE.get(_int(r.get("state")), "?"),
            "family": FAMILY.get(_int(r.get("family")), "?"),
            "interface": r.get("interface") or "", "ifIndex": _int(r.get("ifIndex")),
            "kind": kind, "store": ROUTE_STORE.get(_int(r.get("store"), 1), "?"),
        })
    return out


def get_routes() -> list:
    rows = _ps_json(
        "$m=@{}; foreach($x in @(Get-NetIPInterface)){ $m[\"$($x.ifIndex)-$([int]$x.AddressFamily)\"]=[int]$x.InterfaceMetric };"
        "ConvertTo-Json -Compress -InputObject @(Get-NetRoute | ForEach-Object { [pscustomobject]@{"
        "dest=$_.DestinationPrefix; next_hop=$_.NextHop; ifIndex=[int]$_.ifIndex; interface=$_.InterfaceAlias;"
        "route_metric=[int]$_.RouteMetric; if_metric=$m[\"$($_.ifIndex)-$([int]$_.AddressFamily)\"];"
        "family=[int]$_.AddressFamily; protocol=[int]$_.Protocol; store=[int]$_.Store } })")
    out = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        rm, im = _int(r.get("route_metric")), r.get("if_metric")
        out.append({
            "destination": r.get("dest") or "", "next_hop": r.get("next_hop") or "",
            "on_link": (r.get("next_hop") or "") in ("0.0.0.0", "::"),
            "interface": r.get("interface") or "", "ifIndex": _int(r.get("ifIndex")),
            "route_metric": rm, "interface_metric": _int(im) if im is not None else None,
            "metric": rm + _int(im) if im is not None else rm,
            "family": FAMILY.get(_int(r.get("family")), "?"),
            "protocol": ROUTE_PROTOCOL.get(_int(r.get("protocol")), str(r.get("protocol"))),
            "store": ROUTE_STORE.get(_int(r.get("store"), 1), "?"),
        })
    out.sort(key=lambda x: (x["family"], x["destination"] not in ("0.0.0.0/0", "::/0"), x["metric"]))
    return out


def get_dns_cache() -> list:
    rows = _ps_json(
        "ConvertTo-Json -Compress -InputObject @(Get-DnsClientCache | ForEach-Object { [pscustomobject]@{"
        "entry=$_.Entry; name=$_.Name; type=[int]$_.Type; status=[int]$_.Status; section=[int]$_.Section;"
        "ttl=[int]$_.TimeToLive; data=[string]$_.Data } })")
    out = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        t = _int(r.get("type"))
        st = _int(r.get("status"))
        out.append({
            "entry": r.get("entry") or "", "name": r.get("name") or "",
            "type": DNS_TYPES.get(t, f"TYPE{t}"), "status": DNS_STATUS.get(st, f"Code {st}"),
            "ok": st == 0, "section": DNS_SECTION.get(_int(r.get("section")), "?"),
            "ttl": _int(r.get("ttl")), "data": r.get("data") or "",
        })
    out.sort(key=lambda x: (x["entry"].lower(), x["type"]))
    return out


# ── Actions ────────────────────────────────────────────────────────────────

ACTIONS = {
    "release": {"args": ["/release"], "admin": True, "adapter": True, "label": "Libération du bail IPv4"},
    "renew": {"args": ["/renew"], "admin": True, "adapter": True, "label": "Renouvellement du bail IPv4"},
    "release6": {"args": ["/release6"], "admin": True, "adapter": True, "label": "Libération du bail IPv6"},
    "renew6": {"args": ["/renew6"], "admin": True, "adapter": True, "label": "Renouvellement du bail IPv6"},
    "flushdns": {"args": ["/flushdns"], "admin": False, "adapter": False, "label": "Purge du cache DNS"},
    "registerdns": {"args": ["/registerdns"], "admin": True, "adapter": False, "label": "Réenregistrement DNS"},
}

ADAPTER_NAME_RE = re.compile(r"^[^\"'`$;&|<>\r\n\x00]{1,128}$")
NEEDS_ELEV_RE = re.compile(r"élévation|elevation|privil|administrat|accès refusé|access is denied|access denied", re.I)


def _adapter_names() -> set[str]:
    rows = _ps_json("ConvertTo-Json -Compress -InputObject @(Get-NetAdapter -IncludeHidden | ForEach-Object { $_.Name })")
    return {str(r) for r in rows if r}


def _ps_quote(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"


def run_action(action: str, adapter: Optional[str], force_elevate: bool) -> dict:
    spec = ACTIONS[action]
    args = list(spec["args"])
    if spec["adapter"] and adapter:
        args.append(adapter)
    admin = elevate.is_admin()
    use_elev = not admin and (spec["admin"] or force_elevate)
    if use_elev:
        script = "ipconfig " + " ".join(_ps_quote(a) if not a.startswith("/") else a for a in args)
        res = elevate.run_elevated(script, timeout=180)
        code, out = res["exit_code"], res["output"].replace("\r\n", "\n").strip("\n")
    else:
        code, out = _run_native(["ipconfig", *args], timeout=180)
    needs_admin = (not admin and not use_elev and code != 0 and bool(NEEDS_ELEV_RE.search(out)))
    return {"ok": code == 0, "exit_code": code, "output": out, "elevated": use_elev,
            "needs_admin": needs_admin, "label": spec["label"]}


# ── Routes ─────────────────────────────────────────────────────────────────

@router.get("/status")
async def status():
    return {"is_admin": elevate.is_admin(), "hostname": socket.gethostname()}


async def _wrap(fn):
    try:
        return await asyncio.to_thread(fn)
    except PSError as e:
        return _err(f"Erreur PowerShell : {e}", 500)
    except Exception as e:  # noqa: BLE001
        return _err(f"Erreur : {e}", 500)


@router.get("/adapters")
async def adapters():
    return await _wrap(get_adapters)


@router.get("/arp")
async def arp():
    res = await _wrap(get_arp)
    return res if isinstance(res, JSONResponse) else {"entries": res}


@router.get("/routes")
async def routes():
    res = await _wrap(get_routes)
    return res if isinstance(res, JSONResponse) else {"routes": res}


@router.get("/dns-cache")
async def dns_cache():
    res = await _wrap(get_dns_cache)
    return res if isinstance(res, JSONResponse) else {"entries": res}


@router.get("/ipconfig-all")
async def ipconfig_all():
    try:
        code, out = await asyncio.to_thread(_run_native, ["ipconfig", "/all"], 30)
    except Exception as e:  # noqa: BLE001
        return _err(f"Impossible d'exécuter ipconfig : {e}", 500)
    return {"output": out, "exit_code": code}


class ActionReq(BaseModel):
    action: str
    adapter: Optional[str] = None
    elevate: bool = False


@router.post("/action")
async def action(req: ActionReq):
    if req.action not in ACTIONS:
        return _err("Action inconnue.")
    adapter = (req.adapter or "").strip() or None
    if adapter:
        if not ACTIONS[req.action]["adapter"]:
            return _err("Cette action ne s'applique pas à une carte précise.")
        if not ADAPTER_NAME_RE.match(adapter):
            return _err("Nom de carte invalide.")
        try:
            names = await asyncio.to_thread(_adapter_names)
        except PSError as e:
            return _err(f"Erreur PowerShell : {e}", 500)
        if adapter not in names:
            return _err(f"Carte réseau introuvable : {adapter}")
    try:
        return await asyncio.to_thread(run_action, req.action, adapter, req.elevate)
    except elevate.ElevationCancelled as e:
        return _err(str(e), 403, "UAC_CANCELLED")
    except elevate.ElevationError as e:
        return _err(str(e), 500, "ELEVATION_FAILED")
    except subprocess.TimeoutExpired:
        return _err("Délai dépassé pour la commande ipconfig.", 504)
    except Exception as e:  # noqa: BLE001
        return _err(f"Erreur : {e}", 500)
