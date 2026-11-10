import asyncio
import logging

import formats
from ledroide_sdk import Module

log = logging.getLogger("ledroide.format-converter")

module = Module(__file__)
router = module.router

MAX_CHARS = 20_000_000
LABELS = {"json": "JSON", "yaml": "YAML", "toml": "TOML", "xml": "XML", "csv": "CSV", "env": ".env", "ini": "INI"}


def _options(raw) -> dict:
    raw = raw if isinstance(raw, dict) else {}
    try:
        indent = max(0, min(int(raw.get("indent", 2)), 8))
    except (TypeError, ValueError):
        indent = 2
    return {
        "indent": indent,
        "sort_keys": bool(raw.get("sort_keys", False)),
        "infer_types": bool(raw.get("infer_types", True)),
        "nested": bool(raw.get("nested", True)),
        "csv_delimiter": {"tab": "\t", "\\t": "\t"}.get(str(raw.get("csv_delimiter")), str(raw.get("csv_delimiter")))
        if raw.get("csv_delimiter") in (",", ";", "\t", "|", "tab", "\\t") else "auto",
        "xml_root": str(raw.get("xml_root") or "root")[:100],
    }


def _parse(text: str, fmt: str, opts: dict):
    if fmt == "auto":
        fmt = formats.detect(text)
        if fmt is None:
            return None, None, {"ok": False, "stage": "detect", "message": "Source vide.", "line": None, "column": None}
    if fmt not in formats.PARSERS:
        return None, fmt, {"error": f"Format inconnu : {fmt}", "code": "UNKNOWN_FORMAT"}
    try:
        return formats.PARSERS[fmt](text.lstrip("﻿"), opts), fmt, None
    except formats.ParseError as e:
        return None, fmt, {"ok": False, "stage": "parse", "format": fmt,
                           "message": e.message, "line": e.line, "column": e.column}
    except RecursionError:
        return None, fmt, {"ok": False, "stage": "parse", "format": fmt,
                           "message": "Structure trop profondément imbriquée.", "line": None, "column": None}


def _convert(text: str, src: str, dst: str, opts: dict) -> dict:
    data, detected, problem = _parse(text, src, opts)
    if problem:
        problem["detected"] = detected
        return problem
    if dst not in formats.SERIALIZERS:
        return {"error": f"Format cible inconnu : {dst}", "code": "UNKNOWN_FORMAT"}
    warnings: list[str] = []
    try:
        output = formats.SERIALIZERS[dst](data, opts, warnings)
    except formats.SerializeError as e:
        return {"ok": False, "stage": "serialize", "format": dst, "detected": detected,
                "message": str(e), "line": None, "column": None}
    except (TypeError, ValueError, RecursionError) as e:
        return {"ok": False, "stage": "serialize", "format": dst, "detected": detected,
                "message": f"Conversion vers {LABELS[dst]} impossible : {e}", "line": None, "column": None}
    seen = set()
    warnings = [w for w in warnings if not (w in seen or seen.add(w))][:20]
    return {"ok": True, "output": output, "detected": detected, "warnings": warnings}


def _check(payload: dict) -> tuple[str, dict | None]:
    text = payload.get("text")
    if not isinstance(text, str):
        return "", {"error": "Le champ « text » doit être une chaîne.", "code": "BAD_PAYLOAD"}
    if len(text) > MAX_CHARS:
        return "", {"error": "Source trop volumineuse (20 millions de caractères max).", "code": "TOO_LARGE"}
    return text, None


@module.on("convert")
async def convert(payload: dict):
    text, err = _check(payload)
    if err:
        return err
    src = str(payload.get("from") or "auto")
    dst = str(payload.get("to") or "json")
    if src != "auto" and src not in formats.FORMATS:
        return {"error": f"Format source inconnu : {src}", "code": "UNKNOWN_FORMAT"}
    if not text.strip():
        return {"ok": True, "output": "", "detected": None, "warnings": [], "empty": True}
    return await asyncio.to_thread(_convert, text, src, dst, _options(payload.get("options")))


@module.on("validate")
async def validate(payload: dict):
    text, err = _check(payload)
    if err:
        return err
    fmt = str(payload.get("format") or "auto")
    _, detected, problem = await asyncio.to_thread(_parse, text, fmt, _options(payload.get("options")))
    if problem:
        problem["detected"] = detected
        return problem
    return {"ok": True, "detected": detected}


@module.on("detect")
async def detect(payload: dict):
    text, err = _check(payload)
    if err:
        return err
    return {"format": await asyncio.to_thread(formats.detect, text)}
