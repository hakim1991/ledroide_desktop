"""Parsers / serializers for JSON, YAML, TOML, XML, CSV, .env and INI."""
import configparser
import csv
import datetime as dt
import io
import json
import math
import re
import tomllib
import xml.etree.ElementTree as ET
from typing import Any

import yaml

FORMATS = ("json", "yaml", "toml", "xml", "csv", "env", "ini")
INT_RE = re.compile(r"^[+-]?(0|[1-9]\d*)$")
FLOAT_RE = re.compile(r"^[+-]?(0|[1-9]\d*)(\.\d+)([eE][+-]?\d+)?$|^[+-]?(0|[1-9]\d*)[eE][+-]?\d+$")
BARE_KEY_RE = re.compile(r"^[A-Za-z0-9_-]+$")
XML_NAME_RE = re.compile(r"^[A-Za-z_][\w.\-]*$")
ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.\-]*$")
ENV_LINE_RE = re.compile(r"^\s*(?:export\s+)?[A-Za-z_][A-Za-z0-9_.\-]*=")
INI_ROOT = "__ledroide_root__"
INI_NO_DEFAULT = "__ledroide_no_default__"


class ParseError(Exception):
    def __init__(self, message: str, line: int | None = None, column: int | None = None):
        super().__init__(message)
        self.message = message
        self.line = line
        self.column = column


class SerializeError(Exception):
    pass


def infer(value: str) -> Any:
    v = value.strip()
    low = v.lower()
    if low == "true":
        return True
    if low == "false":
        return False
    if INT_RE.match(v):
        return int(v)
    if FLOAT_RE.match(v):
        return float(v)
    return value


def scalar_str(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (dt.datetime, dt.date, dt.time)):
        return v.isoformat()
    if isinstance(v, (dict, list)):
        return json.dumps(v, ensure_ascii=False, default=json_default)
    return str(v)


def json_default(o: Any):
    if isinstance(o, (dt.datetime, dt.date, dt.time)):
        return o.isoformat()
    if isinstance(o, bytes):
        return o.decode("utf-8", errors="replace")
    if isinstance(o, set):
        return sorted(o, key=str)
    raise TypeError(f"Type non sérialisable : {type(o).__name__}")


def sort_keys(data: Any) -> Any:
    if isinstance(data, dict):
        return {k: sort_keys(data[k]) for k in sorted(data, key=str)}
    if isinstance(data, list):
        return [sort_keys(x) for x in data]
    return data


def flatten(data: dict, sep: str, prefix: str = "") -> dict:
    out: dict = {}
    for k, v in data.items():
        key = f"{prefix}{sep}{k}" if prefix else str(k)
        if isinstance(v, dict) and v:
            out.update(flatten(v, sep, key))
        else:
            out[key] = v
    return out


def unflatten(data: dict, sep: str) -> dict:
    out: dict = {}
    for key, value in data.items():
        parts = [p for p in str(key).split(sep)] if sep in str(key) else [key]
        if any(p == "" for p in parts):
            parts = [key]
        node = out
        for p in parts[:-1]:
            nxt = node.get(p)
            if not isinstance(nxt, dict):
                if nxt is not None:
                    node = None
                    break
                nxt = node[p] = {}
            node = nxt
        if node is None or (parts[-1] in node and isinstance(node[parts[-1]], dict)):
            out[key] = value
        else:
            node[parts[-1]] = value
    return out


# ── Parsers ─────────────────────────────────────────────────────────────────

JSON_MESSAGES = {
    "Expecting value": "valeur attendue",
    "Expecting property name enclosed in double quotes": "nom de propriété entre guillemets doubles attendu (virgule en trop ?)",
    "Expecting ',' delimiter": "virgule attendue",
    "Expecting ':' delimiter": "deux-points attendus",
    "Extra data": "données en trop après la fin du document",
    "Unterminated string starting at": "chaîne non terminée",
    "Invalid control character at": "caractère de contrôle interdit dans une chaîne",
    "Invalid \\escape": "séquence d'échappement invalide",
}


def parse_json(text: str, opts: dict) -> Any:
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        raise ParseError(f"JSON invalide : {JSON_MESSAGES.get(e.msg, e.msg)}", e.lineno, e.colno) from e


def parse_yaml(text: str, opts: dict) -> Any:
    try:
        docs = list(yaml.safe_load_all(text))
    except yaml.MarkedYAMLError as e:
        mark = e.problem_mark or e.context_mark
        msg = " ; ".join(x for x in (e.context, e.problem) if x) or str(e)
        raise ParseError(f"YAML invalide : {msg}", mark.line + 1 if mark else None, mark.column + 1 if mark else None) from e
    except yaml.YAMLError as e:
        raise ParseError(f"YAML invalide : {e}") from e
    if not docs:
        return None
    return docs[0] if len(docs) == 1 else docs


def parse_toml(text: str, opts: dict) -> Any:
    try:
        return tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        msg = str(e)
        m = re.search(r"\(at line (\d+), column (\d+)\)", msg)
        line, col = (int(m.group(1)), int(m.group(2))) if m else (None, None)
        if not m and "end of document" in msg:
            line = text.count("\n") + 1
        clean = re.sub(r"\s*\(at (line \d+, column \d+|end of document)\)", "", msg)
        raise ParseError(f"TOML invalide : {clean}", line, col) from e


def _local(tag: str) -> str:
    return tag.split("}", 1)[1] if tag.startswith("{") else tag


def _elem_to_data(e: ET.Element, types: bool) -> Any:
    d: dict = {}
    for k, v in e.attrib.items():
        d["@" + _local(k)] = infer(v) if types else v
    for child in e:
        if not isinstance(child.tag, str):
            continue
        tag = _local(child.tag)
        val = _elem_to_data(child, types)
        if tag in d:
            existing = d[tag]
            if isinstance(existing, _XmlList):
                existing.append(val)
            else:
                d[tag] = _XmlList([existing, val])
        else:
            d[tag] = val
    text = (e.text or "").strip()
    tails = " ".join((c.tail or "").strip() for c in e if (c.tail or "").strip())
    if tails:
        text = (text + " " + tails).strip()
    if text:
        if not d:
            return infer(text) if types else text
        d["#text"] = infer(text) if types else text
    if not d:
        return None
    return {k: list(v) if isinstance(v, _XmlList) else v for k, v in d.items()}


class _XmlList(list):
    pass


def parse_xml(text: str, opts: dict) -> Any:
    if re.search(r"<!DOCTYPE[^>]*\[", text, re.I) or re.search(r"<!ENTITY", text, re.I):
        raise ParseError("XML avec DTD interne / entités déclarées : refusé par sécurité.")
    try:
        root = ET.fromstring(text.lstrip("﻿"))
    except ET.ParseError as e:
        line, col = e.position
        msg = re.sub(r":\s*line \d+, column \d+$", "", str(e))
        raise ParseError(f"XML invalide : {msg}", line, col + 1) from e
    return {_local(root.tag): _elem_to_data(root, opts.get("infer_types", True))}


def _sniff_delimiter(text: str, wanted: str | None) -> str:
    if wanted and wanted != "auto":
        return "\t" if wanted in ("tab", "\\t") else wanted
    sample = "\n".join(text.splitlines()[:20])
    try:
        return csv.Sniffer().sniff(sample, delimiters=",;\t|").delimiter
    except csv.Error:
        return ","


def parse_csv(text: str, opts: dict) -> Any:
    delim = _sniff_delimiter(text, opts.get("csv_delimiter"))
    reader = csv.reader(io.StringIO(text), delimiter=delim, strict=True)
    types = opts.get("infer_types", True)
    try:
        header = next(reader, None)
        if header is None:
            return []
        if len(set(header)) != len(header):
            dup = next(h for h in header if header.count(h) > 1)
            raise ParseError(f"CSV : colonne « {dup} » en double dans l'en-tête.", 1, None)
        rows = []
        for row in reader:
            if not row or all(c == "" for c in row) and len(row) <= 1:
                continue
            if len(row) != len(header):
                raise ParseError(f"CSV : {len(row)} colonne(s) au lieu de {len(header)} (séparateur « {_delim_name(delim)} »).", reader.line_num, None)
            item = {h: (infer(v) if types and v != "" else v) for h, v in zip(header, row)}
            rows.append(unflatten(item, ".") if opts.get("nested", True) else item)
        return rows
    except csv.Error as e:
        raise ParseError(f"CSV invalide : {e}", reader.line_num, None) from e


def _delim_name(d: str) -> str:
    return {"\t": "tabulation", ",": "virgule", ";": "point-virgule", "|": "barre"}.get(d, d)


def _unquote(v: str) -> str:
    v = v.strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
        return v[1:-1]
    return v


def parse_ini(text: str, opts: dict) -> Any:
    lines = text.splitlines()
    first = next((ln.strip() for ln in lines if ln.strip() and not ln.strip().startswith(("#", ";"))), "")
    offset = 0
    if first and not first.startswith("["):
        text = f"[{INI_ROOT}]\n" + text
        offset = 1
    parser = configparser.ConfigParser(interpolation=None, strict=True, default_section=INI_NO_DEFAULT,
                                       delimiters=("=", ":"), comment_prefixes=("#", ";"), inline_comment_prefixes=None)
    parser.optionxform = str
    try:
        parser.read_string(text)
    except configparser.ParsingError as e:
        lineno, line = e.errors[0] if getattr(e, "errors", None) else (None, "")
        line = str(line)
        if len(line) >= 2 and line[0] in "'\"" and line[-1] == line[0]:
            line = line[1:-1].replace("\\n", "")
        raise ParseError(f"INI invalide : ligne non reconnue « {line.strip()[:60]} » (attendu « clé = valeur » ou « [section] »).",
                         lineno - offset if lineno else None) from e
    except (configparser.DuplicateSectionError, configparser.DuplicateOptionError) as e:
        what = f"option « {e.option} » en double dans [{e.section}]" if isinstance(e, configparser.DuplicateOptionError) else f"section [{e.section}] en double"
        raise ParseError(f"INI invalide : {what}.", e.lineno - offset if e.lineno else None) from e
    except configparser.Error as e:
        raise ParseError(f"INI invalide : {e.message if hasattr(e, 'message') else e}") from e
    types = opts.get("infer_types", True)

    def conv(v):
        v = _unquote(v or "")
        return infer(v) if types else v

    out: dict = {}
    for section in parser.sections():
        items = {k: conv(v) for k, v in parser.items(section, raw=True)}
        if section == INI_ROOT:
            out.update(items)
        else:
            out[section] = items
    if not opts.get("nested", True):
        return out
    result: dict = {}
    for k, v in out.items():
        if isinstance(v, dict) and "." in k:
            parts = k.split(".")
            node, ok = result, True
            for p in parts[:-1]:
                cur = node.get(p)
                if cur is None:
                    cur = node[p] = {}
                elif not isinstance(cur, dict):
                    ok = False
                    break
                node = cur
            if ok and isinstance(node.get(parts[-1], {}), dict):
                node.setdefault(parts[-1], {}).update(v)
                continue
        if isinstance(v, dict) and isinstance(result.get(k), dict):
            result[k].update(v)
        else:
            result[k] = v
    return result


def _env_value(raw: str, lineno: int) -> str:
    raw = raw.strip()
    if raw.startswith('"'):
        out, i = [], 1
        while i < len(raw):
            c = raw[i]
            if c == "\\" and i + 1 < len(raw):
                nxt = raw[i + 1]
                out.append({"n": "\n", "t": "\t", "r": "\r", '"': '"', "\\": "\\", "$": "$"}.get(nxt, "\\" + nxt))
                i += 2
                continue
            if c == '"':
                rest = raw[i + 1:].strip()
                if rest and not rest.startswith("#"):
                    raise ParseError(f".env : texte inattendu après la valeur entre guillemets : « {rest[:30]} ».", lineno)
                return "".join(out)
            out.append(c)
            i += 1
        raise ParseError(".env : guillemet fermant manquant.", lineno)
    if raw.startswith("'"):
        end = raw.find("'", 1)
        if end < 0:
            raise ParseError(".env : apostrophe fermante manquante.", lineno)
        return raw[1:end]
    hash_pos = re.search(r"\s#", raw)
    return (raw[:hash_pos.start()] if hash_pos else raw).strip()


def parse_env(text: str, opts: dict) -> Any:
    out: dict = {}
    types = opts.get("infer_types", True)
    for lineno, line in enumerate(text.splitlines(), 1):
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        if s.startswith("export "):
            s = s[7:].lstrip()
        key, eq, value = s.partition("=")
        key = key.strip()
        if not eq:
            raise ParseError(f".env : « = » manquant (attendu CLE=valeur) : « {s[:40]} ».", lineno, 1)
        if not ENV_KEY_RE.match(key):
            raise ParseError(f".env : nom de variable invalide « {key} ».", lineno, 1)
        if key in out:
            raise ParseError(f".env : variable « {key} » définie deux fois.", lineno, 1)
        v = _env_value(value, lineno)
        quoted = value.strip()[:1] in ("'", '"')
        out[key] = infer(v) if types and not quoted and v != "" else v
    return unflatten(out, "__") if opts.get("nested", True) else out


PARSERS = {"json": parse_json, "yaml": parse_yaml, "toml": parse_toml, "xml": parse_xml,
           "csv": parse_csv, "env": parse_env, "ini": parse_ini}


# ── Serializers ─────────────────────────────────────────────────────────────

def dump_json(data: Any, opts: dict, warnings: list) -> str:
    indent = int(opts.get("indent", 2))
    if indent <= 0:
        return json.dumps(data, ensure_ascii=False, sort_keys=bool(opts.get("sort_keys")), separators=(",", ":"), default=json_default, allow_nan=True)
    if _has_nan(data):
        warnings.append("NaN/Infinity ne sont pas du JSON standard.")
    return json.dumps(data, ensure_ascii=False, indent=indent, sort_keys=bool(opts.get("sort_keys")), default=json_default)


def _has_nan(data: Any) -> bool:
    if isinstance(data, float):
        return math.isnan(data) or math.isinf(data)
    if isinstance(data, dict):
        return any(_has_nan(v) for v in data.values())
    if isinstance(data, list):
        return any(_has_nan(v) for v in data)
    return False


class _YamlDumper(yaml.SafeDumper):
    pass


def _str_presenter(dumper, value):
    if "\n" in value:
        return dumper.represent_scalar("tag:yaml.org,2002:str", value, style="|")
    return dumper.represent_scalar("tag:yaml.org,2002:str", value)


_YamlDumper.add_representer(str, _str_presenter)


def dump_yaml(data: Any, opts: dict, warnings: list) -> str:
    indent = max(2, min(int(opts.get("indent", 2)), 8))
    return yaml.dump(data, Dumper=_YamlDumper, allow_unicode=True, sort_keys=bool(opts.get("sort_keys")),
                     indent=indent, default_flow_style=False, width=4096)


def _toml_key(k: Any) -> str:
    k = str(k)
    return k if BARE_KEY_RE.match(k) else json.dumps(k, ensure_ascii=False).replace("\x7f", "\\u007f")


def _toml_value(v: Any, warnings: list, path: str) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        if math.isnan(v):
            return "nan"
        if math.isinf(v):
            return "inf" if v > 0 else "-inf"
        r = repr(v)
        return r if any(c in r for c in ".en") else r + ".0"
    if isinstance(v, str):
        return json.dumps(v, ensure_ascii=False).replace("\x7f", "\\u007f")
    if isinstance(v, (dt.datetime, dt.date, dt.time)):
        return v.isoformat()
    if isinstance(v, list):
        items = [_toml_value(x, warnings, path) for x in v if x is not None]
        if len(items) != len(v):
            warnings.append(f"{path} : valeurs null retirées du tableau (TOML n'a pas de null).")
        return "[" + ", ".join(items) + "]"
    if isinstance(v, dict):
        parts = []
        for k, x in v.items():
            if x is None:
                warnings.append(f"{path}.{k} : null omis (TOML n'a pas de null).")
                continue
            parts.append(f"{_toml_key(k)} = {_toml_value(x, warnings, f'{path}.{k}')}")
        return "{ " + ", ".join(parts) + " }" if parts else "{}"
    raise SerializeError(f"{path} : type non représentable en TOML ({type(v).__name__}).")


def _is_aot(v: Any) -> bool:
    return isinstance(v, list) and bool(v) and all(isinstance(x, dict) for x in v)


def _toml_table(tbl: dict, path: list[str], lines: list[str], warnings: list, aot: bool = False):
    scalars = [(k, v) for k, v in tbl.items() if not isinstance(v, dict) and not _is_aot(v)]
    tables = [(k, v) for k, v in tbl.items() if isinstance(v, dict)]
    aots = [(k, v) for k, v in tbl.items() if _is_aot(v)]
    header = ".".join(_toml_key(p) for p in path)
    if path and (aot or scalars or not (tables or aots)):
        if lines:
            lines.append("")
        lines.append(f"[[{header}]]" if aot else f"[{header}]")
    for k, v in scalars:
        dotted = ".".join(path + [str(k)]) or str(k)
        if v is None:
            warnings.append(f"{dotted} : null omis (TOML n'a pas de null).")
            continue
        lines.append(f"{_toml_key(k)} = {_toml_value(v, warnings, dotted)}")
    for k, v in tables:
        _toml_table(v, path + [str(k)], lines, warnings)
    for k, v in aots:
        for item in v:
            _toml_table(item, path + [str(k)], lines, warnings, aot=True)


def dump_toml(data: Any, opts: dict, warnings: list) -> str:
    if not isinstance(data, dict):
        if isinstance(data, list):
            warnings.append("TOML exige une table à la racine : le tableau est placé sous la clé « items ».")
            data = {"items": data}
        else:
            raise SerializeError("TOML exige un objet (table) à la racine.")
    if opts.get("sort_keys"):
        data = sort_keys(data)
    lines: list[str] = []
    _toml_table(data, [], lines, warnings)
    return "\n".join(lines) + "\n"


def _xml_name(name: Any, warnings: list) -> str:
    n = str(name)
    if XML_NAME_RE.match(n) and not n.lower().startswith("xml"):
        return n
    fixed = re.sub(r"[^\w.\-]", "_", n) or "_"
    if not re.match(r"[A-Za-z_]", fixed) or fixed.lower().startswith("xml"):
        fixed = "_" + fixed
    warnings.append(f"Nom « {n} » invalide en XML : remplacé par « {fixed} ».")
    return fixed


def _xml_build(parent: ET.Element, key: str, value: Any, warnings: list):
    if isinstance(value, list):
        for item in value:
            _xml_build(parent, key, item, warnings)
        return
    el = ET.SubElement(parent, _xml_name(key, warnings))
    if isinstance(value, dict):
        for k, v in value.items():
            k = str(k)
            if k.startswith("@"):
                if isinstance(v, (dict, list)):
                    warnings.append(f"Attribut « {k} » : valeur complexe convertie en JSON.")
                el.set(_xml_name(k[1:], warnings), scalar_str(v))
            elif k == "#text":
                el.text = scalar_str(v)
            else:
                _xml_build(el, k, v, warnings)
    elif value is not None:
        el.text = scalar_str(value)


def dump_xml(data: Any, opts: dict, warnings: list) -> str:
    if opts.get("sort_keys"):
        data = sort_keys(data)
    root_name = opts.get("xml_root") or "root"
    holder = ET.Element("holder")
    if isinstance(data, dict) and len(data) == 1 and not str(next(iter(data))).startswith(("@", "#")) \
            and not isinstance(next(iter(data.values())), list):
        key, value = next(iter(data.items()))
        _xml_build(holder, key, value, warnings)
    elif isinstance(data, list):
        root = ET.SubElement(holder, _xml_name(root_name, warnings))
        for item in data:
            _xml_build(root, "item", item, warnings)
    else:
        _xml_build(holder, root_name, data, warnings)
    root = holder[0]
    indent = int(opts.get("indent", 2))
    if indent > 0:
        ET.indent(root, space=" " * indent)
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(root, encoding="unicode") + "\n"


def dump_csv(data: Any, opts: dict, warnings: list) -> str:
    if isinstance(data, dict):
        lists = [k for k, v in data.items() if _is_aot(v)]
        if len(data) == 1 and lists:
            warnings.append(f"Seul le tableau « {lists[0]} » est exporté en CSV.")
            data = data[lists[0]]
        else:
            data = [data]
    if not isinstance(data, list):
        data = [{"value": data}]
    rows = []
    for item in data:
        if isinstance(item, dict):
            rows.append(flatten(item, ".") if opts.get("nested", True) else item)
        else:
            rows.append({"value": item})
    columns: list[str] = []
    seen = set()
    for r in rows:
        for k in r:
            if k not in seen:
                seen.add(k)
                columns.append(k)
    if opts.get("sort_keys"):
        columns.sort()
    delim = opts.get("csv_delimiter") or ","
    delim = {"auto": ",", "tab": "\t", "\\t": "\t"}.get(delim, delim)
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=delim, lineterminator="\n")
    w.writerow(columns)
    for r in rows:
        w.writerow([scalar_str(r.get(c)) for c in columns])
    return buf.getvalue()


def _env_quote(v: str) -> str:
    if v == "" or re.search(r"[\s#\"'\\$`=]", v):
        return '"' + v.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t").replace("$", "\\$") + '"'
    return v


def dump_env(data: Any, opts: dict, warnings: list) -> str:
    if not isinstance(data, dict):
        raise SerializeError("Un fichier .env exige un objet (clé → valeur) à la racine.")
    flat = flatten(data, "__") if opts.get("nested", True) else data
    if opts.get("sort_keys"):
        flat = dict(sorted(flat.items()))
    lines = []
    for k, v in flat.items():
        key = re.sub(r"[^A-Za-z0-9_.\-]", "_", str(k))
        if not re.match(r"[A-Za-z_]", key):
            key = "_" + key
        if key != str(k):
            warnings.append(f"Clé « {k} » renommée « {key} ».")
        lines.append(f"{key}={_env_quote(scalar_str(v))}")
    return "\n".join(lines) + ("\n" if lines else "")


def _ini_value(v: Any) -> str:
    s = scalar_str(v)
    return s.replace("\n", "\\n")


def dump_ini(data: Any, opts: dict, warnings: list) -> str:
    if not isinstance(data, dict):
        raise SerializeError("Un fichier INI exige un objet (sections) à la racine.")
    if opts.get("sort_keys"):
        data = sort_keys(data)
    lines: list[str] = []
    for k, v in data.items():
        if not isinstance(v, dict):
            lines.append(f"{k} = {_ini_value(v)}")
    sections: list[tuple[str, dict]] = []

    def collect(name: str, tbl: dict):
        scal = {k: v for k, v in tbl.items() if not isinstance(v, dict)}
        sections.append((name, scal))
        for k, v in tbl.items():
            if isinstance(v, dict):
                collect(f"{name}.{k}", v)

    for k, v in data.items():
        if isinstance(v, dict):
            collect(str(k), v)
    for name, scal in sections:
        if not scal and any(n.startswith(name + ".") for n, _ in sections):
            continue
        if lines:
            lines.append("")
        lines.append(f"[{name}]")
        for k, v in scal.items():
            if isinstance(v, list):
                warnings.append(f"[{name}] {k} : liste écrite en JSON.")
            lines.append(f"{k} = {_ini_value(v)}")
    return "\n".join(lines) + ("\n" if lines else "")


SERIALIZERS = {"json": dump_json, "yaml": dump_yaml, "toml": dump_toml, "xml": dump_xml,
               "csv": dump_csv, "env": dump_env, "ini": dump_ini}


# ── Detection ───────────────────────────────────────────────────────────────

def detect(text: str) -> str | None:
    s = text.lstrip("\ufeff").strip()
    if not s:
        return None
    if s[0] in "{[":
        try:
            json.loads(s)
            return "json"
        except json.JSONDecodeError:
            if s[0] == "{":
                return "json"
    if s[0] == "<":
        return "xml"
    lines = [ln for ln in s.splitlines() if ln.strip() and not ln.strip().startswith("#")]
    if lines and all(ENV_LINE_RE.match(ln) for ln in lines):
        return "env"
    try:
        if tomllib.loads(s):
            return "toml"
    except tomllib.TOMLDecodeError:
        pass
    if re.search(r"^\s*\[[^\]\n]+\]\s*$", s, re.M):
        try:
            parse_ini(s, {})
            return "ini"
        except ParseError:
            pass
    if len(lines) >= 2:
        try:
            dialect = csv.Sniffer().sniff("\n".join(lines[:20]), delimiters=",;\t|")
            counts = {len(r) for r in csv.reader(io.StringIO("\n".join(lines[:20])), delimiter=dialect.delimiter)}
            if len(counts) == 1 and counts.pop() > 1:
                return "csv"
        except csv.Error:
            pass
    try:
        if isinstance(yaml.safe_load(s), (dict, list)):
            return "yaml"
    except yaml.YAMLError:
        pass
    if s[0] == "[":
        return "json"
    return "yaml"
