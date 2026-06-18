from datetime import datetime
from fastapi import APIRouter
from pydantic import BaseModel

router = APIRouter()

_history: list[dict] = []
MAX_HIST = 100


class Calc(BaseModel):
    expression: str
    result: str


@router.get("/history")
async def get_history():
    return _history


@router.post("/history")
async def add_history(c: Calc):
    entry = {
        "id": len(_history),
        "expression": c.expression,
        "result": c.result,
        "ts": datetime.now().isoformat(timespec="seconds"),
    }
    _history.insert(0, entry)
    del _history[MAX_HIST:]
    return entry


@router.delete("/history")
async def clear_history():
    n = len(_history)
    _history.clear()
    return {"cleared": n}
