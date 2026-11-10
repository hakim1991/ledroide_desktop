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
