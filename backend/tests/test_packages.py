import pytest

from conftest import TOKEN


def test_packages_require_token(client):
    assert client.get("/system/packages").status_code == 403
    assert client.post("/system/packages", json={"spec": "requests"}).status_code == 403


def test_list_marks_core_packages_protected(client):
    r = client.get("/system/packages", headers=TOKEN)
    assert r.status_code == 200
    pkgs = {p["key"]: p for p in r.json()["packages"]}
    assert pkgs["fastapi"]["protected"]
    assert pkgs["starlette"]["protected"]
    assert "fastapi" in pkgs["starlette"]["required_by"]


@pytest.mark.parametrize("name", ["fastapi", "Starlette", "pip"])
def test_core_packages_cannot_be_removed_or_upgraded(client, name):
    r = client.delete(f"/system/packages/{name}", headers=TOKEN)
    assert r.status_code == 409 and r.json()["code"] == "PACKAGE_PROTECTED"
    r = client.post(f"/system/packages/{name}/upgrade", headers=TOKEN)
    assert r.status_code == 409


def test_core_package_cannot_be_reinstalled_at_other_version(client):
    r = client.post("/system/packages", json={"spec": "pydantic==1.0"}, headers=TOKEN)
    assert r.status_code == 409


@pytest.mark.parametrize("spec", ["-r evil.txt", "--index-url=http://x", "pkg; rm -rf /", "a b", "http://x/y.whl", ""])
def test_install_rejects_options_and_urls(client, spec):
    r = client.post("/system/packages", json={"spec": spec}, headers=TOKEN)
    assert r.status_code == 422 and r.json()["code"] == "VALIDATION_ERROR"


def test_uninstall_unknown_package(client):
    r = client.delete("/system/packages/surely-not-installed-xyz", headers=TOKEN)
    assert r.status_code == 404 and r.json()["code"] == "PACKAGE_NOT_INSTALLED"


def test_invalid_package_name(client):
    r = client.delete("/system/packages/-y", headers=TOKEN)
    assert r.status_code == 422


def test_module_dependencies_are_reported(client, monkeypatch):
    import main
    original = main.registry.list_modules
    monkeypatch.setattr(main.registry, "list_modules",
                        lambda: [*original(), {"id": "zeta", "dependencies": {"python": ["FastAPI>=0.1"]}}])
    pkgs = {p["key"]: p for p in client.get("/system/packages", headers=TOKEN).json()["packages"]}
    assert pkgs["fastapi"]["modules"] == ["zeta"]
