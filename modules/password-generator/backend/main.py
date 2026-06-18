import math
import secrets
import string
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Query

router = APIRouter()

_history: list[dict] = []
_logs:    list[dict] = []

SYMBOLS  = "!@#$%^&*()_+-=[]{}|;:,.<>?"
MAX_LOGS = 200

ACTION_ICONS = {
    "generated":       "✦",
    "copied":          "⎘",
    "history_cleared": "⊘",
    "logs_cleared":    "○",
    "error":           "⚠",
}


def _log(level: str, action: str, **details):
    _logs.insert(0, {
        "ts":      datetime.now().isoformat(timespec="seconds"),
        "level":   level,
        "action":  action,
        "icon":    ACTION_ICONS.get(action, "·"),
        "details": {k: v for k, v in details.items() if v is not None},
    })
    del _logs[MAX_LOGS:]


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
        _log("error", "error", reason="Aucun type de caractere selectionne")
        return {"error": "Selectionnez au moins un type de caractere."}

    password = "".join(secrets.choice(charset) for _ in range(length))
    entropy  = entropy_bits(length, len(charset))
    info     = strength(entropy)

    types = [
        *( ["maj"]     if uppercase else []),
        *( ["min"]     if lowercase else []),
        *( ["chiffres"] if numbers  else []),
        *( ["symboles"] if symbols  else []),
    ]

    _log("info", "generated",
         length=length,
         entropy=entropy,
         strength=info["label"],
         charset_size=len(charset),
         types=", ".join(types),
    )

    entry = {
        "password":     password,
        "length":       length,
        "entropy":      entropy,
        "strength":     info,
        "charset_size": len(charset),
    }

    _history.insert(0, entry)
    del _history[10:]

    return entry


# ── History ───────────────────────────────────────────────────────────────────

@router.get("/history")
async def get_history():
    return _history


@router.delete("/history")
async def clear_history():
    count = len(_history)
    _history.clear()
    _log("warning", "history_cleared", count=count)
    return {"cleared": True}


# ── Logs ──────────────────────────────────────────────────────────────────────

@router.get("/logs")
async def get_logs(limit: Annotated[int, Query(ge=1, le=200)] = 50):
    return _logs[:limit]


@router.delete("/logs")
async def clear_logs():
    count = len(_logs)
    _logs.clear()
    _log("info", "logs_cleared", previous_count=count)
    return {"cleared": True}


@router.post("/logs/copy")
async def log_copy(source: str = "current"):
    _log("info", "copied", source=source)
    return {"ok": True}
