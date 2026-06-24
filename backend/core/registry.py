import json
import sys
from pathlib import Path
from fastapi import FastAPI, WebSocket
from fastapi.staticfiles import StaticFiles

MODULES_DIR   = Path(__file__).parent.parent.parent / "modules"
AVAILABLE_DIR = Path(__file__).parent.parent.parent / "modules-available"
CONFIGS_FILE  = Path(__file__).parent.parent / "configs.json"
STATE_FILE    = Path(__file__).parent.parent / "state.json"


class ModuleRegistry:
    def __init__(self, app: FastAPI):
        self.app = app
        self._modules: dict[str, dict] = {}
        self._connections: set[WebSocket] = set()
        self._configs: dict[str, dict] = self._load_configs()
        self._state: dict = self._load_state()
        self._module_instances: dict[str, object] = {}

    # ── Persistence ──────────────────────────────────────────────────────────

    def _load_configs(self) -> dict:
        if CONFIGS_FILE.exists():
            return json.loads(CONFIGS_FILE.read_text(encoding="utf-8"))
        return {}

    def _save_configs(self):
        CONFIGS_FILE.write_text(json.dumps(self._configs, indent=2), encoding="utf-8")

    def _load_state(self) -> dict:
        if STATE_FILE.exists():
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        return {"active_modules": []}

    def _save_state(self):
        STATE_FILE.write_text(json.dumps(self._state, indent=2), encoding="utf-8")

    # ── Startup ──────────────────────────────────────────────────────────────

    async def load_all(self):
        """Initial scan then auto-reactivate modules that were active before last shutdown."""
        MODULES_DIR.mkdir(exist_ok=True)
        AVAILABLE_DIR.mkdir(exist_ok=True)
        await self._scan(overwrite=False)

        for mod_id in list(self._state.get("active_modules", [])):
            if mod_id in self._modules:
                print(f"[Registry] auto-reactivating: {mod_id}")
                await self.activate(mod_id)
            else:
                # Module was removed from disk — clean up saved state
                self._state["active_modules"].remove(mod_id)
        self._save_state()

    async def hot_reload(self) -> dict:
        """Re-scan modules/ and catalog without disturbing active modules."""
        new_ids = await self._scan(overwrite=False)
        print(f"[Registry] hot_reload: {len(new_ids)} new module(s) found")
        return {"new_modules": new_ids, "total": len(self._modules)}

    async def _scan(self, overwrite: bool) -> list[str]:
        new_ids: list[str] = []

        for module_dir in sorted(MODULES_DIR.iterdir()):
            if not module_dir.is_dir():
                continue
            manifest_path = module_dir / "manifest.json"
            if not manifest_path.exists():
                continue
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            mod_id = manifest["id"]
            if mod_id not in self._modules or overwrite:
                manifest["status"] = "installed"
                self._modules[mod_id] = manifest
                new_ids.append(mod_id)
                print(f"[Registry] loaded: {mod_id}")

        catalog_path = AVAILABLE_DIR / "catalog.json"
        if catalog_path.exists():
            catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
            for mod in catalog.get("modules", []):
                if mod["id"] not in self._modules:
                    mod["status"] = "available"
                    self._modules[mod["id"]] = mod
                    new_ids.append(mod["id"])

        return new_ids

    # ── Module lifecycle ─────────────────────────────────────────────────────

    def list_modules(self) -> list:
        return list(self._modules.values())

    async def activate(self, module_id: str) -> dict:
        mod = self._modules.get(module_id)
        if not mod:
            return {"error": "MODULE_NOT_FOUND"}
        if mod["status"] == "active":
            return {"status": "active", "id": module_id}
        if mod["status"] != "installed":
            return {"error": "MODULE_NOT_INSTALLED"}

        # Serve frontend static files
        frontend_dir = MODULES_DIR / module_id / "frontend"
        if frontend_dir.exists():
            mount_path = f"/modules/{module_id}/frontend"
            try:
                self.app.mount(
                    mount_path,
                    StaticFiles(directory=str(frontend_dir), html=True),
                    name=f"frontend_{module_id}",
                )
            except Exception as exc:
                print(f"[Registry] mount warning for {module_id}: {exc}")

        # Load backend module and inject SDK dependencies
        await self._load_backend(module_id)

        mod["status"] = "active"
        active = self._state.setdefault("active_modules", [])
        if module_id not in active:
            active.append(module_id)
        self._save_state()
        print(f"[Registry] activated: {module_id}")
        return {"status": "active", "id": module_id}

    async def deactivate(self, module_id: str) -> dict:
        mod = self._modules.get(module_id)
        if not mod:
            return {"error": "MODULE_NOT_FOUND"}
        mod["status"] = "installed"
        self._module_instances.pop(module_id, None)

        # Remove API routes registered by this module
        api_prefix = f"/modules/{module_id}/api"
        self.app.router.routes = [
            r for r in self.app.router.routes
            if not getattr(r, "path", "").startswith(api_prefix)
        ]

        # Remove frontend StaticFiles mount
        frontend_name = f"frontend_{module_id}"
        self.app.router.routes = [
            r for r in self.app.router.routes
            if getattr(r, "name", None) != frontend_name
        ]

        active = self._state.get("active_modules", [])
        if module_id in active:
            active.remove(module_id)
        self._save_state()
        print(f"[Registry] deactivated: {module_id}")
        return {"status": "installed", "id": module_id}

    async def install(self, module_id: str) -> dict:
        mod = self._modules.get(module_id)
        if not mod:
            return {"error": "MODULE_NOT_FOUND"}
        # Phase 5: download from remote catalog
        mod["status"] = "installed"
        return {"status": "installed", "id": module_id}

    async def install_deps(self, module_id: str) -> dict:
        mod = self._modules.get(module_id)
        if not mod:
            return {"error": "MODULE_NOT_FOUND"}
        deps = mod.get("dependencies", {}).get("python", [])
        if not deps:
            return {"ok": True, "results": [], "message": "Aucune dépendance Python déclarée"}

        import asyncio, sys
        results = []
        for pkg in deps:
            print(f"[Registry] pip install {pkg}...")
            proc = await asyncio.create_subprocess_exec(
                sys.executable, "-m", "pip", "install", pkg,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await proc.communicate()
            ok = proc.returncode == 0
            print(f"[Registry] pip install {pkg}: {'OK' if ok else 'ERREUR'}")
            results.append({"package": pkg, "ok": ok, "output": (stdout + stderr).decode()})

        return {"ok": all(r["ok"] for r in results), "results": results}

    async def uninstall(self, module_id: str) -> dict:
        await self.deactivate(module_id)
        self._modules.pop(module_id, None)

        module_dir = MODULES_DIR / module_id
        if module_dir.exists():
            import shutil
            shutil.rmtree(module_dir)
            print(f"[Registry] deleted: {module_dir}")

        return {"status": "uninstalled", "id": module_id}

    # ── Config ───────────────────────────────────────────────────────────────

    def get_config(self, module_id: str) -> dict:
        return self._configs.get(module_id, {})

    def set_config(self, module_id: str, config: dict) -> dict:
        self._configs[module_id] = config
        self._save_configs()
        return {"status": "saved"}

    # ── WebSocket ────────────────────────────────────────────────────────────

    def add_connection(self, ws: WebSocket):
        self._connections.add(ws)

    def remove_connection(self, ws: WebSocket):
        self._connections.discard(ws)

    async def handle_message(self, ws: WebSocket, data: dict):
        module_id  = data.get("module_id")
        action     = data.get("action")
        payload    = data.get("payload", {})
        request_id = data.get("request_id")

        print(f"[WS] ← {module_id}.{action}  payload={payload}")

        result = {"ok": True}
        instance = self._module_instances.get(module_id)
        if not instance:
            print(f"[WS] ✗ aucune instance pour le module '{module_id}'")
            result = {"error": f"Module '{module_id}' non actif", "code": "MODULE_NOT_ACTIVE"}
        else:
            try:
                dispatched = await instance.dispatch(action, payload)
                if dispatched is not None:
                    result = dispatched
                    print(f"[WS] → {module_id}.{action}  result={result}")
                else:
                    print(f"[WS] ✗ {module_id}.{action} — aucun handler enregistré pour cette action")
                    result = {"error": f"Action '{action}' inconnue", "code": "UNKNOWN_ACTION"}
            except Exception as exc:
                print(f"[WS] ✗ dispatch error in {module_id}.{action}: {exc}")
                result = {"error": str(exc), "code": "DISPATCH_ERROR"}

        await ws.send_json({
            "type": "response",
            "module_id": module_id,
            "action": action,
            "request_id": request_id,
            "payload": result,
        })

    async def broadcast(self, module_id: str, event: str, payload: dict):
        msg = {"type": "event", "module_id": module_id, "action": event, "payload": payload}
        for ws in list(self._connections):
            try:
                await ws.send_json(msg)
            except Exception:
                self._connections.discard(ws)

    # ── Internal ─────────────────────────────────────────────────────────────

    async def _load_backend(self, module_id: str):
        module_dir   = MODULES_DIR / module_id
        backend_dir  = module_dir / "backend"
        entry_file   = module_dir / self._modules[module_id].get("entry", {}).get("backend", "backend/main.py")

        if not entry_file.exists():
            return

        # Add module's backend directory to sys.path so imports resolve
        backend_str = str(backend_dir)
        if backend_str not in sys.path:
            sys.path.insert(0, backend_str)

        try:
            import importlib.util
            spec   = importlib.util.spec_from_file_location(f"module_{module_id}", entry_file)
            mod_py = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod_py)

            # Mount the module's FastAPI router under /modules/{id}/api
            if hasattr(mod_py, "router"):
                from fastapi import APIRouter
                self.app.include_router(
                    mod_py.router,
                    prefix=f"/modules/{module_id}/api",
                    tags=[module_id],
                )

            if hasattr(mod_py, "module"):
                # Inject broadcast into the module's events singleton
                instance = mod_py.module
                if hasattr(instance, "_events"):
                    instance._events._inject(
                        lambda ev, pl: self.broadcast(module_id, ev, pl)
                    )
                self._module_instances[module_id] = instance

        except Exception as exc:
            print(f"[Registry] failed to load backend for {module_id}: {exc}")
