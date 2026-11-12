import asyncio
import logging
import re
from datetime import datetime, timezone
from typing import Literal, Optional

from pydantic import BaseModel, Field, ValidationError, field_validator

from ledroide_sdk import Module

log = logging.getLogger("ledroide.family-tree")

module = Module(__file__)
router = module.router

EXPORT_FORMAT = "ledroide-family-tree"
DATE_RE = re.compile(r"^[~<>]?\d{4}(-(0[1-9]|1[0-2])(-(0[1-9]|[12]\d|3[01]))?)?$")
PHOTO_RE = re.compile(r"^data:image/(jpeg|png|webp);base64,[A-Za-z0-9+/=]+$")
MAX_PHOTO = 400_000
MONTHS = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"]
FR_MONTHS = {"JANV": 1, "FEVR": 2, "FÉVR": 2, "MARS": 3, "AVRI": 4, "AVR": 4, "MAI": 5, "JUIN": 6, "JUIL": 7,
             "AOUT": 8, "AOÛT": 8, "SEPT": 9, "OCTO": 10, "OCT": 10, "NOVE": 11, "NOV": 11, "DECE": 12, "DÉCE": 12, "DEC": 12}
PERSON_FIELDS = ("first_name", "last_name", "birth_name", "sex", "birth_date", "birth_place", "death_date",
                 "death_place", "deceased", "occupation", "notes")
UNION_FIELDS = ("kind", "date", "place", "end_kind", "end_date", "notes")

_ready = False
_lock = asyncio.Lock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS trees (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT    NOT NULL,
    root_id    INTEGER,
    position   INTEGER NOT NULL DEFAULT 0,
    created_at TEXT    NOT NULL
);
CREATE TABLE IF NOT EXISTS persons (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    tree_id     INTEGER NOT NULL REFERENCES trees(id) ON DELETE CASCADE,
    first_name  TEXT    NOT NULL DEFAULT '',
    last_name   TEXT    NOT NULL DEFAULT '',
    birth_name  TEXT    NOT NULL DEFAULT '',
    sex         TEXT    NOT NULL DEFAULT 'U',
    birth_date  TEXT    NOT NULL DEFAULT '',
    birth_place TEXT    NOT NULL DEFAULT '',
    death_date  TEXT    NOT NULL DEFAULT '',
    death_place TEXT    NOT NULL DEFAULT '',
    deceased    INTEGER NOT NULL DEFAULT 0,
    occupation  TEXT    NOT NULL DEFAULT '',
    notes       TEXT    NOT NULL DEFAULT '',
    photo       TEXT,
    updated_at  TEXT    NOT NULL
);
CREATE TABLE IF NOT EXISTS unions (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    tree_id   INTEGER NOT NULL REFERENCES trees(id) ON DELETE CASCADE,
    a_id      INTEGER REFERENCES persons(id) ON DELETE SET NULL,
    b_id      INTEGER REFERENCES persons(id) ON DELETE SET NULL,
    kind      TEXT    NOT NULL DEFAULT 'mariage',
    date      TEXT    NOT NULL DEFAULT '',
    place     TEXT    NOT NULL DEFAULT '',
    end_kind  TEXT    NOT NULL DEFAULT '',
    end_date  TEXT    NOT NULL DEFAULT '',
    notes     TEXT    NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS children (
    person_id INTEGER PRIMARY KEY REFERENCES persons(id) ON DELETE CASCADE,
    union_id  INTEGER NOT NULL REFERENCES unions(id) ON DELETE CASCADE,
    tree_id   INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_persons_tree ON persons(tree_id);
CREATE INDEX IF NOT EXISTS idx_unions_tree ON unions(tree_id);
CREATE INDEX IF NOT EXISTS idx_children_union ON children(union_id);
"""


async def _db():
    global _ready
    if not _ready:
        async with _lock:
            if not _ready:
                await module.db.executescript(SCHEMA)
                _ready = True
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


def _check_date(v: str) -> str:
    v = (v or "").strip()
    if v and not DATE_RE.match(v):
        raise ValueError("Date invalide (AAAA, AAAA-MM ou AAAA-MM-JJ, préfixe ~ < > possible)")
    return v


# ── Models ────────────────────────────────────────────────────────────────────

class PersonIn(BaseModel):
    first_name: str = Field("", max_length=120)
    last_name: str = Field("", max_length=120)
    birth_name: str = Field("", max_length=120)
    sex: Literal["M", "F", "U"] = "U"
    birth_date: str = ""
    birth_place: str = Field("", max_length=200)
    death_date: str = ""
    death_place: str = Field("", max_length=200)
    deceased: bool = False
    occupation: str = Field("", max_length=200)
    notes: str = Field("", max_length=20000)

    @field_validator("first_name", "last_name", "birth_name", "birth_place", "death_place", "occupation")
    @classmethod
    def _strip(cls, v: str) -> str:
        return v.strip()

    @field_validator("birth_date", "death_date")
    @classmethod
    def _date(cls, v: str) -> str:
        return _check_date(v)


class UnionIn(BaseModel):
    kind: Literal["mariage", "pacs", "union", "inconnu"] = "mariage"
    date: str = ""
    place: str = Field("", max_length=200)
    end_kind: Literal["", "divorce", "separation", "veuvage"] = ""
    end_date: str = ""
    notes: str = Field("", max_length=20000)

    @field_validator("date", "end_date")
    @classmethod
    def _date(cls, v: str) -> str:
        return _check_date(v)


# ── Loading ───────────────────────────────────────────────────────────────────

async def _tree(tree_id: int) -> Optional[dict]:
    return await (await _db()).fetch_one("SELECT * FROM trees WHERE id=?", (tree_id,))


async def _person(pid) -> Optional[dict]:
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return None
    return await (await _db()).fetch_one("SELECT * FROM persons WHERE id=?", (pid,))


async def _union(uid) -> Optional[dict]:
    try:
        uid = int(uid)
    except (TypeError, ValueError):
        return None
    return await (await _db()).fetch_one("SELECT * FROM unions WHERE id=?", (uid,))


async def _parent_union(person_id: int) -> Optional[dict]:
    return await (await _db()).fetch_one(
        "SELECT u.* FROM unions u JOIN children c ON c.union_id = u.id WHERE c.person_id=?", (person_id,))


async def _prune(tree_id: int):
    """Drops unions with no partner, and single-partner unions without children; keeps a_id filled first."""
    db = await _db()
    await db.execute("UPDATE unions SET a_id=b_id, b_id=NULL WHERE tree_id=? AND a_id IS NULL AND b_id IS NOT NULL", (tree_id,))
    await db.execute("""DELETE FROM children WHERE tree_id=? AND union_id IN
                        (SELECT id FROM unions WHERE tree_id=? AND a_id IS NULL AND b_id IS NULL)""", (tree_id, tree_id))
    await db.execute("""DELETE FROM unions WHERE tree_id=? AND (
                          (a_id IS NULL AND b_id IS NULL) OR
                          (b_id IS NULL AND NOT EXISTS (SELECT 1 FROM children c WHERE c.union_id = unions.id)))""",
                     (tree_id,))
    await db.execute("""UPDATE trees SET root_id=NULL WHERE id=? AND root_id IS NOT NULL
                        AND root_id NOT IN (SELECT id FROM persons WHERE tree_id=?)""", (tree_id, tree_id))


async def _view(tree_id: int) -> dict:
    db = await _db()
    tree = await _tree(tree_id)
    if not tree:
        return _err("Arbre introuvable", "NOT_FOUND")
    cols = ", ".join(("id", "updated_at") + PERSON_FIELDS)
    persons = await db.fetch_all(
        f"SELECT {cols}, photo IS NOT NULL AS has_photo FROM persons WHERE tree_id=? ORDER BY id", (tree_id,))
    for p in persons:
        p["deceased"] = bool(p["deceased"])
        p["has_photo"] = bool(p["has_photo"])
    unions = await db.fetch_all("SELECT * FROM unions WHERE tree_id=? ORDER BY date, id", (tree_id,))
    children = await db.fetch_all("SELECT person_id, union_id FROM children WHERE tree_id=?", (tree_id,))
    return {"tree": tree, "persons": persons, "unions": unions, "children": children}


async def _descendants(person_id: int) -> set[int]:
    db = await _db()
    out, frontier = set(), [person_id]
    while frontier:
        marks = ",".join("?" * len(frontier))
        rows = await db.fetch_all(
            f"SELECT c.person_id FROM children c JOIN unions u ON c.union_id=u.id WHERE u.a_id IN ({marks}) OR u.b_id IN ({marks})",
            (*frontier, *frontier))
        frontier = [r["person_id"] for r in rows if r["person_id"] not in out]
        out.update(frontier)
    return out


# ── Linking helpers ───────────────────────────────────────────────────────────

async def _find_couple(a: int, b: Optional[int]) -> Optional[dict]:
    db = await _db()
    if b is None:
        return await db.fetch_one("SELECT * FROM unions WHERE a_id=? AND b_id IS NULL ORDER BY id LIMIT 1", (a,))
    return await db.fetch_one("SELECT * FROM unions WHERE (a_id=? AND b_id=?) OR (a_id=? AND b_id=?) ORDER BY id LIMIT 1",
                              (a, b, b, a))


async def _new_union(tree_id: int, a: int, b: Optional[int], fields: Optional[dict] = None) -> int:
    f = {"kind": "mariage" if b else "inconnu", "date": "", "place": "", "end_kind": "", "end_date": "", "notes": ""}
    f.update(fields or {})
    row = await (await _db()).fetch_one(
        "INSERT INTO unions (tree_id, a_id, b_id, kind, date, place, end_kind, end_date, notes) VALUES (?,?,?,?,?,?,?,?,?) RETURNING id",
        (tree_id, a, b, f["kind"], f["date"], f["place"], f["end_kind"], f["end_date"], f["notes"]))
    return row["id"]


async def _set_child(tree_id: int, child_id: int, union_id: int):
    await (await _db()).execute(
        "INSERT INTO children (person_id, union_id, tree_id) VALUES (?,?,?) "
        "ON CONFLICT(person_id) DO UPDATE SET union_id=excluded.union_id", (child_id, union_id, tree_id))


async def _link_parent(child: dict, parent: dict) -> Optional[str]:
    if child["tree_id"] != parent["tree_id"]:
        return "Les deux personnes doivent appartenir au même arbre"
    if child["id"] == parent["id"] or parent["id"] in await _descendants(child["id"]):
        return "Une personne ne peut pas être son propre ancêtre"
    tree_id = child["tree_id"]
    u = await _parent_union(child["id"])
    if not u:
        existing = await _find_couple(parent["id"], None)
        uid = existing["id"] if existing else await _new_union(tree_id, parent["id"], None)
        await _set_child(tree_id, child["id"], uid)
        return None
    if parent["id"] in (u["a_id"], u["b_id"]):
        return "Ce lien de parenté existe déjà"
    if u["a_id"] and u["b_id"]:
        return "Cette personne a déjà deux parents"
    other = u["a_id"] or u["b_id"]
    couple = await _find_couple(other, parent["id"]) if other else None
    db = await _db()
    if couple:
        await _set_child(tree_id, child["id"], couple["id"])
    else:
        siblings = await db.fetch_one("SELECT COUNT(*) AS n FROM children WHERE union_id=?", (u["id"],))
        if siblings["n"] > 1:
            uid = await _new_union(tree_id, other, parent["id"])
            await _set_child(tree_id, child["id"], uid)
        else:
            await db.execute("UPDATE unions SET b_id=?, kind='mariage' WHERE id=?", (parent["id"], u["id"]))
    return None


async def _link_child(parent: dict, child: dict, union_id: Optional[int]) -> Optional[str]:
    if child["tree_id"] != parent["tree_id"]:
        return "Les deux personnes doivent appartenir au même arbre"
    if child["id"] == parent["id"] or parent["id"] in await _descendants(child["id"]):
        return "Une personne ne peut pas être son propre ancêtre"
    if await _parent_union(child["id"]):
        return "Cette personne a déjà des parents : modifiez-les depuis sa fiche"
    if union_id:
        u = await _union(union_id)
        if not u or parent["id"] not in (u["a_id"], u["b_id"]):
            return "Couple introuvable"
        await _set_child(parent["tree_id"], child["id"], u["id"])
        return None
    existing = await _find_couple(parent["id"], None)
    uid = existing["id"] if existing else await _new_union(parent["tree_id"], parent["id"], None)
    await _set_child(parent["tree_id"], child["id"], uid)
    return None


async def _link_spouse(a: dict, b: dict, fields: Optional[dict] = None) -> Optional[str]:
    if a["tree_id"] != b["tree_id"]:
        return "Les deux personnes doivent appartenir au même arbre"
    if a["id"] == b["id"]:
        return "Une personne ne peut pas être son propre conjoint"
    if await _find_couple(a["id"], b["id"]):
        return "Ce couple existe déjà"
    await _new_union(a["tree_id"], a["id"], b["id"], fields)
    return None


# ── Trees ─────────────────────────────────────────────────────────────────────

@module.on("trees.list")
async def trees_list(payload: dict):
    db = await _db()
    rows = await db.fetch_all("""SELECT t.*, (SELECT COUNT(*) FROM persons p WHERE p.tree_id=t.id) AS person_count
                                 FROM trees t ORDER BY t.position, t.id""")
    return {"trees": rows}


@module.on("tree.save")
async def tree_save(payload: dict):
    name = str(payload.get("name") or "").strip()[:100]
    if not name:
        return _err("Le nom de l'arbre est requis")
    db = await _db()
    tid = payload.get("id")
    if tid:
        if not await db.execute("UPDATE trees SET name=? WHERE id=?", (name, int(tid))):
            return _err("Arbre introuvable", "NOT_FOUND")
        return await _view(int(tid))
    pos = (await db.fetch_one("SELECT COALESCE(MAX(position)+1, 0) AS p FROM trees"))["p"]
    row = await db.fetch_one("INSERT INTO trees (name, position, created_at) VALUES (?,?,?) RETURNING id", (name, pos, _now()))
    return await _view(row["id"])


@module.on("tree.get")
async def tree_get(payload: dict):
    return await _view(int(payload.get("id") or 0))


@module.on("tree.root")
async def tree_root(payload: dict):
    tid = int(payload.get("id") or 0)
    p = await _person(payload.get("person_id"))
    if not p or p["tree_id"] != tid:
        return _err("Personne introuvable", "NOT_FOUND")
    await (await _db()).execute("UPDATE trees SET root_id=? WHERE id=?", (p["id"], tid))
    return {"ok": True}


@module.on("tree.delete")
async def tree_delete(payload: dict):
    db = await _db()
    tid = int(payload.get("id") or 0)
    await db.execute("DELETE FROM children WHERE tree_id=?", (tid,))
    await db.execute("DELETE FROM unions WHERE tree_id=?", (tid,))
    await db.execute("DELETE FROM persons WHERE tree_id=?", (tid,))
    if not await db.execute("DELETE FROM trees WHERE id=?", (tid,)):
        return _err("Arbre introuvable", "NOT_FOUND")
    return {"ok": True}


# ── Persons ───────────────────────────────────────────────────────────────────

@module.on("person.save")
async def person_save(payload: dict):
    """Creates or updates a person. On creation, `link` = {kind: parent|child|spouse, of: id, union_id?}."""
    try:
        p = PersonIn.model_validate(payload.get("person") or {})
    except ValidationError as exc:
        return _err(_validation_message(exc))
    if not (p.first_name or p.last_name):
        return _err("Indiquez au moins un prénom ou un nom")
    if p.death_date:
        p.deceased = True
    db = await _db()
    values = [getattr(p, f) for f in PERSON_FIELDS]
    values[PERSON_FIELDS.index("deceased")] = int(p.deceased)
    if payload.get("id"):
        old = await _person(payload["id"])
        if not old:
            return _err("Personne introuvable", "NOT_FOUND")
        sets = ", ".join(f"{f}=?" for f in PERSON_FIELDS)
        await db.execute(f"UPDATE persons SET {sets}, updated_at=? WHERE id=?", (*values, _now(), old["id"]))
        return await _view(old["tree_id"])

    tree_id = int(payload.get("tree_id") or 0)
    if not await _tree(tree_id):
        return _err("Arbre introuvable", "NOT_FOUND")
    link = payload.get("link") or None
    other = None
    if link:
        other = await _person(link.get("of"))
        if not other or other["tree_id"] != tree_id:
            return _err("Personne liée introuvable", "NOT_FOUND")
        if link.get("kind") == "parent":
            u = await _parent_union(other["id"])
            if u and u["a_id"] and u["b_id"]:
                return _err("Cette personne a déjà deux parents")
    cols = ", ".join(PERSON_FIELDS)
    row = await db.fetch_one(
        f"INSERT INTO persons (tree_id, {cols}, updated_at) VALUES (?, {', '.join('?' * len(PERSON_FIELDS))}, ?) RETURNING *",
        (tree_id, *values, _now()))
    msg = None
    if link and other:
        kind = link.get("kind")
        if kind == "parent":
            msg = await _link_parent(other, row)
        elif kind == "child":
            msg = await _link_child(other, row, link.get("union_id"))
        elif kind == "spouse":
            msg = await _link_spouse(other, row)
        else:
            msg = "Type de lien inconnu"
    if msg:
        await db.execute("DELETE FROM persons WHERE id=?", (row["id"],))
        return _err(msg)
    tree = await _tree(tree_id)
    if not tree["root_id"]:
        await db.execute("UPDATE trees SET root_id=? WHERE id=?", (row["id"], tree_id))
    view = await _view(tree_id)
    view["created_id"] = row["id"]
    return view


@module.on("person.delete")
async def person_delete(payload: dict):
    old = await _person(payload.get("id"))
    if not old:
        return _err("Personne introuvable", "NOT_FOUND")
    db = await _db()
    await db.execute("DELETE FROM children WHERE person_id=?", (old["id"],))
    await db.execute("UPDATE unions SET a_id=NULL WHERE a_id=?", (old["id"],))
    await db.execute("UPDATE unions SET b_id=NULL WHERE b_id=?", (old["id"],))
    await db.execute("DELETE FROM persons WHERE id=?", (old["id"],))
    await _prune(old["tree_id"])
    return await _view(old["tree_id"])


@module.on("person.photo")
async def person_photo(payload: dict):
    old = await _person(payload.get("id"))
    if not old:
        return _err("Personne introuvable", "NOT_FOUND")
    photo = payload.get("photo") or None
    if photo is not None and (len(photo) > MAX_PHOTO or not PHOTO_RE.match(photo)):
        return _err("Image invalide ou trop volumineuse")
    await (await _db()).execute("UPDATE persons SET photo=?, updated_at=? WHERE id=?", (photo, _now(), old["id"]))
    return await _view(old["tree_id"])


@module.on("photos.get")
async def photos_get(payload: dict):
    ids = [int(i) for i in (payload.get("ids") or [])][:500]
    if not ids:
        return {"photos": {}}
    marks = ",".join("?" * len(ids))
    rows = await (await _db()).fetch_all(
        f"SELECT id, photo FROM persons WHERE tree_id=? AND id IN ({marks}) AND photo IS NOT NULL",
        (int(payload.get("tree_id") or 0), *ids))
    return {"photos": {str(r["id"]): r["photo"] for r in rows}}


# ── Links ─────────────────────────────────────────────────────────────────────

@module.on("link.parent")
async def link_parent(payload: dict):
    child, parent = await _person(payload.get("child_id")), await _person(payload.get("parent_id"))
    if not child or not parent:
        return _err("Personne introuvable", "NOT_FOUND")
    if msg := await _link_parent(child, parent):
        return _err(msg)
    await _prune(child["tree_id"])
    return await _view(child["tree_id"])


@module.on("link.child")
async def link_child(payload: dict):
    parent, child = await _person(payload.get("parent_id")), await _person(payload.get("child_id"))
    if not child or not parent:
        return _err("Personne introuvable", "NOT_FOUND")
    if msg := await _link_child(parent, child, payload.get("union_id")):
        return _err(msg)
    return await _view(parent["tree_id"])


@module.on("link.spouse")
async def link_spouse(payload: dict):
    a, b = await _person(payload.get("a_id")), await _person(payload.get("b_id"))
    if not a or not b:
        return _err("Personne introuvable", "NOT_FOUND")
    if msg := await _link_spouse(a, b):
        return _err(msg)
    return await _view(a["tree_id"])


@module.on("unlink.parents")
async def unlink_parents(payload: dict):
    p = await _person(payload.get("person_id"))
    if not p:
        return _err("Personne introuvable", "NOT_FOUND")
    await (await _db()).execute("DELETE FROM children WHERE person_id=?", (p["id"],))
    await _prune(p["tree_id"])
    return await _view(p["tree_id"])


@module.on("union.save")
async def union_save(payload: dict):
    u = await _union(payload.get("id"))
    if not u:
        return _err("Couple introuvable", "NOT_FOUND")
    try:
        f = UnionIn.model_validate(payload.get("union") or {})
    except ValidationError as exc:
        return _err(_validation_message(exc))
    sets = ", ".join(f"{k}=?" for k in UNION_FIELDS)
    await (await _db()).execute(f"UPDATE unions SET {sets} WHERE id=?", (*[getattr(f, k) for k in UNION_FIELDS], u["id"]))
    return await _view(u["tree_id"])


@module.on("union.delete")
async def union_delete(payload: dict):
    u = await _union(payload.get("id"))
    if not u:
        return _err("Couple introuvable", "NOT_FOUND")
    db = await _db()
    await db.execute("DELETE FROM children WHERE union_id=?", (u["id"],))
    await db.execute("DELETE FROM unions WHERE id=?", (u["id"],))
    return await _view(u["tree_id"])


# ── JSON export / import ──────────────────────────────────────────────────────

@module.on("tree.export")
async def tree_export(payload: dict):
    tid = int(payload.get("id") or 0)
    tree = await _tree(tid)
    if not tree:
        return _err("Arbre introuvable", "NOT_FOUND")
    db = await _db()
    persons = await db.fetch_all("SELECT * FROM persons WHERE tree_id=? ORDER BY id", (tid,))
    unions = await db.fetch_all("SELECT * FROM unions WHERE tree_id=? ORDER BY id", (tid,))
    children = await db.fetch_all("SELECT person_id, union_id FROM children WHERE tree_id=?", (tid,))
    pref = {p["id"]: i for i, p in enumerate(persons)}
    uref = {u["id"]: i for i, u in enumerate(unions)}
    return {
        "format": EXPORT_FORMAT, "version": 1,
        "tree": {"name": tree["name"], "root": pref.get(tree["root_id"])},
        "persons": [{"ref": pref[p["id"]], **{f: p[f] for f in PERSON_FIELDS}, "deceased": bool(p["deceased"]),
                     "photo": p["photo"]} for p in persons],
        "unions": [{"ref": uref[u["id"]], "a": pref.get(u["a_id"]), "b": pref.get(u["b_id"]),
                    **{f: u[f] for f in UNION_FIELDS}} for u in unions],
        "children": [{"union": uref[c["union_id"]], "person": pref[c["person_id"]]} for c in children
                     if c["union_id"] in uref and c["person_id"] in pref],
    }


async def _import(data: dict) -> dict:
    name = str((data.get("tree") or {}).get("name") or "Arbre importé").strip()[:100] or "Arbre importé"
    persons, unions = [], []
    try:
        for raw in data.get("persons") or []:
            p = PersonIn.model_validate({k: raw.get(k) for k in PERSON_FIELDS if raw.get(k) is not None})
            photo = raw.get("photo")
            if photo and (len(photo) > MAX_PHOTO or not PHOTO_RE.match(photo)):
                photo = None
            persons.append((raw.get("ref"), p, photo))
        for raw in data.get("unions") or []:
            f = UnionIn.model_validate({k: raw.get(k) for k in UNION_FIELDS if raw.get(k) is not None})
            unions.append((raw.get("ref"), raw.get("a"), raw.get("b"), f))
    except ValidationError as exc:
        return _err(_validation_message(exc))
    except (TypeError, AttributeError):
        return _err("Fichier invalide")
    db = await _db()
    pos = (await db.fetch_one("SELECT COALESCE(MAX(position)+1, 0) AS p FROM trees"))["p"]
    tid = (await db.fetch_one("INSERT INTO trees (name, position, created_at) VALUES (?,?,?) RETURNING id",
                              (name, pos, _now())))["id"]
    pids: dict = {}
    cols = ", ".join(PERSON_FIELDS)
    for ref, p, photo in persons:
        values = [getattr(p, f) for f in PERSON_FIELDS]
        values[PERSON_FIELDS.index("deceased")] = int(p.deceased or bool(p.death_date))
        row = await db.fetch_one(
            f"INSERT INTO persons (tree_id, {cols}, photo, updated_at) VALUES (?, {', '.join('?' * len(PERSON_FIELDS))}, ?, ?) RETURNING id",
            (tid, *values, photo, _now()))
        pids[ref] = row["id"]
    uids: dict = {}
    for ref, a, b, f in unions:
        pa, pb = pids.get(a), pids.get(b)
        if pa is None and pb is None:
            continue
        if pa is None:
            pa, pb = pb, None
        uids[ref] = await _new_union(tid, pa, pb if pb != pa else None, f.model_dump())
    for c in data.get("children") or []:
        uid, pid = uids.get(c.get("union")), pids.get(c.get("person"))
        if uid is None or pid is None:
            continue
        u = await _union(uid)
        desc = await _descendants(pid)
        if pid in (u["a_id"], u["b_id"]) or any(x in desc for x in (u["a_id"], u["b_id"]) if x):
            continue
        await db.execute("INSERT OR IGNORE INTO children (person_id, union_id, tree_id) VALUES (?,?,?)", (pid, uid, tid))
    root = pids.get((data.get("tree") or {}).get("root"))
    if root is None and pids:
        root = next(iter(pids.values()))
    await db.execute("UPDATE trees SET root_id=? WHERE id=?", (root, tid))
    await _prune(tid)
    return await _view(tid)


@module.on("tree.import")
async def tree_import(payload: dict):
    data = payload.get("data") or {}
    if data.get("format") != EXPORT_FORMAT:
        return _err("Fichier non reconnu (export Arbre généalogique attendu)")
    return await _import(data)


# ── GEDCOM ────────────────────────────────────────────────────────────────────

def _ged_date(v: str) -> str:
    if not v:
        return ""
    prefix = {"~": "ABT ", "<": "BEF ", ">": "AFT "}.get(v[0], "")
    v = v.lstrip("~<>")
    parts = v.split("-")
    out = parts[0]
    if len(parts) >= 2:
        out = f"{MONTHS[int(parts[1]) - 1]} {out}"
    if len(parts) == 3:
        out = f"{int(parts[2])} {out}"
    return prefix + out


def _parse_ged_date(v: str) -> str:
    tokens = v.upper().replace(".", " ").split()
    if not tokens:
        return ""
    prefix = ""
    if tokens[0] in ("ABT", "ABOUT", "EST", "CAL", "CA", "ENV", "VERS"):
        prefix, tokens = "~", tokens[1:]
    elif tokens[0] in ("BEF", "BEFORE", "AVANT"):
        prefix, tokens = "<", tokens[1:]
    elif tokens[0] in ("AFT", "AFTER", "APRES", "APRÈS"):
        prefix, tokens = ">", tokens[1:]
    elif tokens[0] in ("BET", "FROM"):
        prefix, tokens = "~", tokens[1:]
        for stop in ("AND", "TO"):
            if stop in tokens:
                tokens = tokens[:tokens.index(stop)]
    if tokens and tokens[0].startswith("@#"):
        tokens = tokens[1:]
    year = month = day = None
    for t in tokens:
        if t.isdigit():
            if len(t) == 4:
                year = int(t)
            elif day is None and len(t) <= 2:
                day = int(t)
        elif t[:3] in MONTHS:
            month = MONTHS.index(t[:3]) + 1
        elif t in FR_MONTHS or t[:4] in FR_MONTHS:
            month = FR_MONTHS.get(t, FR_MONTHS.get(t[:4]))
    if not year:
        return ""
    out = f"{year:04d}"
    if month:
        out += f"-{month:02d}"
        if day and 1 <= day <= 31:
            out += f"-{day:02d}"
    return prefix + out if DATE_RE.match(prefix + out) else ""


def _ged_text(level: int, tag: str, text: str) -> list[str]:
    lines = text.replace("\r\n", "\n").split("\n")
    out = [f"{level} {tag} {lines[0]}".rstrip()]
    out += [f"{level + 1} CONT {line}".rstrip() for line in lines[1:]]
    return out


@module.on("tree.gedcom_export")
async def gedcom_export(payload: dict):
    data = await tree_export(payload)
    if "error" in data:
        return data
    lines = ["0 HEAD", "1 SOUR LEDROIDE", "2 NAME LedroideDesktop - Arbre genealogique", "1 GEDC", "2 VERS 5.5.1",
             "2 FORM LINEAGE-LINKED", "1 CHAR UTF-8", f"1 DATE {_ged_date(datetime.now().date().isoformat())}"]
    fams: dict[int, list[int]] = {}
    famc: dict[int, int] = {}
    for u in data["unions"]:
        for x in (u["a"], u["b"]):
            if x is not None:
                fams.setdefault(x, []).append(u["ref"])
    for c in data["children"]:
        famc[c["person"]] = c["union"]
    sex = {p["ref"]: p["sex"] for p in data["persons"]}
    for p in data["persons"]:
        r = p["ref"]
        lines.append(f"0 @I{r + 1}@ INDI")
        surname = p["birth_name"] or p["last_name"]
        lines.append(f"1 NAME {p['first_name']} /{surname}/".replace("  ", " ").strip())
        if p["first_name"]:
            lines.append(f"2 GIVN {p['first_name']}")
        if surname:
            lines.append(f"2 SURN {surname}")
        if p["birth_name"] and p["last_name"] and p["last_name"] != p["birth_name"]:
            lines.append(f"1 NAME {p['first_name']} /{p['last_name']}/".strip())
            lines.append("2 TYPE married")
        lines.append(f"1 SEX {p['sex']}")
        if p["birth_date"] or p["birth_place"]:
            lines.append("1 BIRT")
            if p["birth_date"]:
                lines.append(f"2 DATE {_ged_date(p['birth_date'])}")
            if p["birth_place"]:
                lines.append(f"2 PLAC {p['birth_place']}")
        if p["deceased"] or p["death_date"] or p["death_place"]:
            lines.append("1 DEAT" + ("" if p["death_date"] or p["death_place"] else " Y"))
            if p["death_date"]:
                lines.append(f"2 DATE {_ged_date(p['death_date'])}")
            if p["death_place"]:
                lines.append(f"2 PLAC {p['death_place']}")
        if p["occupation"]:
            lines.append(f"1 OCCU {p['occupation']}")
        if p["notes"]:
            lines += _ged_text(1, "NOTE", p["notes"])
        for f in fams.get(r, []):
            lines.append(f"1 FAMS @F{f + 1}@")
        if r in famc:
            lines.append(f"1 FAMC @F{famc[r] + 1}@")
    for u in data["unions"]:
        lines.append(f"0 @F{u['ref'] + 1}@ FAM")
        partners = [x for x in (u["a"], u["b"]) if x is not None]
        if len(partners) == 2 and sex[partners[0]] == "F" and sex[partners[1]] != "F":
            partners.reverse()
        for i, x in enumerate(partners):
            lines.append(f"1 {'HUSB' if i == 0 else 'WIFE'} @I{x + 1}@")
        for c in data["children"]:
            if c["union"] == u["ref"]:
                lines.append(f"1 CHIL @I{c['person'] + 1}@")
        if u["kind"] == "mariage" or u["date"] or u["place"]:
            lines.append("1 MARR" if u["kind"] == "mariage" else "1 EVEN")
            if u["kind"] != "mariage":
                lines.append(f"2 TYPE {u['kind']}")
            if u["date"]:
                lines.append(f"2 DATE {_ged_date(u['date'])}")
            if u["place"]:
                lines.append(f"2 PLAC {u['place']}")
        if u["end_kind"] == "divorce":
            lines.append("1 DIV" + ("" if u["end_date"] else " Y"))
            if u["end_date"]:
                lines.append(f"2 DATE {_ged_date(u['end_date'])}")
        if u["notes"]:
            lines += _ged_text(1, "NOTE", u["notes"])
    lines.append("0 TRLR")
    return {"name": data["tree"]["name"], "gedcom": "\r\n".join(lines) + "\r\n"}


def _parse_gedcom(text: str) -> list[dict]:
    """Returns level-0 records as nested {tag, value, xref, children}."""
    records: list[dict] = []
    stack: list[dict] = []
    for raw in text.lstrip("﻿").splitlines():
        line = raw.strip()
        if not line:
            continue
        m = re.match(r"^(\d+)\s+(@[^@]+@\s+)?(\S+)(?:\s(.*))?$", line)
        if not m:
            continue
        level = int(m.group(1))
        node = {"tag": m.group(3).upper(), "value": (m.group(4) or "").strip() if m.group(3).upper() not in ("CONT", "CONC") else (m.group(4) or ""),
                "xref": (m.group(2) or "").strip(), "children": []}
        if node["tag"] in ("CONT", "CONC") and stack:
            parent = stack[min(level, len(stack)) - 1]
            parent["value"] += ("\n" if node["tag"] == "CONT" else "") + node["value"]
            continue
        if level == 0:
            records.append(node)
            stack = [node]
        else:
            stack = stack[:level]
            if not stack:
                continue
            stack[-1]["children"].append(node)
            stack.append(node)
    return records


def _sub(node: dict, tag: str) -> Optional[dict]:
    return next((c for c in node["children"] if c["tag"] == tag), None)


def _subs(node: dict, tag: str) -> list[dict]:
    return [c for c in node["children"] if c["tag"] == tag]


def _event(node: dict, tag: str) -> tuple[str, str, bool]:
    ev = _sub(node, tag)
    if not ev:
        return "", "", False
    d = _sub(ev, "DATE")
    p = _sub(ev, "PLAC")
    return _parse_ged_date(d["value"]) if d else "", (p["value"] if p else "")[:200], True


@module.on("tree.gedcom_import")
async def gedcom_import(payload: dict):
    text = str(payload.get("gedcom") or "")
    if "0 HEAD" not in text[:2000].upper() and "INDI" not in text:
        return _err("Fichier GEDCOM invalide")
    records = _parse_gedcom(text)
    indis = [r for r in records if r["tag"] == "INDI" and r["xref"]]
    fams = [r for r in records if r["tag"] == "FAM" and r["xref"]]
    if not indis:
        return _err("Aucune personne trouvée dans le fichier GEDCOM")
    notes_by_ref = {r["xref"]: r["value"] for r in records if r["tag"] == "NOTE" and r["xref"]}
    persons, unions, children = [], [], []

    def note_text(node: dict) -> str:
        parts = []
        for n in _subs(node, "NOTE"):
            parts.append(notes_by_ref.get(n["value"], n["value"]))
        return "\n\n".join(p for p in parts if p)[:20000]

    for ind in indis:
        names = _subs(ind, "NAME")
        main = names[0] if names else {"value": "", "children": []}
        given = _sub(main, "GIVN")
        surn = _sub(main, "SURN")
        m = re.match(r"^([^/]*)/([^/]*)/?(.*)$", main["value"])
        first = (given["value"] if given else (m.group(1) if m else main["value"])).strip()
        last = (surn["value"] if surn else (m.group(2) if m else "")).strip()
        married = next((n for n in names[1:] if (_sub(n, "TYPE") or {}).get("value", "").lower() == "married"), None)
        mm = re.match(r"^[^/]*/([^/]*)/", married["value"]) if married else None
        sex = (_sub(ind, "SEX") or {}).get("value", "U")[:1].upper()
        bd, bp, _ = _event(ind, "BIRT")
        if not bd and not bp:
            bd, bp, _ = _event(ind, "BAPM") if _sub(ind, "BAPM") else _event(ind, "CHR")
        dd, dp, dead = _event(ind, "DEAT")
        if not dead:
            dd, dp, dead = _event(ind, "BURI")
        occu = _sub(ind, "OCCU")
        persons.append({
            "ref": ind["xref"], "first_name": first[:120], "last_name": ((mm.group(1) if mm else last) or "")[:120].strip(),
            "birth_name": last[:120] if mm else "", "sex": sex if sex in ("M", "F") else "U",
            "birth_date": bd, "birth_place": bp, "death_date": dd, "death_place": dp, "deceased": dead,
            "occupation": (occu["value"] if occu else "")[:200], "notes": note_text(ind),
        })
    for fam in fams:
        h, w = _sub(fam, "HUSB"), _sub(fam, "WIFE")
        md, mp, married = _event(fam, "MARR")
        _, _, divorced = _event(fam, "DIV")
        dvd, _, _ = _event(fam, "DIV")
        unions.append({"ref": fam["xref"], "a": h["value"] if h else None, "b": w["value"] if w else None,
                       "kind": "mariage" if married else "union", "date": md, "place": mp,
                       "end_kind": "divorce" if divorced else "", "end_date": dvd, "notes": note_text(fam)})
        for c in _subs(fam, "CHIL"):
            children.append({"union": fam["xref"], "person": c["value"]})
    head = next((r for r in records if r["tag"] == "HEAD"), None)
    name = str(payload.get("name") or "").strip() or ((_sub(head, "FILE") or {}).get("value") if head else "") or "Arbre GEDCOM"
    return await _import({"format": EXPORT_FORMAT, "tree": {"name": re.sub(r"\.ged$", "", name, flags=re.I)},
                          "persons": persons, "unions": unions, "children": children})


# ── Example ───────────────────────────────────────────────────────────────────

@module.on("tree.seed")
async def tree_seed(payload: dict):
    P = lambda ref, first, last, sex, bd="", bp="", dd="", **kw: {
        "ref": ref, "first_name": first, "last_name": last, "sex": sex, "birth_date": bd, "birth_place": bp,
        "death_date": dd, "deceased": bool(dd), **kw}
    persons = [
        P(0, "Louis", "Martin", "M", "1920-04-02", "Lyon", "1995-11-20", occupation="Menuisier"),
        P(1, "Jeanne", "Martin", "F", "1923-08-15", "Villeurbanne", "2010-02-03", birth_name="Durand"),
        P(2, "Henri", "Bernard", "M", "~1925", "Marseille", "1999", occupation="Cheminot"),
        P(3, "Marie", "Bernard", "F", "1928-01-30", "Aix-en-Provence", "2015-06-12", birth_name="Petit"),
        P(4, "Pierre", "Martin", "M", "1948-06-10", "Lyon", occupation="Instituteur"),
        P(5, "Anne", "Martin", "F", "1951-03-22", "Marseille", birth_name="Bernard", occupation="Infirmière"),
        P(6, "Paul", "Martin", "M", "1950-12-01", "Lyon"),
        P(7, "Claire", "Bernard", "F", "1955-09-09", "Marseille"),
        P(8, "Sophie", "Martin", "F", "1978-02-14", "Grenoble", occupation="Ingénieure"),
        P(9, "Thomas", "Martin", "M", "1981-07-07", "Grenoble"),
        P(10, "Julien", "Roux", "M", "1976-10-30", "Paris"),
        P(11, "Léa", "Roux", "F", "2008-05-18", "Paris"),
        P(12, "Hugo", "Roux", "M", "2011-09-03", "Paris"),
        P(13, "Camille", "Martin", "F", "1983-04-04", "Annecy", birth_name="Lefèvre"),
        P(14, "Nina", "Martin", "F", "2015-01-27", "Annecy"),
    ]
    U = lambda ref, a, b, date="", place="", **kw: {"ref": ref, "a": a, "b": b, "kind": "mariage", "date": date, "place": place, **kw}
    unions = [U(0, 0, 1, "1946-09-14", "Lyon"), U(1, 2, 3, "1950", "Marseille"), U(2, 4, 5, "1976-07-03", "Marseille"),
              U(3, 10, 8, "2005-06-25", "Grenoble"), U(4, 9, 13, "2012-09-15", "Annecy", kind="pacs")]
    children = [(0, 4), (0, 6), (1, 5), (1, 7), (2, 8), (2, 9), (3, 11), (3, 12), (4, 14)]
    return await _import({"format": EXPORT_FORMAT, "tree": {"name": "Exemple — Famille Martin", "root": 8},
                          "persons": persons, "unions": unions,
                          "children": [{"union": u, "person": p} for u, p in children]})
