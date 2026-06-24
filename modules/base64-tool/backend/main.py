from ledroide_sdk import Module

module = Module(__file__)
router = module.router


@router.get("/ping")
async def ping():
    return {"ok": True}
