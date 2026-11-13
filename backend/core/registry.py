import asyncio
import importlib
import importlib.util
import json
import logging
import os
import re
import shutil
import stat
import sys
import zipfile
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from core import settings
from core.errors import LedroideError, error_body
from core.ipc import Connection, WsCall, WsEvent, WsResponse
from core.deps import missing_requirements
from core.packages import _installed, _protected, normalize, requirement_name, run_pip
from core.remote_catalog import RemoteCatalog

log = logging.getLogger("ledroide.registry")

MODULE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
SYSTEM_CHANNEL = "__system__"


DEFAULT_CATEGORY = "Autres"


def normalize_tags(tags) -> list[str]:
    if not isinstance(tags, list):
        return []
    out: list[str] = []
    for t in tags:
        tag = str(t).strip().lower()[:30]
        if tag and tag not in out:
            out.append(tag)
    return out[:12]


def normalize_env_decl(decl) -> list[dict]:
    """Manifest `env`: ["KEY", …] or [{"key", "description", "required", "secret"}, …]."""
    out: list[dict] = []
    for item in decl if isinstance(decl, list) else []:
        if isinstance(item, str):
            item = {"key": item}
        if isinstance(item, dict) and isinstance(item.get("key"), str):
            out.append({
                "key": item["key"],
                "description": str(item.get("description", "")),
                "required": bool(item.get("required", True)),
                "secret": bool(item.get("secret", False)),
            })
    return out


def validate_module_id(module_id: str) -> str:
    if not MODULE_ID_RE.match(module_id or ""):
        raise LedroideError("INVALID_MODULE_ID", f"Identifiant de module invalide : {module_id!r}", 400)
    return module_id


def _atomic_write_json(path: Path, data: Any):
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def _read_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default
    except (OSError, json.JSONDecodeError):
        log.exception("unreadable %s, using defaults", path)
        return default


def version_key(version) -> tuple[int, ...]:
    """Numeric part of a version ("1.10.0-beta" -> (1, 10, 0)), zero-padded for comparison."""
    core = str(version or "").strip().lstrip("vV").split("-")[0].split("+")[0]
    nums = [int(x) for x in re.findall(r"\d+", core)][:6]
    return tuple(nums + [0] * (6 - len(nums)))


def is_newer(candidate, current) -> bool:
    return version_key(candidate) > version_key(current)


def _python_deps(mod: dict) -> list[str]:
    deps = (mod.get("dependencies") or {}).get("python") or []
    return [d.strip() for d in deps if isinstance(d, str) and d.strip()]


def _rmtree_force(path: Path):
    def onerror(func, p, _exc):
        os.chmod(p, stat.S_IWRITE)
        func(p)
    shutil.rmtree(path, onerror=onerror)


class ModuleRegistry:
    def __init__(self, app: FastAPI):
        self.app = app
        self._modules: dict[str, dict] = {}
        self._connections: set[Connection] = set()
        settings.migrate_legacy_files()
        self._configs: dict[str, dict] = _read_json(settings.CONFIGS_FILE, {})
        self._state: dict = _read_json(settings.STATE_FILE, {"active_modules": []})
        self._meta: dict[str, dict] = _read_json(settings.DATA_DIR / "module_meta.json", {})
        self._dirs: dict[str, Path] = {}
        self._instances: dict[str, Any] = {}
        self._routes: dict[str, list] = {}
        self._lock = asyncio.Lock()
        self.remote = RemoteCatalog()
        self._bg_tasks: set[asyncio.Task] = set()
        self._remote_manifests: dict[str, dict] = {}
        self._repair_lock = asyncio.Lock()
        self._repair: dict = {"running": False}

    def _save_meta(self):
        _atomic_write_json(settings.DATA_DIR / "module_meta.json", self._meta)

    def _save_configs(self):
        _atomic_write_json(settings.CONFIGS_FILE, self._configs)

    def _save_state(self):
        _atomic_write_json(settings.STATE_FILE, self._state)

    def _get(self, module_id: str) -> dict:
        validate_module_id(module_id)
        mod = self._modules.get(module_id)
        if not mod:
            raise LedroideError("MODULE_NOT_FOUND", f"Module '{module_id}' introuvable", 404)
        return mod

    # ── Startup / shutdown ───────────────────────────────────────────────────

    async def load_all(self):
        settings.MODULES_DIR.mkdir(exist_ok=True)
        settings.AVAILABLE_DIR.mkdir(exist_ok=True)
        settings.USER_MODULES_DIR.mkdir(parents=True, exist_ok=True)
        self._scan()
        after_update = self._check_environment()

        # Modules whose libraries vanished (app or Python update, other interpreter) are not
        # imported now: their dependencies are reinstalled in the background, then they are
        # reactivated. They stay in active_modules whatever happens, so the next start retries.
        pending: list[str] = []
        for mod_id in list(self._state.get("active_modules", [])):
            if mod_id not in self._modules or self._modules[mod_id]["status"] == "available":
                self._state["active_modules"].remove(mod_id)
                continue
            missing = missing_requirements(_python_deps(self._modules[mod_id]))
            if missing:
                self._mark_missing(mod_id, missing)
                pending.append(mod_id)
                continue
            log.info("auto-reactivating %s", mod_id)
            try:
                await self.activate(mod_id, persist=False)
            except LedroideError as exc:
                log.error("auto-reactivation of %s failed: %s", mod_id, exc.message)
        self._save_state()
        if pending:
            log.info("restoring %d module(s) with missing dependencies: %s", len(pending), ", ".join(pending))
            self._spawn(self._repair_bg(pending, "update" if after_update else "startup"))
        if self.remote.repo:
            self.schedule_catalog_refresh(force=False)

    def _check_environment(self) -> bool:
        """Records the app and Python versions; True when either changed since the last start."""
        current = {"app": os.environ.get("LEDROIDE_APP_VERSION", ""), "python": f"{sys.version_info[0]}.{sys.version_info[1]}"}
        previous = self._state.get("environment")
        self._state["environment"] = current
        changed = bool(previous) and previous != current
        if changed:
            log.info("environment changed: %s -> %s", previous, current)
        return changed

    def _mark_missing(self, module_id: str, missing: list[str]):
        mod = self._modules[module_id]
        mod["status"] = "error"
        mod["missing_deps"] = missing
        mod["last_error"] = f"Dépendances manquantes : {', '.join(missing)}"

    def _spawn(self, coro):
        task = asyncio.create_task(coro)
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)

    async def _repair_bg(self, ids: list[str], reason: str):
        try:
            await self.repair(ids, reason=reason)
        except Exception:
            log.exception("background repair failed")

    def repair_status(self) -> dict:
        return dict(self._repair)

    async def repair(self, ids: list[str] | None = None, reinstall: bool = False, reason: str = "manual") -> dict:
        """Installs the dependencies of the given modules (default: every module marked active or in
        error), then activates those that should be active. `reinstall` forces fresh copies."""
        if self._repair_lock.locked():
            raise LedroideError("REPAIR_RUNNING", "Une réparation des modules est déjà en cours", 409)
        async with self._repair_lock:
            active = self._state.get("active_modules", [])
            if ids is None:
                ids = [m for m in active if m in self._modules] + \
                      [m for m, d in self._modules.items() if d["status"] == "error" and m not in active]
            targets = [m for m in dict.fromkeys(ids) if m in self._modules and self._modules[m]["status"] != "available"]
            packages = list(dict.fromkeys(d for m in targets for d in _python_deps(self._modules[m])))
            loaded_with_deps = [m for m in targets if self._modules[m]["status"] == "active" and _python_deps(self._modules[m])]
            self._repair = {"running": True, "reason": reason, "modules": targets, "packages": packages,
                            "stage": "deps" if packages else "activate", "line": "", "done": 0}
            await self.broadcast(SYSTEM_CHANNEL, "modules:repair", self.repair_status())

            async def emit(line: str):
                self._repair["line"] = line
                await self.broadcast(SYSTEM_CHANNEL, "modules:repair", {**self.repair_status(), "log": line})

            ok, output = True, []
            if packages:
                if reinstall:
                    protected = _protected(_installed())
                    forced = [d for d in packages if normalize(requirement_name(d) or d) not in protected]
                    if forced:
                        ok, lines = await run_pip(["install", "--force-reinstall", "--no-deps", "--no-cache-dir", *forced], emit)
                        output += lines
                if ok:
                    ok, lines = await run_pip(["install", *packages], emit)
                    output += lines
                log.info("repair: pip install of %d package(s): %s", len(packages), "OK" if ok else "FAILED")

            self._repair["stage"] = "activate"
            activated, failed = [], []
            for index, mod_id in enumerate(targets):
                mod = self._modules[mod_id]
                self._repair["done"] = index
                if mod["status"] == "active":
                    continue
                if mod_id not in active and mod["status"] != "error":
                    continue
                await self.broadcast(SYSTEM_CHANNEL, "modules:repair", self.repair_status())
                try:
                    await self.activate(mod_id)
                    activated.append(mod_id)
                except LedroideError as exc:
                    failed.append({"id": mod_id, "name": mod.get("name", mod_id), "error": exc.message})
            result = {"ok": ok and not failed, "pip_ok": ok, "activated": activated, "failed": failed,
                      "restart": bool(reinstall and ok and loaded_with_deps), "output": "\n".join(output[-200:])}
            self._repair = {"running": False, "reason": reason, "stage": "end", "modules": targets, "packages": packages,
                            "activated": activated, "failed": failed, "pip_ok": ok}
            await self.broadcast(SYSTEM_CHANNEL, "modules:repair", self.repair_status())
            await self.broadcast(SYSTEM_CHANNEL, "modules:changed", {"repaired": activated})
            return result

    def schedule_catalog_refresh(self, force: bool = False):
        self._spawn(self._refresh_catalog_bg(force))

    async def _refresh_catalog_bg(self, force: bool):
        try:
            await self.refresh_catalog(force=force)
        except Exception:
            log.exception("background catalog refresh failed")

    async def refresh_catalog(self, force: bool = True) -> dict:
        info = await self.remote.refresh(force=force)
        new_ids = self._scan()
        await self.broadcast(SYSTEM_CHANNEL, "modules:changed", {"catalog": info, "new_modules": new_ids})
        return {**info, "new_modules": new_ids}

    def catalog_info(self) -> dict:
        return self.remote.info()

    async def shutdown(self):
        for task in list(self._bg_tasks):
            task.cancel()
        for mod_id, instance in list(self._instances.items()):
            await self._shutdown_instance(mod_id, instance)
        self._instances.clear()

    async def hot_reload(self) -> dict:
        if self.remote.repo:
            await self.remote.refresh(force=False)
        new_ids = self._scan()
        log.info("hot_reload: %d new module(s)", len(new_ids))
        return {"new_modules": new_ids, "total": len(self._modules)}

    def module_dir(self, module_id: str) -> Path:
        return self._dirs.get(module_id) or settings.MODULES_DIR / module_id

    def _scan(self) -> list[str]:
        new_ids: list[str] = []
        removed = set(self._state.get("removed_bundled", []))
        seen: set[str] = set()
        # User-installed modules first: they take precedence over a bundled copy.
        for root, bundled in ((settings.USER_MODULES_DIR, False), (settings.MODULES_DIR, True)):
            if not root.is_dir():
                continue
            for module_dir in sorted(root.iterdir()):
                manifest_path = module_dir / "manifest.json"
                if not module_dir.is_dir() or not manifest_path.exists():
                    continue
                manifest = _read_json(manifest_path, None)
                if not manifest or not MODULE_ID_RE.match(str(manifest.get("id", ""))):
                    log.warning("ignored %s: invalid manifest", module_dir.name)
                    continue
                mod_id = manifest["id"]
                if mod_id != module_dir.name:
                    log.warning("ignored %s: manifest id '%s' differs from folder name", module_dir.name, mod_id)
                    continue
                if mod_id in seen:
                    continue
                seen.add(mod_id)
                status = "available" if bundled and mod_id in removed else "installed"
                current = self._modules.get(mod_id)
                if current is None or (current["status"] == "available" and status == "installed"):
                    self._modules[mod_id] = {**manifest, "status": status, "bundled": bundled}
                    self._dirs[mod_id] = module_dir
                    new_ids.append(mod_id)
                    log.info("loaded manifest: %s", mod_id)

        catalog = _read_json(settings.AVAILABLE_DIR / "catalog.json", {})
        for mod in catalog.get("modules", []):
            if MODULE_ID_RE.match(str(mod.get("id", ""))) and mod["id"] not in self._modules:
                self._modules[mod["id"]] = {**mod, "status": "available"}
                new_ids.append(mod["id"])

        # Remote repository: modules not present locally.
        remote_dirs = self.remote.module_dirs()
        self._remote_manifests = {}
        for mod_id, module_dir in remote_dirs.items():
            manifest = _read_json(module_dir / "manifest.json", None)
            if manifest and manifest.get("id") == mod_id:
                self._remote_manifests[mod_id] = manifest
        for mod_id in [m for m, d in self._modules.items() if d.get("source") == "remote" and d["status"] == "available"]:
            if mod_id not in remote_dirs:
                self._modules.pop(mod_id)
                self._dirs.pop(mod_id, None)
        for mod_id, module_dir in remote_dirs.items():
            if mod_id in self._modules or not MODULE_ID_RE.match(mod_id):
                continue
            manifest = _read_json(module_dir / "manifest.json", None)
            if not manifest or manifest.get("id") != mod_id:
                continue
            self._modules[mod_id] = {**manifest, "status": "available", "bundled": False, "source": "remote"}
            self._dirs[mod_id] = module_dir
            new_ids.append(mod_id)
        return new_ids

    # ── Module lifecycle ─────────────────────────────────────────────────────

    def list_modules(self) -> list:
        return [self._describe(mod) for mod in self._modules.values()]

    def _describe(self, mod: dict) -> dict:
        override = self._meta.get(mod["id"], {})
        env_decl = normalize_env_decl(mod.get("env"))
        installed = mod["status"] != "available"
        remote = self._remote_manifests.get(mod["id"]) if installed else None
        remote_version = str(remote.get("version", "")) if remote else None
        return {
            **mod,
            "category": override.get("category") or mod.get("category") or DEFAULT_CATEGORY,
            "tags": override["tags"] if "tags" in override else normalize_tags(mod.get("tags")),
            "meta_overridden": bool(override),
            "env": env_decl,
            "env_missing": [e["key"] for e in env_decl if e["required"] and not os.environ.get(e["key"])],
            "remote_version": remote_version,
            "update_available": bool(remote_version and is_newer(remote_version, mod.get("version"))),
            "remote_dependencies": _python_deps(remote) if remote else None,
        }

    # ── Category / tags ──────────────────────────────────────────────────────

    def set_meta(self, module_id: str, category: str | None, tags: list[str] | None) -> dict:
        mod = self._get(module_id)
        override: dict = {}
        if category is not None and category.strip():
            override["category"] = category.strip()[:40]
        if tags is not None:
            override["tags"] = normalize_tags(tags)
        if override:
            self._meta[module_id] = override
        else:
            self._meta.pop(module_id, None)
        self._save_meta()
        return self._describe(mod)

    async def activate(self, module_id: str, persist: bool = True) -> dict:
        async with self._lock:
            mod = self._get(module_id)
            if mod["status"] == "active":
                return {"status": "active", "id": module_id}
            if mod["status"] == "available":
                raise LedroideError("MODULE_NOT_INSTALLED", f"Module '{module_id}' non installé", 409)

            missing = missing_requirements(_python_deps(mod))
            if missing:
                self._mark_missing(module_id, missing)
                raise LedroideError("MISSING_DEPS", mod["last_error"], 409)
            mod.pop("missing_deps", None)

            try:
                await self._load_backend(module_id)
            except Exception as exc:
                log.exception("failed to load backend for %s", module_id)
                self._unmount(module_id)
                mod["status"] = "error"
                mod["last_error"] = f"{type(exc).__name__}: {exc}"
                raise LedroideError("MODULE_LOAD_FAILED", f"Échec du chargement de '{module_id}' : {exc}", 500)

            self._mount_frontend(module_id)
            mod["status"] = "active"
            mod.pop("last_error", None)
            active = self._state.setdefault("active_modules", [])
            if module_id not in active:
                active.append(module_id)
            if persist:
                self._save_state()
            log.info("activated %s", module_id)
            return {"status": "active", "id": module_id}

    async def deactivate(self, module_id: str) -> dict:
        async with self._lock:
            mod = self._get(module_id)
            instance = self._instances.pop(module_id, None)
            if instance is not None:
                await self._shutdown_instance(module_id, instance)
            self._unmount(module_id)
            if mod["status"] in ("active", "error"):
                mod["status"] = "installed"
            mod.pop("last_error", None)
            active = self._state.get("active_modules", [])
            if module_id in active:
                active.remove(module_id)
            self._save_state()
            log.info("deactivated %s", module_id)
            return {"status": "installed", "id": module_id}

    async def install(self, module_id: str) -> dict:
        mod = self._get(module_id)
        if mod["status"] != "available":
            return {"status": mod["status"], "id": module_id}

        if mod.get("bundled"):
            self._state["removed_bundled"] = [m for m in self._state.get("removed_bundled", []) if m != module_id]
            self._save_state()
            mod["status"] = "installed"
            log.info("restored bundled %s", module_id)
            return {"status": "installed", "id": module_id}

        settings.USER_MODULES_DIR.mkdir(parents=True, exist_ok=True)
        target = settings.USER_MODULES_DIR / module_id
        if target.exists():
            await asyncio.to_thread(_rmtree_force, target)
        source_dir = self._dirs[module_id] if mod.get("source") == "remote" else settings.AVAILABLE_DIR / module_id
        archive = mod.get("archive")
        if source_dir.is_dir() and (source_dir / "manifest.json").exists():
            await asyncio.to_thread(shutil.copytree, source_dir, target)
        elif archive:
            await asyncio.to_thread(self._extract_archive, settings.AVAILABLE_DIR / archive, target)
        else:
            raise LedroideError(
                "CATALOG_SOURCE_MISSING",
                f"Aucune source pour '{module_id}' (attendu : modules-available/{module_id}/ ou champ 'archive')",
                501,
            )

        manifest = _read_json(target / "manifest.json", None)
        if not manifest or manifest.get("id") != module_id:
            _rmtree_force(target)
            raise LedroideError("INVALID_PACKAGE", "Le paquet ne contient pas de manifest.json valide", 422)
        self._modules[module_id] = {**manifest, "status": "installed", "bundled": False}
        self._dirs[module_id] = target
        log.info("installed %s", module_id)
        return {"status": "installed", "id": module_id}

    async def install_many(self, module_ids: list[str]) -> dict:
        """Installs one after the other: a failure is reported and does not stop the batch."""
        ids = list(dict.fromkeys(module_ids))
        results: list[dict] = []
        for index, module_id in enumerate(ids):
            await self.broadcast(SYSTEM_CHANNEL, "modules:install", {"id": module_id, "index": index, "total": len(ids)})
            try:
                results.append(await self.install(module_id))
            except LedroideError as exc:
                results.append({"id": module_id, "status": "error", "error": exc.message, "code": exc.code})
            except Exception as exc:
                log.exception("install of %s failed", module_id)
                results.append({"id": module_id, "status": "error", "error": str(exc), "code": "INSTALL_FAILED"})
        installed = sum(r["status"] == "installed" for r in results)
        return {"results": results, "installed": installed, "failed": len(results) - installed}

    @staticmethod
    def _extract_archive(archive: Path, target: Path):
        if not archive.is_file():
            raise LedroideError("CATALOG_SOURCE_MISSING", f"Archive introuvable : {archive.name}", 404)
        target_resolved = target.resolve()
        with zipfile.ZipFile(archive) as zf:
            for name in zf.namelist():
                dest = (target / name).resolve()
                if target_resolved not in dest.parents and dest != target_resolved:
                    raise LedroideError("INVALID_PACKAGE", f"Chemin interdit dans l'archive : {name}", 422)
            zf.extractall(target)

    async def uninstall(self, module_id: str) -> dict:
        mod = self._get(module_id)
        if mod["status"] == "available":
            raise LedroideError("MODULE_NOT_INSTALLED", f"Module '{module_id}' non installé", 409)
        await self.deactivate(module_id)

        # Bundled modules live in the install dir, which is never written to:
        # uninstalling one only hides it, and the choice survives app updates.
        if mod.get("bundled"):
            removed = self._state.setdefault("removed_bundled", [])
            if module_id not in removed:
                removed.append(module_id)
            self._save_state()
            mod["status"] = "available"
            log.info("hid bundled %s", module_id)
            return {"status": "uninstalled", "id": module_id}

        module_dir = self.module_dir(module_id).resolve()
        if module_dir.parent != settings.USER_MODULES_DIR.resolve():
            raise LedroideError("INVALID_MODULE_ID", "Chemin de module invalide", 400)
        if module_dir.exists():
            await asyncio.to_thread(_rmtree_force, module_dir)
            log.info("deleted %s", module_dir)
        self._modules.pop(module_id, None)
        self._dirs.pop(module_id, None)
        self._scan()
        return {"status": "uninstalled", "id": module_id}

    async def update(self, module_id: str) -> dict:
        """Replaces an installed module by the remote catalog's copy, keeping its data and state.

        The new files go to the user modules dir, which takes precedence over a bundled copy.
        An active module is deactivated during the swap and reactivated afterwards.
        """
        mod = self._get(module_id)
        if mod["status"] == "available":
            raise LedroideError("MODULE_NOT_INSTALLED", f"Module '{module_id}' non installé", 409)
        source = self.remote.module_dirs().get(module_id)
        manifest = _read_json(source / "manifest.json", None) if source else None
        if not manifest or manifest.get("id") != module_id:
            raise LedroideError("UPDATE_UNAVAILABLE", f"'{module_id}' est absent du dépôt de modules", 404)

        previous = str(mod.get("version", ""))
        old_deps = _python_deps(mod)
        was_active = mod["status"] == "active"
        if mod["status"] in ("active", "error"):
            await self.deactivate(module_id)

        root = settings.USER_MODULES_DIR
        root.mkdir(parents=True, exist_ok=True)
        target, staging, backup = root / module_id, root / f".{module_id}.new", root / f".{module_id}.old"
        for p in (staging, backup):
            if p.exists():
                await asyncio.to_thread(_rmtree_force, p)
        await asyncio.to_thread(shutil.copytree, source, staging)
        if target.exists():
            os.replace(target, backup)
        try:
            os.replace(staging, target)
        except OSError:
            if backup.exists():
                os.replace(backup, target)
            raise
        if backup.exists():
            await asyncio.to_thread(_rmtree_force, backup)

        self._modules[module_id] = {**manifest, "status": "installed", "bundled": False}
        self._dirs[module_id] = target
        log.info("updated %s %s -> %s", module_id, previous, manifest.get("version"))

        result = {"id": module_id, "status": "installed", "version": manifest.get("version"),
                  "previous_version": previous, "deps": None, "restart": False}
        new_deps = _python_deps(manifest)
        if new_deps and sorted(new_deps) != sorted(old_deps):
            deps = await self.install_deps(module_id)
            result["deps"] = deps
            if not deps["ok"]:
                await self.broadcast(SYSTEM_CHANNEL, "modules:changed", {"updated": [module_id]})
                raise LedroideError("DEPS_FAILED", "Module mis à jour, mais l'installation des dépendances a échoué", 500)
            result["restart"] = was_active
        if was_active:
            try:
                result["status"] = (await self.activate(module_id))["status"]
            except LedroideError as exc:
                result.update(status="error", error=exc.message)
        await self.broadcast(SYSTEM_CHANNEL, "modules:changed", {"updated": [module_id]})
        return result

    async def install_deps(self, module_id: str, reinstall: bool = False) -> dict:
        """pip install of the manifest's Python dependencies. `reinstall` first forces a fresh copy
        of each declared package (core packages excepted), then installs whatever they require."""
        mod = self._get(module_id)
        deps = _python_deps(mod)
        if not deps:
            return {"ok": True, "output": "", "message": "Aucune dépendance Python déclarée"}
        for dep in deps:
            if dep.startswith("-"):
                raise LedroideError("INVALID_DEPENDENCY", f"Dépendance invalide : {dep}", 422)

        await self.broadcast(SYSTEM_CHANNEL, "deps:start", {"module_id": module_id, "packages": deps, "reinstall": reinstall})

        async def emit(line: str):
            await self.broadcast(SYSTEM_CHANNEL, "deps:log", {"module_id": module_id, "line": line})

        output: list[str] = []
        ok = True
        if reinstall:
            protected = _protected(_installed())
            forced = [d for d in deps if normalize(requirement_name(d) or d) not in protected]
            skipped = [d for d in deps if d not in forced]
            if skipped:
                line = f"Paquets du cœur non réinstallés : {', '.join(skipped)}"
                output.append(line)
                await emit(line)
            if forced:
                ok, lines = await run_pip(["install", "--force-reinstall", "--no-deps", "--no-cache-dir", *forced], emit)
                output += lines
        if ok:
            ok, lines = await run_pip(["install", *deps], emit)
            output += lines
        await self.broadcast(SYSTEM_CHANNEL, "deps:end", {"module_id": module_id, "ok": ok})
        log.info("pip %s %s for %s: %s", "reinstall" if reinstall else "install", deps, module_id, "OK" if ok else "FAILED")
        return {"ok": ok, "output": "\n".join(output[-200:])}

    # ── Config ───────────────────────────────────────────────────────────────

    def _config_defaults(self, module_id: str) -> dict:
        schema = self._modules.get(module_id, {}).get("config_schema") or {}
        return {k: v["default"] for k, v in schema.items() if isinstance(v, dict) and "default" in v}

    def get_config(self, module_id: str) -> dict:
        self._get(module_id)
        return {**self._config_defaults(module_id), **self._configs.get(module_id, {})}

    def set_config(self, module_id: str, config: dict) -> dict:
        self._get(module_id)
        values = self._configs.setdefault(module_id, {})
        values.clear()
        values.update(config)
        self._save_configs()
        return {"status": "saved", "config": self.get_config(module_id)}

    # ── WebSocket ────────────────────────────────────────────────────────────

    def add_connection(self, conn: Connection):
        self._connections.add(conn)

    def remove_connection(self, conn: Connection):
        self._connections.discard(conn)
        conn.cancel_all()

    async def handle_call(self, conn: Connection, call: WsCall):
        log.debug("WS <- %s.%s (%d keys)", call.module_id, call.action, len(call.payload))
        instance = self._instances.get(call.module_id)
        if instance is None:
            result = error_body(f"Module '{call.module_id}' non actif", "MODULE_NOT_ACTIVE")
        elif hasattr(instance, "has_handler") and not instance.has_handler(call.action):
            result = error_body(f"Action '{call.action}' inconnue", "UNKNOWN_ACTION")
        else:
            try:
                result = await instance.dispatch(call.action, call.payload)
                if result is None:
                    result = {"ok": True}
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.exception("dispatch error in %s.%s", call.module_id, call.action)
                result = error_body(str(exc), "DISPATCH_ERROR")

        response = WsResponse(module_id=call.module_id, action=call.action,
                              request_id=call.request_id, payload=result)
        try:
            await conn.send(response.model_dump())
        except Exception:
            self.remove_connection(conn)

    async def broadcast(self, module_id: str, event: str, payload: Any):
        msg = WsEvent(module_id=module_id, action=event, payload=payload).model_dump()
        for conn in list(self._connections):
            try:
                await conn.send(msg)
            except Exception:
                self._connections.discard(conn)

    # ── Internal ─────────────────────────────────────────────────────────────

    def _track_routes(self, module_id: str, register):
        # Route objects are tracked by identity: FastAPI >= 0.137 adds one opaque
        # _IncludedRouter entry per include_router, without a usable `path`.
        before = {id(r) for r in self.app.router.routes}
        register()
        added = [r for r in self.app.router.routes if id(r) not in before]
        self._routes.setdefault(module_id, []).extend(added)

    def _mount_frontend(self, module_id: str):
        frontend_dir = self.module_dir(module_id) / "frontend"
        if frontend_dir.exists():
            self._track_routes(module_id, lambda: self.app.mount(
                f"/modules/{module_id}/frontend",
                StaticFiles(directory=str(frontend_dir), html=True),
                name=f"frontend_{module_id}",
            ))

    def _unmount(self, module_id: str):
        owned = {id(r) for r in self._routes.pop(module_id, [])}
        if owned:
            self.app.router.routes[:] = [r for r in self.app.router.routes if id(r) not in owned]
        self.app.openapi_schema = None
        sys.modules.pop(self._py_name(module_id), None)

    @staticmethod
    def _py_name(module_id: str) -> str:
        return f"ledroide_module_{module_id.replace('-', '_')}"

    async def _shutdown_instance(self, module_id: str, instance: Any):
        if hasattr(instance, "_shutdown"):
            try:
                await instance._shutdown()
            except Exception:
                log.exception("shutdown failed for %s", module_id)

    async def _load_backend(self, module_id: str):
        module_dir = self.module_dir(module_id)
        backend_dir = module_dir / "backend"
        entry = self._modules[module_id].get("entry", {}).get("backend", "backend/main.py")
        entry_file = (module_dir / entry).resolve()
        if module_dir.resolve() not in entry_file.parents:
            raise ValueError(f"entry.backend sort du dossier du module : {entry}")
        if not entry_file.exists():
            return

        # Helper files are imported by bare name (e.g. `import utils`): the module's
        # backend dir is on sys.path only while it loads, and same-named helpers
        # cached by another module are evicted so each module gets its own.
        helpers = {p.stem for p in backend_dir.glob("*.py") if p.stem not in ("main", "__init__")}
        for name in helpers:
            sys.modules.pop(name, None)
        backend_str = str(backend_dir)
        sys.path.insert(0, backend_str)
        py_name = self._py_name(module_id)
        try:
            spec = importlib.util.spec_from_file_location(py_name, entry_file)
            mod_py = importlib.util.module_from_spec(spec)
            sys.modules[py_name] = mod_py
            spec.loader.exec_module(mod_py)
        except BaseException:
            sys.modules.pop(py_name, None)
            raise
        finally:
            if backend_str in sys.path:
                sys.path.remove(backend_str)
            for name in helpers:
                sys.modules.pop(name, None)

        instance = getattr(mod_py, "module", None)
        if instance is not None:
            if hasattr(instance, "_events"):
                instance._events._inject(lambda ev, pl, _id=module_id: self.broadcast(_id, ev, pl))
                if hasattr(mod_py, "events"):
                    mod_py.events = instance._events
            if hasattr(instance, "_config"):
                instance._config._inject(
                    self._configs.setdefault(module_id, {}),
                    lambda _vals, _id=module_id: self._save_configs(),
                )
                if hasattr(mod_py, "config"):
                    mod_py.config = instance._config
            if hasattr(instance, "_db") and hasattr(mod_py, "db"):
                mod_py.db = instance._db

        router = getattr(mod_py, "router", None) or getattr(instance, "_router", None)
        if router is not None:
            self._track_routes(module_id, lambda: self.app.include_router(
                router, prefix=f"/modules/{module_id}/api", tags=[module_id]))

        if instance is not None:
            self._instances[module_id] = instance
