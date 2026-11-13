import time
import io
import json
import zipfile

import pytest

from conftest import TOKEN, write_module

SIMPLE = "from ledroide_sdk import Module\nmodule = Module(__file__)\n"


def _statuses(client):
    return {m["id"]: m for m in client.get("/modules", headers=TOKEN).json()}


def test_remote_modules_listed_only_when_absent_locally(client):
    write_module(client.remote_dir, "delta", SIMPLE, category="Réseau")
    write_module(client.remote_dir, "alpha", SIMPLE)
    client.post("/system/reload", headers=TOKEN)
    mods = _statuses(client)
    assert mods["delta"]["status"] == "available" and mods["delta"]["source"] == "remote"
    assert mods["alpha"]["status"] == "installed" and "source" not in mods["alpha"]


def test_install_from_remote_then_uninstall_makes_it_available_again(client):
    write_module(client.remote_dir, "delta", SIMPLE)
    client.post("/system/reload", headers=TOKEN)
    r = client.post("/modules/install", json={"ids": ["delta"]}, headers=TOKEN).json()
    assert r["installed"] == 1, r
    assert (client.user_modules_dir / "delta" / "manifest.json").exists()
    assert _statuses(client)["delta"]["status"] == "installed"
    assert client.post("/modules/delta/activate", headers=TOKEN).json()["status"] == "active"
    client.delete("/modules/delta", headers=TOKEN)
    assert not (client.user_modules_dir / "delta").exists()
    assert _statuses(client)["delta"]["status"] == "available"


def test_module_removed_from_remote_disappears(client):
    write_module(client.remote_dir, "delta", SIMPLE)
    client.post("/system/reload", headers=TOKEN)
    assert "delta" in _statuses(client)
    import shutil
    shutil.rmtree(client.remote_dir / "delta")
    client.post("/system/reload", headers=TOKEN)
    assert "delta" not in _statuses(client)


def _zipball(entries: dict[str, str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, content in entries.items():
            zf.writestr(name, content)
    return buf.getvalue()


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


def test_refresh_downloads_and_extracts_zipball(client, monkeypatch):
    from core import settings
    monkeypatch.setattr(settings, "CATALOG_REPO", "owner/repo")
    remote = client.app_main.registry.remote
    manifest = json.dumps({"id": "epsilon", "name": "Epsilon", "version": "1.0.0"})
    archive = _zipball({"owner-repo-abc/epsilon/manifest.json": manifest,
                        "owner-repo-abc/epsilon/backend/main.py": SIMPLE,
                        "owner-repo-abc/README.md": "x"})
    calls = []

    def fake_request(url):
        calls.append(url)
        if "/commits/" in url:
            return _Resp(json.dumps({"sha": "abc"}).encode())
        return _Resp(archive)

    monkeypatch.setattr(remote, "_request", fake_request)
    info = client.post("/modules/catalog/refresh", headers=TOKEN).json()
    assert info["error"] is None and info["count"] == 1 and info["sha"] == "abc", info
    assert _statuses(client)["epsilon"]["status"] == "available"
    assert calls[-1].endswith("/zipball/abc")
    # same sha: no second download
    client.post("/modules/catalog/refresh", headers=TOKEN)
    assert sum("/zipball/" in c for c in calls) == 1


def test_refresh_rejects_path_traversal(client, monkeypatch):
    from core import settings
    monkeypatch.setattr(settings, "CATALOG_REPO", "owner/repo")
    remote = client.app_main.registry.remote
    archive = _zipball({"owner-repo-abc/../../evil.txt": "x"})
    monkeypatch.setattr(remote, "_request", lambda url: _Resp(json.dumps({"sha": "z"}).encode())
                        if "/commits/" in url else _Resp(archive))
    info = client.post("/modules/catalog/refresh", headers=TOKEN).json()
    assert info["code"] == "CATALOG_BAD_ARCHIVE", info
    assert not (settings.REMOTE_CATALOG_DIR.parent / "evil.txt").exists()


def test_refresh_reports_missing_token_on_private_repo(client, monkeypatch):
    import urllib.error
    from core import settings
    monkeypatch.setattr(settings, "CATALOG_REPO", "owner/private")
    for k in ("GITHUB_TOKEN", "GH_TOKEN", "LEDROIDE_GITHUB_TOKEN"):
        monkeypatch.delenv(k, raising=False)

    class Opener:
        def open(self, req, timeout=None):
            raise urllib.error.HTTPError(req.full_url, 404, "Not Found", {}, None)

    monkeypatch.setattr("urllib.request.build_opener", lambda *a: Opener())
    info = client.post("/modules/catalog/refresh", headers=TOKEN).json()
    assert info["code"] == "CATALOG_NOT_FOUND" and "GITHUB_TOKEN" in info["error"], info
    assert client.get("/modules/catalog", headers=TOKEN).json()["has_token"] is False


def test_catalog_routes_require_token(client):
    assert client.get("/modules/catalog").status_code == 403
    assert client.post("/modules/catalog/refresh").status_code == 403


@pytest.mark.parametrize("token_var", ["GITHUB_TOKEN"])
def test_token_sent_only_to_github_api_host(monkeypatch, token_var):
    import urllib.request
    from core.remote_catalog import _KeepAuthRedirect
    h = _KeepAuthRedirect()
    req = urllib.request.Request("https://api.github.com/x", headers={"Authorization": "Bearer t"})
    new = h.redirect_request(req, None, 302, "Found", {}, "https://codeload.github.com/y")
    assert "Authorization" not in new.headers


def _bump(root, mid, version, code=SIMPLE, **extra):
    import shutil
    shutil.rmtree(root / mid, ignore_errors=True)
    write_module(root, mid, code, version=version, **extra)


def test_version_comparison():
    from core.registry import is_newer
    assert is_newer("1.10.0", "1.9.9")
    assert is_newer("2", "1.99")
    assert not is_newer("1.0.0", "1.0")
    assert not is_newer("1.0.0-beta", "1.0.0")
    assert not is_newer("", "1.0.0")


def test_update_available_for_installed_and_bundled(client):
    _bump(client.remote_dir, "alpha", "1.1.0")
    _bump(client.remote_dir, "beta", "0.9.0")
    client.post("/system/reload", headers=TOKEN)
    mods = _statuses(client)
    assert mods["alpha"]["update_available"] is True and mods["alpha"]["remote_version"] == "1.1.0"
    assert mods["beta"]["update_available"] is False
    assert mods["broken"]["update_available"] is False and mods["broken"]["remote_version"] is None


def test_update_replaces_files_keeps_data_and_reactivates(client):
    code_v2 = SIMPLE + "@module.on('ver')\nasync def ver(payload):\n    return {'v': 2}\n"
    write_module(client.remote_dir, "delta", SIMPLE)
    client.post("/system/reload", headers=TOKEN)
    client.post("/modules/install", json={"ids": ["delta"]}, headers=TOKEN)
    assert client.post("/modules/delta/activate", headers=TOKEN).json()["status"] == "active"
    marker = client.user_modules_dir / "delta" / "frontend" / "old.txt"
    marker.write_text("x")

    _bump(client.remote_dir, "delta", "2.0.0", code_v2)
    client.post("/system/reload", headers=TOKEN)
    assert _statuses(client)["delta"]["update_available"] is True

    r = client.post("/modules/delta/update", headers=TOKEN).json()
    assert r["status"] == "active" and r["version"] == "2.0.0" and r["previous_version"] == "1.0.0", r
    assert not marker.exists()
    assert not list(client.user_modules_dir.glob(".delta.*"))
    mods = _statuses(client)
    assert mods["delta"]["version"] == "2.0.0" and mods["delta"]["update_available"] is False
    with client.websocket_connect("ws://127.0.0.1:8765/ws?token=test-token") as ws:
        ws.send_json({"type": "call", "module_id": "delta", "action": "ver", "payload": {}, "request_id": "1"})
        while (msg := ws.receive_json()).get("request_id") != "1":
            pass
        assert msg["payload"] == {"v": 2}


def test_update_bundled_module_installs_user_copy(client):
    _bump(client.remote_dir, "beta", "1.5.0", helper="remote")
    client.post("/system/reload", headers=TOKEN)
    r = client.post("/modules/beta/update", headers=TOKEN).json()
    assert r["status"] == "installed" and r["version"] == "1.5.0", r
    assert (client.user_modules_dir / "beta" / "manifest.json").exists()
    assert (client.modules_dir / "beta" / "manifest.json").exists()
    assert _statuses(client)["beta"]["bundled"] is False
    client.delete("/modules/beta", headers=TOKEN)
    mods = _statuses(client)
    assert mods["beta"]["version"] == "1.0.0" and mods["beta"]["bundled"] is True


def test_update_errors(client):
    assert client.post("/modules/alpha/update", headers=TOKEN).json()["code"] == "UPDATE_UNAVAILABLE"
    assert client.post("/modules/gamma/update", headers=TOKEN).json()["code"] == "MODULE_NOT_INSTALLED"
    assert client.post("/modules/alpha/update").status_code in (401, 403)


def test_reinstall_deps_forces_declared_packages(client, monkeypatch):
    from core import registry as reg
    calls = []

    async def fake_pip(args, on_line=None):
        calls.append(args)
        return True, ["ok"]

    monkeypatch.setattr(reg, "run_pip", fake_pip)
    mod = client.app_main.registry._modules["alpha"]
    mod["dependencies"] = {"python": ["Pillow>=11,<12", "fastapi"]}
    r = client.post("/modules/alpha/deps?reinstall=true", headers=TOKEN).json()
    assert r["ok"] is True
    assert calls[0][:4] == ["install", "--force-reinstall", "--no-deps", "--no-cache-dir"]
    assert "Pillow>=11,<12" in calls[0] and "fastapi" not in calls[0]
    assert calls[1] == ["install", "Pillow>=11,<12", "fastapi"]
    calls.clear()
    client.post("/modules/alpha/deps", headers=TOKEN)
    assert calls == [["install", "Pillow>=11,<12", "fastapi"]]


def test_missing_requirements():
    from core.deps import missing_requirements
    assert missing_requirements(["pytest", "fastapi>=0.1"]) == []
    assert missing_requirements(["not-a-real-pkg-xyz", "pytest<1", "pytest; python_version < '3'"]) == ["not-a-real-pkg-xyz", "pytest<1"]


def _fake_pip(monkeypatch, installs=True):
    from core import registry as reg
    state = {"installed": False, "calls": []}

    async def fake_pip(args, on_line=None):
        state["calls"].append(args)
        if on_line:
            await on_line("Collecting fake")
        state["installed"] = installs
        return installs, ["Collecting fake"]

    real = reg.missing_requirements
    monkeypatch.setattr(reg, "run_pip", fake_pip)
    monkeypatch.setattr(reg, "missing_requirements",
                        lambda reqs: [] if state["installed"] else [r for r in real(reqs)])
    return state


def test_activate_reports_missing_deps_then_repair_activates(client, monkeypatch):
    write_module(client.modules_dir, "needy", SIMPLE, dependencies={"python": ["not-a-real-pkg-xyz>=1"]})
    client.post("/system/reload", headers=TOKEN)
    r = client.post("/modules/needy/activate", headers=TOKEN).json()
    assert r["code"] == "MISSING_DEPS" and "not-a-real-pkg-xyz" in r["error"]
    mod = _statuses(client)["needy"]
    assert mod["status"] == "error" and mod["missing_deps"] == ["not-a-real-pkg-xyz>=1"]

    pip = _fake_pip(monkeypatch)
    r = client.post("/modules/repair", headers=TOKEN).json()
    assert r["ok"] and r["activated"] == ["needy"], r
    assert pip["calls"] == [["install", "not-a-real-pkg-xyz>=1"]]
    assert _statuses(client)["needy"]["status"] == "active"
    assert client.get("/modules/repair", headers=TOKEN).json()["running"] is False


def test_startup_restores_active_modules_with_missing_deps(client, monkeypatch):
    write_module(client.modules_dir, "needy", SIMPLE, dependencies={"python": ["not-a-real-pkg-xyz>=1"]})
    registry = client.app_main.registry
    registry._state["active_modules"] = ["alpha", "needy"]
    registry._state["environment"] = {"app": "0.0.1", "python": "3.0"}
    pip = _fake_pip(monkeypatch)
    events = []
    real_broadcast = registry.broadcast

    async def spy(module_id, event, payload):
        events.append((event, payload))
        await real_broadcast(module_id, event, payload)

    monkeypatch.setattr(registry, "broadcast", spy)
    client.portal.call(registry.load_all)
    for _ in range(100):
        if _statuses(client)["needy"]["status"] == "active":
            break
        time.sleep(0.05)
    mods = _statuses(client)
    assert mods["needy"]["status"] == "active" and mods["alpha"]["status"] == "active"
    assert pip["calls"] == [["install", "not-a-real-pkg-xyz>=1"]]
    repair_events = [p for e, p in events if e == "modules:repair"]
    assert repair_events[0]["reason"] == "update" and repair_events[-1]["stage"] == "end"
    assert "needy" in registry._state["active_modules"]


def test_failed_restore_keeps_module_marked_active(client, monkeypatch):
    write_module(client.modules_dir, "needy", SIMPLE, dependencies={"python": ["not-a-real-pkg-xyz>=1"]})
    registry = client.app_main.registry
    registry._state["active_modules"] = ["needy"]
    _fake_pip(monkeypatch, installs=False)
    client.portal.call(registry.load_all)
    for _ in range(100):
        if registry.repair_status().get("stage") == "end":
            break
        time.sleep(0.05)
    st = registry.repair_status()
    assert st["pip_ok"] is False and st["failed"][0]["id"] == "needy"
    assert _statuses(client)["needy"]["status"] == "error"
    assert "needy" in registry._state["active_modules"]
