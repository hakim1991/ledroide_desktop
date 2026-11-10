import logging
import re
from datetime import datetime, timezone

from pydantic import BaseModel, Field, ValidationError, field_validator

from ledroide_sdk import Module

log = logging.getLogger("ledroide.snippets")

module = Module(__file__)
router = module.router

COLLECTION = "snippets"
LANGUAGES = ("text", "python", "javascript", "typescript", "sql", "bash", "powershell", "json",
             "html", "css", "yaml", "go")
LANG_ALIASES = {"js": "javascript", "ts": "typescript", "sh": "bash", "shell": "bash", "ps1": "powershell",
                "py": "python", "yml": "yaml", "plain": "text", "txt": "text", "": "text"}
PLACEHOLDER = re.compile(r"\{\{\s*([\w.-]+)\s*(?::([^}]*))?\}\}")
MAX_CONTENT = 200_000
MAX_IMPORT = 5000


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _lang(value: str) -> str:
    v = str(value or "").strip().lower()
    v = LANG_ALIASES.get(v, v)
    return v if v in LANGUAGES else "text"


class SnippetIn(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    language: str = "text"
    tags: list[str] = []
    description: str = Field("", max_length=5000)
    content: str = Field(min_length=1, max_length=MAX_CONTENT)
    favorite: bool = False

    @field_validator("title", mode="before")
    @classmethod
    def _title(cls, v):
        return str(v or "").strip()

    @field_validator("language", mode="before")
    @classmethod
    def _language(cls, v):
        return _lang(v)

    @field_validator("tags", mode="before")
    @classmethod
    def _tags(cls, v):
        if isinstance(v, str):
            v = v.split(",")
        if v is None:
            v = []
        if not isinstance(v, (list, tuple)):
            raise ValueError("liste de tags attendue")
        seen, out = set(), []
        for t in v or []:
            t = str(t).strip().lower().lstrip("#")[:40]
            if t and t not in seen:
                seen.add(t)
                out.append(t)
        return out[:20]


def _placeholders(content: str) -> list[dict]:
    seen, out = set(), []
    for m in PLACEHOLDER.finditer(content or ""):
        if m.group(1) not in seen:
            seen.add(m.group(1))
            out.append({"name": m.group(1), "default": (m.group(2) or "").strip()})
    return out


def _validation_error(exc: ValidationError) -> dict:
    first = exc.errors()[0]
    field = ".".join(map(str, first["loc"]))
    labels = {"title": "Le titre est obligatoire", "content": "Le contenu est obligatoire"}
    if first["type"] in ("string_too_short", "missing") and field in labels:
        return {"error": labels[field], "code": "VALIDATION_ERROR"}
    return {"error": f"Champ invalide ({field} : {first['msg']})", "code": "VALIDATION_ERROR"}


def _sort(items: list[dict], sort: str) -> list[dict]:
    if sort == "used":
        return sorted(items, key=lambda d: (-int(d.get("uses") or 0), d.get("title", "").lower()))
    if sort == "title":
        return sorted(items, key=lambda d: d.get("title", "").lower())
    return sorted(items, key=lambda d: d.get("updated") or "", reverse=True)


def _matches(doc: dict, q: str, language: str, tag: str, favorite: bool) -> bool:
    if language and doc.get("language") != language:
        return False
    if tag and tag not in (doc.get("tags") or []):
        return False
    if favorite and not doc.get("favorite"):
        return False
    if q:
        hay = " ".join([doc.get("title", ""), doc.get("description", ""), doc.get("content", ""),
                        " ".join(doc.get("tags") or []), doc.get("language", "")]).lower()
        return all(word in hay for word in q.lower().split())
    return True


@module.on("list")
async def list_snippets(payload: dict):
    docs = await module.db.query(COLLECTION)
    q = str(payload.get("q") or "").strip()
    language = _lang(payload["language"]) if payload.get("language") else ""
    tag = str(payload.get("tag") or "").strip().lower()
    items = [d for d in docs if _matches(d, q, language, tag, bool(payload.get("favorite")))]
    items = _sort(items, str(payload.get("sort") or "recent"))
    for d in items:
        d["placeholders"] = _placeholders(d.get("content", ""))
    tags: dict[str, int] = {}
    langs: dict[str, int] = {}
    for d in docs:
        for t in d.get("tags") or []:
            tags[t] = tags.get(t, 0) + 1
        langs[d.get("language", "text")] = langs.get(d.get("language", "text"), 0) + 1
    return {"items": items, "total": len(docs), "tags": dict(sorted(tags.items())),
            "languages": dict(sorted(langs.items())), "all_languages": list(LANGUAGES)}


@module.on("get")
async def get_snippet(payload: dict):
    doc = await module.db.get(COLLECTION, str(payload.get("id", "")))
    if doc is None:
        return {"error": "Snippet introuvable", "code": "NOT_FOUND"}
    return {**doc, "placeholders": _placeholders(doc.get("content", ""))}


@module.on("save")
async def save_snippet(payload: dict):
    try:
        data = SnippetIn.model_validate(payload)
    except ValidationError as exc:
        return _validation_error(exc)
    now = _now()
    existing = None
    if payload.get("id"):
        existing = await module.db.get(COLLECTION, str(payload["id"]))
        if existing is None:
            return {"error": "Snippet introuvable", "code": "NOT_FOUND"}
    doc = {**(existing or {"uses": 0, "last_used": None, "created": now}), **data.model_dump(), "updated": now}
    saved = await module.db.save(COLLECTION, doc)
    log.info("snippet %s saved", saved["_id"])
    return {**saved, "placeholders": _placeholders(saved["content"])}


@module.on("delete")
async def delete_snippet(payload: dict):
    r = await module.db.delete(COLLECTION, str(payload.get("id", "")))
    if not r["ok"]:
        return {"error": "Snippet introuvable", "code": "NOT_FOUND"}
    return r


@module.on("favorite")
async def toggle_favorite(payload: dict):
    doc = await module.db.get(COLLECTION, str(payload.get("id", "")))
    if doc is None:
        return {"error": "Snippet introuvable", "code": "NOT_FOUND"}
    doc["favorite"] = bool(payload["value"]) if "value" in payload else not doc.get("favorite")
    return await module.db.save(COLLECTION, doc)


@module.on("use")
async def mark_used(payload: dict):
    doc = await module.db.get(COLLECTION, str(payload.get("id", "")))
    if doc is None:
        return {"error": "Snippet introuvable", "code": "NOT_FOUND"}
    doc["uses"] = int(doc.get("uses") or 0) + 1
    doc["last_used"] = _now()
    saved = await module.db.save(COLLECTION, doc)
    return {"_id": saved["_id"], "uses": saved["uses"], "last_used": saved["last_used"]}


@module.on("fill")
async def fill(payload: dict):
    """Substitutes {{name}} / {{name:default}} placeholders (also done client-side)."""
    content = str(payload.get("content") or "")
    values = payload.get("values") or {}

    def sub(m):
        v = values.get(m.group(1))
        return str(v) if v not in (None, "") else (m.group(2) or "").strip()
    return {"content": PLACEHOLDER.sub(sub, content)}


@module.on("export")
async def export(payload: dict):
    ids = payload.get("ids")
    docs = await module.db.query(COLLECTION)
    if ids:
        wanted = set(map(str, ids))
        docs = [d for d in docs if d["_id"] in wanted]
    keys = ("title", "language", "tags", "description", "content", "favorite", "uses", "created", "updated")
    items = [{k: d.get(k) for k in keys} for d in _sort(docs, "title")]
    return {"format": "ledroide-snippets", "version": 1, "exported": _now(), "count": len(items), "items": items}


@module.on("import")
async def import_snippets(payload: dict):
    data = payload.get("data")
    if isinstance(data, dict):
        data = data.get("items")
    if not isinstance(data, list):
        return {"error": "Format invalide : un tableau de snippets ou un objet {\"items\": [...]} est attendu",
                "code": "BAD_FORMAT"}
    if len(data) > MAX_IMPORT:
        return {"error": f"Trop d'éléments (maximum {MAX_IMPORT})", "code": "TOO_MANY"}
    existing = await module.db.query(COLLECTION)
    known = {(d.get("title", "").lower(), d.get("content", "")) for d in existing}
    imported = skipped = 0
    invalid = []
    now = _now()
    for i, raw in enumerate(data):
        if not isinstance(raw, dict):
            invalid.append(i + 1)
            continue
        raw = {**raw}
        if "code" in raw and "content" not in raw:
            raw["content"] = raw["code"]
        if "lang" in raw and "language" not in raw:
            raw["language"] = raw["lang"]
        try:
            s = SnippetIn.model_validate(raw)
        except ValidationError:
            invalid.append(i + 1)
            continue
        key = (s.title.lower(), s.content)
        if key in known:
            skipped += 1
            continue
        known.add(key)
        uses = raw.get("uses")
        await module.db.save(COLLECTION, {
            **s.model_dump(), "uses": uses if isinstance(uses, int) and uses >= 0 else 0,
            "last_used": None, "created": str(raw.get("created") or now), "updated": now,
        })
        imported += 1
    log.info("import: %d imported, %d skipped, %d invalid", imported, skipped, len(invalid))
    return {"imported": imported, "skipped": skipped, "invalid": invalid}
