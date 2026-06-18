from ledroide_sdk import Module, router, events, config

module = Module()

_counter = 0


@router.get("/count")
async def get_count():
    return {"count": _counter}


@router.post("/increment")
async def increment():
    global _counter
    step = config.get("step", 1)
    _counter += step
    await events.emit("count_updated", {"count": _counter})
    return {"count": _counter}


@router.post("/decrement")
async def decrement():
    global _counter
    step = config.get("step", 1)
    _counter -= step
    await events.emit("count_updated", {"count": _counter})
    return {"count": _counter}


@module.on("reset")
async def on_reset(payload):
    global _counter
    _counter = 0
    await events.emit("count_updated", {"count": _counter})
