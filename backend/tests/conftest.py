import json
import os
import sys
import tempfile
import textwrap
from pathlib import Path

import pytest

_DATA = tempfile.mkdtemp(prefix="ledroide-test-data-")
os.environ["LEDROIDE_DATA_DIR"] = _DATA
os.environ["LEDROIDE_TOKEN"] = "test-token"
os.environ["LEDROIDE_BACKEND_PORT"] = "8765"

BACKEND = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(BACKEND), str(BACKEND / "sdk")]

TOKEN = {"X-Ledroide-Token": "test-token"}
BASE_URL = "http://127.0.0.1:8765"

ALPHA = '''
import asyncio
from ledroide_sdk import Module
from helper import WHO

module = Module(__file__)
router = module.router

@router.get("/ping")
async def ping():
    return {"pong": True, "who": WHO}

@module.on("echo")
async def echo(payload):
    return {"echo": payload.get("value")}

@module.on("slow")
async def slow(payload):
    await asyncio.sleep(payload.get("delay", 0.5))
    return {"slow": True}

@module.job("work")
async def work(payload, job):
    await job.progress(50, "half")
    return {"total": payload.get("n", 0) * 2}

@module.on("store")
async def store(payload):
    doc = await module.db.save("items", {"name": payload["name"]})
    return {"doc": doc, "all": await module.db.query("items")}
'''

BETA = '''
from ledroide_sdk import Module
from helper import WHO
module = Module(__file__)

@module.on("who")
async def who(payload):
    return {"who": WHO}
'''


def _write_module(root: Path, mid: str, code: str, helper: str | None = None, **manifest):
    d = root / mid
    (d / "backend").mkdir(parents=True)
    (d / "frontend").mkdir()
    (d / "frontend" / "index.html").write_text("<h1>" + mid + "</h1>")
    (d / "backend" / "main.py").write_text(textwrap.dedent(code))
    if helper is not None:
        (d / "backend" / "helper.py").write_text(f"WHO = {helper!r}\n")
    (d / "manifest.json").write_text(json.dumps({"id": mid, "name": mid, "version": "1.0.0", **manifest}))


@pytest.fixture()
def client(tmp_path, monkeypatch):
    from core import settings
    modules = tmp_path / "modules"
    available = tmp_path / "modules-available"
    modules.mkdir()
    available.mkdir()
    _write_module(modules, "alpha", ALPHA, helper="alpha", database={"type": "sqlite"},
                  config_schema={"step": {"type": "integer", "default": 1}})
    _write_module(modules, "beta", BETA, helper="beta")
    _write_module(modules, "broken", "raise RuntimeError('boom')\n")
    _write_module(available, "gamma", "from ledroide_sdk import Module\nmodule = Module(__file__)\n")
    (available / "catalog.json").write_text(
        json.dumps({"modules": [{"id": "gamma", "name": "Gamma", "version": "1.0.0"}]})
    )

    monkeypatch.setattr(settings, "MODULES_DIR", modules)
    monkeypatch.setattr(settings, "AVAILABLE_DIR", available)
    monkeypatch.setattr(settings, "USER_MODULES_DIR", tmp_path / "installed-modules")
    for f in (settings.STATE_FILE, settings.CONFIGS_FILE):
        f.unlink(missing_ok=True)

    import importlib
    import main
    importlib.reload(main)
    from fastapi.testclient import TestClient
    with TestClient(main.app, base_url=BASE_URL) as c:
        c.modules_dir = modules
        c.user_modules_dir = tmp_path / "installed-modules"
        yield c
