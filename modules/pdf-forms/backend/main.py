import asyncio
import base64
import logging
import os
import time
from datetime import datetime
from pathlib import Path

from pydantic import ValidationError

from ledroide_sdk import Module

from pdfforms_render import RenderError, Template, render_pdf, safe_filename, subst, build_vars, normalize_values, \
    validation_message
from pdfforms_examples import EXAMPLES

log = logging.getLogger("ledroide.pdf-forms")

module = Module(__file__)
router = module.router

MAX_BATCH = 2000
KEEP_PREVIEWS = 8


def err(message: str, code: str, **extra) -> dict:
    return {"error": message, "code": code, **extra}


def parse_template(raw) -> Template:
    if not isinstance(raw, dict):
        raise RenderError("Modèle manquant", "INVALID_TEMPLATE")
    data = {k: v for k, v in raw.items() if not k.startswith("_") and k not in ("created_at", "updated_at")}
    try:
        return Template.model_validate(data)
    except ValidationError as exc:
        raise RenderError(validation_message(exc), "INVALID_TEMPLATE")


def output_dir() -> Path:
    custom = (module.config.get("output_dir") or "").strip()
    d = Path(os.path.expandvars(os.path.expanduser(custom))) if custom else module.data_dir / "documents"
    d.mkdir(parents=True, exist_ok=True)
    return d


def unique_path(folder: Path, name: str) -> Path:
    p = folder / name
    stem, n = p.stem, 2
    while p.exists():
        p = folder / f"{stem} ({n}).pdf"
        n += 1
    return p


def file_name_for(tpl: Template, values: dict) -> str:
    now = datetime.now()
    v = build_vars(tpl, normalize_values(tpl, values), now)
    v.update(date=now.strftime("%Y-%m-%d"), heure=now.strftime("%Hh%M"), datetime=now.strftime("%Y-%m-%d %Hh%M"))
    v["doc_title"] = subst(tpl.doc_title, v).strip()
    return safe_filename(subst(tpl.file_name, v))


async def load_template(payload: dict) -> tuple[Template, dict | None]:
    if payload.get("template"):
        tid = payload.get("template_id")
        return parse_template(payload["template"]), ({"_id": str(tid)} if tid else None)
    tid = str(payload.get("template_id") or "")
    doc = await module.db.get("templates", tid) if tid else None
    if not doc:
        raise RenderError("Modèle introuvable", "NOT_FOUND")
    return parse_template(doc), doc


async def open_path(p: Path):
    if not hasattr(os, "startfile"):
        raise RenderError("Ouverture non prise en charge sur ce système", "UNSUPPORTED")
    await asyncio.to_thread(os.startfile, str(p))


def guard(fn):
    async def wrapper(payload: dict):
        try:
            return await fn(payload or {})
        except RenderError as exc:
            out = err(str(exc), exc.code)
            if exc.details is not None:
                out["details"] = exc.details
            return out
        except OSError as exc:
            log.warning("pdf-forms: %s", exc)
            return err(f"Erreur fichier : {exc.strerror or exc}", "IO_ERROR")
    wrapper.__name__ = fn.__name__
    return wrapper


# ── Templates ────────────────────────────────────────────────────────────────

def summary(doc: dict) -> dict:
    fields = doc.get("fields") or []
    return {"_id": doc["_id"], "name": doc.get("name", ""), "description": doc.get("description", ""),
            "fields": sum(1 for f in fields if f.get("type") not in ("section", "paragraph", "pagebreak", "spacer")),
            "updated_at": doc.get("updated_at")}


async def seed_examples():
    if await module.db.get("meta", "seeded"):
        return
    await module.db.save("meta", {"_id": "seeded", "at": time.time()})
    if await module.db.query("templates"):
        return
    for ex in EXAMPLES:
        await module.db.save("templates", parse_template(ex).model_dump())


@module.on("templates_list")
@guard
async def templates_list(payload):
    await seed_examples()
    docs = await module.db.query("templates")
    return {"items": [summary(d) for d in docs]}


@module.on("template_get")
@guard
async def template_get(payload):
    doc = await module.db.get("templates", str(payload.get("id") or ""))
    if not doc:
        return err("Modèle introuvable", "NOT_FOUND")
    draft = await module.db.get("drafts", doc["_id"])
    return {"template": doc, "draft": (draft or {}).get("values")}


@module.on("template_save")
@guard
async def template_save(payload):
    raw = payload.get("template") or {}
    tpl = parse_template(raw)
    doc = tpl.model_dump()
    tid = raw.get("_id")
    if tid:
        if not await module.db.get("templates", str(tid)):
            return err("Modèle introuvable", "NOT_FOUND")
        doc["_id"] = str(tid)
    saved = await module.db.save("templates", doc)
    log.info("pdf-forms: template saved (%d fields)", len(tpl.fields))
    return {"template": saved}


@module.on("template_duplicate")
@guard
async def template_duplicate(payload):
    doc = await module.db.get("templates", str(payload.get("id") or ""))
    if not doc:
        return err("Modèle introuvable", "NOT_FOUND")
    tpl = parse_template(doc)
    tpl.name = (tpl.name + " (copie)")[:120]
    return {"template": await module.db.save("templates", tpl.model_dump())}


@module.on("template_delete")
@guard
async def template_delete(payload):
    tid = str(payload.get("id") or "")
    r = await module.db.delete("templates", tid)
    await module.db.delete("drafts", tid)
    if not r["ok"]:
        return err("Modèle introuvable", "NOT_FOUND")
    return {"ok": True}


@module.on("template_validate")
@guard
async def template_validate(payload):
    tpl = parse_template(payload.get("template"))
    return {"ok": True, "template": tpl.model_dump()}


@module.on("draft_save")
@guard
async def draft_save(payload):
    tid = str(payload.get("template_id") or "")
    if not tid or not await module.db.get("templates", tid):
        return err("Modèle introuvable", "NOT_FOUND")
    values = payload.get("values")
    if not isinstance(values, dict):
        return err("Valeurs invalides", "INVALID_VALUES")
    await module.db.save("drafts", {"_id": tid, "values": values})
    return {"ok": True}


@module.on("draft_clear")
@guard
async def draft_clear(payload):
    await module.db.delete("drafts", str(payload.get("template_id") or ""))
    return {"ok": True}


# ── Generation ───────────────────────────────────────────────────────────────

async def _render(tpl: Template, values: dict, strict: bool = True) -> tuple[bytes, int]:
    return await asyncio.to_thread(render_pdf, tpl, values, None, strict)


def _prune_previews(folder: Path):
    try:
        files = sorted(folder.glob("apercu-*.pdf"), key=lambda p: p.stat().st_mtime, reverse=True)
        for old in files[KEEP_PREVIEWS:]:
            try:
                old.unlink()
            except OSError:
                pass
    except OSError:
        pass


@module.on("preview")
@guard
async def preview(payload):
    tpl, _ = await load_template(payload)
    data, pages = await _render(tpl, payload.get("values") or {}, strict=False)
    folder = module.data_dir / "preview"
    folder.mkdir(parents=True, exist_ok=True)
    _prune_previews(folder)
    p = folder / f"apercu-{int(time.time() * 1000)}.pdf"
    await asyncio.to_thread(p.write_bytes, data)
    opened = False
    if payload.get("open", True):
        await open_path(p)
        opened = True
    return {"path": str(p), "pages": pages, "size": len(data), "opened": opened}


@module.on("generate")
@guard
async def generate(payload):
    tpl, doc = await load_template(payload)
    values = payload.get("values") or {}
    data, pages = await _render(tpl, values)
    name = file_name_for(tpl, values)
    result = {"file_name": name, "pages": pages, "size": len(data)}
    if payload.get("download"):
        result["pdf_b64"] = base64.b64encode(data).decode("ascii")
        return result
    p = unique_path(output_dir(), name)
    await asyncio.to_thread(p.write_bytes, data)
    entry = await module.db.save("documents", {
        "template_id": (doc or {}).get("_id"), "template_name": tpl.name, "file_name": p.name,
        "path": str(p), "pages": pages, "size": len(data), "values": normalize_values(tpl, values),
        "created": datetime.now().isoformat(timespec="seconds"),
    })
    result.update(path=str(p), file_name=p.name, document_id=entry["_id"], opened=False)
    open_after = payload.get("open")
    if open_after is None:
        open_after = bool(module.config.get("open_after", True))
    if open_after:
        try:
            await open_path(p)
            result["opened"] = True
        except (OSError, RenderError) as exc:
            log.warning("pdf-forms: open failed: %s", exc)
    log.info("pdf-forms: generated %d page(s), %d bytes", pages, len(data))
    return result


@module.job("generate_batch")
async def generate_batch(payload, job):
    tpl, doc = await load_template(payload)
    rows = payload.get("rows")
    if not isinstance(rows, list) or not rows:
        raise RenderError("Aucune ligne à générer", "EMPTY_BATCH")
    if len(rows) > MAX_BATCH:
        raise RenderError(f"Trop de lignes ({len(rows)}, max. {MAX_BATCH})", "BATCH_TOO_LARGE")
    folder = output_dir() / safe_filename(f"Lot {tpl.name} {datetime.now():%Y-%m-%d %H-%M-%S}")[:-4]
    folder.mkdir(parents=True, exist_ok=True)
    ok, errors = 0, []
    for i, values in enumerate(rows):
        try:
            if not isinstance(values, dict):
                raise RenderError("ligne invalide", "INVALID_VALUES")
            data, _ = await _render(tpl, values)
            p = unique_path(folder, file_name_for(tpl, values))
            await asyncio.to_thread(p.write_bytes, data)
            ok += 1
        except RenderError as exc:
            errors.append({"row": i + 1, "error": str(exc)})
        await job.progress(round((i + 1) * 100 / len(rows)), f"{i + 1} / {len(rows)}")
    if ok:
        await module.db.save("documents", {
            "template_id": (doc or {}).get("_id"), "template_name": tpl.name, "file_name": folder.name,
            "path": str(folder), "batch": True, "count": ok,
            "created": datetime.now().isoformat(timespec="seconds"),
        })
    log.info("pdf-forms: batch done, %d ok, %d errors", ok, len(errors))
    return {"folder": str(folder), "count": ok, "errors": errors[:200], "error_count": len(errors)}


# ── Documents ────────────────────────────────────────────────────────────────

@module.on("documents_list")
@guard
async def documents_list(payload):
    docs = await module.db.query("documents")
    tid = payload.get("template_id")
    if tid:
        docs = [d for d in docs if d.get("template_id") == tid]
    items = []
    for d in docs[:500]:
        items.append({**{k: v for k, v in d.items() if k != "values"}, "has_values": bool(d.get("values")),
                      "exists": Path(d.get("path", "")).exists()})
    return {"items": items}


@module.on("document_get")
@guard
async def document_get(payload):
    d = await module.db.get("documents", str(payload.get("id") or ""))
    if not d:
        return err("Document introuvable", "NOT_FOUND")
    return {"document": d}


@module.on("document_open")
@guard
async def document_open(payload):
    d = await module.db.get("documents", str(payload.get("id") or ""))
    if not d:
        return err("Document introuvable", "NOT_FOUND")
    p = Path(d["path"])
    if not p.exists():
        return err("Le fichier n'existe plus", "FILE_MISSING")
    await open_path(p)
    return {"ok": True}


@module.on("document_delete")
@guard
async def document_delete(payload):
    did = str(payload.get("id") or "")
    d = await module.db.get("documents", did)
    if not d:
        return err("Document introuvable", "NOT_FOUND")
    deleted_file = False
    if payload.get("delete_file") and not d.get("batch"):
        p = Path(d["path"])
        if p.is_file() and p.suffix.lower() == ".pdf":
            await asyncio.to_thread(p.unlink)
            deleted_file = True
    await module.db.delete("documents", did)
    return {"ok": True, "deleted_file": deleted_file}


@module.on("open_folder")
@guard
async def open_folder(payload):
    await open_path(output_dir())
    return {"ok": True, "path": str(output_dir())}


@module.on("info")
@guard
async def info(payload):
    try:
        import fpdf
        version = getattr(fpdf, "FPDF_VERSION", None) or getattr(fpdf, "__version__", "")
    except ImportError:
        version = None
    return {"output_dir": str(output_dir()), "fpdf": version, "open_after": bool(module.config.get("open_after", True))}
