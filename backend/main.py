import os
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
import uvicorn

_BRIDGE_JS = r"""
(function () {
  var pending   = new Map();
  var listeners = new Map();

  window.addEventListener('message', function (e) {
    var d = e.data;
    if (d.type === 'ledroide:response' && pending.has(d.requestId)) {
      var p = pending.get(d.requestId);
      clearTimeout(p.timer);
      pending.delete(d.requestId);
      if (d.payload && d.payload.error) p.reject(new Error(d.payload.error));
      else p.resolve(d.payload);
    }
    if (d.type === 'ledroide:event') {
      var key = d.moduleId + ':' + d.action;
      var cbs = listeners.get(key);
      if (cbs) cbs.forEach(function (cb) { cb(d.payload); });
    }
  });

  window.ledroide = {
    backendPort: parseInt(window.location.port) || 8765,

    call: function (moduleId, action, payload) {
      payload = payload || {};
      return new Promise(function (resolve, reject) {
        var rid   = Math.random().toString(36).slice(2) + Math.random().toString(36).slice(2);
        var timer = setTimeout(function () {
          pending.delete(rid);
          reject(new Error('RPC timeout'));
        }, 10000);
        pending.set(rid, { resolve: resolve, reject: reject, timer: timer });
        window.parent.postMessage(
          { type: 'ledroide:call', moduleId: moduleId, action: action, payload: payload, requestId: rid },
          '*'
        );
      });
    },

    on: function (moduleId, event, callback) {
      var key = moduleId + ':' + event;
      if (!listeners.has(key)) listeners.set(key, new Set());
      listeners.get(key).add(callback);
      window.parent.postMessage({ type: 'ledroide:subscribe', moduleId: moduleId, event: event }, '*');
    },

    off: function (moduleId, event, callback) {
      var key = moduleId + ':' + event;
      var cbs = listeners.get(key);
      if (cbs) cbs.delete(callback);
    },
  };
})();
"""

from core.registry import ModuleRegistry

PORT = int(os.environ.get("LEDROIDE_BACKEND_PORT", 8765))

app = FastAPI(title="LedroideDesktop", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

class NoCacheModuleFrontend(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        if "/frontend/" in request.url.path:
            response.headers["Cache-Control"] = "no-store"
        return response

app.add_middleware(NoCacheModuleFrontend)

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


@app.get("/modules/{module_id}/icon")
async def module_icon(module_id: str):
    from fastapi.responses import FileResponse
    from pathlib import Path
    icon_path = Path(__file__).parent.parent / "modules" / module_id / "icon.png"
    if not icon_path.exists():
        return Response(status_code=404)
    return FileResponse(str(icon_path), media_type="image/png")


@app.post("/modules/{module_id}/deps")
async def install_deps(module_id: str):
    return await registry.install_deps(module_id)


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


# ── Bridge SDK ──────────────────────────────────────────────────────────────

@app.get("/ledroide-bridge.js")
async def ledroide_bridge():
    return Response(content=_BRIDGE_JS, media_type="application/javascript")


# ── System ───────────────────────────────────────────────────────────────────

@app.post("/system/reload")
async def system_reload():
    """Hot-reload: pick up new modules without restarting the process."""
    result = await registry.hot_reload()
    return {"ok": True, **result}


if __name__ == "__main__":
    uvicorn.run("main:app", host="127.0.0.1", port=PORT, reload=False)
