from fastapi import APIRouter

router = APIRouter()

_counter = 0


@router.get("/count")
async def get_count():
    return {"count": _counter}


@router.post("/increment")
async def increment():
    global _counter
    _counter += 1
    return {"count": _counter}


@router.post("/decrement")
async def decrement():
    global _counter
    _counter -= 1
    return {"count": _counter}


@router.post("/reset")
async def reset():
    global _counter
    _counter = 0
    return {"count": _counter}
