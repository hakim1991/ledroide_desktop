import asyncio
import csv
import html
import io
import json
import logging
import random
import re
from datetime import date, datetime, timedelta
from html.parser import HTMLParser
from typing import Optional

from pydantic import BaseModel, Field, ValidationError, field_validator

from ledroide_sdk import Module

log = logging.getLogger("ledroide.flashcards")

module = Module(__file__)
router = module.router

COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
IMG_RE = re.compile(r"^data:image/(png|jpeg|gif|webp);base64,[A-Za-z0-9+/=]+$")
MAX_FIELD = 3_000_000
MAX_IMPORT_CHARS = 10_000_000
MAX_IMPORT_ROWS = 20_000
FMT = "%Y-%m-%d %H:%M:%S"

LEARN_STEPS = (1, 10)
RELEARN_STEPS = (10,)
GRADUATING_IVL = 1
EASY_IVL = 4
START_EASE = 2.5
MIN_EASE = 1.3
HARD_FACTOR = 1.2
EASY_BONUS = 1.3
LAPSE_FACTOR = 0.5
MAX_IVL = 36500
MATURE_IVL = 21

_ready = False
_lock = asyncio.Lock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS decks (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT    NOT NULL,
    description   TEXT    NOT NULL DEFAULT '',
    color         TEXT    NOT NULL DEFAULT '#6366f1',
    new_per_day   INTEGER NOT NULL DEFAULT 20,
    max_reviews   INTEGER NOT NULL DEFAULT 200,
    type_answer   INTEGER NOT NULL DEFAULT 0,
    position      INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT    NOT NULL
);
CREATE TABLE IF NOT EXISTS cards (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    deck_id     INTEGER NOT NULL REFERENCES decks(id) ON DELETE CASCADE,
    front       TEXT    NOT NULL,
    back        TEXT    NOT NULL,
    front_text  TEXT    NOT NULL DEFAULT '',
    back_text   TEXT    NOT NULL DEFAULT '',
    tags        TEXT    NOT NULL DEFAULT '',
    state       TEXT    NOT NULL DEFAULT 'new',
    due         TEXT,
    ivl         REAL    NOT NULL DEFAULT 0,
    ease        REAL    NOT NULL DEFAULT 2.5,
    reps        INTEGER NOT NULL DEFAULT 0,
    lapses      INTEGER NOT NULL DEFAULT 0,
    step        INTEGER NOT NULL DEFAULT 0,
    last_review TEXT,
    suspended   INTEGER NOT NULL DEFAULT 0,
    new_pos     INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT    NOT NULL,
    updated_at  TEXT    NOT NULL
);
CREATE TABLE IF NOT EXISTS reviews (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    card_id    INTEGER NOT NULL,
    deck_id    INTEGER NOT NULL,
    ts         TEXT    NOT NULL,
    day        TEXT    NOT NULL,
    rating     INTEGER NOT NULL,
    prev_state TEXT    NOT NULL,
    ivl        REAL    NOT NULL,
    ease       REAL    NOT NULL,
    elapsed_ms INTEGER NOT NULL DEFAULT 0,
    snapshot   TEXT    NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_cards_deck ON cards(deck_id, state, due);
CREATE INDEX IF NOT EXISTS idx_reviews_day ON reviews(day);
CREATE INDEX IF NOT EXISTS idx_reviews_deck ON reviews(deck_id, day);
"""

CARD_COLS = ("state", "due", "ivl", "ease", "reps", "lapses", "step", "last_review", "suspended")


async def _db():
    global _ready
    if not _ready:
        async with _lock:
            if not _ready:
                await module.db.executescript(SCHEMA)
                _ready = True
    await module.db.execute("PRAGMA foreign_keys = ON")
    return module.db


def _now() -> datetime:
    return datetime.now().replace(microsecond=0)


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


# ── Sanitizing ────────────────────────────────────────────────────────────────

ALLOWED = {"b", "strong", "i", "em", "u", "s", "br", "div", "p", "ul", "ol", "li", "img", "sub", "sup", "code", "pre",
           "mark", "h3", "h4", "blockquote", "span"}
VOID = {"br", "img"}


class _Sanitizer(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self.text: list[str] = []
        self.stack: list[str] = []
        self.skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "head", "title"):
            self.skip += 1
            return
        if self.skip or tag not in ALLOWED:
            if tag in ("br", "p", "div", "li", "tr"):
                self.text.append(" ")
            return
        if tag == "img":
            src = dict(attrs).get("src") or ""
            if IMG_RE.match(src):
                self.out.append(f'<img src="{src}">')
                self.text.append(" [image] ")
            return
        if tag == "br":
            self.out.append("<br>")
            self.text.append(" ")
            return
        self.out.append(f"<{tag}>")
        self.stack.append(tag)
        if tag in ("div", "p", "li"):
            self.text.append(" ")

    def handle_endtag(self, tag):
        if tag in ("script", "style", "head", "title"):
            self.skip = max(0, self.skip - 1)
            return
        if self.skip or tag not in ALLOWED or tag in VOID or tag not in self.stack:
            return
        while self.stack:
            t = self.stack.pop()
            self.out.append(f"</{t}>")
            if t == tag:
                break

    def handle_data(self, data):
        if self.skip:
            return
        self.out.append(html.escape(data, quote=False))
        self.text.append(data)

    def result(self) -> tuple[str, str]:
        while self.stack:
            self.out.append(f"</{self.stack.pop()}>")
        return "".join(self.out).strip(), re.sub(r"\s+", " ", "".join(self.text)).strip()


def sanitize(raw: str) -> tuple[str, str]:
    p = _Sanitizer()
    p.feed(raw or "")
    p.close()
    return p.result()


def plain_to_html(text: str) -> str:
    return html.escape(text or "", quote=False).replace("\r\n", "\n").replace("\n", "<br>")


# ── Scheduler (SM-2, Anki-style) ──────────────────────────────────────────────

def _fuzz(ivl: float, seed: int) -> int:
    if ivl < 7:
        return max(1, round(ivl))
    spread = max(1, round(ivl * 0.05))
    return max(1, round(ivl) + random.Random(seed).randint(-spread, spread))


def schedule(card: dict, rating: int, now: datetime) -> dict:
    """Returns the new scheduling fields for `card` answered with rating 1=Encore 2=Difficile 3=Bien 4=Facile."""
    c = {k: card[k] for k in CARD_COLS}
    c["reps"] += 1
    c["last_review"] = now.strftime(FMT)
    seed = card["id"] * 7919 + c["reps"]
    if c["state"] in ("new", "learning", "relearning"):
        steps = RELEARN_STEPS if c["state"] == "relearning" else LEARN_STEPS
        if c["state"] == "new":
            c["state"], c["step"] = "learning", 0
        step = min(c["step"], len(steps) - 1)
        if rating == 1:
            c["step"] = 0
            c["due"] = (now + timedelta(minutes=steps[0])).strftime(FMT)
        elif rating == 2:
            delay = (steps[0] + steps[1]) / 2 if step == 0 and len(steps) > 1 else steps[step] * 1.5
            c["due"] = (now + timedelta(minutes=delay)).strftime(FMT)
        elif rating == 3 and step + 1 < len(steps):
            c["step"] = step + 1
            c["due"] = (now + timedelta(minutes=steps[step + 1])).strftime(FMT)
        else:
            if c["state"] == "relearning":
                ivl = max(1, c["ivl"]) * (1.5 if rating == 4 else 1)
            else:
                ivl = EASY_IVL if rating == 4 else GRADUATING_IVL
            c["state"], c["step"], c["ivl"] = "review", 0, float(_fuzz(ivl, seed))
            c["due"] = (datetime.combine(now.date(), datetime.min.time()) + timedelta(days=c["ivl"])).strftime(FMT)
        return c
    ivl = max(1.0, c["ivl"])
    due = datetime.strptime(c["due"], FMT) if c["due"] else now
    delay = max(0, (now.date() - due.date()).days)
    if rating == 1:
        c["lapses"] += 1
        c["ease"] = max(MIN_EASE, c["ease"] - 0.2)
        c["ivl"] = float(max(1, round(ivl * LAPSE_FACTOR)))
        c["state"], c["step"] = "relearning", 0
        c["due"] = (now + timedelta(minutes=RELEARN_STEPS[0])).strftime(FMT)
        return c
    if rating == 2:
        new = max(ivl + 1, ivl * HARD_FACTOR)
        c["ease"] = max(MIN_EASE, c["ease"] - 0.15)
    elif rating == 3:
        new = max(ivl + 1, (ivl + delay / 2) * c["ease"])
    else:
        new = max(ivl + 2, (ivl + delay) * c["ease"] * EASY_BONUS)
        c["ease"] += 0.15
    c["ivl"] = float(min(MAX_IVL, _fuzz(new, seed)))
    c["due"] = (datetime.combine(now.date(), datetime.min.time()) + timedelta(days=c["ivl"])).strftime(FMT)
    return c


def _fmt_delay(card: dict, new: dict, now: datetime) -> str:
    if new["state"] == "review":
        d = new["ivl"]
        if d < 31:
            return f"{int(d)} j"
        if d < 365:
            return f"{d / 30.4:.1f} mois".replace(".0 ", " ").replace(".", ",")
        return f"{d / 365:.1f} an".replace(".", ",") + ("s" if d >= 730 else "")
    mins = (datetime.strptime(new["due"], FMT) - now).total_seconds() / 60
    return f"{max(1, round(mins))} min" if mins < 60 else f"{mins / 60:.1f} h".replace(".", ",")


def previews(card: dict, now: datetime) -> dict:
    return {str(r): _fmt_delay(card, schedule(card, r, now), now) for r in (1, 2, 3, 4)}


# ── Models ────────────────────────────────────────────────────────────────────

class DeckIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    description: str = Field("", max_length=2000)
    color: str = "#6366f1"
    new_per_day: int = Field(20, ge=0, le=9999)
    max_reviews: int = Field(200, ge=0, le=99999)
    type_answer: bool = False

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("Le nom est requis")
        return v

    @field_validator("color")
    @classmethod
    def _color(cls, v: str) -> str:
        if not COLOR_RE.match(v):
            raise ValueError("Couleur invalide (#rrggbb)")
        return v


def _card_fields(raw: dict) -> tuple[Optional[tuple], Optional[str]]:
    front, back = str(raw.get("front") or ""), str(raw.get("back") or "")
    if len(front) > MAX_FIELD or len(back) > MAX_FIELD:
        return None, "Carte trop volumineuse (images trop lourdes ?)"
    fh, ft = sanitize(front)
    bh, bt = sanitize(back)
    if not ft and "<img" not in fh:
        return None, "Le recto est vide"
    if not bt and "<img" not in bh:
        return None, "Le verso est vide"
    tags = " ".join(sorted({t.lower() for t in re.split(r"[\s,]+", str(raw.get("tags") or "")) if t}))[:500]
    return (fh, bh, ft[:2000], bt[:2000], tags), None


# ── Decks ─────────────────────────────────────────────────────────────────────

def _end_of_today(now: datetime) -> str:
    return datetime.combine(now.date(), datetime.max.time()).replace(microsecond=0).strftime(FMT)


async def _counts(deck_id: int, now: datetime) -> dict:
    db = await _db()
    today = now.date().isoformat()
    d = await db.fetch_one("SELECT * FROM decks WHERE id=?", (deck_id,))
    if not d:
        return {}
    done = await db.fetch_one(
        "SELECT SUM(prev_state='new') AS new_done, SUM(prev_state='review') AS rev_done, COUNT(*) AS total"
        " FROM reviews WHERE deck_id=? AND day=?", (deck_id, today))
    row = await db.fetch_one("""
        SELECT SUM(state='new' AND suspended=0) AS new_total,
               SUM(state IN ('learning','relearning') AND suspended=0 AND due <= ?) AS learn_now,
               SUM(state IN ('learning','relearning') AND suspended=0 AND due <= ?) AS learn_today,
               SUM(state='review' AND suspended=0 AND due <= ?) AS review_due,
               COUNT(*) AS total, SUM(suspended) AS suspended,
               SUM(state='review' AND ivl >= ?) AS mature
        FROM cards WHERE deck_id=?""", (now.strftime(FMT), _end_of_today(now), _end_of_today(now), MATURE_IVL, deck_id))
    new_left = max(0, d["new_per_day"] - (done["new_done"] or 0))
    rev_left = max(0, d["max_reviews"] - (done["rev_done"] or 0))
    return {"new": min(new_left, row["new_total"] or 0), "learning": row["learn_today"] or 0, "learn_now": row["learn_now"] or 0,
            "review": min(rev_left, row["review_due"] or 0), "total": row["total"] or 0, "suspended": row["suspended"] or 0,
            "new_total": row["new_total"] or 0, "mature": row["mature"] or 0, "done_today": done["total"] or 0}


async def _deck_view(d: dict, now: datetime) -> dict:
    return {**d, "type_answer": bool(d["type_answer"]), "counts": await _counts(d["id"], now)}


@module.on("decks.list")
async def decks_list(payload: dict):
    now = _now()
    rows = await (await _db()).fetch_all("SELECT * FROM decks ORDER BY position, id")
    return {"decks": [await _deck_view(d, now) for d in rows], "today": now.date().isoformat()}


@module.on("deck.save")
async def deck_save(payload: dict):
    try:
        d = DeckIn.model_validate(payload.get("deck") or {})
    except ValidationError as exc:
        return _err(_validation_message(exc))
    db = await _db()
    did = _int(payload.get("id"))
    vals = (d.name, d.description, d.color, d.new_per_day, d.max_reviews, int(d.type_answer))
    if did:
        if not await db.execute("UPDATE decks SET name=?, description=?, color=?, new_per_day=?, max_reviews=?, type_answer=? WHERE id=?",
                                (*vals, did)):
            return _err("Paquet introuvable", "NOT_FOUND")
    else:
        pos = (await db.fetch_one("SELECT COALESCE(MAX(position)+1, 0) AS p FROM decks"))["p"]
        did = (await db.fetch_one(
            "INSERT INTO decks (name, description, color, new_per_day, max_reviews, type_answer, position, created_at)"
            " VALUES (?,?,?,?,?,?,?,?) RETURNING id", (*vals, pos, _now().strftime(FMT))))["id"]
    return {"deck": await _deck_view(await db.fetch_one("SELECT * FROM decks WHERE id=?", (did,)), _now())}


@module.on("deck.delete")
async def deck_delete(payload: dict):
    db = await _db()
    did = _int(payload.get("id"))
    await db.execute("DELETE FROM cards WHERE deck_id=?", (did,))
    await db.execute("DELETE FROM reviews WHERE deck_id=?", (did,))
    if not await db.execute("DELETE FROM decks WHERE id=?", (did,)):
        return _err("Paquet introuvable", "NOT_FOUND")
    return {"ok": True}


# ── Cards ─────────────────────────────────────────────────────────────────────

def _card_public(c: dict, full: bool = True) -> dict:
    out = {k: c[k] for k in ("id", "deck_id", "front_text", "back_text", "tags", "state", "due", "ivl", "ease", "reps",
                             "lapses", "created_at", "updated_at")}
    out["suspended"] = bool(c["suspended"])
    if full:
        out["front"], out["back"] = c["front"], c["back"]
    return out


@module.on("cards.list")
async def cards_list(payload: dict):
    db = await _db()
    where, params = ["deck_id=?"], [_int(payload.get("deck_id"))]
    if q := str(payload.get("search") or "").strip():
        where.append("(front_text LIKE ? OR back_text LIKE ? OR tags LIKE ?)")
        params += [f"%{q}%"] * 3
    st = payload.get("state")
    if st in ("new", "learning", "review"):
        where.append("state IN ('learning','relearning')" if st == "learning" else "state=?")
        if st != "learning":
            params.append(st)
    elif st == "suspended":
        where.append("suspended=1")
    elif st == "due":
        where.append("suspended=0 AND state!='new' AND due <= ?")
        params.append(_end_of_today(_now()))
    order = {"created": "id DESC", "due": "state='new', due", "ease": "ease", "lapses": "lapses DESC"}.get(payload.get("sort"), "id DESC")
    limit = max(1, min(2000, _int(payload.get("limit"), 500)))
    sql = " AND ".join(where)
    total = (await db.fetch_one(f"SELECT COUNT(*) AS n FROM cards WHERE {sql}", tuple(params)))["n"]
    rows = await db.fetch_all(f"SELECT * FROM cards WHERE {sql} ORDER BY {order} LIMIT ?", (*params, limit))
    return {"cards": [_card_public(r, full=False) for r in rows], "total": total}


@module.on("card.get")
async def card_get(payload: dict):
    c = await (await _db()).fetch_one("SELECT * FROM cards WHERE id=?", (_int(payload.get("id")),))
    if not c:
        return _err("Carte introuvable", "NOT_FOUND")
    return {"card": _card_public(c)}


@module.on("card.save")
async def card_save(payload: dict):
    raw = payload.get("card") or {}
    fields, msg = _card_fields(raw)
    if msg:
        return _err(msg)
    db = await _db()
    now = _now().strftime(FMT)
    cid = _int(payload.get("id"))
    deck_id = _int(raw.get("deck_id"))
    if not await db.fetch_one("SELECT 1 AS x FROM decks WHERE id=?", (deck_id,)):
        return _err("Paquet introuvable", "NOT_FOUND")
    if cid:
        if not await db.execute(
                "UPDATE cards SET deck_id=?, front=?, back=?, front_text=?, back_text=?, tags=?, updated_at=? WHERE id=?",
                (deck_id, *fields, now, cid)):
            return _err("Carte introuvable", "NOT_FOUND")
        await db.execute("UPDATE reviews SET deck_id=? WHERE card_id=?", (deck_id, cid))
    else:
        pos = (await db.fetch_one("SELECT COALESCE(MAX(new_pos)+1, 0) AS p FROM cards WHERE deck_id=?", (deck_id,)))["p"]
        cid = (await db.fetch_one(
            "INSERT INTO cards (deck_id, front, back, front_text, back_text, tags, new_pos, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?) RETURNING id", (deck_id, *fields, pos, now, now)))["id"]
    return {"card": _card_public(await db.fetch_one("SELECT * FROM cards WHERE id=?", (cid,)))}


def _ids(payload: dict) -> list[int]:
    return [i for i in (_int(x) for x in (payload.get("ids") or [payload.get("id")])) if i]


@module.on("cards.delete")
async def cards_delete(payload: dict):
    ids = _ids(payload)
    if not ids:
        return _err("Aucune carte indiquée")
    marks = ",".join("?" * len(ids))
    db = await _db()
    n = await db.execute(f"DELETE FROM cards WHERE id IN ({marks})", tuple(ids))
    await db.execute(f"DELETE FROM reviews WHERE card_id IN ({marks})", tuple(ids))
    return {"deleted": n}


@module.on("cards.suspend")
async def cards_suspend(payload: dict):
    ids = _ids(payload)
    if not ids:
        return _err("Aucune carte indiquée")
    marks = ",".join("?" * len(ids))
    n = await (await _db()).execute(f"UPDATE cards SET suspended=? WHERE id IN ({marks})",
                                    (1 if payload.get("suspended", True) else 0, *ids))
    return {"updated": n}


@module.on("cards.reset")
async def cards_reset(payload: dict):
    ids = _ids(payload)
    if not ids:
        return _err("Aucune carte indiquée")
    marks = ",".join("?" * len(ids))
    n = await (await _db()).execute(
        f"UPDATE cards SET state='new', due=NULL, ivl=0, ease=?, reps=0, lapses=0, step=0, last_review=NULL WHERE id IN ({marks})",
        (START_EASE, *ids))
    return {"updated": n}


@module.on("cards.move")
async def cards_move(payload: dict):
    ids = _ids(payload)
    db = await _db()
    did = _int(payload.get("deck_id"))
    if not ids:
        return _err("Aucune carte indiquée")
    if not await db.fetch_one("SELECT 1 AS x FROM decks WHERE id=?", (did,)):
        return _err("Paquet introuvable", "NOT_FOUND")
    marks = ",".join("?" * len(ids))
    n = await db.execute(f"UPDATE cards SET deck_id=? WHERE id IN ({marks})", (did, *ids))
    await db.execute(f"UPDATE reviews SET deck_id=? WHERE card_id IN ({marks})", (did, *ids))
    return {"updated": n}


# ── Study ─────────────────────────────────────────────────────────────────────

async def _next_card(deck_id: int, now: datetime, exclude: int = 0) -> dict:
    db = await _db()
    counts = await _counts(deck_id, now)
    if not counts:
        return _err("Paquet introuvable", "NOT_FOUND")
    nows = now.strftime(FMT)
    card = await db.fetch_one(
        "SELECT * FROM cards WHERE deck_id=? AND suspended=0 AND state IN ('learning','relearning') AND due <= ? AND id != ?"
        " ORDER BY due LIMIT 1", (deck_id, nows, exclude))
    if not card and counts["review"]:
        card = await db.fetch_one(
            "SELECT * FROM cards WHERE deck_id=? AND suspended=0 AND state='review' AND due <= ? AND id != ? ORDER BY due, random() LIMIT 1",
            (deck_id, _end_of_today(now), exclude))
    if not card and counts["new"]:
        card = await db.fetch_one(
            "SELECT * FROM cards WHERE deck_id=? AND suspended=0 AND state='new' AND id != ? ORDER BY new_pos, id LIMIT 1",
            (deck_id, exclude))
    if not card and exclude:
        card = await db.fetch_one(
            "SELECT * FROM cards WHERE id=? AND suspended=0 AND ((state IN ('learning','relearning') AND due <= ?) OR (state='review' AND due <= ?))",
            (exclude, nows, _end_of_today(now)))
    if not card:
        nxt = await db.fetch_one(
            "SELECT MIN(due) AS d FROM cards WHERE deck_id=? AND suspended=0 AND state IN ('learning','relearning') AND due <= ?",
            (deck_id, _end_of_today(now)))
        return {"card": None, "counts": counts, "wait_until": nxt["d"] if nxt and nxt["d"] else None}
    return {"card": _card_public(card), "previews": previews(card, now), "counts": counts}


@module.on("study.next")
async def study_next(payload: dict):
    """Learning cards due now first, then today's reviews, then new cards (within the deck's daily limits)."""
    now = _now()
    if payload.get("learn_ahead"):
        now = now + timedelta(minutes=20)
    return await _next_card(_int(payload.get("deck_id")), now)


@module.on("study.answer")
async def study_answer(payload: dict):
    rating = _int(payload.get("rating"))
    if rating not in (1, 2, 3, 4):
        return _err("Réponse invalide (1 à 4)")
    db = await _db()
    card = await db.fetch_one("SELECT * FROM cards WHERE id=?", (_int(payload.get("card_id")),))
    if not card:
        return _err("Carte introuvable", "NOT_FOUND")
    now = _now()
    new = schedule(card, rating, now)
    snapshot = json.dumps({k: card[k] for k in CARD_COLS})
    await db.execute(f"UPDATE cards SET {', '.join(f'{k}=?' for k in CARD_COLS)} WHERE id=?",
                     (*[new[k] for k in CARD_COLS], card["id"]))
    await db.execute(
        "INSERT INTO reviews (card_id, deck_id, ts, day, rating, prev_state, ivl, ease, elapsed_ms, snapshot) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (card["id"], card["deck_id"], now.strftime(FMT), now.date().isoformat(), rating, card["state"], new["ivl"], new["ease"],
         max(0, min(3_600_000, _int(payload.get("elapsed_ms")))), snapshot))
    nxt = await _next_card(card["deck_id"], now, exclude=card["id"])
    return {**nxt, "answered": {"id": card["id"], "state": new["state"], "due": new["due"], "ivl": new["ivl"]}}


@module.on("study.undo")
async def study_undo(payload: dict):
    db = await _db()
    did = _int(payload.get("deck_id"))
    last = await db.fetch_one("SELECT * FROM reviews WHERE deck_id=? AND snapshot != '' ORDER BY id DESC LIMIT 1", (did,))
    if not last:
        return _err("Rien à annuler", "NOT_FOUND")
    snap = json.loads(last["snapshot"])
    if not await db.execute(f"UPDATE cards SET {', '.join(f'{k}=?' for k in CARD_COLS)} WHERE id=?",
                            (*[snap[k] for k in CARD_COLS], last["card_id"])):
        await db.execute("DELETE FROM reviews WHERE id=?", (last["id"],))
        return _err("Carte introuvable", "NOT_FOUND")
    await db.execute("DELETE FROM reviews WHERE id=?", (last["id"],))
    card = await db.fetch_one("SELECT * FROM cards WHERE id=?", (last["card_id"],))
    now = _now()
    return {"card": _card_public(card), "previews": previews(card, now), "counts": await _counts(did, now)}


# ── Stats ─────────────────────────────────────────────────────────────────────

@module.on("stats")
async def stats(payload: dict):
    db = await _db()
    now = _now()
    days = max(7, min(365, _int(payload.get("days"), 30)))
    did = _int(payload.get("deck_id"))
    dsql, dp = ("AND deck_id=?", (did,)) if did else ("", ())
    since = (now.date() - timedelta(days=days - 1)).isoformat()
    per_day = {r["day"]: r for r in await db.fetch_all(
        f"""SELECT day, COUNT(*) AS n, SUM(rating=1) AS again, SUM(rating=2) AS hard, SUM(rating=3) AS good, SUM(rating=4) AS easy,
            SUM(prev_state='new') AS new, SUM(elapsed_ms) AS ms FROM reviews WHERE day >= ? {dsql} GROUP BY day""", (since, *dp))}
    series = []
    for i in range(days):
        d = (now.date() - timedelta(days=days - 1 - i)).isoformat()
        r = per_day.get(d) or {}
        series.append({"day": d, "n": r.get("n", 0), "again": r.get("again", 0) or 0, "hard": r.get("hard", 0) or 0,
                       "good": r.get("good", 0) or 0, "easy": r.get("easy", 0) or 0, "new": r.get("new", 0) or 0,
                       "minutes": round((r.get("ms") or 0) / 60000, 1)})
    ret = await db.fetch_one(
        f"""SELECT COUNT(*) AS n, SUM(rating > 1) AS ok FROM reviews WHERE prev_state='review' AND day >= ? {dsql}""", (since, *dp))
    mature_ret = await db.fetch_one(
        f"""SELECT COUNT(*) AS n, SUM(r.rating > 1) AS ok FROM reviews r JOIN cards c ON c.id = r.card_id
            WHERE r.prev_state='review' AND r.day >= ? AND c.ivl >= {MATURE_IVL} {dsql.replace('deck_id', 'r.deck_id')}""", (since, *dp))
    states = await db.fetch_one(
        f"""SELECT SUM(state='new') AS new, SUM(state IN ('learning','relearning')) AS learning,
            SUM(state='review' AND ivl < {MATURE_IVL}) AS young, SUM(state='review' AND ivl >= {MATURE_IVL}) AS mature,
            SUM(suspended) AS suspended, COUNT(*) AS total, AVG(CASE WHEN state='review' THEN ease END) AS ease
            FROM cards WHERE 1=1 {dsql}""", dp)
    forecast_days = 30
    fc = {r["d"]: r["n"] for r in await db.fetch_all(
        f"""SELECT substr(due, 1, 10) AS d, COUNT(*) AS n FROM cards WHERE suspended=0 AND state != 'new' AND due IS NOT NULL
            AND due <= ? {dsql} GROUP BY d""", ((now.date() + timedelta(days=forecast_days)).isoformat() + " 23:59:59", *dp))}
    today = now.date().isoformat()
    overdue = sum(n for d, n in fc.items() if d < today)
    forecast = [{"day": (now.date() + timedelta(days=i)).isoformat(),
                 "n": fc.get((now.date() + timedelta(days=i)).isoformat(), 0) + (overdue if i == 0 else 0)} for i in range(forecast_days)]
    streak = 0
    all_days = {r["day"] for r in await db.fetch_all(f"SELECT DISTINCT day FROM reviews WHERE 1=1 {dsql}", dp)}
    d = now.date() if today in all_days else now.date() - timedelta(days=1)
    while d.isoformat() in all_days:
        streak += 1
        d -= timedelta(days=1)
    total_reviews = (await db.fetch_one(f"SELECT COUNT(*) AS n FROM reviews WHERE 1=1 {dsql}", dp))["n"]
    return {"series": series, "forecast": forecast, "streak": streak, "total_reviews": total_reviews,
            "retention": round(ret["ok"] / ret["n"], 3) if ret["n"] else None, "retention_n": ret["n"],
            "mature_retention": round(mature_ret["ok"] / mature_ret["n"], 3) if mature_ret["n"] else None,
            "states": {k: states[k] or 0 for k in ("new", "learning", "young", "mature", "suspended", "total")},
            "avg_ease": round(states["ease"], 2) if states["ease"] else None}


# ── Import / export ───────────────────────────────────────────────────────────

SEPARATORS = {"tab": "\t", ";": ";", ",": ",", "|": "|"}


def _rows(text: str, sep: str) -> list[list[str]]:
    reader = csv.reader(io.StringIO(text), delimiter=sep)
    out = []
    for row in reader:
        if any(c.strip() for c in row):
            out.append(row)
        if len(out) > MAX_IMPORT_ROWS + 1:
            break
    return out


@module.on("import.preview")
async def import_preview(payload: dict):
    text = str(payload.get("text") or "").lstrip("﻿")
    if not text.strip():
        return _err("Fichier vide")
    if len(text) > MAX_IMPORT_CHARS:
        return _err("Fichier trop volumineux")
    sep = payload.get("separator")
    if sep not in SEPARATORS:
        sample = "\n".join(text.splitlines()[:20])
        sep = max(SEPARATORS, key=lambda k: sample.count(SEPARATORS[k]))
    rows = _rows(text, SEPARATORS[sep])
    return {"separator": sep, "rows": [r[:10] for r in rows[:15]], "row_count": len(rows),
            "columns": max((len(r) for r in rows[:50]), default=0)}


@module.on("import.commit")
async def import_commit(payload: dict):
    text = str(payload.get("text") or "").lstrip("﻿")
    sep = payload.get("separator")
    if sep not in SEPARATORS:
        return _err("Séparateur invalide")
    if len(text) > MAX_IMPORT_CHARS:
        return _err("Fichier trop volumineux")
    db = await _db()
    did = _int(payload.get("deck_id"))
    if not await db.fetch_one("SELECT 1 AS x FROM decks WHERE id=?", (did,)):
        return _err("Paquet introuvable", "NOT_FOUND")
    cf, cb = _int(payload.get("front_col"), 0), _int(payload.get("back_col"), 1)
    ct = payload.get("tags_col")
    ct = None if ct in (None, "") else _int(ct, -1)
    if cf == cb or cf < 0 or cb < 0:
        return _err("Choisissez deux colonnes différentes pour le recto et le verso")
    allow_html = bool(payload.get("html"))
    rows = _rows(text, SEPARATORS[sep])
    if payload.get("has_header"):
        rows = rows[1:]
    existing = {r["front_text"] for r in await db.fetch_all("SELECT front_text FROM cards WHERE deck_id=?", (did,))}
    skip_dup = payload.get("skip_duplicates", True)
    added = dup = bad = 0
    now = _now().strftime(FMT)
    pos = (await db.fetch_one("SELECT COALESCE(MAX(new_pos)+1, 0) AS p FROM cards WHERE deck_id=?", (did,)))["p"]
    for r in rows[:MAX_IMPORT_ROWS]:
        get = lambda i: r[i].strip() if i is not None and 0 <= i < len(r) else ""
        front, back = get(cf), get(cb)
        if not allow_html:
            front, back = plain_to_html(front), plain_to_html(back)
        fields, msg = _card_fields({"front": front, "back": back, "tags": get(ct)})
        if msg:
            bad += 1
            continue
        if skip_dup and fields[2] in existing:
            dup += 1
            continue
        existing.add(fields[2])
        await db.execute(
            "INSERT INTO cards (deck_id, front, back, front_text, back_text, tags, new_pos, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (did, *fields, pos, now, now))
        pos += 1
        added += 1
    log.info("Import : %d carte(s) ajoutée(s), %d doublon(s), %d invalide(s)", added, dup, bad)
    return {"added": added, "duplicates": dup, "invalid": bad}


@module.on("export")
async def export(payload: dict):
    db = await _db()
    did = _int(payload.get("deck_id"))
    deck = await db.fetch_one("SELECT * FROM decks WHERE id=?", (did,))
    if not deck:
        return _err("Paquet introuvable", "NOT_FOUND")
    sep = SEPARATORS.get(payload.get("separator"), "\t")
    as_html = bool(payload.get("html"))
    rows = await db.fetch_all("SELECT front, back, front_text, back_text, tags FROM cards WHERE deck_id=? ORDER BY new_pos, id", (did,))
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=sep, lineterminator="\r\n")
    if payload.get("header", True):
        w.writerow(["Recto", "Verso", "Étiquettes"])
    for r in rows:
        if as_html:
            w.writerow([r["front"], r["back"], r["tags"]])
        else:
            w.writerow([_html_to_text(r["front"]), _html_to_text(r["back"]), r["tags"]])
    return {"text": buf.getvalue(), "count": len(rows), "name": deck["name"]}


def _html_to_text(h: str) -> str:
    h = re.sub(r"<br\s*/?>|</(div|p|li)>", "\n", h)
    h = re.sub(r"<img[^>]*>", "[image]", h)
    return html.unescape(re.sub(r"<[^>]+>", "", h)).strip()


@module.on("demo.seed")
async def demo_seed(payload: dict):
    deck = (await deck_save({"deck": {"name": "Capitales européennes", "description": "Paquet d'exemple", "color": "#38bdf8",
                                      "new_per_day": 10}}))["deck"]
    pairs = [("France", "Paris"), ("Allemagne", "Berlin"), ("Italie", "Rome"), ("Espagne", "Madrid"), ("Portugal", "Lisbonne"),
             ("Belgique", "Bruxelles"), ("Pays-Bas", "Amsterdam"), ("Autriche", "Vienne"), ("Pologne", "Varsovie"),
             ("Suède", "Stockholm"), ("Norvège", "Oslo"), ("Finlande", "Helsinki"), ("Danemark", "Copenhague"),
             ("Grèce", "Athènes"), ("Hongrie", "Budapest"), ("Tchéquie", "Prague"), ("Irlande", "Dublin"), ("Roumanie", "Bucarest")]
    for country, capital in pairs:
        await card_save({"card": {"deck_id": deck["id"], "front": f"Capitale de <b>{html.escape(country)}</b> ?", "back": capital,
                                  "tags": "géographie europe"}})
    deck2 = (await deck_save({"deck": {"name": "Vocabulaire anglais", "color": "#f59e0b", "type_answer": True}}))["deck"]
    for fr, en in [("le chat", "cat"), ("la maison", "house"), ("le livre", "book"), ("courir", "to run"), ("heureux", "happy"),
                   ("la pomme", "apple"), ("demain", "tomorrow"), ("apprendre", "to learn")]:
        await card_save({"card": {"deck_id": deck2["id"], "front": f"<i>{fr}</i>", "back": en, "tags": "anglais"}})
    return await decks_list({})
