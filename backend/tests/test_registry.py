import time

import pytest
from starlette.websockets import WebSocketDisconnect

from conftest import TOKEN

WS = "ws://127.0.0.1:8765/ws?token=test-token"


def call(ws, module_id, action, payload=None, rid="1"):
    ws.send_json({"type": "call", "module_id": module_id, "action": action, "payload": payload or {}, "request_id": rid})


def test_management_requires_token(client):
    assert client.get("/modules").status_code == 403
    assert client.get("/modules", headers=TOKEN).status_code == 200


def test_foreign_origin_rejected_even_on_module_api(client):
    client.post("/modules/alpha/activate", headers=TOKEN)
    assert client.get("/modules/alpha/api/ping", headers={"Origin": "https://evil.example"}).status_code == 403
    assert client.get("/modules/alpha/api/ping", headers={"Origin": "http://127.0.0.1:8765"}).status_code == 200


def test_dns_rebinding_host_rejected(client):
    assert client.get("/system/health", headers={"Host": "evil.example:8765"}).status_code == 403


def test_activate_mounts_routes_once(client):
    baseline = len(client.app.router.routes)
    client.post("/modules/alpha/activate", headers=TOKEN)
    mounted = len(client.app.router.routes)
    for _ in range(3):
        assert client.post("/modules/alpha/deactivate", headers=TOKEN).status_code == 200
        assert len(client.app.router.routes) == baseline
        assert client.post("/modules/alpha/activate", headers=TOKEN).status_code == 200
        assert len(client.app.router.routes) == mounted
    assert client.get("/modules/alpha/api/ping").json() == {"pong": True, "who": "alpha"}
    assert client.get("/modules/alpha/frontend/").status_code == 200


def test_deactivate_unmounts(client):
    client.post("/modules/alpha/activate", headers=TOKEN)
    client.post("/modules/alpha/deactivate", headers=TOKEN)
    assert client.get("/modules/alpha/api/ping").status_code == 404
    assert client.get("/modules/alpha/frontend/").status_code == 404


def test_same_named_helpers_are_isolated(client):
    client.post("/modules/alpha/activate", headers=TOKEN)
    client.post("/modules/beta/activate", headers=TOKEN)
    assert client.get("/modules/alpha/api/ping").json()["who"] == "alpha"
    with client.websocket_connect(WS) as ws:
        call(ws, "beta", "who")
        assert ws.receive_json()["payload"] == {"who": "beta"}


def test_broken_module_reports_error(client):
    r = client.post("/modules/broken/activate", headers=TOKEN)
    assert r.status_code == 500
    assert r.json()["code"] == "MODULE_LOAD_FAILED"
    mods = {m["id"]: m for m in client.get("/modules", headers=TOKEN).json()}
    assert mods["broken"]["status"] == "error"
    assert "boom" in mods["broken"]["last_error"]


@pytest.mark.parametrize("bad", ["..", "%2E%2E", "a b", "UPPER", "x" * 80])
def test_invalid_ids_never_delete(client, bad):
    r = client.delete(f"/modules/{bad}", headers=TOKEN)
    assert r.status_code in (400, 404, 405)
    assert client.modules_dir.exists()
    assert (client.modules_dir / "alpha").exists()


def test_uninstall_unknown_does_not_delete(client):
    r = client.delete("/modules/nope", headers=TOKEN)
    assert r.status_code == 404
    assert r.json() == {"error": "Module 'nope' introuvable", "code": "MODULE_NOT_FOUND"}


def test_install_from_local_catalog_and_uninstall(client):
    assert client.post("/modules/gamma/install", headers=TOKEN).json()["status"] == "installed"
    assert (client.modules_dir / "gamma" / "manifest.json").exists()
    assert client.post("/modules/gamma/activate", headers=TOKEN).status_code == 200
    assert client.delete("/modules/gamma", headers=TOKEN).json()["status"] == "uninstalled"
    assert not (client.modules_dir / "gamma").exists()
    mods = {m["id"]: m for m in client.get("/modules", headers=TOKEN).json()}
    assert mods["gamma"]["status"] == "available"


def test_config_defaults_and_persist(client):
    assert client.get("/modules/alpha/config", headers=TOKEN).json() == {"step": 1}
    client.put("/modules/alpha/config", headers=TOKEN, json={"step": 5})
    assert client.get("/modules/alpha/config", headers=TOKEN).json() == {"step": 5}


def test_ws_requires_token(client):
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("ws://127.0.0.1:8765/ws") as ws:
            ws.receive_json()


def test_ws_calls_run_concurrently(client):
    client.post("/modules/alpha/activate", headers=TOKEN)
    with client.websocket_connect(WS) as ws:
        start = time.monotonic()
        call(ws, "alpha", "slow", {"delay": 1}, rid="slow")
        call(ws, "alpha", "echo", {"value": 42}, rid="fast")
        first = ws.receive_json()
        assert first["request_id"] == "fast"
        assert first["payload"] == {"echo": 42}
        assert time.monotonic() - start < 0.9
        assert ws.receive_json()["request_id"] == "slow"


def test_unknown_action_and_inactive_module(client):
    client.post("/modules/alpha/activate", headers=TOKEN)
    with client.websocket_connect(WS) as ws:
        call(ws, "alpha", "nope")
        assert ws.receive_json()["payload"]["code"] == "UNKNOWN_ACTION"
        call(ws, "beta", "who")
        assert ws.receive_json()["payload"]["code"] == "MODULE_NOT_ACTIVE"


def test_jobs_emit_progress_and_done(client):
    client.post("/modules/alpha/activate", headers=TOKEN)
    with client.websocket_connect(WS) as ws:
        call(ws, "alpha", "work", {"n": 21})
        msgs = [ws.receive_json() for _ in range(3)]
        job_id = next(m for m in msgs if m["type"] == "response")["payload"]["job_id"]
        events = {m["action"]: m["payload"] for m in msgs if m["type"] == "event"}
        assert events["job:progress"]["percent"] == 50
        assert events["job:done"] == {"job_id": job_id, "result": {"total": 42}}


def test_sqlite_document_store(client):
    client.post("/modules/alpha/activate", headers=TOKEN)
    with client.websocket_connect(WS) as ws:
        call(ws, "alpha", "store", {"name": "a"})
        payload = ws.receive_json()["payload"]
        assert payload["doc"]["name"] == "a"
        assert [d["name"] for d in payload["all"]] == ["a"]


def test_http_exception_keeps_detail_and_adds_error_code(client):
    r = client.get("/modules/alpha/api/does-not-exist")
    assert r.status_code == 404
    body = r.json()
    assert body["code"] == "HTTP_404" and "detail" in body


def test_env_vars_applied_and_removed(client):
    import os
    r = client.put("/system/env", headers=TOKEN, json={"vars": [
        {"key": "MY_API_KEY", "value": "abc", "secret": True},
        {"key": "OTHER", "value": "1"},
    ]})
    assert r.status_code == 200
    assert sorted(r.json()["changed"]) == ["MY_API_KEY", "OTHER"]
    assert os.environ["MY_API_KEY"] == "abc"
    assert client.get("/system/env", headers=TOKEN).json()["vars"][0]["secret"] is True
    client.put("/system/env", headers=TOKEN, json={"vars": [{"key": "OTHER", "value": "2"}]})
    assert "MY_API_KEY" not in os.environ and os.environ["OTHER"] == "2"
    client.put("/system/env", headers=TOKEN, json={"vars": []})
    assert "OTHER" not in os.environ


@pytest.mark.parametrize("key", ["PATH", "LEDROIDE_TOKEN", "1BAD", "with space"])
def test_env_rejects_reserved_or_invalid_keys(client, key):
    r = client.put("/system/env", headers=TOKEN, json={"vars": [{"key": key, "value": "x"}]})
    assert r.status_code == 422


def test_env_requires_token(client):
    assert client.get("/system/env").status_code == 403


def test_category_tags_defaults_and_override(client):
    mods = {m["id"]: m for m in client.get("/modules", headers=TOKEN).json()}
    assert mods["beta"]["category"] == "Autres" and mods["beta"]["tags"] == []
    r = client.put("/modules/beta/meta", headers=TOKEN, json={"category": "Réseau", "tags": ["DNS", "dns", " outil "]})
    assert r.json()["category"] == "Réseau"
    assert r.json()["tags"] == ["dns", "outil"]
    mods = {m["id"]: m for m in client.get("/modules", headers=TOKEN).json()}
    assert mods["beta"]["meta_overridden"] is True
    client.put("/modules/beta/meta", headers=TOKEN, json={})
    mods = {m["id"]: m for m in client.get("/modules", headers=TOKEN).json()}
    assert mods["beta"]["category"] == "Autres" and mods["beta"]["meta_overridden"] is False


def test_declared_env_reports_missing(client, monkeypatch):
    import json
    manifest = client.modules_dir / "beta" / "manifest.json"
    data = json.loads(manifest.read_text())
    data["env"] = [{"key": "BETA_TOKEN", "required": True}, "BETA_OPTIONAL"]
    manifest.write_text(json.dumps(data))
    client.post("/system/reload", headers=TOKEN)
    client.post("/modules/beta/activate", headers=TOKEN)
    client.post("/modules/beta/deactivate", headers=TOKEN)
    import main
    main.registry._modules["beta"]["env"] = data["env"]
    mods = {m["id"]: m for m in client.get("/modules", headers=TOKEN).json()}
    assert set(mods["beta"]["env_missing"]) == {"BETA_TOKEN", "BETA_OPTIONAL"}
    client.put("/system/env", headers=TOKEN, json={"vars": [{"key": "BETA_TOKEN", "value": "x"}]})
    mods = {m["id"]: m for m in client.get("/modules", headers=TOKEN).json()}
    assert mods["beta"]["env_missing"] == ["BETA_OPTIONAL"]
    client.put("/system/env", headers=TOKEN, json={"vars": []})
