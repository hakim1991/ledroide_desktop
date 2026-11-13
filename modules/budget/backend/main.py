import asyncio
import calendar
import csv
import io
import logging
import re
import unicodedata
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Literal, Optional

from pydantic import BaseModel, Field, ValidationError, field_validator

from ledroide_sdk import Module

log = logging.getLogger("ledroide.budget")

module = Module(__file__)
router = module.router

COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
MONTH_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
MAX_CENTS = 10**13
MAX_IMPORT_CHARS = 8_000_000
MAX_IMPORT_ROWS = 20_000
MAX_RECURRING_RUN = 400
ACCOUNT_TYPES = ("courant", "epargne", "especes", "carte", "investissement", "autre")
FREQS = ("daily", "weekly", "monthly", "yearly")
DATE_FORMATS = ("JJ/MM/AAAA", "AAAA-MM-JJ", "MM/JJ/AAAA", "AAAAMMJJ")

_ready = False
_lock = asyncio.Lock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS accounts (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT    NOT NULL,
    type          TEXT    NOT NULL DEFAULT 'courant',
    initial_cents INTEGER NOT NULL DEFAULT 0,
    color         TEXT    NOT NULL DEFAULT '#6366f1',
    archived      INTEGER NOT NULL DEFAULT 0,
    position      INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT    NOT NULL
);
CREATE TABLE IF NOT EXISTS categories (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    parent_id INTEGER REFERENCES categories(id) ON DELETE CASCADE,
    name      TEXT    NOT NULL,
    color     TEXT    NOT NULL DEFAULT '#71717a',
    kind      TEXT    NOT NULL DEFAULT 'expense',
    position  INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS transactions (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id   INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    date         TEXT    NOT NULL,
    label        TEXT    NOT NULL,
    amount_cents INTEGER NOT NULL,
    category_id  INTEGER REFERENCES categories(id) ON DELETE SET NULL,
    notes        TEXT    NOT NULL DEFAULT '',
    peer_id      INTEGER,
    recurring_id INTEGER,
    import_ref   TEXT    NOT NULL DEFAULT '',
    created_at   TEXT    NOT NULL
);
CREATE TABLE IF NOT EXISTS rules (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    keyword     TEXT    NOT NULL,
    category_id INTEGER NOT NULL REFERENCES categories(id) ON DELETE CASCADE,
    position    INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS recurring (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id    INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    to_account_id INTEGER REFERENCES accounts(id) ON DELETE CASCADE,
    label         TEXT    NOT NULL,
    amount_cents  INTEGER NOT NULL,
    category_id   INTEGER REFERENCES categories(id) ON DELETE SET NULL,
    notes         TEXT    NOT NULL DEFAULT '',
    freq          TEXT    NOT NULL DEFAULT 'monthly',
    interval      INTEGER NOT NULL DEFAULT 1,
    anchor_day    INTEGER NOT NULL DEFAULT 1,
    next_date     TEXT    NOT NULL,
    end_date      TEXT,
    active        INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS budgets (
    category_id  INTEGER NOT NULL REFERENCES categories(id) ON DELETE CASCADE,
    month        TEXT    NOT NULL DEFAULT '',
    amount_cents INTEGER NOT NULL,
    PRIMARY KEY (category_id, month)
);
CREATE INDEX IF NOT EXISTS idx_tx_account_date ON transactions(account_id, date);
CREATE INDEX IF NOT EXISTS idx_tx_date ON transactions(date);
CREATE INDEX IF NOT EXISTS idx_tx_category ON transactions(category_id);
"""

DEFAULT_CATEGORIES = [
    ("Alimentation", "#22c55e", "expense", ["Courses", "Restaurants", "Boulangerie"]),
    ("Logement", "#6366f1", "expense", ["Loyer / crédit", "Énergie", "Eau", "Assurance habitation", "Travaux"]),
    ("Transports", "#38bdf8", "expense", ["Carburant", "Transports en commun", "Entretien véhicule", "Péages / parking"]),
    ("Abonnements", "#a855f7", "expense", ["Téléphone / internet", "Streaming", "Presse"]),
    ("Santé", "#ef4444", "expense", ["Médecin", "Pharmacie", "Mutuelle"]),
    ("Loisirs", "#f97316", "expense", ["Sorties", "Sport", "Voyages", "Culture"]),
    ("Shopping", "#ec4899", "expense", ["Vêtements", "High-tech", "Maison"]),
    ("Enfants", "#eab308", "expense", ["Garde / école", "Activités"]),
    ("Impôts & taxes", "#78716c", "expense", []),
    ("Banque", "#64748b", "expense", ["Frais bancaires"]),
    ("Divers", "#a1a1aa", "expense", ["Cadeaux", "Dons", "Retraits d'espèces"]),
    ("Revenus", "#10b981", "income", ["Salaire", "Remboursements", "Allocations", "Intérêts", "Autres revenus"]),
]

DEFAULT_RULES = [
    ("carrefour", "Courses"), ("leclerc", "Courses"), ("auchan", "Courses"), ("lidl", "Courses"),
    ("intermarche", "Courses"), ("monoprix", "Courses"), ("franprix", "Courses"),
    ("sncf", "Transports en commun"), ("ratp", "Transports en commun"), ("navigo", "Transports en commun"),
    ("total", "Carburant"), ("esso", "Carburant"), ("netflix", "Streaming"), ("spotify", "Streaming"),
    ("deezer", "Streaming"), ("free mobile", "Téléphone / internet"), ("orange", "Téléphone / internet"),
    ("sfr", "Téléphone / internet"), ("bouygues", "Téléphone / internet"), ("edf", "Énergie"), ("engie", "Énergie"),
    ("loyer", "Loyer / crédit"), ("boulangerie", "Boulangerie"), ("restaurant", "Restaurants"), ("cinema", "Sorties"), ("pharmacie", "Pharmacie"), ("amazon", "Shopping"), ("salaire", "Salaire"), ("retrait", "Retraits d'espèces"),
    ("dab", "Retraits d'espèces"), ("cotisation", "Frais bancaires"), ("impots", "Impôts & taxes"), ("dgfip", "Impôts & taxes"),
]


async def _db():
    global _ready
    if not _ready:
        async with _lock:
            if not _ready:
                await module.db.executescript(SCHEMA)
                if not await module.db.fetch_one("SELECT value FROM meta WHERE key='seeded'"):
                    await _seed_defaults()
                    await module.db.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('seeded', '1')")
                _ready = True
    await module.db.execute("PRAGMA foreign_keys = ON")
    return module.db


async def _seed_defaults() -> None:
    db = module.db
    by_name: dict[str, int] = {}
    for pos, (name, color, kind, subs) in enumerate(DEFAULT_CATEGORIES):
        pid = (await db.fetch_one(
            "INSERT INTO categories (parent_id, name, color, kind, position) VALUES (NULL,?,?,?,?) RETURNING id",
            (name, color, kind, pos)))["id"]
        by_name[name] = pid
        for i, sub in enumerate(subs):
            by_name[sub] = (await db.fetch_one(
                "INSERT INTO categories (parent_id, name, color, kind, position) VALUES (?,?,?,?,?) RETURNING id",
                (pid, sub, color, kind, i)))["id"]
    for pos, (kw, cat) in enumerate(DEFAULT_RULES):
        if cat in by_name:
            await db.execute("INSERT INTO rules (keyword, category_id, position) VALUES (?,?,?)", (kw, by_name[cat], pos))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _err(message: str, code: str = "BAD_REQUEST") -> dict:
    return {"error": message, "code": code}


def _validation_message(exc: ValidationError) -> str:
    first = exc.errors()[0]
    loc = ".".join(str(p) for p in first.get("loc", ()))
    msg = str(first.get("msg", "Données invalides")).removeprefix("Value error, ")
    return f"{loc} : {msg}" if loc else msg


def _int(v, default: int = 0) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", s)).strip()


def _month_bounds(month: str) -> tuple[str, str]:
    y, m = int(month[:4]), int(month[5:7])
    return f"{month}-01", f"{month}-{calendar.monthrange(y, m)[1]:02d}"


def _shift_month(month: str, delta: int) -> str:
    y, m = int(month[:4]), int(month[5:7]) - 1 + delta
    return f"{y + m // 12:04d}-{m % 12 + 1:02d}"


def _iso(v: str) -> str:
    try:
        return date.fromisoformat(str(v)[:10]).isoformat()
    except (TypeError, ValueError):
        raise ValueError("Date invalide (AAAA-MM-JJ)")


def _strip_req(v: str) -> str:
    v = (v or "").strip()
    if not v:
        raise ValueError("Champ requis")
    return v


def _color(v: str) -> str:
    if not COLOR_RE.match(v or ""):
        raise ValueError("Couleur invalide (#rrggbb)")
    return v


# ── Models ────────────────────────────────────────────────────────────────────

class AccountIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    type: Literal[ACCOUNT_TYPES] = "courant"
    initial_cents: int = Field(0, gt=-MAX_CENTS, lt=MAX_CENTS)
    color: str = "#6366f1"
    archived: bool = False

    @field_validator("name")
    @classmethod
    def vn(cls, v):
        return _strip_req(v)
    @field_validator("color")
    @classmethod
    def vc(cls, v):
        return _color(v)


class CategoryIn(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    color: str = "#71717a"
    kind: Literal["expense", "income"] = "expense"
    parent_id: Optional[int] = None

    @field_validator("name")
    @classmethod
    def vn(cls, v):
        return _strip_req(v)
    @field_validator("color")
    @classmethod
    def vc(cls, v):
        return _color(v)


class TxIn(BaseModel):
    account_id: int
    date: str
    label: str = Field(min_length=1, max_length=200)
    amount_cents: int = Field(gt=-MAX_CENTS, lt=MAX_CENTS)
    category_id: Optional[int] = None
    notes: str = Field("", max_length=5000)

    @field_validator("date")
    @classmethod
    def vd(cls, v):
        return _iso(v)
    @field_validator("label")
    @classmethod
    def vn(cls, v):
        return _strip_req(v)


class TransferIn(BaseModel):
    from_account_id: int
    to_account_id: int
    date: str
    amount_cents: int = Field(gt=0, lt=MAX_CENTS)
    label: str = Field("Virement", max_length=200)
    notes: str = Field("", max_length=5000)

    @field_validator("date")
    @classmethod
    def vd(cls, v):
        return _iso(v)


class RuleIn(BaseModel):
    keyword: str = Field(min_length=1, max_length=100)
    category_id: int

    @field_validator("keyword")
    @classmethod
    def vn(cls, v):
        return _strip_req(v)


class RecurringIn(BaseModel):
    account_id: int
    to_account_id: Optional[int] = None
    label: str = Field(min_length=1, max_length=200)
    amount_cents: int = Field(gt=-MAX_CENTS, lt=MAX_CENTS)
    category_id: Optional[int] = None
    notes: str = Field("", max_length=5000)
    freq: Literal[FREQS] = "monthly"
    interval: int = Field(1, ge=1, le=60)
    next_date: str
    end_date: Optional[str] = None
    active: bool = True

    @field_validator("next_date")
    @classmethod
    def vd(cls, v):
        return _iso(v)
    @field_validator("label")
    @classmethod
    def vn(cls, v):
        return _strip_req(v)

    @field_validator("end_date")
    @classmethod
    def _end(cls, v):
        return _iso(v) if v else None


# ── Lookups ───────────────────────────────────────────────────────────────────

async def _accounts() -> list[dict]:
    db = await _db()
    rows = await db.fetch_all("""
        SELECT a.*, a.initial_cents + COALESCE(SUM(t.amount_cents), 0) AS balance_cents,
               COUNT(t.id) AS tx_count
        FROM accounts a LEFT JOIN transactions t ON t.account_id = a.id
        GROUP BY a.id ORDER BY a.archived, a.position, a.id""")
    today = date.today().isoformat()
    future = {r["account_id"]: r["s"] for r in await db.fetch_all(
        "SELECT account_id, SUM(amount_cents) AS s FROM transactions WHERE date > ? GROUP BY account_id", (today,))}
    for r in rows:
        r["archived"] = bool(r["archived"])
        r["today_cents"] = r["balance_cents"] - (future.get(r["id"]) or 0)
    return rows


async def _categories() -> list[dict]:
    db = await _db()
    return await db.fetch_all("""
        SELECT c.*, (SELECT COUNT(*) FROM transactions t WHERE t.category_id = c.id) AS tx_count
        FROM categories c ORDER BY COALESCE(c.parent_id, c.id), c.parent_id IS NOT NULL, c.position, c.id""")


async def _account_exists(aid: int) -> bool:
    return bool(await (await _db()).fetch_one("SELECT 1 AS x FROM accounts WHERE id=?", (aid,)))


async def _category_ok(cid: Optional[int]) -> bool:
    return cid is None or bool(await (await _db()).fetch_one("SELECT 1 AS x FROM categories WHERE id=?", (cid,)))


async def _rules() -> list[tuple[str, int]]:
    rows = await (await _db()).fetch_all("SELECT keyword, category_id FROM rules ORDER BY position, id")
    return [(_norm(r["keyword"]), r["category_id"]) for r in rows if _norm(r["keyword"])]


def _match_rule(label: str, rules: list[tuple[str, int]]) -> Optional[int]:
    text = f" {_norm(label)} "
    for kw, cid in rules:
        if f" {kw} " in text or (len(kw) >= 4 and kw in text):
            return cid
    return None


async def _category_ids_with_children(cid: int) -> list[int]:
    rows = await (await _db()).fetch_all("SELECT id FROM categories WHERE id=? OR parent_id=?", (cid, cid))
    return [r["id"] for r in rows]


# ── Bootstrap / accounts ──────────────────────────────────────────────────────

@module.on("bootstrap")
async def bootstrap(payload: dict):
    generated = await _run_recurring()
    return {"accounts": await _accounts(), "categories": await _categories(),
            "generated": generated, "today": date.today().isoformat()}


@module.on("accounts.list")
async def accounts_list(payload: dict):
    return {"accounts": await _accounts()}


@module.on("account.save")
async def account_save(payload: dict):
    try:
        a = AccountIn.model_validate(payload.get("account") or {})
    except ValidationError as exc:
        return _err(_validation_message(exc))
    db = await _db()
    aid = _int(payload.get("id"))
    if aid:
        n = await db.execute("UPDATE accounts SET name=?, type=?, initial_cents=?, color=?, archived=? WHERE id=?",
                             (a.name, a.type, a.initial_cents, a.color, int(a.archived), aid))
        if not n:
            return _err("Compte introuvable", "NOT_FOUND")
    else:
        pos = (await db.fetch_one("SELECT COALESCE(MAX(position)+1, 0) AS p FROM accounts"))["p"]
        aid = (await db.fetch_one(
            "INSERT INTO accounts (name, type, initial_cents, color, archived, position, created_at) VALUES (?,?,?,?,?,?,?) RETURNING id",
            (a.name, a.type, a.initial_cents, a.color, int(a.archived), pos, _now())))["id"]
    return {"id": aid, "accounts": await _accounts()}


@module.on("account.delete")
async def account_delete(payload: dict):
    db = await _db()
    aid = _int(payload.get("id"))
    if not await _account_exists(aid):
        return _err("Compte introuvable", "NOT_FOUND")
    await db.execute("UPDATE transactions SET peer_id=NULL WHERE peer_id IN (SELECT id FROM transactions WHERE account_id=?)", (aid,))
    await db.execute("DELETE FROM transactions WHERE account_id=?", (aid,))
    await db.execute("DELETE FROM recurring WHERE account_id=? OR to_account_id=?", (aid, aid))
    await db.execute("DELETE FROM accounts WHERE id=?", (aid,))
    return {"accounts": await _accounts()}


@module.on("account.reorder")
async def account_reorder(payload: dict):
    db = await _db()
    for i, aid in enumerate(payload.get("order") or []):
        await db.execute("UPDATE accounts SET position=? WHERE id=?", (i, _int(aid)))
    return {"accounts": await _accounts()}


# ── Categories ────────────────────────────────────────────────────────────────

@module.on("categories.list")
async def categories_list(payload: dict):
    return {"categories": await _categories()}


@module.on("category.save")
async def category_save(payload: dict):
    try:
        c = CategoryIn.model_validate(payload.get("category") or {})
    except ValidationError as exc:
        return _err(_validation_message(exc))
    db = await _db()
    cid = _int(payload.get("id"))
    if c.parent_id is not None:
        parent = await db.fetch_one("SELECT * FROM categories WHERE id=?", (c.parent_id,))
        if not parent:
            return _err("Catégorie parente introuvable", "NOT_FOUND")
        if parent["parent_id"] is not None:
            return _err("Deux niveaux maximum : choisissez une catégorie principale comme parente")
        if parent["id"] == cid:
            return _err("Une catégorie ne peut pas être sa propre parente")
        if cid and await db.fetch_one("SELECT 1 AS x FROM categories WHERE parent_id=?", (cid,)):
            return _err("Cette catégorie a des sous-catégories : elle doit rester principale")
        c.kind = parent["kind"]
    if cid:
        if not await db.execute("UPDATE categories SET name=?, color=?, kind=?, parent_id=? WHERE id=?",
                                (c.name, c.color, c.kind, c.parent_id, cid)):
            return _err("Catégorie introuvable", "NOT_FOUND")
        await db.execute("UPDATE categories SET kind=? WHERE parent_id=?", (c.kind, cid))
    else:
        pos = (await db.fetch_one("SELECT COALESCE(MAX(position)+1, 0) AS p FROM categories WHERE parent_id IS ?",
                                  (c.parent_id,)))["p"]
        cid = (await db.fetch_one(
            "INSERT INTO categories (parent_id, name, color, kind, position) VALUES (?,?,?,?,?) RETURNING id",
            (c.parent_id, c.name, c.color, c.kind, pos)))["id"]
    return {"id": cid, "categories": await _categories()}


@module.on("category.delete")
async def category_delete(payload: dict):
    db = await _db()
    cid = _int(payload.get("id"))
    ids = await _category_ids_with_children(cid)
    if not ids:
        return _err("Catégorie introuvable", "NOT_FOUND")
    marks = ",".join("?" * len(ids))
    await db.execute(f"UPDATE transactions SET category_id=NULL WHERE category_id IN ({marks})", tuple(ids))
    await db.execute(f"UPDATE recurring SET category_id=NULL WHERE category_id IN ({marks})", tuple(ids))
    await db.execute(f"DELETE FROM rules WHERE category_id IN ({marks})", tuple(ids))
    await db.execute(f"DELETE FROM budgets WHERE category_id IN ({marks})", tuple(ids))
    await db.execute(f"DELETE FROM categories WHERE id IN ({marks})", tuple(ids))
    return {"categories": await _categories()}


# ── Transactions ──────────────────────────────────────────────────────────────

def _tx_filters(payload: dict) -> tuple[str, list]:
    where, params = ["1=1"], []
    if aid := _int(payload.get("account_id")):
        where.append("t.account_id=?")
        params.append(aid)
    month = payload.get("month") or ""
    if month and MONTH_RE.match(month):
        a, b = _month_bounds(month)
        where.append("t.date BETWEEN ? AND ?")
        params += [a, b]
    for key, op in (("date_from", ">="), ("date_to", "<=")):
        if v := payload.get(key):
            try:
                params.append(_iso(v))
                where.append(f"t.date {op} ?")
            except ValueError:
                pass
    cat = payload.get("category_id")
    if cat == "none":
        where.append("t.category_id IS NULL AND t.peer_id IS NULL")
    elif cat == "transfer":
        where.append("t.peer_id IS NOT NULL")
    elif _int(cat):
        where.append("t.category_id IN (SELECT id FROM categories WHERE id=? OR parent_id=?)")
        params += [_int(cat), _int(cat)]
    kind = payload.get("kind")
    if kind == "expense":
        where.append("t.amount_cents < 0")
    elif kind == "income":
        where.append("t.amount_cents > 0")
    if q := str(payload.get("search") or "").strip():
        where.append("(t.label LIKE ? OR t.notes LIKE ?)")
        params += [f"%{q}%", f"%{q}%"]
    return " AND ".join(where), params


@module.on("tx.list")
async def tx_list(payload: dict):
    db = await _db()
    where, params = _tx_filters(payload)
    limit = max(1, min(1000, _int(payload.get("limit"), 200)))
    offset = max(0, _int(payload.get("offset")))
    agg = await db.fetch_one(f"""SELECT COUNT(*) AS n,
        COALESCE(SUM(CASE WHEN t.amount_cents > 0 THEN t.amount_cents END), 0) AS income,
        COALESCE(SUM(CASE WHEN t.amount_cents < 0 THEN t.amount_cents END), 0) AS expense
        FROM transactions t WHERE {where}""", tuple(params))
    rows = await db.fetch_all(f"""
        SELECT t.*, p.account_id AS peer_account_id FROM transactions t
        LEFT JOIN transactions p ON p.id = t.peer_id
        WHERE {where} ORDER BY t.date DESC, t.id DESC LIMIT ? OFFSET ?""", (*params, limit, offset))
    aid = _int(payload.get("account_id"))
    contiguous = not any(payload.get(k) for k in ("category_id", "kind", "search"))
    if aid and rows and contiguous:
        top = rows[0]
        cur = (await db.fetch_one(
            "SELECT a.initial_cents + COALESCE((SELECT SUM(amount_cents) FROM transactions WHERE account_id=a.id"
            " AND (date < ? OR (date = ? AND id <= ?))), 0) AS s FROM accounts a WHERE a.id=?",
            (top["date"], top["date"], top["id"], aid)))["s"]
        for r in rows:
            r["running_cents"] = cur
            cur -= r["amount_cents"]
    return {"transactions": rows, "total": agg["n"], "income_cents": agg["income"], "expense_cents": agg["expense"]}


@module.on("tx.save")
async def tx_save(payload: dict):
    try:
        t = TxIn.model_validate(payload.get("tx") or {})
    except ValidationError as exc:
        return _err(_validation_message(exc))
    if t.amount_cents == 0:
        return _err("Le montant ne peut pas être nul")
    if not await _account_exists(t.account_id):
        return _err("Compte introuvable", "NOT_FOUND")
    if not await _category_ok(t.category_id):
        return _err("Catégorie introuvable", "NOT_FOUND")
    db = await _db()
    tid = _int(payload.get("id"))
    if tid:
        old = await db.fetch_one("SELECT * FROM transactions WHERE id=?", (tid,))
        if not old:
            return _err("Opération introuvable", "NOT_FOUND")
        if old["peer_id"]:
            peer = await db.fetch_one("SELECT account_id FROM transactions WHERE id=?", (old["peer_id"],))
            if peer and peer["account_id"] == t.account_id:
                return _err("Un virement doit concerner deux comptes différents")
            await db.execute("UPDATE transactions SET account_id=?, date=?, label=?, amount_cents=?, category_id=NULL, notes=? WHERE id=?",
                             (t.account_id, t.date, t.label, t.amount_cents, t.notes, tid))
            await db.execute("UPDATE transactions SET date=?, label=?, amount_cents=?, notes=? WHERE id=?",
                             (t.date, t.label, -t.amount_cents, t.notes, old["peer_id"]))
        else:
            await db.execute("UPDATE transactions SET account_id=?, date=?, label=?, amount_cents=?, category_id=?, notes=? WHERE id=?",
                             (t.account_id, t.date, t.label, t.amount_cents, t.category_id, t.notes, tid))
    else:
        cat = t.category_id
        if cat is None and payload.get("auto_category", True):
            cat = _match_rule(t.label, await _rules())
        tid = (await db.fetch_one(
            "INSERT INTO transactions (account_id, date, label, amount_cents, category_id, notes, created_at) VALUES (?,?,?,?,?,?,?) RETURNING id",
            (t.account_id, t.date, t.label, t.amount_cents, cat, t.notes, _now())))["id"]
    return {"id": tid, "accounts": await _accounts()}


@module.on("tx.delete")
async def tx_delete(payload: dict):
    db = await _db()
    ids = [_int(i) for i in (payload.get("ids") or [payload.get("id")]) if _int(i)]
    if not ids:
        return _err("Aucune opération indiquée")
    marks = ",".join("?" * len(ids))
    peers = [r["peer_id"] for r in await db.fetch_all(
        f"SELECT peer_id FROM transactions WHERE id IN ({marks}) AND peer_id IS NOT NULL", tuple(ids))]
    all_ids = list({*ids, *peers})
    marks = ",".join("?" * len(all_ids))
    n = await db.execute(f"DELETE FROM transactions WHERE id IN ({marks})", tuple(all_ids))
    return {"deleted": n, "accounts": await _accounts()}


@module.on("tx.set_category")
async def tx_set_category(payload: dict):
    db = await _db()
    ids = [_int(i) for i in payload.get("ids") or [] if _int(i)]
    cid = payload.get("category_id")
    cid = _int(cid) if cid not in (None, "") else None
    if not ids:
        return _err("Aucune opération indiquée")
    if not await _category_ok(cid):
        return _err("Catégorie introuvable", "NOT_FOUND")
    marks = ",".join("?" * len(ids))
    n = await db.execute(f"UPDATE transactions SET category_id=? WHERE id IN ({marks}) AND peer_id IS NULL", (cid, *ids))
    return {"updated": n}


@module.on("transfer.save")
async def transfer_save(payload: dict):
    try:
        t = TransferIn.model_validate(payload.get("transfer") or {})
    except ValidationError as exc:
        return _err(_validation_message(exc))
    if t.from_account_id == t.to_account_id:
        return _err("Choisissez deux comptes différents")
    if not await _account_exists(t.from_account_id) or not await _account_exists(t.to_account_id):
        return _err("Compte introuvable", "NOT_FOUND")
    label = t.label.strip() or "Virement"
    a, b = await _insert_transfer(t.from_account_id, t.to_account_id, t.date, t.amount_cents, label, t.notes, None)
    return {"ids": [a, b], "accounts": await _accounts()}


async def _insert_transfer(src: int, dst: int, day: str, cents: int, label: str, notes: str, rec_id: Optional[int]) -> tuple[int, int]:
    db = await _db()
    now = _now()
    a = (await db.fetch_one(
        "INSERT INTO transactions (account_id, date, label, amount_cents, notes, recurring_id, created_at) VALUES (?,?,?,?,?,?,?) RETURNING id",
        (src, day, label, -abs(cents), notes, rec_id, now)))["id"]
    b = (await db.fetch_one(
        "INSERT INTO transactions (account_id, date, label, amount_cents, notes, recurring_id, peer_id, created_at) VALUES (?,?,?,?,?,?,?,?) RETURNING id",
        (dst, day, label, abs(cents), notes, rec_id, a, now)))["id"]
    await db.execute("UPDATE transactions SET peer_id=? WHERE id=?", (b, a))
    return a, b


# ── Rules ─────────────────────────────────────────────────────────────────────

@module.on("rules.list")
async def rules_list(payload: dict):
    rows = await (await _db()).fetch_all("SELECT * FROM rules ORDER BY position, id")
    return {"rules": rows}


@module.on("rule.save")
async def rule_save(payload: dict):
    try:
        r = RuleIn.model_validate(payload.get("rule") or {})
    except ValidationError as exc:
        return _err(_validation_message(exc))
    if not _norm(r.keyword):
        return _err("Le mot-clé doit contenir des lettres ou des chiffres")
    db = await _db()
    if not await db.fetch_one("SELECT 1 AS x FROM categories WHERE id=?", (r.category_id,)):
        return _err("Catégorie introuvable", "NOT_FOUND")
    rid = _int(payload.get("id"))
    if rid:
        if not await db.execute("UPDATE rules SET keyword=?, category_id=? WHERE id=?", (r.keyword, r.category_id, rid)):
            return _err("Règle introuvable", "NOT_FOUND")
    else:
        await db.execute("UPDATE rules SET position = position + 1")
        rid = (await db.fetch_one("INSERT INTO rules (keyword, category_id, position) VALUES (?,?,0) RETURNING id",
                                  (r.keyword, r.category_id)))["id"]
    return {"id": rid, **(await rules_list({}))}


@module.on("rule.delete")
async def rule_delete(payload: dict):
    db = await _db()
    if not await db.execute("DELETE FROM rules WHERE id=?", (_int(payload.get("id")),)):
        return _err("Règle introuvable", "NOT_FOUND")
    return await rules_list({})


@module.on("rule.reorder")
async def rule_reorder(payload: dict):
    db = await _db()
    for i, rid in enumerate(payload.get("order") or []):
        await db.execute("UPDATE rules SET position=? WHERE id=?", (i, _int(rid)))
    return await rules_list({})


@module.on("rules.apply")
async def rules_apply(payload: dict):
    """Categorizes uncategorized operations (or all of them when `overwrite`) with the rules."""
    db = await _db()
    rules = await _rules()
    cond = "peer_id IS NULL" + ("" if payload.get("overwrite") else " AND category_id IS NULL")
    rows = await db.fetch_all(f"SELECT id, label, category_id FROM transactions WHERE {cond}")
    n = 0
    for r in rows:
        cid = _match_rule(r["label"], rules)
        if cid and cid != r["category_id"]:
            await db.execute("UPDATE transactions SET category_id=? WHERE id=?", (cid, r["id"]))
            n += 1
    return {"updated": n}


# ── Recurring ─────────────────────────────────────────────────────────────────

def _advance(d: date, freq: str, interval: int, anchor: int) -> date:
    if freq == "daily":
        return d + timedelta(days=interval)
    if freq == "weekly":
        return d + timedelta(weeks=interval)
    months = interval * (12 if freq == "yearly" else 1)
    m = d.month - 1 + months
    y, m = d.year + m // 12, m % 12 + 1
    return date(y, m, min(anchor, calendar.monthrange(y, m)[1]))


async def _run_recurring(until: Optional[date] = None) -> int:
    db = await _db()
    until = until or date.today()
    count = 0
    for r in await db.fetch_all("SELECT * FROM recurring WHERE active=1 AND next_date <= ?", (until.isoformat(),)):
        nxt = date.fromisoformat(r["next_date"])
        end = date.fromisoformat(r["end_date"]) if r["end_date"] else None
        steps = 0
        while nxt <= until and (end is None or nxt <= end) and steps < MAX_RECURRING_RUN:
            if r["to_account_id"]:
                await _insert_transfer(r["account_id"], r["to_account_id"], nxt.isoformat(), r["amount_cents"],
                                       r["label"], r["notes"], r["id"])
            else:
                await db.execute(
                    "INSERT INTO transactions (account_id, date, label, amount_cents, category_id, notes, recurring_id, created_at) VALUES (?,?,?,?,?,?,?,?)",
                    (r["account_id"], nxt.isoformat(), r["label"], r["amount_cents"], r["category_id"], r["notes"], r["id"], _now()))
            nxt = _advance(nxt, r["freq"], r["interval"], r["anchor_day"])
            steps += 1
            count += 1
        active = 0 if end is not None and nxt > end else 1
        await db.execute("UPDATE recurring SET next_date=?, active=? WHERE id=?", (nxt.isoformat(), active, r["id"]))
    if count:
        log.info("%d opération(s) récurrente(s) générée(s)", count)
    return count


@module.on("recurring.list")
async def recurring_list(payload: dict):
    rows = await (await _db()).fetch_all("SELECT * FROM recurring ORDER BY active DESC, next_date, id")
    for r in rows:
        r["active"] = bool(r["active"])
    return {"recurring": rows}


@module.on("recurring.save")
async def recurring_save(payload: dict):
    try:
        r = RecurringIn.model_validate(payload.get("recurring") or {})
    except ValidationError as exc:
        return _err(_validation_message(exc))
    if r.amount_cents == 0:
        return _err("Le montant ne peut pas être nul")
    if not await _account_exists(r.account_id):
        return _err("Compte introuvable", "NOT_FOUND")
    if r.to_account_id is not None:
        if r.to_account_id == r.account_id:
            return _err("Choisissez deux comptes différents")
        if not await _account_exists(r.to_account_id):
            return _err("Compte destinataire introuvable", "NOT_FOUND")
        r.amount_cents = abs(r.amount_cents)
        r.category_id = None
    if not await _category_ok(r.category_id):
        return _err("Catégorie introuvable", "NOT_FOUND")
    if r.end_date and r.end_date < r.next_date:
        return _err("La date de fin précède la prochaine échéance")
    anchor = date.fromisoformat(r.next_date).day
    db = await _db()
    rid = _int(payload.get("id"))
    vals = (r.account_id, r.to_account_id, r.label, r.amount_cents, r.category_id, r.notes, r.freq, r.interval,
            anchor, r.next_date, r.end_date, int(r.active))
    if rid:
        if not await db.execute(
                "UPDATE recurring SET account_id=?, to_account_id=?, label=?, amount_cents=?, category_id=?, notes=?, freq=?, interval=?,"
                " anchor_day=?, next_date=?, end_date=?, active=? WHERE id=?", (*vals, rid)):
            return _err("Opération récurrente introuvable", "NOT_FOUND")
    else:
        rid = (await db.fetch_one(
            "INSERT INTO recurring (account_id, to_account_id, label, amount_cents, category_id, notes, freq, interval, anchor_day,"
            " next_date, end_date, active) VALUES (?,?,?,?,?,?,?,?,?,?,?,?) RETURNING id", vals))["id"]
    generated = await _run_recurring()
    return {"id": rid, "generated": generated, "accounts": await _accounts(), **(await recurring_list({}))}


@module.on("recurring.delete")
async def recurring_delete(payload: dict):
    db = await _db()
    rid = _int(payload.get("id"))
    if not await db.execute("DELETE FROM recurring WHERE id=?", (rid,)):
        return _err("Opération récurrente introuvable", "NOT_FOUND")
    await db.execute("UPDATE transactions SET recurring_id=NULL WHERE recurring_id=?", (rid,))
    return await recurring_list({})


@module.on("recurring.upcoming")
async def recurring_upcoming(payload: dict):
    """Next occurrences over `days` days (default 45), without writing anything."""
    days = max(1, min(366, _int(payload.get("days"), 45)))
    limit = date.today() + timedelta(days=days)
    out = []
    for r in (await recurring_list({}))["recurring"]:
        if not r["active"]:
            continue
        nxt = date.fromisoformat(r["next_date"])
        end = date.fromisoformat(r["end_date"]) if r["end_date"] else None
        for _ in range(60):
            if nxt > limit or (end and nxt > end):
                break
            out.append({"recurring_id": r["id"], "date": nxt.isoformat(), "label": r["label"],
                        "amount_cents": r["amount_cents"], "account_id": r["account_id"], "to_account_id": r["to_account_id"]})
            nxt = _advance(nxt, r["freq"], r["interval"], r["anchor_day"])
    out.sort(key=lambda x: x["date"])
    return {"upcoming": out}


# ── Budgets ───────────────────────────────────────────────────────────────────

@module.on("budget.set")
async def budget_set(payload: dict):
    db = await _db()
    cid = _int(payload.get("category_id"))
    month = payload.get("month") or ""
    if month and not MONTH_RE.match(month):
        return _err("Mois invalide (AAAA-MM)")
    if not await db.fetch_one("SELECT 1 AS x FROM categories WHERE id=?", (cid,)):
        return _err("Catégorie introuvable", "NOT_FOUND")
    amount = payload.get("amount_cents")
    if amount is None or amount == "":
        await db.execute("DELETE FROM budgets WHERE category_id=? AND month=?", (cid, month))
    else:
        amount = _int(amount, -1)
        if amount < 0 or amount >= MAX_CENTS:
            return _err("Montant de budget invalide")
        await db.execute("INSERT OR REPLACE INTO budgets (category_id, month, amount_cents) VALUES (?,?,?)", (cid, month, amount))
    return await budgets_status({"month": payload.get("view_month") or month or date.today().strftime("%Y-%m")})


@module.on("budgets.status")
async def budgets_status(payload: dict):
    month = payload.get("month") or date.today().strftime("%Y-%m")
    if not MONTH_RE.match(month):
        return _err("Mois invalide (AAAA-MM)")
    db = await _db()
    a, b = _month_bounds(month)
    cats = await _categories()
    budgets = await db.fetch_all("SELECT * FROM budgets WHERE month IN ('', ?)", (month,))
    amounts: dict[int, dict] = {}
    for bud in budgets:
        slot = amounts.setdefault(bud["category_id"], {})
        slot["month" if bud["month"] else "default"] = bud["amount_cents"]
    spent = {r["category_id"]: r["s"] for r in await db.fetch_all(
        "SELECT category_id, -SUM(amount_cents) AS s FROM transactions WHERE date BETWEEN ? AND ? AND peer_id IS NULL"
        " AND category_id IS NOT NULL GROUP BY category_id", (a, b))}
    children: dict[int, list[int]] = {}
    for c in cats:
        if c["parent_id"]:
            children.setdefault(c["parent_id"], []).append(c["id"])
    items = []
    for c in cats:
        slot = amounts.get(c["id"], {})
        amount = slot.get("month", slot.get("default"))
        total = (spent.get(c["id"]) or 0) + sum(spent.get(k) or 0 for k in children.get(c["id"], []))
        if c["kind"] == "income":
            total = -total
        items.append({"category_id": c["id"], "parent_id": c["parent_id"], "name": c["name"], "color": c["color"],
                      "kind": c["kind"], "budget_cents": amount, "default_cents": slot.get("default"),
                      "override": "month" in slot, "spent_cents": total})
    y, m = int(month[:4]), int(month[5:7])
    days = calendar.monthrange(y, m)[1]
    today = date.today()
    elapsed = 1.0 if (y, m) < (today.year, today.month) else 0.0 if (y, m) > (today.year, today.month) else today.day / days
    return {"month": month, "items": items, "elapsed": elapsed}


# ── Stats ─────────────────────────────────────────────────────────────────────

@module.on("stats.dashboard")
async def stats_dashboard(payload: dict):
    month = payload.get("month") or date.today().strftime("%Y-%m")
    if not MONTH_RE.match(month):
        return _err("Mois invalide (AAAA-MM)")
    db = await _db()
    aid = _int(payload.get("account_id"))
    acc_sql, acc_params = ("AND account_id=?", (aid,)) if aid else ("", ())
    a, b = _month_bounds(month)

    cats = {c["id"]: c for c in await _categories()}
    by_cat: dict = {}
    for r in await db.fetch_all(
            f"SELECT category_id, SUM(amount_cents) AS s FROM transactions WHERE date BETWEEN ? AND ? AND peer_id IS NULL"
            f" AND amount_cents < 0 {acc_sql} GROUP BY category_id", (a, b, *acc_params)):
        c = cats.get(r["category_id"])
        top = cats.get(c["parent_id"]) if c and c["parent_id"] else c
        key = top["id"] if top else 0
        slot = by_cat.setdefault(key, {"category_id": key or None, "name": top["name"] if top else "Sans catégorie",
                                       "color": top["color"] if top else "#52525b", "cents": 0, "children": {}})
        slot["cents"] += -r["s"]
        if c and c["parent_id"]:
            slot["children"][c["name"]] = slot["children"].get(c["name"], 0) - r["s"]
    expenses = sorted(by_cat.values(), key=lambda x: -x["cents"])
    for e in expenses:
        e["children"] = sorted(({"name": k, "cents": v} for k, v in e["children"].items()), key=lambda x: -x["cents"])

    first = _shift_month(month, -11)
    fa, _ = _month_bounds(first)
    monthly = {r["m"]: r for r in await db.fetch_all(
        f"""SELECT substr(date, 1, 7) AS m,
            COALESCE(SUM(CASE WHEN amount_cents > 0 THEN amount_cents END), 0) AS income,
            COALESCE(-SUM(CASE WHEN amount_cents < 0 THEN amount_cents END), 0) AS expense
            FROM transactions WHERE date BETWEEN ? AND ? AND peer_id IS NULL {acc_sql} GROUP BY m""", (fa, b, *acc_params))}
    months = [_shift_month(first, i) for i in range(12)]
    flow = [{"month": m, "income_cents": monthly.get(m, {}).get("income", 0),
             "expense_cents": monthly.get(m, {}).get("expense", 0)} for m in months]

    if aid:
        init = (await db.fetch_one("SELECT initial_cents FROM accounts WHERE id=?", (aid,)) or {"initial_cents": 0})["initial_cents"]
    else:
        init = (await db.fetch_one("SELECT COALESCE(SUM(initial_cents),0) AS s FROM accounts WHERE archived=0"))["s"]
        acc_sql = "AND account_id IN (SELECT id FROM accounts WHERE archived=0)"
        acc_params = ()
    before = (await db.fetch_one(f"SELECT COALESCE(SUM(amount_cents),0) AS s FROM transactions WHERE date < ? {acc_sql}",
                                 (fa, *acc_params)))["s"]
    daily = {r["date"]: r["s"] for r in await db.fetch_all(
        f"SELECT date, SUM(amount_cents) AS s FROM transactions WHERE date BETWEEN ? AND ? {acc_sql} GROUP BY date",
        (fa, b, *acc_params))}
    balance, cur, d, end = [], init + before, date.fromisoformat(fa), date.fromisoformat(b)
    while d <= end:
        cur += daily.get(d.isoformat(), 0)
        balance.append([d.isoformat(), cur])
        d += timedelta(days=1)

    cur_month = monthly.get(month, {})
    return {"month": month, "expenses": expenses, "flow": flow, "balance": balance,
            "income_cents": cur_month.get("income", 0), "expense_cents": cur_month.get("expense", 0),
            "uncategorized": (await db.fetch_one(
                f"SELECT COUNT(*) AS n FROM transactions WHERE category_id IS NULL AND peer_id IS NULL AND date BETWEEN ? AND ?",
                (a, b)))["n"]}


# ── Import ────────────────────────────────────────────────────────────────────

def _parse_amount(raw: str, decimal_sep: str) -> Optional[int]:
    s = (raw or "").strip()
    if not s:
        return None
    neg = False
    if s.startswith("(") and s.endswith(")"):
        neg, s = True, s[1:-1]
    s = re.sub(r"[\s  €$£]|EUR", "", s, flags=re.I)
    if s.endswith("-"):
        neg, s = not neg, s[:-1]
    if s.startswith("+"):
        s = s[1:]
    if s.startswith("-"):
        neg, s = not neg, s[1:]
    if decimal_sep == ",":
        s = s.replace(".", "").replace("'", "").replace(",", ".")
    else:
        s = s.replace(",", "").replace("'", "")
    if not re.fullmatch(r"\d+(\.\d+)?", s):
        return None
    try:
        cents = int((Decimal(s) * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    except InvalidOperation:
        return None
    if cents >= MAX_CENTS:
        return None
    return -cents if neg else cents


def _parse_date(raw: str, fmt: str) -> Optional[str]:
    s = (raw or "").strip()
    if not s:
        return None
    if fmt == "AAAAMMJJ" or re.fullmatch(r"\d{8}", s):
        m = re.match(r"(\d{4})(\d{2})(\d{2})", s)
        parts, order = ([m.group(1), m.group(2), m.group(3)] if m else None), "ymd"
    else:
        parts = re.findall(r"\d+", s)[:3]
        order = {"JJ/MM/AAAA": "dmy", "AAAA-MM-JJ": "ymd", "MM/JJ/AAAA": "mdy"}.get(fmt, "dmy")
        if len(parts) == 3 and len(parts[0]) == 4:
            order = "ymd"
    if not parts or len(parts) != 3:
        return None
    vals = dict(zip(order, (int(p) for p in parts)))
    y = vals["y"]
    if y < 100:
        y += 2000 if y < 70 else 1900
    try:
        return date(y, vals["m"], vals["d"]).isoformat()
    except ValueError:
        return None


def _sniff_delimiter(text: str) -> str:
    sample = "\n".join(text.splitlines()[:20])
    counts = {d: sample.count(d) for d in (";", ",", "\t", "|")}
    return max(counts, key=counts.get) if any(counts.values()) else ";"


def _read_csv(text: str, delimiter: str) -> list[list[str]]:
    reader = csv.reader(io.StringIO(text), delimiter=delimiter)
    rows = []
    for row in reader:
        if any(c.strip() for c in row):
            rows.append([c.strip() for c in row])
        if len(rows) > MAX_IMPORT_ROWS + 50:
            break
    return rows


@module.on("import.preview")
async def import_preview(payload: dict):
    """First step of the CSV assistant: raw rows so the user can map the columns."""
    text = str(payload.get("text") or "").lstrip("﻿")
    if not text.strip():
        return _err("Fichier vide")
    if len(text) > MAX_IMPORT_CHARS:
        return _err("Fichier trop volumineux")
    delimiter = payload.get("delimiter") or _sniff_delimiter(text)
    if delimiter not in (";", ",", "\t", "|"):
        return _err("Séparateur invalide")
    rows = _read_csv(text, delimiter)
    width = max((len(r) for r in rows[:50]), default=0)
    guess = _guess_mapping(rows[:30], width)
    return {"delimiter": delimiter, "rows": rows[:30], "row_count": len(rows), "columns": width, "guess": guess}


def _guess_mapping(rows: list[list[str]], width: int) -> dict:
    header = [_norm(c) for c in rows[0]] if rows else []
    m: dict = {"has_header": False, "date": None, "label": None, "amount": None, "debit": None, "credit": None,
               "notes": None, "category": None, "date_format": "JJ/MM/AAAA", "decimal": ","}
    keys = {
        "date": ("date operation", "date", "date de l operation", "date comptable", "date valeur"),
        "label": ("libelle", "libelle operation", "label", "description", "intitule", "nature de l operation", "beneficiaire"),
        "amount": ("montant", "amount", "montant eur", "montant euros", "somme"),
        "debit": ("debit", "debit eur", "debit euros", "depense"),
        "credit": ("credit", "credit eur", "credit euros", "recette"),
        "notes": ("note", "notes", "commentaire", "memo", "detail"),
        "category": ("categorie", "category", "sous categorie"),
    }
    for field, names in keys.items():
        for i, h in enumerate(header):
            if h in names or any(h.startswith(n) for n in names if len(n) > 4):
                if m[field] is None:
                    m[field] = i
    if any(m[k] is not None for k in ("date", "label", "amount", "debit")):
        m["has_header"] = True
    data = rows[1:] if m["has_header"] else rows
    if m["date"] is None:
        for i in range(width):
            if data and all(_parse_date(r[i] if i < len(r) else "", "JJ/MM/AAAA") for r in data[:5]):
                m["date"] = i
                break
    if m["amount"] is None and m["debit"] is None:
        for i in range(width):
            if i != m["date"] and data and all(_parse_amount(r[i] if i < len(r) else "", ",") is not None for r in data[:5]):
                m["amount"] = i
                break
    if m["label"] is None:
        best, best_len = None, 0
        for i in range(width):
            if i in (m["date"], m["amount"]):
                continue
            ln = sum(len(r[i]) for r in data[:10] if i < len(r))
            if ln > best_len:
                best, best_len = i, ln
        m["label"] = best
    if m["date"] is not None and data:
        sample = [r[m["date"]] for r in data[:20] if m["date"] < len(r)]
        if sample and all(re.match(r"^\d{4}-\d{2}-\d{2}", s) for s in sample):
            m["date_format"] = "AAAA-MM-JJ"
        elif sample and all(re.fullmatch(r"\d{8}", s) for s in sample):
            m["date_format"] = "AAAAMMJJ"
    col = m["amount"] if m["amount"] is not None else m["debit"]
    if col is not None and data:
        vals = [r[col] for r in data[:20] if col < len(r) and r[col]]
        dots = sum(1 for v in vals if re.search(r"\.\d{1,2}$", v))
        commas = sum(1 for v in vals if re.search(r",\d{1,2}$", v))
        if dots > commas:
            m["decimal"] = "."
    return m


def _rows_from_csv(text: str, mp: dict) -> tuple[list[dict], list[str]]:
    delimiter = mp.get("delimiter") or _sniff_delimiter(text)
    if delimiter not in (";", ",", "\t", "|"):
        raise ValueError("Séparateur invalide")
    rows = _read_csv(text, delimiter)
    skip = max(0, min(50, _int(mp.get("skip_rows"))))
    rows = rows[skip:]
    if mp.get("has_header"):
        rows = rows[1:]
    fmt = mp.get("date_format") if mp.get("date_format") in DATE_FORMATS else "JJ/MM/AAAA"
    dec = "." if mp.get("decimal") == "." else ","

    def col(name):
        v = mp.get(name)
        return None if v is None or v == "" else _int(v, -1)

    c_date, c_label, c_label2 = col("date"), col("label"), col("label2")
    c_amount, c_debit, c_credit, c_notes, c_cat = col("amount"), col("debit"), col("credit"), col("notes"), col("category")
    if c_date is None or c_label is None:
        raise ValueError("Indiquez au moins les colonnes Date et Libellé")
    if c_amount is None and c_debit is None and c_credit is None:
        raise ValueError("Indiquez la colonne Montant (ou Débit / Crédit)")
    cell = lambda r, i: r[i] if i is not None and 0 <= i < len(r) else ""
    out, errors = [], []
    for n, r in enumerate(rows[:MAX_IMPORT_ROWS], start=1 + skip + (1 if mp.get("has_header") else 0)):
        d = _parse_date(cell(r, c_date), fmt)
        label = " ".join(x for x in (cell(r, c_label), cell(r, c_label2)) if x).strip()
        if c_amount is not None:
            amount = _parse_amount(cell(r, c_amount), dec)
            if amount is not None and mp.get("invert"):
                amount = -amount
        else:
            deb = _parse_amount(cell(r, c_debit), dec)
            cre = _parse_amount(cell(r, c_credit), dec)
            amount = None
            if deb:
                amount = -abs(deb)
            if cre:
                amount = (amount or 0) + abs(cre)
            if amount is None and (deb == 0 or cre == 0):
                amount = 0
        if d is None or amount is None:
            errors.append(f"Ligne {n} ignorée : {'date' if d is None else 'montant'} illisible")
            continue
        if amount == 0:
            continue
        out.append({"date": d, "label": (label or "Sans libellé")[:200], "amount_cents": amount,
                    "notes": cell(r, c_notes)[:5000], "category_name": cell(r, c_cat), "ref": ""})
    return out, errors


def _ofx_tag(block: str, tag: str) -> str:
    m = re.search(rf"<{tag}>([^<\r\n]*)", block, re.I)
    return m.group(1).strip() if m else ""


def _rows_from_ofx(text: str) -> tuple[list[dict], list[str]]:
    out, errors = [], []
    blocks = re.findall(r"<STMTTRN>(.*?)(?:</STMTTRN>|(?=<STMTTRN>)|(?=</BANKTRANLIST>))", text, re.I | re.S)
    if not blocks:
        raise ValueError("Aucune opération trouvée dans le fichier OFX")
    for i, b in enumerate(blocks[:MAX_IMPORT_ROWS], start=1):
        d = _parse_date(_ofx_tag(b, "DTPOSTED")[:8], "AAAAMMJJ")
        amount = _parse_amount(_ofx_tag(b, "TRNAMT"), "," if "," in _ofx_tag(b, "TRNAMT") else ".")
        name = _ofx_tag(b, "NAME")
        memo = _ofx_tag(b, "MEMO")
        if d is None or amount is None:
            errors.append(f"Opération {i} ignorée : date ou montant illisible")
            continue
        if amount == 0:
            continue
        label = name or memo or "Sans libellé"
        notes = memo if name and memo and memo != name else ""
        out.append({"date": d, "label": label[:200], "amount_cents": amount, "notes": notes[:5000],
                    "category_name": "", "ref": _ofx_tag(b, "FITID")[:100]})
    return out, errors


def _rows_from_qif(text: str, fmt: str, dec: str) -> tuple[list[dict], list[str]]:
    out, errors, cur = [], [], {}
    lines = text.splitlines() + ["^"]
    n = 0
    for line in lines:
        if not line or line.startswith("!"):
            continue
        code, val = line[0], line[1:].strip()
        if code == "^":
            if cur:
                n += 1
                d = _parse_date(cur.get("D", "").replace("'", "/"), fmt)
                amount = _parse_amount(cur.get("T") or cur.get("U", ""), dec)
                if d is None or amount is None:
                    errors.append(f"Opération {n} ignorée : date ou montant illisible")
                elif amount:
                    label = cur.get("P") or cur.get("M") or "Sans libellé"
                    notes = cur.get("M", "") if cur.get("P") else ""
                    out.append({"date": d, "label": label[:200], "amount_cents": amount, "notes": notes[:5000],
                                "category_name": cur.get("L", "").split(":")[-1].strip("[]"), "ref": ""})
                if len(out) >= MAX_IMPORT_ROWS:
                    break
            cur = {}
        elif code in "DTUPML":
            cur[code] = val
    if not out and not errors:
        raise ValueError("Aucune opération trouvée dans le fichier QIF")
    return out, errors


@module.on("import.parse")
async def import_parse(payload: dict):
    """Parses a statement and flags duplicates and suggested categories, without writing anything."""
    text = str(payload.get("text") or "").lstrip("﻿")
    fmt = payload.get("format") or "csv"
    aid = _int(payload.get("account_id"))
    if not text.strip():
        return _err("Fichier vide")
    if len(text) > MAX_IMPORT_CHARS:
        return _err("Fichier trop volumineux")
    if not await _account_exists(aid):
        return _err("Choisissez le compte de destination", "NOT_FOUND")
    mp = payload.get("mapping") or {}
    try:
        if fmt == "csv":
            rows, errors = _rows_from_csv(text, mp)
        elif fmt == "ofx":
            rows, errors = _rows_from_ofx(text)
        elif fmt == "qif":
            qfmt = mp.get("date_format") if mp.get("date_format") in DATE_FORMATS else "JJ/MM/AAAA"
            rows, errors = _rows_from_qif(text, qfmt, "," if mp.get("decimal") == "," else ".")
        else:
            return _err("Format non pris en charge (CSV, OFX, QIF)")
    except ValueError as exc:
        return _err(str(exc))
    db = await _db()
    rules = await _rules()
    cats = await _categories()
    by_name = {_norm(c["name"]): c["id"] for c in cats}
    existing = Counter()
    refs = set()
    if rows:
        lo, hi = min(r["date"] for r in rows), max(r["date"] for r in rows)
        for t in await db.fetch_all(
                "SELECT date, amount_cents, label, import_ref FROM transactions WHERE account_id=? AND date BETWEEN ? AND ?",
                (aid, lo, hi)):
            existing[(t["date"], t["amount_cents"], _norm(t["label"]))] += 1
            if t["import_ref"]:
                refs.add(t["import_ref"])
        loose = Counter((t["date"], t["amount_cents"]) for t in await db.fetch_all(
            "SELECT date, amount_cents FROM transactions WHERE account_id=? AND date BETWEEN ? AND ?", (aid, lo, hi)))
    else:
        loose = Counter()
    seen = Counter()
    seen_loose = Counter()
    for r in rows:
        key = (r["date"], r["amount_cents"], _norm(r["label"]))
        seen[key] += 1
        seen_loose[key[:2]] += 1
        if r["ref"] and r["ref"] in refs:
            r["duplicate"] = "sure"
        elif seen[key] <= existing[key]:
            r["duplicate"] = "sure"
        elif seen_loose[key[:2]] <= loose[key[:2]]:
            r["duplicate"] = "maybe"
        else:
            r["duplicate"] = ""
        cid = by_name.get(_norm(r.pop("category_name", ""))) if r.get("category_name") else None
        r["category_id"] = cid or _match_rule(r["label"], rules)
    return {"rows": rows, "errors": errors[:50], "error_count": len(errors)}


@module.on("import.commit")
async def import_commit(payload: dict):
    aid = _int(payload.get("account_id"))
    if not await _account_exists(aid):
        return _err("Compte introuvable", "NOT_FOUND")
    raw = payload.get("rows") or []
    if not isinstance(raw, list) or len(raw) > MAX_IMPORT_ROWS:
        return _err("Liste d'opérations invalide")
    valid_cats = {c["id"] for c in await _categories()}
    items = []
    for i, r in enumerate(raw, start=1):
        try:
            t = TxIn.model_validate({**r, "account_id": aid})
        except (ValidationError, TypeError) as exc:
            return _err(f"Opération {i} : " + (_validation_message(exc) if isinstance(exc, ValidationError) else "invalide"))
        items.append((t, str(r.get("ref") or "")[:100]))
    db = await _db()
    now = _now()
    for t, ref in items:
        await db.execute(
            "INSERT INTO transactions (account_id, date, label, amount_cents, category_id, notes, import_ref, created_at) VALUES (?,?,?,?,?,?,?,?)",
            (aid, t.date, t.label, t.amount_cents, t.category_id if t.category_id in valid_cats else None, t.notes, ref, now))
    log.info("Import : %d opération(s) ajoutée(s)", len(items))
    return {"imported": len(items), "accounts": await _accounts()}


# ── Export ────────────────────────────────────────────────────────────────────

def _fr_amount(cents: int) -> str:
    sign = "-" if cents < 0 else ""
    cents = abs(cents)
    return f"{sign}{cents // 100},{cents % 100:02d}"


@module.on("tx.export_csv")
async def tx_export_csv(payload: dict):
    db = await _db()
    where, params = _tx_filters(payload)
    rows = await db.fetch_all(f"""
        SELECT t.*, a.name AS account, c.name AS cat, pc.name AS parent_cat, pa.name AS peer_account
        FROM transactions t JOIN accounts a ON a.id = t.account_id
        LEFT JOIN categories c ON c.id = t.category_id LEFT JOIN categories pc ON pc.id = c.parent_id
        LEFT JOIN transactions p ON p.id = t.peer_id LEFT JOIN accounts pa ON pa.id = p.account_id
        WHERE {where} ORDER BY t.date, t.id""", tuple(params))
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";", lineterminator="\r\n")
    w.writerow(["Date", "Compte", "Libellé", "Montant (€)", "Catégorie", "Sous-catégorie", "Virement avec", "Notes"])
    for r in rows:
        d = r["date"]
        top, sub = (r["parent_cat"], r["cat"]) if r["parent_cat"] else (r["cat"] or "", "")
        w.writerow([f"{d[8:10]}/{d[5:7]}/{d[:4]}", r["account"], r["label"], _fr_amount(r["amount_cents"]),
                    top, sub, r["peer_account"] or "", r["notes"]])
    return {"csv": "﻿" + buf.getvalue(), "count": len(rows)}


@module.on("demo.seed")
async def demo_seed(payload: dict):
    """Creates two sample accounts with a few months of operations."""
    import random
    db = await _db()
    rnd = random.Random(42)
    cur = (await account_save({"account": {"name": "Compte courant (exemple)", "type": "courant", "initial_cents": 120000, "color": "#6366f1"}}))["id"]
    sav = (await account_save({"account": {"name": "Livret A (exemple)", "type": "epargne", "initial_cents": 500000, "color": "#10b981"}}))["id"]
    rules = await _rules()
    today = date.today()
    start = date(today.year, today.month, 1)
    for k in range(5, -1, -1):
        m0 = _shift_month(start.strftime("%Y-%m"), -k)
        y, m = int(m0[:4]), int(m0[5:7])
        last = min(calendar.monthrange(y, m)[1], today.day if k == 0 else 31)
        ops = [(1, "VIR SALAIRE ACME SAS", 245000), (5, "PRLV LOYER AGENCE DUPONT", -78000),
               (8, "PRLV EDF CLIENTS PARTICULIERS", -6400), (10, "PRLV FREE MOBILE", -1999),
               (12, "CB NETFLIX.COM", -1349), (15, "PRLV SPOTIFY", -1099)]
        for _ in range(9):
            ops.append((rnd.randint(1, 28), rnd.choice(["CB CARREFOUR MARKET", "CB LIDL", "CB MONOPRIX", "CB LECLERC DRIVE"]),
                        -rnd.randint(1500, 9500)))
        for _ in range(3):
            ops.append((rnd.randint(1, 28), rnd.choice(["CB TOTAL ENERGIES", "CB ESSO", "CB SNCF INTERNET"]), -rnd.randint(2500, 7000)))
        for _ in range(4):
            ops.append((rnd.randint(1, 28), rnd.choice(["CB RESTAURANT LE PETIT ZINC", "CB CINEMA PATHE", "CB AMAZON EU",
                                                       "CB PHARMACIE CENTRALE", "RETRAIT DAB"]), -rnd.randint(800, 6000)))
        for day, label, cents in ops:
            if day > last:
                continue
            await db.execute(
                "INSERT INTO transactions (account_id, date, label, amount_cents, category_id, created_at) VALUES (?,?,?,?,?,?)",
                (cur, date(y, m, day).isoformat(), label, cents, _match_rule(label, rules), _now()))
        if 3 <= last:
            await _insert_transfer(cur, sav, date(y, m, 3).isoformat(), 20000, "Virement épargne", "", None)
    cats = {c["name"]: c["id"] for c in await _categories()}
    nxt = _shift_month(today.strftime("%Y-%m"), 1)
    for label, cents, day, cat, to in (("Loyer", -78000, 5, "Loyer / crédit", None), ("Salaire", 245000, 1, "Salaire", None),
                                      ("Épargne automatique", 20000, 3, None, sav)):
        await db.execute(
            "INSERT INTO recurring (account_id, to_account_id, label, amount_cents, category_id, freq, interval, anchor_day, next_date, active)"
            " VALUES (?,?,?,?,?,'monthly',1,?,?,1)", (cur, to, label, cents, cats.get(cat) if cat else None, day, f"{nxt}-{day:02d}"))
    for name, cents in (("Alimentation", 45000), ("Transports", 15000), ("Loisirs", 12000), ("Abonnements", 5000)):
        if name in cats:
            await db.execute("INSERT OR REPLACE INTO budgets (category_id, month, amount_cents) VALUES (?,?,?)", (cats[name], "", cents))
    return {"accounts": await _accounts()}
