import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field, ValidationError, field_validator

from ledroide_sdk import Module

log = logging.getLogger("ledroide.mini-games")

module = Module(__file__)
router = module.router

GAME_RE = r"^(_hub|[a-z0-9][a-z0-9-]{0,31})$"
VARIANT_RE = r"^[A-Za-z0-9_.:-]{0,40}$"
KEEP_SCORES = 50
MAX_STATE = 3_000_000
MAX_PREFS = 6_000_000

_ready = False
_lock = asyncio.Lock()
_stats_lock = asyncio.Lock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS scores (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    game        TEXT    NOT NULL,
    variant     TEXT    NOT NULL DEFAULT '',
    score       REAL    NOT NULL,
    lower_better INTEGER NOT NULL DEFAULT 0,
    player      TEXT    NOT NULL DEFAULT '',
    meta        TEXT    NOT NULL DEFAULT '{}',
    created_at  TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_scores_game ON scores(game, variant);
CREATE TABLE IF NOT EXISTS saves (
    game        TEXT PRIMARY KEY,
    state       TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS stats (
    game        TEXT PRIMARY KEY,
    data        TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS prefs (
    game        TEXT PRIMARY KEY,
    data        TEXT NOT NULL
);
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


def _parse(model, payload):
    try:
        return model.model_validate(payload or {}), None
    except ValidationError as exc:
        return None, _err(_validation_message(exc), "VALIDATION_ERROR")


def _loads(s: Optional[str], default):
    try:
        return json.loads(s) if s else default
    except ValueError:
        return default


def _order(lower_better: bool) -> str:
    return "score ASC, id ASC" if lower_better else "score DESC, id ASC"


# ── Models ────────────────────────────────────────────────────────────────────

class GameIn(BaseModel):
    game: str = Field(pattern=GAME_RE)


class ScoresIn(GameIn):
    variant: str = Field("", pattern=VARIANT_RE)
    limit: int = Field(10, ge=1, le=KEEP_SCORES)
    lower_better: bool = False


class ScoreIn(GameIn):
    variant: str = Field("", pattern=VARIANT_RE)
    score: float
    lower_better: bool = False
    player: str = Field("", max_length=40)
    meta: dict[str, Any] = Field(default_factory=dict)

    @field_validator("meta")
    @classmethod
    def _meta_size(cls, v):
        if len(json.dumps(v)) > 4000:
            raise ValueError("Métadonnées trop volumineuses")
        return v


class ClearScoresIn(GameIn):
    variant: Optional[str] = Field(None, pattern=VARIANT_RE)


class SaveIn(GameIn):
    state: Any


class StatsIn(GameIn):
    inc: dict[str, int | float] = Field(default_factory=dict)
    max: dict[str, int | float] = Field(default_factory=dict)
    min: dict[str, int | float] = Field(default_factory=dict)
    set: dict[str, Any] = Field(default_factory=dict)


class PrefsIn(GameIn):
    data: dict[str, Any]


class ResetIn(GameIn):
    what: Literal["all", "scores", "stats", "save"] = "all"


# ── Hub ───────────────────────────────────────────────────────────────────────

@module.on("hub.summary")
async def hub_summary(payload: dict):
    db = await _db()
    games: dict[str, dict] = {}

    def g(name: str) -> dict:
        return games.setdefault(name, {"best": {}, "has_save": False, "stats": {}})

    rows = await db.fetch_all(
        "SELECT game, variant, lower_better, "
        "CASE WHEN lower_better THEN MIN(score) ELSE MAX(score) END AS best, COUNT(*) AS n "
        "FROM scores GROUP BY game, variant, lower_better"
    )
    for r in rows:
        g(r["game"])["best"][r["variant"]] = {"score": r["best"], "count": r["n"], "lower_better": bool(r["lower_better"])}
    for r in await db.fetch_all("SELECT game, updated_at FROM saves"):
        g(r["game"])["has_save"] = True
        g(r["game"])["saved_at"] = r["updated_at"]
    for r in await db.fetch_all("SELECT game, data FROM stats"):
        g(r["game"])["stats"] = _loads(r["data"], {})
    hub = await db.fetch_one("SELECT data FROM prefs WHERE game = '_hub'")
    return {"games": games, "prefs": _loads(hub["data"] if hub else None, {})}


# ── Scores ────────────────────────────────────────────────────────────────────

@module.on("scores.list")
async def scores_list(payload: dict):
    p, err = _parse(ScoresIn, payload)
    if err:
        return err
    db = await _db()
    rows = await db.fetch_all(
        f"SELECT id, score, player, meta, created_at FROM scores WHERE game = ? AND variant = ? "
        f"ORDER BY {_order(p.lower_better)} LIMIT ?",
        (p.game, p.variant, p.limit),
    )
    return {"scores": [{**dict(r), "meta": _loads(r["meta"], {})} for r in rows]}


@module.on("score.submit")
async def score_submit(payload: dict):
    p, err = _parse(ScoreIn, payload)
    if err:
        return err
    db = await _db()
    row = await db.fetch_one(
        "INSERT INTO scores (game, variant, score, lower_better, player, meta, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?) RETURNING id",
        (p.game, p.variant, p.score, int(p.lower_better), p.player.strip(), json.dumps(p.meta), _now()),
    )
    new_id = row["id"]
    ranked = await db.fetch_all(
        f"SELECT id FROM scores WHERE game = ? AND variant = ? ORDER BY {_order(p.lower_better)}",
        (p.game, p.variant),
    )
    ids = [r["id"] for r in ranked]
    stale = ids[KEEP_SCORES:]
    if stale:
        await db.execute(f"DELETE FROM scores WHERE id IN ({','.join('?' * len(stale))})", stale)
    rank = ids.index(new_id) + 1 if new_id in ids[:KEEP_SCORES] else None
    return {"id": new_id, "rank": rank, "record": rank == 1, "total": min(len(ids), KEEP_SCORES)}


@module.on("scores.clear")
async def scores_clear(payload: dict):
    p, err = _parse(ClearScoresIn, payload)
    if err:
        return err
    db = await _db()
    if p.variant is None:
        n = await db.execute("DELETE FROM scores WHERE game = ?", (p.game,))
    else:
        n = await db.execute("DELETE FROM scores WHERE game = ? AND variant = ?", (p.game, p.variant))
    return {"deleted": n}


# ── Saves ─────────────────────────────────────────────────────────────────────

@module.on("save.get")
async def save_get(payload: dict):
    p, err = _parse(GameIn, payload)
    if err:
        return err
    db = await _db()
    row = await db.fetch_one("SELECT state, updated_at FROM saves WHERE game = ?", (p.game,))
    if not row:
        return {"state": None}
    return {"state": _loads(row["state"], None), "updated_at": row["updated_at"]}


@module.on("save.set")
async def save_set(payload: dict):
    p, err = _parse(SaveIn, payload)
    if err:
        return err
    if p.state is None:
        return await save_clear({"game": p.game})
    data = json.dumps(p.state, separators=(",", ":"))
    if len(data) > MAX_STATE:
        return _err("Sauvegarde trop volumineuse", "TOO_LARGE")
    db = await _db()
    await db.execute(
        "INSERT INTO saves (game, state, updated_at) VALUES (?, ?, ?) "
        "ON CONFLICT(game) DO UPDATE SET state = excluded.state, updated_at = excluded.updated_at",
        (p.game, data, _now()),
    )
    return {"ok": True}


@module.on("save.clear")
async def save_clear(payload: dict):
    p, err = _parse(GameIn, payload)
    if err:
        return err
    db = await _db()
    await db.execute("DELETE FROM saves WHERE game = ?", (p.game,))
    return {"ok": True}


# ── Stats ─────────────────────────────────────────────────────────────────────

@module.on("stats.get")
async def stats_get(payload: dict):
    p, err = _parse(GameIn, payload)
    if err:
        return err
    db = await _db()
    row = await db.fetch_one("SELECT data FROM stats WHERE game = ?", (p.game,))
    return {"stats": _loads(row["data"] if row else None, {})}


@module.on("stats.update")
async def stats_update(payload: dict):
    p, err = _parse(StatsIn, payload)
    if err:
        return err
    db = await _db()
    # Read-modify-write: serialised so concurrent WS calls don't lose increments.
    async with _stats_lock:
        row = await db.fetch_one("SELECT data FROM stats WHERE game = ?", (p.game,))
        data = _loads(row["data"] if row else None, {})
        for k, v in p.inc.items():
            cur = data.get(k)
            data[k] = (cur if isinstance(cur, (int, float)) else 0) + v
        for k, v in p.max.items():
            cur = data.get(k)
            data[k] = v if not isinstance(cur, (int, float)) else max(cur, v)
        for k, v in p.min.items():
            cur = data.get(k)
            data[k] = v if not isinstance(cur, (int, float)) else min(cur, v)
        data.update(p.set)
        blob = json.dumps(data)
        if len(blob) > 200_000:
            return _err("Statistiques trop volumineuses", "TOO_LARGE")
        await db.execute(
            "INSERT INTO stats (game, data) VALUES (?, ?) ON CONFLICT(game) DO UPDATE SET data = excluded.data",
            (p.game, blob),
        )
    return {"stats": data}


# ── Prefs ─────────────────────────────────────────────────────────────────────

@module.on("prefs.get")
async def prefs_get(payload: dict):
    p, err = _parse(GameIn, payload)
    if err:
        return err
    db = await _db()
    row = await db.fetch_one("SELECT data FROM prefs WHERE game = ?", (p.game,))
    return {"prefs": _loads(row["data"] if row else None, {})}


@module.on("prefs.set")
async def prefs_set(payload: dict):
    p, err = _parse(PrefsIn, payload)
    if err:
        return err
    blob = json.dumps(p.data)
    if len(blob) > MAX_PREFS:
        return _err("Préférences trop volumineuses", "TOO_LARGE")
    db = await _db()
    await db.execute(
        "INSERT INTO prefs (game, data) VALUES (?, ?) ON CONFLICT(game) DO UPDATE SET data = excluded.data",
        (p.game, blob),
    )
    return {"ok": True}


@module.on("game.reset")
async def game_reset(payload: dict):
    p, err = _parse(ResetIn, payload)
    if err:
        return err
    db = await _db()
    if p.what in ("all", "scores"):
        await db.execute("DELETE FROM scores WHERE game = ?", (p.game,))
    if p.what in ("all", "stats"):
        await db.execute("DELETE FROM stats WHERE game = ?", (p.game,))
    if p.what in ("all", "save"):
        await db.execute("DELETE FROM saves WHERE game = ?", (p.game,))
    log.info("reset %s (%s)", p.game, p.what)
    return {"ok": True}
