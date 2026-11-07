import asyncio
import json
import math
import secrets
import sqlite3
import string
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Annotated

from ledroide_sdk.http import APIRouter, Query
from pydantic import BaseModel
from ledroide_sdk import Module, config, data_path

router = APIRouter()
module = Module(__file__)

DB_PATH  = data_path(__file__, "pwgen.db")
SYMBOLS  = "!@#$%^&*()_+-=[]{}|;:,.<>?"

ACTION_ICONS = {
    "generated":       "✦",
    "copied":          "⎘",
    "history_cleared": "⊘",
    "logs_cleared":    "○",
    "error":           "⚠",
}


# ── Database ──────────────────────────────────────────────────────────────────

@contextmanager
def _db():
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _init():
    with _db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS history (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                password       TEXT    NOT NULL,
                length         INTEGER NOT NULL,
                entropy        REAL    NOT NULL,
                strength_label TEXT    NOT NULL,
                strength_level INTEGER NOT NULL,
                strength_color TEXT    NOT NULL,
                charset_size   INTEGER NOT NULL,
                ts             TEXT    NOT NULL DEFAULT (datetime('now', 'localtime'))
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS logs (
                id      INTEGER PRIMARY KEY AUTOINCREMENT,
                ts      TEXT    NOT NULL DEFAULT (datetime('now', 'localtime')),
                level   TEXT    NOT NULL,
                action  TEXT    NOT NULL,
                icon    TEXT    NOT NULL,
                details TEXT    NOT NULL DEFAULT '{}'
            )
        """)


_init()


# ── Helpers ───────────────────────────────────────────────────────────────────

def _history_limit() -> int:
    return config.get("history_size", 10)


def _default_length() -> int:
    return config.get("default_length", 16)


def build_charset(uppercase: bool, lowercase: bool, numbers: bool, symbols: bool) -> str:
    charset = ""
    if uppercase: charset += string.ascii_uppercase
    if lowercase: charset += string.ascii_lowercase
    if numbers:   charset += string.digits
    if symbols:   charset += SYMBOLS
    return charset


def entropy_bits(length: int, charset_size: int) -> float:
    if charset_size == 0:
        return 0.0
    return round(length * math.log2(charset_size), 1)


def strength(entropy: float) -> dict:
    if entropy < 28:
        return {"label": "Tres faible", "level": 1, "color": "#ef4444"}
    if entropy < 40:
        return {"label": "Faible",      "level": 2, "color": "#f97316"}
    if entropy < 60:
        return {"label": "Moyen",       "level": 3, "color": "#eab308"}
    if entropy < 80:
        return {"label": "Fort",        "level": 4, "color": "#3b82f6"}
    return     {"label": "Tres fort",   "level": 5, "color": "#22c55e"}


def _log_sync(level: str, action: str, **details):
    with _db() as conn:
        conn.execute(
            "INSERT INTO logs (level, action, icon, details) VALUES (?, ?, ?, ?)",
            (level, action, ACTION_ICONS.get(action, "·"), json.dumps(details)),
        )
        conn.execute("DELETE FROM logs WHERE id NOT IN (SELECT id FROM logs ORDER BY id DESC LIMIT 200)")


async def _log(level: str, action: str, **details):
    await asyncio.to_thread(_log_sync, level, action, **details)


def _row_to_history(row) -> dict:
    r = dict(row)
    r["strength"] = {
        "label": r.pop("strength_label"),
        "level": r.pop("strength_level"),
        "color": r.pop("strength_color"),
    }
    return r


def _row_to_log(row) -> dict:
    r = dict(row)
    r["details"] = json.loads(r["details"])
    return r


# ── Password generation ───────────────────────────────────────────────────────

@router.get("/generate")
async def generate(
    length:    Annotated[int,  Query(ge=4, le=128)] = 16,
    uppercase: Annotated[bool, Query()] = True,
    lowercase: Annotated[bool, Query()] = True,
    numbers:   Annotated[bool, Query()] = True,
    symbols:   Annotated[bool, Query()] = False,
):
    charset = build_charset(uppercase, lowercase, numbers, symbols)
    if not charset:
        await _log("error", "error", reason="Aucun type de caractere selectionne")
        return {"error": "Selectionnez au moins un type de caractere."}

    password = "".join(secrets.choice(charset) for _ in range(length))
    entropy  = entropy_bits(length, len(charset))
    info     = strength(entropy)

    types = [
        *( ["maj"]      if uppercase else []),
        *( ["min"]      if lowercase else []),
        *( ["chiffres"] if numbers   else []),
        *( ["symboles"] if symbols   else []),
    ]

    hist_limit = _history_limit()

    def _save():
        with _db() as conn:
            conn.execute(
                "INSERT INTO history (password, length, entropy, strength_label, strength_level, strength_color, charset_size)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (password, length, entropy, info["label"], info["level"], info["color"], len(charset)),
            )
            conn.execute(
                f"DELETE FROM history WHERE id NOT IN (SELECT id FROM history ORDER BY id DESC LIMIT {hist_limit})"
            )

    await asyncio.to_thread(_save)
    await _log("info", "generated",
               length=length, entropy=entropy, strength=info["label"],
               charset_size=len(charset), types=", ".join(types))

    return {
        "password":     password,
        "length":       length,
        "entropy":      entropy,
        "strength":     info,
        "charset_size": len(charset),
    }


# ── History ───────────────────────────────────────────────────────────────────

@router.get("/history")
async def get_history():
    def _q():
        with _db() as conn:
            rows = conn.execute(
                "SELECT * FROM history ORDER BY id DESC"
            ).fetchall()
            return [_row_to_history(r) for r in rows]

    return await asyncio.to_thread(_q)


@router.delete("/history")
async def clear_history():
    def _q():
        with _db() as conn:
            n = conn.execute("SELECT COUNT(*) FROM history").fetchone()[0]
            conn.execute("DELETE FROM history")
            return n

    n = await asyncio.to_thread(_q)
    await _log("warning", "history_cleared", count=n)
    return {"cleared": True}


# ── Logs ──────────────────────────────────────────────────────────────────────

@router.get("/logs")
async def get_logs(limit: Annotated[int, Query(ge=1, le=200)] = 50):
    def _q():
        with _db() as conn:
            rows = conn.execute(
                "SELECT * FROM logs ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
            return [_row_to_log(r) for r in rows]

    return await asyncio.to_thread(_q)


@router.delete("/logs")
async def clear_logs():
    def _q():
        with _db() as conn:
            n = conn.execute("SELECT COUNT(*) FROM logs").fetchone()[0]
            conn.execute("DELETE FROM logs")
            return n

    n = await asyncio.to_thread(_q)
    await _log("info", "logs_cleared", previous_count=n)
    return {"cleared": True}


@router.post("/logs/copy")
async def log_copy(source: str = "current"):
    await _log("info", "copied", source=source)
    return {"ok": True}


# ── Config ────────────────────────────────────────────────────────────────────

@router.get("/config/defaults")
async def get_config_defaults():
    return {
        "default_length": config.get("default_length", 16),
        "history_size":   config.get("history_size",   10),
        "uppercase":      config.get("uppercase",       True),
        "lowercase":      config.get("lowercase",       True),
        "numbers":        config.get("numbers",         True),
        "symbols":        config.get("symbols",         False),
    }


class ConfigDefaults(BaseModel):
    default_length: int  = 16
    history_size:   int  = 10
    uppercase:      bool = True
    lowercase:      bool = True
    numbers:        bool = True
    symbols:        bool = False


@router.put("/config/defaults")
async def save_config_defaults(c: ConfigDefaults):
    config.set("default_length", c.default_length)
    config.set("history_size",   c.history_size)
    config.set("uppercase",      c.uppercase)
    config.set("lowercase",      c.lowercase)
    config.set("numbers",        c.numbers)
    config.set("symbols",        c.symbols)
    return {"saved": True}
