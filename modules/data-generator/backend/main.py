import asyncio
import csv
import hashlib
import io
import json
import logging
import random
import re
import secrets
from datetime import datetime, timezone

from ledroide_sdk import Module

import dg_generators as gen

log = logging.getLogger("ledroide.data-generator")

module = Module(__file__)
router = module.router

MAX_ROWS = 100_000
MAX_FIELDS = 60
PREVIEW_ROWS = 20
CHUNK = 5000
FORMATS = ("json", "csv", "sql", "ndjson")
SQL_BATCH = 100
NAME_RE = re.compile(r"^[^\x00-\x1f]{1,64}$")


class SchemaError(Exception):
    def __init__(self, message: str, field: int | None = None):
        super().__init__(message)
        self.message = message
        self.field = field


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _compile(fields) -> list[tuple[str, callable, float]]:
    if not isinstance(fields, list) or not fields:
        raise SchemaError("Ajoutez au moins un champ")
    if len(fields) > MAX_FIELDS:
        raise SchemaError(f"Trop de champs ({MAX_FIELDS} maximum)")
    out, seen = [], set()
    for i, f in enumerate(fields):
        if not isinstance(f, dict):
            raise SchemaError("Champ invalide", i)
        name = str(f.get("name") or "").strip()
        label = f"Champ {i + 1}" + (f" « {name} »" if name else "")
        if not name or not NAME_RE.match(name):
            raise SchemaError(f"{label} : nom manquant ou invalide", i)
        if name in seen:
            raise SchemaError(f"{label} : nom en double", i)
        seen.add(name)
        if f.get("type") not in gen.TYPE_IDS:
            raise SchemaError(f"{label} : type inconnu « {f.get('type')} »", i)
        try:
            fn = gen.build(f)
            null_pct = float((f.get("options") or {}).get("null_pct") or 0)
        except (ValueError, TypeError) as exc:
            raise SchemaError(f"{label} : {exc}", i)
        if not 0 <= null_pct <= 100:
            raise SchemaError(f"{label} : le pourcentage de valeurs nulles doit être entre 0 et 100", i)
        out.append((name, fn, null_pct))
    return out


def _rows(compiled, rng: random.Random, start: int, count: int) -> list[list]:
    rows = []
    for idx in range(start, start + count):
        row = gen.Row(rng, idx)
        values = []
        for _, fn, null_pct in compiled:
            v = fn(row)
            if null_pct and rng.random() * 100 < null_pct:
                v = None
            values.append(v)
        rows.append(values)
    return rows


def _seed(payload: dict) -> int:
    raw = payload.get("seed")
    if raw in (None, ""):
        return secrets.randbelow(2 ** 31)
    if isinstance(raw, bool):
        raise SchemaError("Graine invalide")
    if isinstance(raw, int):
        return raw
    text = str(raw).strip()
    if re.fullmatch(r"-?\d{1,18}", text):
        return int(text)
    # Any text works as a seed: hash it to a stable integer.
    return int.from_bytes(hashlib.sha256(text.encode("utf-8")).digest()[:8], "big")


def _err(exc: SchemaError) -> dict:
    out = {"error": exc.message, "code": "INVALID_SCHEMA"}
    if exc.field is not None:
        out["field"] = exc.field
    return out


# ── Formatting ───────────────────────────────────────────────────────────────

def _sql_value(v) -> str:
    if v is None:
        return "NULL"
    if isinstance(v, bool):
        return "TRUE" if v else "FALSE"
    if isinstance(v, (int, float)):
        return repr(v)
    return "'" + str(v).replace("'", "''") + "'"


def _sql_ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _csv_value(v):
    if v is None:
        return ""
    if isinstance(v, bool):
        return "true" if v else "false"
    return v


def _format_chunk(fmt: str, names: list[str], rows: list[list], first: bool, last: bool, opts: dict) -> str:
    if fmt == "ndjson":
        return "".join(json.dumps(dict(zip(names, r)), ensure_ascii=False) + "\n" for r in rows)
    if fmt == "json":
        pretty = opts.get("pretty", True)
        parts = []
        for r in rows:
            obj = json.dumps(dict(zip(names, r)), ensure_ascii=False, indent=2 if pretty else None)
            parts.append(("  " + obj.replace("\n", "\n  ")) if pretty else obj)
        sep = ",\n" if pretty else ","
        body = sep.join(parts)
        head = ("[\n" if pretty else "[") if first else sep
        tail = ("\n]\n" if pretty else "]\n") if last else ""
        return head + body + tail if rows else (("[]\n" if first else "") if last else "")
    if fmt == "csv":
        buf = io.StringIO()
        w = csv.writer(buf, delimiter=opts.get("delimiter", ","), lineterminator="\r\n")
        if first:
            w.writerow(names)
        for r in rows:
            w.writerow([_csv_value(v) for v in r])
        return buf.getvalue()
    table = _sql_ident(opts.get("table") or "donnees")
    cols = ", ".join(_sql_ident(n) for n in names)
    out = []
    for i in range(0, len(rows), SQL_BATCH):
        batch = rows[i:i + SQL_BATCH]
        values = ",\n".join("  (" + ", ".join(_sql_value(v) for v in r) + ")" for r in batch)
        out.append(f"INSERT INTO {table} ({cols}) VALUES\n{values};\n")
    return "".join(out)


def _options(payload: dict) -> tuple[str, dict]:
    fmt = payload.get("format", "json")
    if fmt not in FORMATS:
        raise SchemaError(f"Format inconnu « {fmt} »")
    delimiter = str(payload.get("delimiter") or ",")
    if delimiter == "\\t":
        delimiter = "\t"
    if len(delimiter) != 1:
        raise SchemaError("Le séparateur CSV doit être un seul caractère")
    table = str(payload.get("table") or "donnees").strip()
    if not NAME_RE.match(table):
        raise SchemaError("Nom de table invalide")
    return fmt, {"delimiter": delimiter, "table": table, "pretty": payload.get("pretty", True) is not False}


# ── Actions ──────────────────────────────────────────────────────────────────

@module.on("types")
async def types(payload: dict):
    return {"types": gen.TYPES, "max_rows": MAX_ROWS, "formats": list(FORMATS)}


@module.on("preview")
async def preview(payload: dict):
    try:
        compiled = _compile(payload.get("fields"))
        seed = _seed(payload)
    except SchemaError as exc:
        return _err(exc)
    count = max(1, min(int(payload.get("count") or PREVIEW_ROWS), 100))
    rows = await asyncio.to_thread(_rows, compiled, random.Random(seed), 0, count)
    return {"columns": [n for n, _, _ in compiled], "rows": rows, "seed": seed}


@module.on("validate")
async def validate(payload: dict):
    try:
        _compile(payload.get("fields"))
        _options(payload)
    except SchemaError as exc:
        return _err(exc)
    return {"ok": True}


@module.job("generate")
async def generate(payload: dict, job):
    try:
        compiled = _compile(payload.get("fields"))
        fmt, opts = _options(payload)
        seed = _seed(payload)
        total = int(payload.get("rows") or 0)
    except (SchemaError, TypeError, ValueError) as exc:
        raise ValueError(exc.message if isinstance(exc, SchemaError) else "Nombre de lignes invalide") from None
    if not 1 <= total <= MAX_ROWS:
        raise ValueError(f"Le nombre de lignes doit être compris entre 1 et {MAX_ROWS:,}".replace(",", " "))

    names = [n for n, _, _ in compiled]
    rng = random.Random(seed)
    parts = []
    done = 0
    await job.progress(0, "Génération…")
    while done < total:
        n = min(CHUNK, total - done)
        rows = await asyncio.to_thread(_rows, compiled, rng, done, n)
        first, last = done == 0, done + n >= total
        parts.append(await asyncio.to_thread(_format_chunk, fmt, names, rows, first, last, opts))
        done += n
        await job.progress(round(done * 100 / total, 1), f"{done:,} / {total:,} lignes".replace(",", " "))
    content = "".join(parts)
    log.info("generated %d rows x %d fields as %s (%d chars)", total, len(names), fmt, len(content))
    ext = {"json": "json", "csv": "csv", "sql": "sql", "ndjson": "ndjson"}[fmt]
    mime = {"json": "application/json", "csv": "text/csv", "sql": "application/sql", "ndjson": "application/x-ndjson"}[fmt]
    return {"content": content, "rows": total, "seed": seed, "format": fmt, "size": len(content.encode("utf-8")),
            "filename": f"{opts['table'] if fmt == 'sql' else 'donnees'}.{ext}", "mime": mime}


# ── Saved schemas ────────────────────────────────────────────────────────────

@module.on("schemas_list")
async def schemas_list(payload: dict):
    items = await module.db.query("schemas")
    items.sort(key=lambda d: d.get("name", "").lower())
    return {"items": items}


@module.on("schema_save")
async def schema_save(payload: dict):
    name = str(payload.get("name") or "").strip()[:100]
    if not name:
        return {"error": "Donnez un nom au schéma", "code": "NAME_REQUIRED"}
    try:
        _compile(payload.get("fields"))
    except SchemaError as exc:
        return _err(exc)
    doc = {
        "name": name,
        "fields": payload["fields"],
        "rows": max(1, min(int(payload.get("rows") or 100), MAX_ROWS)),
        "seed": str(payload.get("seed") or "")[:40],
        "format": payload.get("format") if payload.get("format") in FORMATS else "json",
        "table": str(payload.get("table") or "donnees")[:64],
        "delimiter": str(payload.get("delimiter") or ",")[:2],
        "updated": _now(),
    }
    existing = None
    if payload.get("id"):
        existing = await module.db.get("schemas", str(payload["id"]))
    if existing is None:
        same = [d for d in await module.db.query("schemas") if d.get("name", "").lower() == name.lower()]
        existing = same[0] if same else None
    if existing is not None:
        doc["_id"] = existing["_id"]
    return await module.db.save("schemas", doc)


@module.on("schema_delete")
async def schema_delete(payload: dict):
    r = await module.db.delete("schemas", str(payload.get("id", "")))
    if not r["ok"]:
        return {"error": "Schéma introuvable", "code": "NOT_FOUND"}
    return r
