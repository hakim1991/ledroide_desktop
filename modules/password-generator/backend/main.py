import math
import secrets
import string
from typing import Annotated

from fastapi import Query
from ledroide_sdk import Module, router, events, config

module = Module()

_history: list[dict] = []

SYMBOLS = "!@#$%^&*()_+-=[]{}|;:,.<>?"


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
        return {"label": "Très faible", "level": 1, "color": "#ef4444"}
    if entropy < 40:
        return {"label": "Faible",      "level": 2, "color": "#f97316"}
    if entropy < 60:
        return {"label": "Moyen",       "level": 3, "color": "#eab308"}
    if entropy < 80:
        return {"label": "Fort",        "level": 4, "color": "#3b82f6"}
    return     {"label": "Très fort",   "level": 5, "color": "#22c55e"}


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
        return {"error": "Sélectionnez au moins un type de caractère."}

    # secrets.choice is cryptographically secure
    password = "".join(secrets.choice(charset) for _ in range(length))
    entropy  = entropy_bits(length, len(charset))
    info     = strength(entropy)

    entry = {
        "password": password,
        "length":   length,
        "entropy":  entropy,
        "strength": info,
        "charset_size": len(charset),
    }

    history_size = config.get("history_size", 10)
    _history.insert(0, entry)
    del _history[history_size:]

    await events.emit("generated", entry)
    return entry


@router.get("/history")
async def get_history():
    return _history


@router.delete("/history")
async def clear_history():
    _history.clear()
    return {"cleared": True}
