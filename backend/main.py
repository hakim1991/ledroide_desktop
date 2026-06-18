import os
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
import uvicorn

from core.registry import ModuleRegistry

PORT = int(os.environ.get("LEDROIDE_BACKEND_PORT", 8765))

app = FastAPI(title="LedroideDesktop", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

registry = ModuleRegistry(app)


@app.on_event("startup")
async def startup():
    await registry.load_all()


# ── Module management ────────────────────────────────────────────────────────

@app.get("/modules")
async def list_modules():
    return registry.list_modules()


@app.post("/modules/{module_id}/activate")
async def activate_module(module_id: str):
    return await registry.activate(module_id)


@app.post("/modules/{module_id}/deactivate")
async def deactivate_module(module_id: str):
    return await registry.deactivate(module_id)


@app.post("/modules/{module_id}/install")
async def install_module(module_id: str):
    return await registry.install(module_id)


@app.delete("/modules/{module_id}")
async def uninstall_module(module_id: str):
    return await registry.uninstall(module_id)


@app.get("/modules/{module_id}/config")
async def get_config(module_id: str):
    return registry.get_config(module_id)


@app.put("/modules/{module_id}/config")
async def set_config(module_id: str, config: dict):
    return registry.set_config(module_id, config)


# ── WebSocket IPC ────────────────────────────────────────────────────────────

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    registry.add_connection(websocket)
    try:
        while True:
            data = await websocket.receive_json()
            await registry.handle_message(websocket, data)
    except WebSocketDisconnect:
        registry.remove_connection(websocket)
    except Exception as exc:
        registry.remove_connection(websocket)
        print(f"[WS] error: {exc}")


if __name__ == "__main__":
    uvicorn.run("main:app", host="127.0.0.1", port=PORT, reload=False)
