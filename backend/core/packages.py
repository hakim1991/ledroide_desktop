import asyncio
import importlib
import importlib.metadata as md
import json
import logging
import os
import re
import site
import sys
from typing import Awaitable, Callable, Iterable

from pydantic import BaseModel, field_validator

from core import settings
from core.errors import LedroideError

log = logging.getLogger("ledroide.packages")

NAME_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._-]{0,98}[A-Za-z0-9])?$")
_OP = r"(?:===|==|~=|!=|<=|>=|<|>)\s*[A-Za-z0-9.*+!_-]+"
SPEC_RE = re.compile(
    r"^[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?"
    r"(?:\[[A-Za-z0-9._-]+(?:\s*,\s*[A-Za-z0-9._-]+)*\])?"
    rf"\s*(?:{_OP}\s*(?:,\s*{_OP}\s*)*)?$"
)
_REQ_NAME_RE = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)")

# Packages the backend itself needs: removing or replacing them in place breaks the app.
CORE_ROOTS = {"pip", "setuptools", "wheel", "fastapi", "uvicorn", "pydantic", "starlette",
              "websockets", "python-multipart", "aiofiles", "ledroide-sdk"}

# One pip at a time: concurrent installs corrupt site-packages.
pip_lock = asyncio.Lock()

# The bundled Python lives in the install dir, which every app update wipes:
# libraries go to the user site instead (PYTHONUSERBASE, set by Electron under
# LEDROIDE_DATA_DIR). Inside a venv the user site is disabled and pip installs in place.
USE_USER_SITE = bool(site.ENABLE_USER_SITE and os.environ.get("PYTHONUSERBASE"))


def ensure_user_site():
    if not USE_USER_SITE:
        return
    path = site.getusersitepackages()
    os.makedirs(path, exist_ok=True)
    if path not in sys.path:
        site.addsitedir(path)


ensure_user_site()


def normalize(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def requirement_name(req: str) -> str | None:
    m = _REQ_NAME_RE.match(req)
    return normalize(m.group(1)) if m else None


def validate_name(name: str) -> str:
    if not NAME_RE.match(name):
        raise LedroideError("INVALID_PACKAGE_NAME", f"Nom de bibliothèque invalide : {name}", 422)
    return name


class InstallPayload(BaseModel):
    spec: str

    @field_validator("spec")
    @classmethod
    def _check_spec(cls, spec: str) -> str:
        spec = spec.strip()
        if len(spec) > 200 or not SPEC_RE.match(spec):
            raise ValueError("Format attendu : nom, nom==1.2.3 ou nom>=1,<2")
        return spec


async def run_pip(args: list[str], on_line: Callable[[str], Awaitable[None]] | None = None) -> tuple[bool, list[str]]:
    if USE_USER_SITE and args[:1] == ["install"]:
        args = ["install", "--user", *args[1:]]
    async with pip_lock:
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "pip", *args, "--disable-pip-version-check", "--no-input",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        lines: list[str] = []
        assert proc.stdout is not None
        async for raw in proc.stdout:
            line = raw.decode(errors="replace").rstrip()
            lines.append(line)
            if on_line is not None:
                await on_line(line)
        ok = (await proc.wait()) == 0
        ensure_user_site()
        importlib.invalidate_caches()
        return ok, lines


def _installed() -> dict[str, md.Distribution]:
    out: dict[str, md.Distribution] = {}
    for dist in md.distributions():
        name = dist.metadata["Name"]
        if name and normalize(name) not in out:
            out[normalize(name)] = dist
    return out


def _requires(dist: md.Distribution) -> set[str]:
    return {n for r in (dist.requires or []) if (n := requirement_name(r))}


def _protected(dists: dict[str, md.Distribution]) -> set[str]:
    seen: set[str] = set()
    stack = [n for n in CORE_ROOTS if n in dists]
    while stack:
        name = stack.pop()
        if name in seen:
            continue
        seen.add(name)
        stack.extend(n for n in _requires(dists[name]) if n in dists and n not in seen)
    return seen


def _loaded(name: str) -> bool:
    """True when the distribution's code is already imported: changes only apply after a restart."""
    for top, dist_names in md.packages_distributions().items():
        if any(normalize(d) == name for d in dist_names) and top in sys.modules:
            return True
    return False


class PackageManager:
    def __init__(self, broadcast: Callable[[str, dict], Awaitable[None]]):
        self._broadcast = broadcast

    def listing(self, modules: Iterable[dict]) -> dict:
        dists = _installed()
        protected = _protected(dists)
        required_by: dict[str, list[str]] = {}
        for name, dist in dists.items():
            for dep in _requires(dist):
                required_by.setdefault(dep, []).append(dist.metadata["Name"])
        used_by: dict[str, list[str]] = {}
        for mod in modules:
            for req in (mod.get("dependencies") or {}).get("python", []) or []:
                if isinstance(req, str) and (n := requirement_name(req)):
                    used_by.setdefault(n, []).append(mod["id"])

        packages = [
            {
                "name": dist.metadata["Name"],
                "key": name,
                "version": dist.version,
                "summary": dist.metadata.get("Summary") or "",
                "protected": name in protected,
                "required_by": sorted(required_by.get(name, []), key=str.lower),
                "modules": sorted(used_by.get(name, [])),
            }
            for name, dist in dists.items()
        ]
        packages.sort(key=lambda p: p["key"])
        return {"python": sys.version.split()[0], "executable": sys.executable, "packages": packages}

    async def outdated(self) -> dict:
        ok, lines = await run_pip(["list", "--outdated", "--format=json"])
        if not ok:
            raise LedroideError("PIP_FAILED", "Impossible de vérifier les mises à jour (connexion ?)", 502)
        try:
            data = json.loads(next(l for l in lines if l.lstrip().startswith("[")))
        except (StopIteration, ValueError):
            raise LedroideError("PIP_FAILED", "Réponse de pip illisible", 502)
        return {"outdated": {normalize(d["name"]): d.get("latest_version") for d in data if "name" in d}}

    async def install(self, spec: str) -> dict:
        name = requirement_name(spec) or spec
        self._check_mutable(name, "installer")
        return await self._run("install", name, ["install", *self._constraints(), spec], restart=_loaded(name))

    async def upgrade(self, name: str) -> dict:
        key = normalize(validate_name(name))
        self._check_mutable(key, "mettre à jour")
        if key not in _installed():
            raise LedroideError("PACKAGE_NOT_INSTALLED", f"{name} n'est pas installée", 404)
        return await self._run("upgrade", key, ["install", "--upgrade", *self._constraints(), name], restart=_loaded(key))

    async def uninstall(self, name: str) -> dict:
        key = normalize(validate_name(name))
        self._check_mutable(key, "désinstaller")
        if key not in _installed():
            raise LedroideError("PACKAGE_NOT_INSTALLED", f"{name} n'est pas installée", 404)
        return await self._run("uninstall", key, ["uninstall", "-y", name], restart=_loaded(key))

    def _constraints(self) -> list[str]:
        """Pins the core packages to their current version so a new library cannot
        silently upgrade or downgrade what the backend runs on."""
        dists = _installed()
        pins = sorted(f"{dists[n].metadata['Name']}=={dists[n].version}" for n in _protected(dists))
        path = settings.DATA_DIR / "pip-constraints.txt"
        path.write_text("\n".join(pins) + "\n", encoding="utf-8")
        return ["--constraint", str(path)]

    def _check_mutable(self, key: str, verb: str):
        dists = _installed()
        if key in CORE_ROOTS or key in _protected(dists):
            raise LedroideError(
                "PACKAGE_PROTECTED",
                f"Impossible de {verb} {key} : bibliothèque utilisée par Ledroide lui-même",
                409,
            )

    async def _run(self, action: str, target: str, args: list[str], restart: bool) -> dict:
        await self._broadcast("packages:start", {"action": action, "target": target})

        async def emit(line: str):
            await self._broadcast("packages:log", {"target": target, "line": line})

        ok, lines = await run_pip(args, emit)
        await self._broadcast("packages:end", {"action": action, "target": target, "ok": ok})
        log.info("pip %s %s: %s", action, target, "OK" if ok else "FAILED")
        return {"ok": ok, "output": "\n".join(lines[-200:]), "restart_recommended": ok and restart}

