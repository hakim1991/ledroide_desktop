import asyncio
import csv
import io
import json
import re
import tempfile
import time
from pathlib import Path
from typing import Any, Optional

from ledroide_sdk.http import FileResponse, JSONResponse
from pydantic import BaseModel
from ledroide_sdk import Module

module = Module(__file__)
router = module.router

TEMP_DIR = Path(tempfile.gettempdir()) / "ledroide_json-formatter"
TEMP_DIR.mkdir(parents=True, exist_ok=True)

FORMATS = ("json", "yaml", "csv")


class ParseError(Exception):
    def __init__(self, message: str, line: Optional[int] = None, column: Optional[int] = None, code: str = "PARSE_ERROR"):
        super().__init__(message)
        self.message = message
        self.line = line
        self.column = column
        self.code = code


def _err(exc: ParseError, status: int = 400):
    content = {"error": exc.message, "code": exc.code}
    if exc.line is not None:
        content["line"] = exc.line
        content["column"] = exc.column
    return JSONResponse(status_code=status, content=content)


def _yaml():
    try:
        import yaml  # noqa
        return yaml
    except ImportError:
        raise ParseError("PyYAML n'est pas installé. Installez les dépendances du module.", code="MISSING_DEP")


# ── Parsing ───────────────────────────────────────────────────────────────────

def _parse_json(text: str) -> Any:
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        raise ParseError(f"JSON invalide : {e.msg}", e.lineno, e.colno)


def _parse_yaml(text: str) -> Any:
    yaml = _yaml()
    try:
        return yaml.safe_load(text)
    except yaml.YAMLError as e:
        mark = getattr(e, "problem_mark", None)
        problem = getattr(e, "problem", None) or str(e)
        if mark is not None:
            raise ParseError(f"YAML invalide : {problem}", mark.line + 1, mark.column + 1)
        raise ParseError(f"YAML invalide : {problem}")


_NUM_RE = re.compile(r"^-?(0|[1-9]\d*)(\.\d+)?([eE][+-]?\d+)?$")


def _infer(value: str) -> Any:
    if value == "":
        return ""
    low = value.lower()
    if low == "true":
        return True
    if low == "false":
        return False
    if low == "null":
        return None
    if _NUM_RE.match(value):
        try:
            return int(value)
        except ValueError:
            return float(value)
    return value


def _listify(obj: Any) -> Any:
    """Turn dicts whose keys are exactly 0..n-1 into lists (recursively)."""
    if isinstance(obj, dict):
        obj = {k: _listify(v) for k, v in obj.items()}
        keys = list(obj.keys())
        if keys and all(k.isdigit() for k in keys) and sorted(int(k) for k in keys) == list(range(len(keys))):
            return [obj[str(i)] for i in range(len(keys))]
    return obj


def _unflatten(row: dict) -> dict:
    out: dict = {}
    for key, value in row.items():
        parts = key.split(".") if key else [key]
        cur = out
        ok = True
        for p in parts[:-1]:
            nxt = cur.get(p)
            if nxt is None:
                nxt = cur[p] = {}
            elif not isinstance(nxt, dict):
                ok = False
                break
            cur = nxt
        if ok and not isinstance(cur.get(parts[-1]), dict):
            cur[parts[-1]] = value
        else:
            out[key] = value
    return _listify(out)


def _parse_csv(text: str, delimiter: str, infer: bool, unflatten: bool) -> Any:
    if not text.strip():
        return []
    if delimiter == "auto":
        try:
            delimiter = csv.Sniffer().sniff(text[:4096], delimiters=",;\t|").delimiter
        except csv.Error:
            delimiter = ","
    reader = csv.reader(io.StringIO(text), delimiter=delimiter)
    try:
        rows = list(reader)
    except csv.Error as e:
        raise ParseError(f"CSV invalide : {e}", reader.line_num, 1)
    rows = [r for r in rows if any(c.strip() for c in r)]
    if not rows:
        return []
    header = rows[0]
    result = []
    for i, r in enumerate(rows[1:], start=2):
        if len(r) > len(header):
            raise ParseError(f"CSV invalide : la ligne {i} contient {len(r)} colonnes, l'en-tête en a {len(header)}", i, 1)
        rec = {}
        for j, h in enumerate(header):
            v = r[j] if j < len(r) else ""
            rec[h] = _infer(v) if infer else v
        result.append(_unflatten(rec) if unflatten else rec)
    return result


# ── Dumping ───────────────────────────────────────────────────────────────────

def _flatten(obj: Any, prefix: str = "", out: Optional[dict] = None) -> dict:
    if out is None:
        out = {}
    if isinstance(obj, dict):
        if not obj and prefix:
            out[prefix] = ""
        for k, v in obj.items():
            _flatten(v, f"{prefix}.{k}" if prefix else str(k), out)
    elif isinstance(obj, list):
        if not obj and prefix:
            out[prefix] = ""
        for i, v in enumerate(obj):
            _flatten(v, f"{prefix}.{i}" if prefix else str(i), out)
    else:
        out[prefix or "value"] = obj
    return out


def _cell(v: Any) -> str:
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "true" if v else "false"
    return str(v)


def _dump_csv(data: Any, delimiter: str) -> str:
    if delimiter == "auto":
        delimiter = ","
    if isinstance(data, dict):
        data = [data]
    if not isinstance(data, list):
        raise ParseError("Conversion CSV impossible : les données doivent être un tableau d'objets.", code="CONVERT_ERROR")
    rows = [_flatten(item) if isinstance(item, (dict, list)) else {"value": item} for item in data]
    headers: list = []
    seen = set()
    for r in rows:
        for k in r:
            if k not in seen:
                seen.add(k)
                headers.append(k)
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=delimiter, lineterminator="\n")
    w.writerow(headers)
    for r in rows:
        w.writerow([_cell(r[h]) if h in r else "" for h in headers])
    return buf.getvalue()


def _sort(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: _sort(obj[k]) for k in sorted(obj.keys(), key=str)}
    if isinstance(obj, list):
        return [_sort(v) for v in obj]
    return obj


def _jsonable(obj: Any) -> Any:
    """YAML may produce dates, sets, non-str keys... make it JSON-safe."""
    if isinstance(obj, dict):
        return {str(k) if not isinstance(k, str) else k: _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        return [_jsonable(v) for v in obj]
    if isinstance(obj, (str, int, float, bool)) or obj is None:
        return obj
    if hasattr(obj, "isoformat"):
        return obj.isoformat()
    if isinstance(obj, bytes):
        return obj.decode("utf-8", "replace")
    return str(obj)


def _parse(text: str, fmt: str, opts: "ConvertIn") -> Any:
    if fmt == "json":
        return _parse_json(text)
    if fmt == "yaml":
        return _jsonable(_parse_yaml(text))
    return _parse_csv(text, opts.delimiter, opts.infer_types, opts.unflatten)


def _dump(data: Any, fmt: str, opts: "ConvertIn") -> str:
    if opts.sort_keys:
        data = _sort(data)
    if fmt == "json":
        if opts.indent == "min":
            return json.dumps(data, ensure_ascii=False, separators=(",", ":"))
        ind: Any = "\t" if opts.indent == "tab" else int(opts.indent or 2)
        return json.dumps(data, ensure_ascii=False, indent=ind)
    if fmt == "yaml":
        yaml = _yaml()
        ind = 4 if str(opts.indent) == "4" else 2
        return yaml.safe_dump(data, allow_unicode=True, sort_keys=False, indent=ind, default_flow_style=False)
    return _dump_csv(data, opts.delimiter)


# ── Routes ────────────────────────────────────────────────────────────────────

class ConvertIn(BaseModel):
    text: str
    source: str = "json"
    target: str = "json"
    indent: str = "2"           # "2" | "4" | "tab" | "min"
    sort_keys: bool = False
    delimiter: str = "auto"     # CSV: auto , ; \t |
    infer_types: bool = True    # CSV → typed values
    unflatten: bool = True      # CSV dot keys → nested objects


class ValidateIn(BaseModel):
    text: str
    source: str = "json"


class ExportIn(BaseModel):
    content: str
    filename: str = "donnees.json"


@router.get("/health")
async def health():
    try:
        import yaml  # noqa
        has_yaml = True
    except ImportError:
        has_yaml = False
    return {"ok": True, "yaml": has_yaml}


@router.post("/convert")
async def convert(body: ConvertIn):
    if body.source not in FORMATS or body.target not in FORMATS:
        return JSONResponse(status_code=400, content={"error": "Format inconnu."})
    if body.indent not in ("2", "4", "tab", "min"):
        return JSONResponse(status_code=400, content={"error": "Indentation invalide."})

    def _run():
        data = _parse(body.text, body.source, body)
        out = _dump(data, body.target, body)
        # Always also return the JSON data for the tree view
        return {"result": out, "data": data}

    try:
        return await asyncio.to_thread(_run)
    except ParseError as e:
        return _err(e)
    except RecursionError:
        return JSONResponse(status_code=400, content={"error": "Structure trop profonde."})


@router.post("/validate")
async def validate(body: ValidateIn):
    if body.source not in FORMATS:
        return JSONResponse(status_code=400, content={"error": "Format inconnu."})

    def _run():
        opts = ConvertIn(text=body.text, source=body.source)
        data = _parse(body.text, body.source, opts)
        kind = type(data).__name__
        size = len(data) if isinstance(data, (list, dict)) else None
        return {"valid": True, "type": {"dict": "objet", "list": "tableau"}.get(kind, kind), "size": size}

    try:
        return await asyncio.to_thread(_run)
    except ParseError as e:
        if e.code == "MISSING_DEP":
            return _err(e)
        return {"valid": False, "error": e.message, "line": e.line, "column": e.column}


_SAFE_NAME = re.compile(r"[^A-Za-z0-9._ -]+")


@router.post("/export")
async def export(body: ExportIn):
    name = _SAFE_NAME.sub("_", body.filename).strip() or "donnees.txt"
    name = name.replace("..", "_")
    fname = f"{int(time.time() * 1000)}_{name}"
    await asyncio.to_thread((TEMP_DIR / fname).write_text, body.content, "utf-8")
    return {"filename": fname}


@router.get("/file/{filename}")
async def get_file(filename: str):
    if "/" in filename or "\\" in filename or ".." in filename:
        return JSONResponse(status_code=400, content={"error": "Nom de fichier invalide"})
    path = TEMP_DIR / filename
    if not path.is_file():
        return JSONResponse(status_code=404, content={"error": "Fichier introuvable"})
    download = filename.split("_", 1)[1] if "_" in filename else filename
    return FileResponse(path, media_type="application/octet-stream",
                        headers={"Content-Disposition": f'attachment; filename="{download}"'})
