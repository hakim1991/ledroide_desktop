import os
import sys

BACKEND_DIR = os.path.dirname(os.path.abspath(__file__))
for _p in (BACKEND_DIR, os.path.join(BACKEND_DIR, "sdk")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, Field
from starlette.requests import Request
from typing import Optional

from core import settings
from core.env import EnvPayload, EnvStore
from core.errors import install_handlers
from core.ipc import Connection, parse_call
from core.log import setup_logging
from core.packages import InstallPayload, PackageManager
from core.registry import SYSTEM_CHANNEL, ModuleRegistry, validate_module_id
from core.security import SecurityMiddleware

setup_logging()
log = logging.getLogger("ledroide")

BRIDGE_JS = (Path(BACKEND_DIR) / "core" / "bridge.js").read_text(encoding="utf-8")


@asynccontextmanager
async def lifespan(app: FastAPI):
    await registry.load_all()
    log.info("backend ready on 127.0.0.1:%s (data: %s)", settings.PORT, settings.DATA_DIR)
    if not settings.TOKEN:
        log.warning("LEDROIDE_TOKEN absent: management endpoints are unauthenticated (dev only)")
    yield
    await registry.shutdown()


app = FastAPI(title="LedroideDesktop", version="0.1.0", lifespan=lifespan)
install_handlers(app)

# Registered first so CORS (outermost) answers preflights before the security check.
app.add_middleware(SecurityMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=sorted(settings.SELF_ORIGINS) + settings.RENDERER_ORIGINS,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def no_cache_module_frontend(request: Request, call_next):
    response = await call_next(request)
    if "/frontend/" in request.url.path:
        response.headers["Cache-Control"] = "no-store"
    return response


env_store = EnvStore()
registry = ModuleRegistry(app)
packages = PackageManager(lambda event, payload: registry.broadcast(SYSTEM_CHANNEL, event, payload))


class InstallManyPayload(BaseModel):
    ids: list[str] = Field(min_length=1, max_length=200)


class ModuleMeta(BaseModel):
    category: Optional[str] = None
    tags: Optional[list[str]] = None


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


@app.get("/modules/catalog")
async def catalog_info():
    return registry.catalog_info()


@app.post("/modules/catalog/refresh")
async def catalog_refresh():
    return await registry.refresh_catalog(force=True)


@app.post("/modules/install")
async def install_modules(payload: InstallManyPayload):
    return await registry.install_many(payload.ids)


@app.post("/modules/{module_id}/install")
async def install_module(module_id: str):
    return await registry.install(module_id)


@app.delete("/modules/{module_id}")
async def uninstall_module(module_id: str):
    return await registry.uninstall(module_id)


@app.get("/modules/{module_id}/icon")
async def module_icon(module_id: str):
    validate_module_id(module_id)
    icon_path = registry.module_dir(module_id) / "icon.png"
    if not icon_path.is_file():
        return Response(status_code=404)
    return FileResponse(str(icon_path), media_type="image/png")


@app.post("/modules/{module_id}/deps")
async def install_deps(module_id: str, reinstall: bool = False):
    return await registry.install_deps(module_id, reinstall=reinstall)


@app.get("/modules/repair")
async def repair_status():
    return registry.repair_status()


@app.post("/modules/repair")
async def repair_modules(reinstall: bool = False):
    return await registry.repair(reinstall=reinstall)


@app.post("/modules/{module_id}/update")
async def update_module(module_id: str):
    return await registry.update(module_id)


@app.put("/modules/{module_id}/meta")
async def set_module_meta(module_id: str, meta: ModuleMeta):
    return registry.set_meta(module_id, meta.category, meta.tags)


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
    conn = Connection(websocket)
    registry.add_connection(conn)
    try:
        while True:
            call = parse_call(await websocket.receive_json())
            if call is not None:
                conn.spawn(registry.handle_call(conn, call))
    except WebSocketDisconnect:
        pass
    except Exception:
        log.exception("WS error")
    finally:
        registry.remove_connection(conn)


@app.get("/ledroide-bridge.js")
async def ledroide_bridge():
    return Response(content=BRIDGE_JS, media_type="application/javascript")


# ── System ───────────────────────────────────────────────────────────────────

@app.get("/system/health")
async def system_health():
    return {"ok": True}


@app.post("/system/reload")
async def system_reload():
    return {"ok": True, **await registry.hot_reload()}


@app.get("/system/env")
async def get_env():
    return {"vars": env_store.all()}


@app.put("/system/env")
async def set_env(payload: EnvPayload):
    variables, changed = env_store.replace(payload)
    await registry.broadcast(SYSTEM_CHANNEL, "env:changed", {"keys": changed})
    if registry.remote.repo and set(changed) & {"GITHUB_TOKEN", "GH_TOKEN"}:
        registry.schedule_catalog_refresh(force=True)
    return {"vars": variables, "changed": changed}


@app.get("/system/packages")
async def list_packages():
    return packages.listing(registry.list_modules())


@app.get("/system/packages/outdated")
async def outdated_packages():
    return await packages.outdated()


@app.post("/system/packages")
async def install_package(payload: InstallPayload):
    return await packages.install(payload.spec)


@app.post("/system/packages/{name}/upgrade")
async def upgrade_package(name: str):
    return await packages.upgrade(name)


@app.delete("/system/packages/{name}")
async def uninstall_package(name: str):
    return await packages.uninstall(name)


@app.post("/system/shutdown")
async def system_shutdown():
    server = getattr(app.state, "server", None)
    if server is not None:
        asyncio.get_running_loop().call_later(0.1, setattr, server, "should_exit", True)
    return {"ok": True}


if __name__ == "__main__":
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=settings.PORT, log_config=None))
    app.state.server = server
    server.run()
