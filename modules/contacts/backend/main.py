import asyncio
import base64
import csv
import io
import json
import logging
import re
import unicodedata
from datetime import date, datetime, timedelta, timezone
from typing import Optional

from pydantic import BaseModel, Field, ValidationError, field_validator

from ledroide_sdk import Module

log = logging.getLogger("ledroide.contacts")

module = Module(__file__)
router = module.router

KINDS = ("call", "email", "meeting", "message", "note")
PHONE_TYPES = ("mobile", "work", "home", "fax", "other")
EMAIL_TYPES = ("work", "home", "other")
PHOTO_RE = re.compile(r"^data:image/(png|jpeg|jpg|gif|webp);base64,[A-Za-z0-9+/=]+$")
MAX_PHOTO = 400_000
BDAY_RE = re.compile(r"^(\d{4}-|--)(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$")
MAX_IMPORT = 20000

_ready = False
_lock = asyncio.Lock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS contacts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    first_name  TEXT NOT NULL DEFAULT '',
    last_name   TEXT NOT NULL DEFAULT '',
    company     TEXT NOT NULL DEFAULT '',
    job_title   TEXT NOT NULL DEFAULT '',
    emails      TEXT NOT NULL DEFAULT '[]',
    phones      TEXT NOT NULL DEFAULT '[]',
    address     TEXT NOT NULL DEFAULT '{}',
    birthday    TEXT NOT NULL DEFAULT '',
    website     TEXT NOT NULL DEFAULT '',
    notes       TEXT NOT NULL DEFAULT '',
    tags        TEXT NOT NULL DEFAULT '[]',
    photo       TEXT,
    favorite    INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS interactions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    contact_id  INTEGER NOT NULL REFERENCES contacts(id) ON DELETE CASCADE,
    kind        TEXT NOT NULL,
    at          TEXT NOT NULL,
    note        TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS followups (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    contact_id  INTEGER NOT NULL REFERENCES contacts(id) ON DELETE CASCADE,
    due         TEXT NOT NULL,
    title       TEXT NOT NULL,
    done        INTEGER NOT NULL DEFAULT 0,
    done_at     TEXT,
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_inter_contact ON interactions(contact_id);
CREATE INDEX IF NOT EXISTS idx_follow_contact ON followups(contact_id);
"""


async def _db():
    global _ready
    if not _ready:
        async with _lock:
            if not _ready:
                await module.db.executescript(SCHEMA)
                _ready = True
    await module.db.execute("PRAGMA foreign_keys = ON")
    return module.db


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _err(message: str, code: str = "BAD_REQUEST") -> dict:
    return {"error": message, "code": code}


def _validation_message(exc: ValidationError) -> str:
    first = exc.errors()[0]
    loc = ".".join(str(p) for p in first.get("loc", ()))
    msg = str(first.get("msg", "Données invalides")).removeprefix("Value error, ")
    return f"{loc} : {msg}" if loc else msg


def _norm(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", s or "") if not unicodedata.combining(c)).lower().strip()


def _phone_key(s: str) -> str:
    digits = re.sub(r"\D", "", s or "")
    return digits[-9:] if len(digits) >= 8 else ""


def _int(v) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


# ── Models ────────────────────────────────────────────────────────────────────

class Item(BaseModel):
    type: str = "other"
    value: str = Field("", max_length=200)

    @field_validator("value")
    @classmethod
    def _strip(cls, v: str) -> str:
        return v.strip()


class Address(BaseModel):
    street: str = Field("", max_length=300)
    postal_code: str = Field("", max_length=20)
    city: str = Field("", max_length=100)
    region: str = Field("", max_length=100)
    country: str = Field("", max_length=100)


class ContactIn(BaseModel):
    first_name: str = Field("", max_length=100)
    last_name: str = Field("", max_length=100)
    company: str = Field("", max_length=200)
    job_title: str = Field("", max_length=200)
    emails: list[Item] = Field(default_factory=list, max_length=20)
    phones: list[Item] = Field(default_factory=list, max_length=20)
    address: Address = Field(default_factory=Address)
    birthday: str = ""
    website: str = Field("", max_length=500)
    notes: str = Field("", max_length=50000)
    tags: list[str] = Field(default_factory=list, max_length=30)
    photo: Optional[str] = None
    favorite: bool = False

    @field_validator("first_name", "last_name", "company", "job_title", "website")
    @classmethod
    def _strip(cls, v: str) -> str:
        return v.strip()

    @field_validator("emails")
    @classmethod
    def _emails(cls, v: list[Item]) -> list[Item]:
        out = []
        for it in v:
            if not it.value:
                continue
            if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", it.value):
                raise ValueError(f"Adresse e-mail invalide : {it.value}")
            it.type = it.type if it.type in EMAIL_TYPES else "other"
            out.append(it)
        return out

    @field_validator("phones")
    @classmethod
    def _phones(cls, v: list[Item]) -> list[Item]:
        out = []
        for it in v:
            if not it.value:
                continue
            if not re.fullmatch(r"[0-9+()./\s-]{3,40}(\s*(x|ext\.?|poste)\s*\d+)?", it.value, re.I):
                raise ValueError(f"Numéro de téléphone invalide : {it.value}")
            it.type = it.type if it.type in PHONE_TYPES else "other"
            out.append(it)
        return out

    @field_validator("birthday")
    @classmethod
    def _bday(cls, v: str) -> str:
        v = v.strip()
        if not v:
            return ""
        if not BDAY_RE.match(v):
            raise ValueError("Date d'anniversaire invalide (AAAA-MM-JJ, ou --MM-JJ sans année)")
        try:
            date.fromisoformat(("2000" if v.startswith("--") else v[:4]) + v[-6:])
        except ValueError:
            raise ValueError("Date d'anniversaire invalide")
        return v

    @field_validator("tags")
    @classmethod
    def _tags(cls, v: list[str]) -> list[str]:
        out = []
        for t in v:
            t = str(t).strip().lower()[:40]
            if t and t not in out:
                out.append(t)
        return out

    @field_validator("photo")
    @classmethod
    def _photo(cls, v: Optional[str]) -> Optional[str]:
        if v and (len(v) > MAX_PHOTO or not PHOTO_RE.match(v)):
            raise ValueError("Photo invalide ou trop volumineuse (400 Ko max)")
        return v or None


def _check_contact(c: ContactIn) -> Optional[str]:
    if not (c.first_name or c.last_name or c.company or c.emails or c.phones):
        return "Indiquez au moins un nom, une société, un e-mail ou un téléphone"
    return None


class InteractionIn(BaseModel):
    kind: str = "note"
    at: str
    note: str = Field("", max_length=20000)

    @field_validator("kind")
    @classmethod
    def _kind(cls, v: str) -> str:
        if v not in KINDS:
            raise ValueError("Type d'échange inconnu")
        return v

    @field_validator("at")
    @classmethod
    def _at(cls, v: str) -> str:
        try:
            return datetime.fromisoformat(v.strip()).strftime("%Y-%m-%dT%H:%M")
        except (TypeError, ValueError):
            raise ValueError("Date invalide")


class FollowupIn(BaseModel):
    due: str
    title: str = Field(min_length=1, max_length=300)

    @field_validator("title")
    @classmethod
    def _title(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("L'intitulé est requis")
        return v

    @field_validator("due")
    @classmethod
    def _due(cls, v: str) -> str:
        try:
            return date.fromisoformat(v.strip()[:10]).isoformat()
        except (TypeError, ValueError):
            raise ValueError("Date d'échéance invalide (AAAA-MM-JJ)")


# ── Helpers ───────────────────────────────────────────────────────────────────

def _row(r: dict) -> dict:
    return {**r, "emails": json.loads(r["emails"] or "[]"), "phones": json.loads(r["phones"] or "[]"),
            "address": json.loads(r["address"] or "{}"), "tags": json.loads(r["tags"] or "[]"),
            "favorite": bool(r["favorite"])}


def display_name(c: dict) -> str:
    name = " ".join(p for p in (c.get("first_name"), c.get("last_name")) if p)
    return name or c.get("company") or (c.get("emails") or [{}])[0].get("value") or \
        (c.get("phones") or [{}])[0].get("value") or "Sans nom"


def _next_birthday(bday: str, today: date) -> Optional[tuple[date, Optional[int]]]:
    if not bday:
        return None
    m, d = int(bday[-5:-3]), int(bday[-2:])
    for year in (today.year, today.year + 1):
        try:
            nxt = date(year, m, d)
        except ValueError:
            nxt = date(year, 3, 1)
        if nxt >= today:
            age = None if bday.startswith("--") else year - int(bday[:4])
            return nxt, age
    return None


def _values(c: ContactIn) -> tuple:
    return (c.first_name, c.last_name, c.company, c.job_title,
            json.dumps([i.model_dump() for i in c.emails], ensure_ascii=False),
            json.dumps([i.model_dump() for i in c.phones], ensure_ascii=False),
            json.dumps(c.address.model_dump(), ensure_ascii=False), c.birthday, c.website, c.notes,
            json.dumps(c.tags, ensure_ascii=False), c.photo, int(c.favorite))


async def _insert(c: ContactIn) -> int:
    db = await _db()
    now = _now()
    row = await db.fetch_one(
        "INSERT INTO contacts (first_name, last_name, company, job_title, emails, phones, address, birthday, website,"
        " notes, tags, photo, favorite, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) RETURNING id",
        (*_values(c), now, now))
    return row["id"]


async def _get(cid: int) -> Optional[dict]:
    r = await (await _db()).fetch_one("SELECT * FROM contacts WHERE id=?", (cid,))
    return _row(r) if r else None


# ── Contacts ──────────────────────────────────────────────────────────────────

@module.on("contacts.list")
async def contacts_list(payload: dict):
    db = await _db()
    today = date.today().isoformat()
    rows = [_row(r) for r in await db.fetch_all("SELECT * FROM contacts")]
    last = {r["contact_id"]: r["at"] for r in await db.fetch_all(
        "SELECT contact_id, MAX(at) AS at FROM interactions GROUP BY contact_id")}
    nxt = {r["contact_id"]: r for r in await db.fetch_all(
        "SELECT contact_id, MIN(due) AS due, SUM(due < ?) AS overdue, COUNT(*) AS n FROM followups WHERE done=0 GROUP BY contact_id",
        (today,))}
    q = _norm(str(payload.get("q") or ""))
    tag = str(payload.get("tag") or "")
    view = str(payload.get("view") or "all")
    tags: dict[str, int] = {}
    items = []
    tday = date.today()
    birthdays = []
    for c in rows:
        for t in c["tags"]:
            tags[t] = tags.get(t, 0) + 1
        nb = _next_birthday(c["birthday"], tday)
        if nb and (nb[0] - tday).days <= 30:
            birthdays.append({"id": c["id"], "name": display_name(c), "date": nb[0].isoformat(), "age": nb[1],
                              "in_days": (nb[0] - tday).days})
        if tag and tag not in c["tags"]:
            continue
        if view == "favorites" and not c["favorite"]:
            continue
        if view == "followup" and c["id"] not in nxt:
            continue
        if q:
            hay = _norm(" ".join([display_name(c), c["first_name"], c["last_name"], c["company"], c["job_title"], c["notes"],
                                  " ".join(e["value"] for e in c["emails"]), " ".join(p["value"] for p in c["phones"]),
                                  c["address"].get("city", ""), " ".join(c["tags"])]))
            digits = re.sub(r"\D", "", q)
            phone_hit = len(digits) >= 3 and any(digits in re.sub(r"\D", "", p["value"]) for p in c["phones"])
            if not all(w in hay for w in q.split()) and not phone_hit:
                continue
        f = nxt.get(c["id"])
        items.append({"id": c["id"], "name": display_name(c), "first_name": c["first_name"], "last_name": c["last_name"],
                      "company": c["company"], "job_title": c["job_title"],
                      "email": c["emails"][0]["value"] if c["emails"] else "",
                      "phone": c["phones"][0]["value"] if c["phones"] else "",
                      "tags": c["tags"], "favorite": c["favorite"], "photo": c["photo"],
                      "last_interaction": last.get(c["id"]), "next_due": f["due"] if f else None,
                      "overdue": bool(f and f["overdue"]), "updated_at": c["updated_at"]})
    sort = str(payload.get("sort") or "last_name")
    if sort == "recent":
        items.sort(key=lambda i: i["last_interaction"] or "", reverse=True)
    elif sort == "first_name":
        items.sort(key=lambda i: _norm(i["first_name"] or i["name"]))
    else:
        items.sort(key=lambda i: (_norm(i["last_name"] or i["name"]), _norm(i["first_name"])))
    birthdays.sort(key=lambda b: b["in_days"])
    counts = await db.fetch_one(
        "SELECT SUM(done=0) AS todo, SUM(done=0 AND due < ?) AS overdue, SUM(done=0 AND due = ?) AS today FROM followups",
        (today, today))
    return {"items": items, "total": len(rows), "tags": dict(sorted(tags.items())),
            "favorites": sum(1 for c in rows if c["favorite"]), "birthdays": birthdays,
            "followups": {k: counts[k] or 0 for k in ("todo", "overdue", "today")}}


@module.on("contact.get")
async def contact_get(payload: dict):
    cid = _int(payload.get("id"))
    c = await _get(cid)
    if not c:
        return _err("Contact introuvable", "NOT_FOUND")
    db = await _db()
    c["name"] = display_name(c)
    c["interactions"] = await db.fetch_all("SELECT * FROM interactions WHERE contact_id=? ORDER BY at DESC, id DESC", (cid,))
    c["followups"] = [{**f, "done": bool(f["done"])} for f in await db.fetch_all(
        "SELECT * FROM followups WHERE contact_id=? ORDER BY done, due, id", (cid,))]
    nb = _next_birthday(c["birthday"], date.today())
    c["next_birthday"] = {"date": nb[0].isoformat(), "age": nb[1]} if nb else None
    return c


@module.on("contact.save")
async def contact_save(payload: dict):
    try:
        c = ContactIn.model_validate(payload.get("contact") or {})
    except ValidationError as exc:
        return _err(_validation_message(exc))
    if msg := _check_contact(c):
        return _err(msg)
    db = await _db()
    cid = _int(payload.get("id"))
    if cid:
        n = await db.execute(
            "UPDATE contacts SET first_name=?, last_name=?, company=?, job_title=?, emails=?, phones=?, address=?,"
            " birthday=?, website=?, notes=?, tags=?, photo=?, favorite=?, updated_at=? WHERE id=?",
            (*_values(c), _now(), cid))
        if not n:
            return _err("Contact introuvable", "NOT_FOUND")
    else:
        cid = await _insert(c)
    return await contact_get({"id": cid})


@module.on("contact.favorite")
async def contact_favorite(payload: dict):
    n = await (await _db()).execute("UPDATE contacts SET favorite=? WHERE id=?",
                                    (int(bool(payload.get("favorite"))), _int(payload.get("id"))))
    return {"ok": True} if n else _err("Contact introuvable", "NOT_FOUND")


@module.on("contact.delete")
async def contact_delete(payload: dict):
    db = await _db()
    ids = [_int(i) for i in (payload.get("ids") or [payload.get("id")]) if _int(i)]
    if not ids:
        return _err("Contact introuvable", "NOT_FOUND")
    marks = ",".join("?" * len(ids))
    await db.execute(f"DELETE FROM interactions WHERE contact_id IN ({marks})", tuple(ids))
    await db.execute(f"DELETE FROM followups WHERE contact_id IN ({marks})", tuple(ids))
    n = await db.execute(f"DELETE FROM contacts WHERE id IN ({marks})", tuple(ids))
    if not n:
        return _err("Contact introuvable", "NOT_FOUND")
    return {"ok": True, "deleted": n}


@module.on("tags.rename")
async def tags_rename(payload: dict):
    old = str(payload.get("old") or "").strip().lower()
    new = str(payload.get("new") or "").strip().lower()[:40]
    if not old:
        return _err("Tag invalide")
    db = await _db()
    n = 0
    for r in await db.fetch_all("SELECT id, tags FROM contacts"):
        tags = json.loads(r["tags"] or "[]")
        if old in tags:
            out = []
            for t in tags:
                t = new if t == old else t
                if t and t not in out:
                    out.append(t)
            await db.execute("UPDATE contacts SET tags=? WHERE id=?", (json.dumps(out, ensure_ascii=False), r["id"]))
            n += 1
    return {"ok": True, "updated": n}


# ── Interactions / follow-ups ─────────────────────────────────────────────────

@module.on("interaction.save")
async def interaction_save(payload: dict):
    try:
        it = InteractionIn.model_validate(payload.get("interaction") or {})
    except ValidationError as exc:
        return _err(_validation_message(exc))
    db = await _db()
    iid = _int(payload.get("id"))
    if iid:
        row = await db.fetch_one("SELECT contact_id FROM interactions WHERE id=?", (iid,))
        if not row:
            return _err("Échange introuvable", "NOT_FOUND")
        cid = row["contact_id"]
        await db.execute("UPDATE interactions SET kind=?, at=?, note=? WHERE id=?", (it.kind, it.at, it.note, iid))
    else:
        cid = _int(payload.get("contact_id"))
        if not await _get(cid):
            return _err("Contact introuvable", "NOT_FOUND")
        await db.execute("INSERT INTO interactions (contact_id, kind, at, note) VALUES (?,?,?,?)", (cid, it.kind, it.at, it.note))
    await db.execute("UPDATE contacts SET updated_at=? WHERE id=?", (_now(), cid))
    return await contact_get({"id": cid})


@module.on("interaction.delete")
async def interaction_delete(payload: dict):
    db = await _db()
    row = await db.fetch_one("SELECT contact_id FROM interactions WHERE id=?", (_int(payload.get("id")),))
    if not row:
        return _err("Échange introuvable", "NOT_FOUND")
    await db.execute("DELETE FROM interactions WHERE id=?", (_int(payload.get("id")),))
    return await contact_get({"id": row["contact_id"]})


@module.on("followup.save")
async def followup_save(payload: dict):
    try:
        f = FollowupIn.model_validate(payload.get("followup") or {})
    except ValidationError as exc:
        return _err(_validation_message(exc))
    db = await _db()
    fid = _int(payload.get("id"))
    if fid:
        row = await db.fetch_one("SELECT contact_id FROM followups WHERE id=?", (fid,))
        if not row:
            return _err("Relance introuvable", "NOT_FOUND")
        cid = row["contact_id"]
        await db.execute("UPDATE followups SET due=?, title=? WHERE id=?", (f.due, f.title, fid))
    else:
        cid = _int(payload.get("contact_id"))
        if not await _get(cid):
            return _err("Contact introuvable", "NOT_FOUND")
        await db.execute("INSERT INTO followups (contact_id, due, title, created_at) VALUES (?,?,?,?)",
                         (cid, f.due, f.title, _now()))
    return await contact_get({"id": cid})


@module.on("followup.toggle")
async def followup_toggle(payload: dict):
    db = await _db()
    fid = _int(payload.get("id"))
    row = await db.fetch_one("SELECT * FROM followups WHERE id=?", (fid,))
    if not row:
        return _err("Relance introuvable", "NOT_FOUND")
    done = bool(payload.get("done", not row["done"]))
    await db.execute("UPDATE followups SET done=?, done_at=? WHERE id=?", (int(done), _now() if done else None, fid))
    if done and payload.get("log"):
        await db.execute("INSERT INTO interactions (contact_id, kind, at, note) VALUES (?,?,?,?)",
                         (row["contact_id"], "note", datetime.now().strftime("%Y-%m-%dT%H:%M"), f"Relance faite : {row['title']}"))
    return await contact_get({"id": row["contact_id"]})


@module.on("followup.snooze")
async def followup_snooze(payload: dict):
    db = await _db()
    fid = _int(payload.get("id"))
    row = await db.fetch_one("SELECT * FROM followups WHERE id=?", (fid,))
    if not row:
        return _err("Relance introuvable", "NOT_FOUND")
    days = max(1, min(365, _int(payload.get("days")) or 1))
    base = max(date.today(), date.fromisoformat(row["due"]))
    await db.execute("UPDATE followups SET due=? WHERE id=?", ((base + timedelta(days=days)).isoformat(), fid))
    return {"ok": True}


@module.on("followup.delete")
async def followup_delete(payload: dict):
    db = await _db()
    row = await db.fetch_one("SELECT contact_id FROM followups WHERE id=?", (_int(payload.get("id")),))
    if not row:
        return _err("Relance introuvable", "NOT_FOUND")
    await db.execute("DELETE FROM followups WHERE id=?", (_int(payload.get("id")),))
    return await contact_get({"id": row["contact_id"]})


@module.on("followups.list")
async def followups_list(payload: dict):
    db = await _db()
    scope = str(payload.get("scope") or "todo")
    today = date.today().isoformat()
    where = {"overdue": "f.done=0 AND f.due < ?", "done": "f.done=1 AND ? IS NOT NULL",
             "week": "f.done=0 AND f.due <= date(?, '+7 days')"}.get(scope, "f.done=0 AND ? IS NOT NULL")
    order = "f.done_at DESC LIMIT 200" if scope == "done" else "f.due, f.id"
    rows = await db.fetch_all(
        f"SELECT f.*, c.first_name, c.last_name, c.company, c.emails, c.phones, c.photo FROM followups f "
        f"JOIN contacts c ON c.id = f.contact_id WHERE {where} ORDER BY {order}", (today,))
    out = []
    for r in rows:
        c = {"first_name": r["first_name"], "last_name": r["last_name"], "company": r["company"],
             "emails": json.loads(r["emails"]), "phones": json.loads(r["phones"])}
        out.append({"id": r["id"], "contact_id": r["contact_id"], "due": r["due"], "title": r["title"],
                    "done": bool(r["done"]), "done_at": r["done_at"], "name": display_name(c), "company": r["company"],
                    "photo": r["photo"], "overdue": not r["done"] and r["due"] < today, "today": r["due"] == today})
    return {"items": out, "today": today}


# ── vCard ─────────────────────────────────────────────────────────────────────

def _unescape(v: str) -> str:
    return re.sub(r"\\(.)", lambda m: "\n" if m.group(1) in "nN" else m.group(1), v)


def _split(v: str, sep: str) -> list[str]:
    parts, cur, i = [], "", 0
    while i < len(v):
        ch = v[i]
        if ch == "\\" and i + 1 < len(v):
            cur += v[i:i + 2]
            i += 2
            continue
        if ch == sep:
            parts.append(cur)
            cur = ""
        else:
            cur += ch
        i += 1
    parts.append(cur)
    return parts


def _qp_decode(v: str, charset: str) -> str:
    raw = re.sub(r"=([0-9A-Fa-f]{2})", lambda m: chr(int(m.group(1), 16)), v).encode("latin-1", "replace")
    try:
        return raw.decode(charset or "utf-8")
    except (LookupError, UnicodeDecodeError):
        return raw.decode("latin-1")


def _unfold(text: str) -> list[str]:
    lines: list[str] = []
    for line in re.split(r"\r\n|\r|\n", text):
        if line[:1] in (" ", "\t") and lines:
            lines[-1] += line[1:]
        elif lines and lines[-1].endswith("=") and "QUOTED-PRINTABLE" in lines[-1].split(":", 1)[0].upper():
            lines[-1] = lines[-1][:-1] + line
        else:
            lines.append(line)
    return lines


def _parse_line(line: str) -> Optional[tuple[str, dict, str]]:
    in_q = False
    for i, ch in enumerate(line):
        if ch == '"':
            in_q = not in_q
        elif ch == ":" and not in_q:
            head, value = line[:i], line[i + 1:]
            break
    else:
        return None
    parts = head.split(";")
    name = parts[0].split(".")[-1].upper()
    params: dict[str, list[str]] = {}
    for p in parts[1:]:
        if "=" in p:
            k, v = p.split("=", 1)
            params.setdefault(k.upper(), []).extend(x.strip('"').lower() for x in v.split(","))
        elif p:
            params.setdefault("TYPE", []).append(p.lower())
    return name, params, value


def _ptype(params: dict, allowed: tuple, default="other") -> str:
    types = params.get("TYPE", [])
    if "cell" in types or "mobile" in types:
        return "mobile" if "mobile" in allowed else default
    if "fax" in types:
        return "fax" if "fax" in allowed else default
    if "work" in types:
        return "work"
    if "home" in types:
        return "home"
    return default


def parse_vcards(text: str) -> list[dict]:
    cards, cur = [], None
    for line in _unfold(text.lstrip("﻿")):
        if not line.strip():
            continue
        parsed = _parse_line(line)
        if not parsed:
            continue
        name, params, value = parsed
        if name == "BEGIN" and value.strip().upper() == "VCARD":
            cur = {"first_name": "", "last_name": "", "company": "", "job_title": "", "emails": [], "phones": [],
                   "address": {}, "birthday": "", "website": "", "notes": "", "tags": [], "photo": None, "_fn": ""}
            continue
        if cur is None:
            continue
        if name == "END":
            fn = cur.pop("_fn")
            if not (cur["first_name"] or cur["last_name"]) and fn and fn != cur["company"]:
                bits = fn.rsplit(" ", 1)
                cur["first_name"], cur["last_name"] = (bits[0], bits[1]) if len(bits) == 2 else ("", bits[0])
            cards.append(cur)
            cur = None
            if len(cards) >= MAX_IMPORT:
                break
            continue
        enc = (params.get("ENCODING") or [""])[0]
        if enc == "quoted-printable":
            value = _qp_decode(value, (params.get("CHARSET") or ["utf-8"])[0])
        if name == "N":
            comps = [_unescape(x).strip() for x in _split(value, ";")] + [""] * 5
            cur["last_name"], cur["first_name"] = comps[0][:100], " ".join(x for x in (comps[1], comps[2]) if x)[:100]
        elif name == "FN":
            cur["_fn"] = _unescape(value).strip()
        elif name == "ORG":
            cur["company"] = _unescape(_split(value, ";")[0]).strip()[:200]
        elif name == "TITLE":
            cur["job_title"] = _unescape(value).strip()[:200]
        elif name == "EMAIL":
            v = _unescape(value).strip().removeprefix("mailto:")
            if v and len(cur["emails"]) < 20:
                cur["emails"].append({"type": _ptype(params, EMAIL_TYPES), "value": v[:200]})
        elif name == "TEL":
            v = _unescape(value).strip().removeprefix("tel:")
            if v and len(cur["phones"]) < 20:
                cur["phones"].append({"type": _ptype(params, PHONE_TYPES), "value": v[:200]})
        elif name == "ADR" and not cur["address"]:
            comps = [_unescape(x).strip() for x in _split(value, ";")] + [""] * 7
            street = ", ".join(x for x in (comps[2], comps[1], comps[0]) if x)
            cur["address"] = {"street": street[:300], "city": comps[3][:100], "region": comps[4][:100],
                              "postal_code": comps[5][:20], "country": comps[6][:100]}
        elif name == "BDAY":
            v = value.strip()
            m = re.match(r"^(\d{4})-?(\d{2})-?(\d{2})", v) or re.match(r"^(--)-?(\d{2})-?(\d{2})", v)
            if m:
                y = m.group(1)
                cur["birthday"] = f"--{m.group(2)}-{m.group(3)}" if y == "--" else f"{y}-{m.group(2)}-{m.group(3)}"
        elif name == "NOTE":
            cur["notes"] = (cur["notes"] + "\n" + _unescape(value)).strip()[:50000]
        elif name == "URL" and not cur["website"]:
            cur["website"] = _unescape(value).strip()[:500]
        elif name == "CATEGORIES":
            for t in _split(value, ","):
                t = _unescape(t).strip().lower()[:40]
                if t and t not in cur["tags"]:
                    cur["tags"].append(t)
        elif name == "PHOTO":
            v = value.strip()
            if v.startswith("data:image/"):
                cur["photo"] = v if len(v) <= MAX_PHOTO and PHOTO_RE.match(v) else None
            elif enc in ("b", "base64"):
                kind = next((t for t in params.get("TYPE", []) if t in ("jpeg", "jpg", "png", "gif", "webp")), "jpeg")
                data = f"data:image/{kind};base64,{re.sub(r'\s', '', v)}"
                cur["photo"] = data if len(data) <= MAX_PHOTO and PHOTO_RE.match(data) else None
    return cards


def _escape(v: str) -> str:
    return (v or "").replace("\\", "\\\\").replace("\n", "\\n").replace(",", "\\,").replace(";", "\\;")


def _fold(line: str) -> str:
    out, cur, size = [], "", 0
    for ch in line:
        n = len(ch.encode("utf-8"))
        if size + n > (75 if not out else 74):
            out.append(cur)
            cur, size = "", 0
        cur += ch
        size += n
    out.append(cur)
    return "\r\n ".join(out)


def to_vcard(c: dict, version: str) -> str:
    v4 = version == "4.0"
    lines = ["BEGIN:VCARD", f"VERSION:{version}"]
    lines.append(f"N:{_escape(c['last_name'])};{_escape(c['first_name'])};;;")
    lines.append(f"FN:{_escape(display_name(c))}")
    if c["company"]:
        lines.append(f"ORG:{_escape(c['company'])}")
    if c["job_title"]:
        lines.append(f"TITLE:{_escape(c['job_title'])}")
    tmap = {"mobile": "cell", "work": "work", "home": "home", "fax": "fax"}
    for e in c["emails"]:
        t = tmap.get(e["type"])
        lines.append(f"EMAIL;TYPE={t if v4 else 'INTERNET' + (',' + t.upper() if t else '')}:{_escape(e['value'])}"
                     if (t or not v4) else f"EMAIL:{_escape(e['value'])}")
    for p in c["phones"]:
        t = tmap.get(p["type"], "voice")
        lines.append(f"TEL;TYPE={t if v4 else t.upper()}:{_escape(p['value'])}")
    a = c["address"] or {}
    if any(a.values()):
        lines.append("ADR;TYPE=" + ("home" if v4 else "HOME") + ":;;" + ";".join(
            _escape(a.get(k, "")) for k in ("street", "city", "region", "postal_code", "country")))
    if c["birthday"]:
        b = c["birthday"]
        lines.append(f"BDAY:{b.replace('-', '') if v4 and not b.startswith('--') else ('--' + b[-5:].replace('-', '') if v4 else b)}")
    if c.get("website"):
        lines.append(f"URL:{c['website']}")
    if c["notes"]:
        lines.append(f"NOTE:{_escape(c['notes'])}")
    if c["tags"]:
        lines.append("CATEGORIES:" + ",".join(_escape(t) for t in c["tags"]))
    if c.get("photo"):
        m = re.match(r"^data:image/(\w+);base64,(.*)$", c["photo"])
        if m:
            lines.append(f"PHOTO:{c['photo']}" if v4 else f"PHOTO;ENCODING=b;TYPE={m.group(1).upper()}:{m.group(2)}")
    lines.append(f"REV:{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}")
    lines.append("END:VCARD")
    return "\r\n".join(_fold(line) for line in lines) + "\r\n"


async def _import_dicts(cards: list[dict]) -> dict:
    imported, errors = 0, []
    for n, raw in enumerate(cards, start=1):
        raw.setdefault("address", {})
        try:
            c = ContactIn.model_validate(raw)
        except ValidationError as exc:
            msg = _validation_message(exc)
            if "emails" in msg or "phones" in msg or "birthday" in msg or "photo" in msg:
                raw["emails"] = [e for e in raw.get("emails", []) if re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", e.get("value", ""))]
                raw["phones"] = [p for p in raw.get("phones", []) if re.fullmatch(r"[0-9+()./\s-]{3,40}", p.get("value", ""))]
                if "birthday" in msg:
                    raw["birthday"] = ""
                if "photo" in msg:
                    raw["photo"] = None
                try:
                    c = ContactIn.model_validate(raw)
                except ValidationError as exc2:
                    errors.append(f"Fiche {n} : {_validation_message(exc2)}")
                    continue
            else:
                errors.append(f"Fiche {n} : {msg}")
                continue
        if _check_contact(c):
            errors.append(f"Fiche {n} : fiche vide ignorée")
            continue
        await _insert(c)
        imported += 1
    return {"imported": imported, "errors": errors[:50], "error_count": len(errors)}


@module.on("vcard.import")
async def vcard_import(payload: dict):
    text = str(payload.get("text") or "")
    if len(text) > 50_000_000:
        return _err("Fichier trop volumineux", "TOO_LARGE")
    cards = parse_vcards(text)
    if not cards:
        return _err("Aucune fiche vCard trouvée dans ce fichier", "BAD_FILE")
    r = await _import_dicts(cards)
    log.info("vcard import: %d imported, %d errors", r["imported"], r["error_count"])
    return r


async def _selected(payload: dict) -> list[dict]:
    db = await _db()
    ids = [_int(i) for i in payload.get("ids") or [] if _int(i)]
    if ids:
        marks = ",".join("?" * len(ids))
        rows = await db.fetch_all(f"SELECT * FROM contacts WHERE id IN ({marks}) ORDER BY last_name, first_name", tuple(ids))
    else:
        rows = await db.fetch_all("SELECT * FROM contacts ORDER BY last_name, first_name")
    return [_row(r) for r in rows]


@module.on("vcard.export")
async def vcard_export(payload: dict):
    version = "4.0" if str(payload.get("version")) == "4.0" else "3.0"
    rows = await _selected(payload)
    return {"text": "".join(to_vcard(c, version) for c in rows), "count": len(rows)}


CSV_COLS = ["prenom", "nom", "societe", "fonction", "emails", "telephones", "rue", "code_postal", "ville", "region",
            "pays", "anniversaire", "site_web", "tags", "notes"]


@module.on("csv.export")
async def csv_export(payload: dict):
    rows = await _selected(payload)
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";" if payload.get("semicolon", True) else ",", lineterminator="\r\n")
    w.writerow(CSV_COLS)
    for c in rows:
        a = c["address"] or {}
        w.writerow([c["first_name"], c["last_name"], c["company"], c["job_title"],
                    " | ".join(e["value"] for e in c["emails"]), " | ".join(p["value"] for p in c["phones"]),
                    a.get("street", ""), a.get("postal_code", ""), a.get("city", ""), a.get("region", ""),
                    a.get("country", ""), c["birthday"], c.get("website", ""), ", ".join(c["tags"]), c["notes"]])
    return {"text": "﻿" + buf.getvalue(), "count": len(rows)}


def _csv_kind(col: str) -> Optional[tuple[str, str]]:
    k = re.sub(r"[^a-z0-9]+", " ", _norm(col)).strip()
    if k in ("prenom", "first name", "given name", "firstname"):
        return "first_name", ""
    if k in ("nom", "last name", "family name", "surname", "lastname", "nom de famille"):
        return "last_name", ""
    if k in ("name", "nom complet", "full name", "display name"):
        return "full_name", ""
    if k in ("societe", "company", "organization", "organisation", "organization name", "organization 1 name", "entreprise"):
        return "company", ""
    if k in ("fonction", "job title", "title", "organization title", "organization 1 title", "poste"):
        return "job_title", ""
    if "mail" in k and "type" not in k and "label" not in k:
        return "emails", "work" if "business" in k or "work" in k else "home" if "home" in k else "other"
    if any(w in k for w in ("phone", "telephone", "mobile", "tel", "fax")) and "type" not in k and "label" not in k:
        t = "mobile" if "mobile" in k or "cell" in k else "fax" if "fax" in k else \
            "work" if "business" in k or "work" in k else "home" if "home" in k else "other"
        return "phones", t
    if k in ("rue", "street", "address 1 street", "home street", "business street", "adresse"):
        return "street", ""
    if k in ("code postal", "code_postal", "postal code", "zip", "address 1 postal code", "home postal code", "business postal code"):
        return "postal_code", ""
    if k in ("ville", "city", "address 1 city", "home city", "business city"):
        return "city", ""
    if k in ("region", "state", "address 1 region", "home state", "business state"):
        return "region", ""
    if k in ("pays", "country", "address 1 country", "home country region", "business country region"):
        return "country", ""
    if k in ("anniversaire", "birthday", "date de naissance"):
        return "birthday", ""
    if k in ("site web", "site_web", "website", "web page", "website 1 value"):
        return "website", ""
    if k in ("tags", "categories", "group membership", "labels"):
        return "tags", ""
    if k in ("notes", "note", "commentaires"):
        return "notes", ""
    return None


def _label_type(label: str, default: str, field: str) -> str:
    lab = _norm(label)
    if any(w in lab for w in ("mobile", "cell", "portable")):
        return "mobile" if field == "phones" else default
    if "fax" in lab:
        return "fax" if field == "phones" else default
    if any(w in lab for w in ("work", "business", "travail", "bureau", "pro")):
        return "work"
    if any(w in lab for w in ("home", "domicile", "perso")):
        return "home"
    return default


def _parse_bday(v: str) -> str:
    v = v.strip()
    m = re.match(r"^(\d{4})-(\d{1,2})-(\d{1,2})", v)
    if m:
        return f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    m = re.match(r"^(\d{1,2})[/.](\d{1,2})[/.](\d{4})$", v)
    if m:
        return f"{m.group(3)}-{int(m.group(2)):02d}-{int(m.group(1)):02d}"
    m = re.match(r"^--(\d{2})-?(\d{2})$", v)
    return f"--{m.group(1)}-{m.group(2)}" if m else ""


def parse_csv(text: str) -> list[dict]:
    text = text.lstrip("﻿")
    first = text.split("\n", 1)[0]
    reader = csv.reader(io.StringIO(text), delimiter=max(",;\t", key=first.count))
    header = next(reader, None)
    if not header:
        return []
    kinds = [_csv_kind(h) for h in header]
    prefixes = [_norm(h).rsplit(" - ", 1)[0] if " - " in h else None for h in header]
    label_idx = {prefixes[i]: i for i, h in enumerate(header) if _norm(h).endswith(("- label", "- type"))}
    if not any(kinds):
        raise ValueError("Colonnes non reconnues (prénom, nom, e-mail, téléphone… attendus)")
    out = []
    for row in reader:
        if len(out) >= MAX_IMPORT:
            break
        c = {"first_name": "", "last_name": "", "company": "", "job_title": "", "emails": [], "phones": [],
             "address": {}, "birthday": "", "website": "", "notes": "", "tags": []}
        full = ""
        for i, (kind, val) in enumerate(zip(kinds, row)):
            val = (val or "").strip()
            if not kind or not val:
                continue
            field, typ = kind
            if field in ("emails", "phones"):
                li = label_idx.get(prefixes[i])
                if li is not None and li < len(row):
                    typ = _label_type(row[li], typ, field)
                for v in re.split(r"\s*(?:\||:::|;)\s*", val):
                    if v:
                        c[field].append({"type": typ, "value": v[:200]})
            elif field in ("street", "postal_code", "city", "region", "country"):
                c["address"][field] = val[:300]
            elif field == "tags":
                c["tags"] += [t.strip().lower().lstrip("*").strip() for t in re.split(r"[,;]|:::", val)
                              if t.strip() and t.strip().lower() not in ("* mycontacts", "* starred")]
            elif field == "birthday":
                c["birthday"] = _parse_bday(val)
            elif field == "full_name":
                full = val
            elif field == "notes":
                c["notes"] = val[:50000]
            else:
                c[field] = val[:200]
        if full and not (c["first_name"] or c["last_name"]):
            bits = full.rsplit(" ", 1)
            c["first_name"], c["last_name"] = (bits[0], bits[1]) if len(bits) == 2 else ("", bits[0])
        out.append(c)
    return out


@module.on("csv.import")
async def csv_import(payload: dict):
    text = str(payload.get("text") or "")
    if len(text) > 50_000_000:
        return _err("Fichier trop volumineux", "TOO_LARGE")
    try:
        rows = parse_csv(text)
    except ValueError as exc:
        return _err(str(exc), "BAD_FILE")
    if not rows:
        return _err("Fichier CSV vide", "BAD_FILE")
    return await _import_dicts(rows)


# ── Duplicates / merge ────────────────────────────────────────────────────────

@module.on("duplicates.find")
async def duplicates_find(payload: dict):
    db = await _db()
    rows = [_row(r) for r in await db.fetch_all("SELECT * FROM contacts ORDER BY id")]
    parent = {c["id"]: c["id"] for c in rows}
    reasons: dict[tuple[int, int], set[str]] = {}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    seen: dict[str, int] = {}
    for c in rows:
        keys = [("e-mail", "e:" + e["value"].lower()) for e in c["emails"]]
        keys += [("téléphone", "p:" + k) for k in (_phone_key(p["value"]) for p in c["phones"]) if k]
        name = " ".join(sorted(_norm(f"{c['first_name']} {c['last_name']}").split()))
        if len(name) >= 3 and (c["first_name"] and c["last_name"]):
            keys.append(("nom", "n:" + name))
        for why, k in keys:
            if k in seen and find(seen[k]) != find(c["id"]):
                a, b = find(seen[k]), find(c["id"])
                parent[b] = a
            if k in seen:
                reasons.setdefault(tuple(sorted((seen[k], c["id"]))), set()).add(why)
            else:
                seen[k] = c["id"]
    groups: dict[int, list[dict]] = {}
    for c in rows:
        groups.setdefault(find(c["id"]), []).append(c)
    out = []
    for members in groups.values():
        if len(members) < 2:
            continue
        ids = {m["id"] for m in members}
        why = sorted({w for (a, b), ws in reasons.items() if a in ids and b in ids for w in ws})
        out.append({"reasons": why, "contacts": [{"id": m["id"], "name": display_name(m), "company": m["company"],
                                                    "emails": [e["value"] for e in m["emails"]],
                                                    "phones": [p["value"] for p in m["phones"]], "photo": m["photo"],
                                                    "updated_at": m["updated_at"]} for m in members]})
    return {"groups": out}


@module.on("contacts.merge")
async def contacts_merge(payload: dict):
    keep_id = _int(payload.get("keep_id"))
    others = [i for i in dict.fromkeys(_int(x) for x in payload.get("merge_ids") or []) if i and i != keep_id]
    if not others:
        return _err("Choisissez au moins une fiche à fusionner")
    keep = await _get(keep_id)
    if not keep:
        return _err("Contact introuvable", "NOT_FOUND")
    db = await _db()
    for oid in others:
        o = await _get(oid)
        if not o:
            return _err("Contact à fusionner introuvable", "NOT_FOUND")
        for f in ("first_name", "last_name", "company", "job_title", "birthday", "website", "photo"):
            if not keep.get(f) and o.get(f):
                keep[f] = o[f]
        for f, keyf in (("emails", lambda v: v.lower()), ("phones", lambda v: _phone_key(v) or v)):
            have = {keyf(i["value"]) for i in keep[f]}
            for i in o[f]:
                if keyf(i["value"]) not in have:
                    keep[f].append(i)
                    have.add(keyf(i["value"]))
        if not any((keep["address"] or {}).values()) and any((o["address"] or {}).values()):
            keep["address"] = o["address"]
        keep["tags"] = list(dict.fromkeys(keep["tags"] + o["tags"]))
        if o["notes"] and o["notes"] not in keep["notes"]:
            keep["notes"] = (keep["notes"] + "\n\n" + o["notes"]).strip()
        keep["favorite"] = keep["favorite"] or o["favorite"]
    try:
        c = ContactIn.model_validate({**keep, "emails": keep["emails"][:20], "phones": keep["phones"][:20],
                                      "tags": keep["tags"][:30], "notes": keep["notes"][:50000]})
    except ValidationError as exc:
        return _err(_validation_message(exc))
    marks = ",".join("?" * len(others))
    await db.execute(f"UPDATE interactions SET contact_id=? WHERE contact_id IN ({marks})", (keep_id, *others))
    await db.execute(f"UPDATE followups SET contact_id=? WHERE contact_id IN ({marks})", (keep_id, *others))
    await db.execute(
        "UPDATE contacts SET first_name=?, last_name=?, company=?, job_title=?, emails=?, phones=?, address=?,"
        " birthday=?, website=?, notes=?, tags=?, photo=?, favorite=?, updated_at=? WHERE id=?",
        (*_values(c), _now(), keep_id))
    await db.execute(f"DELETE FROM contacts WHERE id IN ({marks})", tuple(others))
    log.info("merged %d contact(s) into %d", len(others), keep_id)
    return await contact_get({"id": keep_id})


@module.on("contacts.seed")
async def contacts_seed(payload: dict):
    today = date.today()
    samples = [
        {"first_name": "Camille", "last_name": "Martin", "company": "Atelier Lumière", "job_title": "Directrice artistique",
         "emails": [{"type": "work", "value": "camille.martin@atelier-lumiere.fr"}],
         "phones": [{"type": "mobile", "value": "06 12 34 56 78"}],
         "address": {"street": "12 rue des Lilas", "postal_code": "69003", "city": "Lyon", "country": "France"},
         "birthday": f"1988-{(today + timedelta(days=5)).strftime('%m-%d')}", "tags": ["client", "design"], "favorite": True,
         "notes": "Rencontrée au salon Graphika. Préfère être contactée le matin."},
        {"first_name": "Julien", "last_name": "Bernard", "company": "Transports Bernard", "job_title": "Gérant",
         "emails": [{"type": "work", "value": "j.bernard@transports-bernard.fr"}],
         "phones": [{"type": "work", "value": "04 72 00 11 22"}, {"type": "mobile", "value": "07 98 76 54 32"}],
         "tags": ["prospect"]},
        {"first_name": "Sophie", "last_name": "Leroy", "company": "", "emails": [{"type": "home", "value": "sophie.leroy@mail.fr"}],
         "phones": [{"type": "mobile", "value": "+33 6 55 44 33 22"}], "birthday": "--11-30", "tags": ["perso"]},
        {"first_name": "Karim", "last_name": "Haddad", "company": "Nexa Conseil", "job_title": "Consultant SI",
         "emails": [{"type": "work", "value": "k.haddad@nexa-conseil.com"}], "tags": ["partenaire", "client"]},
    ]
    ids = []
    for s in samples:
        ids.append(await _insert(ContactIn.model_validate(s)))
    db = await _db()
    now = datetime.now()
    inter = [(ids[0], "meeting", now - timedelta(days=12), "Présentation du devis refonte site. Intéressée, attend une V2."),
             (ids[0], "email", now - timedelta(days=3), "Envoi de la V2 du devis."),
             (ids[1], "call", now - timedelta(days=20), "Premier appel : besoin d'un outil de suivi des tournées."),
             (ids[3], "call", now - timedelta(days=1), "Point sur le partenariat.")]
    for cid, kind, at, note in inter:
        await db.execute("INSERT INTO interactions (contact_id, kind, at, note) VALUES (?,?,?,?)",
                         (cid, kind, at.strftime("%Y-%m-%dT%H:%M"), note))
    fol = [(ids[0], today + timedelta(days=2), "Relancer pour la signature du devis"),
           (ids[1], today - timedelta(days=4), "Rappeler pour proposer une démo"),
           (ids[3], today, "Envoyer la proposition de partenariat")]
    for cid, due, title in fol:
        await db.execute("INSERT INTO followups (contact_id, due, title, created_at) VALUES (?,?,?,?)",
                         (cid, due.isoformat(), title, _now()))
    return {"ok": True, "created": len(ids)}
