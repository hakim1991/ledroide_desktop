import asyncio
import itertools
import json
import logging
import re
import ssl
import threading
import time
import urllib.parse
import uuid

from ledroide_sdk import Module

log = logging.getLogger("ledroide.ws-mqtt-tester")

module = Module(__file__)
router = module.router

_seq = itertools.count(1)
_log: list[dict] = []


def _err(message: str, code: str) -> dict:
    return {"error": message, "code": code}


def _clip(text: str) -> tuple[str, bool]:
    limit = int(module.config.get("max_message_kb", 64)) * 1024
    if len(text) > limit:
        return text[:limit], True
    return text, False


async def _record(kind: str, direction: str, data: str = "", **extra) -> dict:
    data, truncated = _clip(data)
    entry = {"id": next(_seq), "ts": time.time(), "kind": kind, "dir": direction, "data": data,
             "truncated": truncated, **extra}
    _log.append(entry)
    excess = len(_log) - int(module.config.get("log_size", 1000))
    if excess > 0:
        del _log[:excess]
    await module.events.emit("log", entry)
    return entry


async def _status_changed():
    await module.events.emit("status", await status({}))


# ── WebSocket ────────────────────────────────────────────────────────────────

class WsSession:
    def __init__(self, ws, url: str):
        self.ws = ws
        self.url = url
        self.since = time.time()
        self.reader: asyncio.Task | None = None


_ws: WsSession | None = None


def _parse_headers(items) -> list[tuple[str, str]]:
    out = []
    for h in items or []:
        if isinstance(h, dict):
            k, v = str(h.get("key", "")).strip(), str(h.get("value", ""))
        else:
            k, _, v = str(h).partition(":")
            k = k.strip()
        if not k:
            continue
        if not re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+", k) or "\n" in v or "\r" in v:
            raise ValueError(f"En-tête invalide : {k}")
        out.append((k, v.strip()))
    return out


async def _ws_reader(sess: WsSession):
    global _ws
    from websockets.exceptions import ConnectionClosed
    reason = ""
    try:
        async for msg in sess.ws:
            if isinstance(msg, bytes):
                await _record("ws", "in", msg.hex(" "), binary=True, size=len(msg))
            else:
                await _record("ws", "in", msg, size=len(msg.encode("utf-8", "surrogatepass")))
    except ConnectionClosed:
        pass
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        reason = str(exc)
        log.warning("ws reader error: %s", type(exc).__name__)
    finally:
        if _ws is sess:
            _ws = None
            code = sess.ws.close_code
            text = sess.ws.close_reason or reason
            await _record("ws", "sys", f"Connexion fermée (code {code if code is not None else '—'}{', ' + text if text else ''})", event="closed")
            await _status_changed()


@module.on("ws_connect")
async def ws_connect(payload: dict):
    global _ws
    try:
        from websockets.asyncio.client import connect
        from websockets.exceptions import InvalidHandshake, InvalidStatus, InvalidURI
    except ImportError:
        return _err("La bibliothèque « websockets » est introuvable", "DEPENDENCY_MISSING")

    url = str(payload.get("url", "")).strip()
    scheme = urllib.parse.urlsplit(url).scheme.lower()
    if scheme not in ("ws", "wss"):
        return _err("L'URL doit commencer par ws:// ou wss://", "BAD_URL")
    try:
        headers = _parse_headers(payload.get("headers"))
    except ValueError as exc:
        return _err(str(exc), "BAD_HEADER")
    subprotocols = [s.strip() for s in str(payload.get("subprotocols", "")).split(",") if s.strip()]
    timeout = max(1.0, min(60.0, float(payload.get("timeout") or 10)))
    kwargs = {"additional_headers": headers or None, "subprotocols": subprotocols or None,
              "open_timeout": timeout, "max_size": 16 * 1024 * 1024, "ping_interval": None}
    if scheme == "wss":
        ctx = ssl.create_default_context()
        if payload.get("verify_ssl") is False:
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        kwargs["ssl"] = ctx

    await _ws_close()
    host = urllib.parse.urlsplit(url).netloc
    try:
        ws = await connect(url, **kwargs)
    except InvalidStatus as exc:
        status_code = getattr(getattr(exc, "response", None), "status_code", "?")
        return _err(f"Le serveur a refusé la connexion WebSocket (HTTP {status_code})", "HANDSHAKE_FAILED")
    except InvalidURI:
        return _err("URL WebSocket invalide", "BAD_URL")
    except InvalidHandshake as exc:
        return _err(f"Négociation WebSocket refusée : {exc}", "HANDSHAKE_FAILED")
    except ssl.SSLCertVerificationError as exc:
        return _err(f"Certificat SSL invalide : {exc.verify_message}", "SSL_ERROR")
    except (TimeoutError, asyncio.TimeoutError):
        return _err(f"Délai de connexion dépassé ({timeout:g} s)", "TIMEOUT")
    except OSError as exc:
        return _err(f"Connexion impossible à {host} : {exc.strerror or exc}", "CONNECT_FAILED")
    log.info("ws connected to %s", host)
    sess = WsSession(ws, url)
    _ws = sess
    sess.reader = asyncio.create_task(_ws_reader(sess))
    await _record("ws", "sys", f"Connecté à {url}" + (f" (sous-protocole : {ws.subprotocol})" if ws.subprotocol else ""), event="open")
    await _status_changed()
    return await status({})


async def _ws_close(code: int = 1000, reason: str = ""):
    sess = _ws
    if sess is None:
        return
    try:
        await asyncio.wait_for(sess.ws.close(code, reason), 5)
    except Exception:
        pass
    if sess.reader:
        try:
            await asyncio.wait_for(asyncio.shield(sess.reader), 3)
        except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
            sess.reader.cancel()


@module.on("ws_disconnect")
async def ws_disconnect(payload: dict):
    if _ws is None:
        return _err("Aucune connexion WebSocket ouverte", "NOT_CONNECTED")
    await _ws_close(1000, str(payload.get("reason", ""))[:120])
    return {"ok": True}


@module.on("ws_send")
async def ws_send(payload: dict):
    sess = _ws
    if sess is None:
        return _err("Aucune connexion WebSocket ouverte", "NOT_CONNECTED")
    text = str(payload.get("text", ""))
    mode = payload.get("mode", "text")
    if mode == "json":
        try:
            json.loads(text)
        except json.JSONDecodeError as exc:
            return _err(f"JSON invalide (ligne {exc.lineno}, colonne {exc.colno}) : {exc.msg}", "BAD_JSON")
    elif mode == "hex":
        try:
            data = bytes.fromhex(text)
        except ValueError:
            return _err("Hexadécimal invalide", "BAD_HEX")
    try:
        if mode == "hex":
            await sess.ws.send(data)
            entry = await _record("ws", "out", data.hex(" "), binary=True, size=len(data))
        else:
            await sess.ws.send(text)
            entry = await _record("ws", "out", text, size=len(text.encode("utf-8", "surrogatepass")))
    except Exception as exc:
        return _err(f"Envoi impossible : {exc}", "SEND_FAILED")
    return {"ok": True, "id": entry["id"]}


@module.on("ws_ping")
async def ws_ping(payload: dict):
    sess = _ws
    if sess is None:
        return _err("Aucune connexion WebSocket ouverte", "NOT_CONNECTED")
    started = time.perf_counter()
    try:
        waiter = await sess.ws.ping()
        await asyncio.wait_for(waiter, 10)
    except asyncio.TimeoutError:
        await _record("ws", "sys", "Ping : pas de pong après 10 s", event="ping")
        return _err("Pas de réponse au ping (10 s)", "TIMEOUT")
    except Exception as exc:
        return _err(f"Ping impossible : {exc}", "PING_FAILED")
    ms = round((time.perf_counter() - started) * 1000, 2)
    await _record("ws", "sys", f"Ping → pong en {ms} ms", event="ping")
    return {"latency_ms": ms}


# ── MQTT ─────────────────────────────────────────────────────────────────────

class MqttSession:
    def __init__(self, client, loop, info: dict):
        self.client = client
        self.loop = loop
        self.info = info
        self.subs: dict[str, int] = {}
        self.connected = False
        self.closing = False
        self.lock = threading.Lock()
        self.waiters: dict[int, asyncio.Future] = {}
        self.early: dict[int, list] = {}
        self.connect_future: asyncio.Future = loop.create_future()

    def emit(self, coro):
        try:
            asyncio.run_coroutine_threadsafe(coro, self.loop)
        except RuntimeError:
            coro.close()

    def resolve(self, mid: int, value):
        def done():
            fut = self.waiters.pop(mid, None)
            if fut is not None and not fut.done():
                fut.set_result(value)
            elif fut is None:
                self.early[mid] = value
        self.loop.call_soon_threadsafe(done)

    async def wait(self, mid: int, timeout: float):
        if mid in self.early:
            return self.early.pop(mid)
        fut = self.loop.create_future()
        self.waiters[mid] = fut
        try:
            return await asyncio.wait_for(fut, timeout)
        finally:
            self.waiters.pop(mid, None)


_mqtt: MqttSession | None = None


def _rc_failed(rc) -> bool:
    return bool(getattr(rc, "is_failure", False)) or (isinstance(rc, int) and rc >= 0x80)


def validate_topic(topic: str, allow_wildcards: bool) -> str:
    topic = (topic or "").strip()
    if not topic:
        raise ValueError("Le topic est obligatoire")
    if len(topic.encode("utf-8")) > 65535 or "\x00" in topic:
        raise ValueError("Topic invalide")
    levels = topic.split("/")
    for i, lvl in enumerate(levels):
        if "#" in lvl or "+" in lvl:
            if not allow_wildcards:
                raise ValueError("Les jokers + et # sont interdits dans un topic de publication")
            if lvl == "#" and i != len(levels) - 1:
                raise ValueError("Le joker # doit être le dernier niveau du topic")
            if lvl not in ("#", "+"):
                raise ValueError("Un joker doit occuper un niveau entier (ex. capteurs/+/temp)")
    return topic


def _payload_text(raw: bytes) -> tuple[str, bool]:
    try:
        return raw.decode("utf-8"), False
    except UnicodeDecodeError:
        return raw.hex(" "), True


def _make_client(mqtt, p: dict, client_id: str):
    version = mqtt.MQTTv5 if str(p.get("version")) == "5" else mqtt.MQTTv311
    kwargs = {"callback_api_version": mqtt.CallbackAPIVersion.VERSION2, "client_id": client_id, "protocol": version}
    if version != mqtt.MQTTv5:
        kwargs["clean_session"] = p.get("clean_session", True) is not False
    if p.get("transport") == "websockets":
        kwargs["transport"] = "websockets"
    return mqtt.Client(**kwargs)


@module.on("mqtt_connect")
async def mqtt_connect(payload: dict):
    global _mqtt
    try:
        import paho.mqtt.client as mqtt
    except ImportError:
        return _err("La bibliothèque « paho-mqtt » n'est pas installée. Réactivez le module ou installez « paho-mqtt>=2,<3 » "
                    "depuis le menu Bibliothèques.", "DEPENDENCY_MISSING")

    host = str(payload.get("host", "")).strip()
    if not host or not re.fullmatch(r"[A-Za-z0-9.\-:\[\]_]{1,253}", host):
        return _err("Adresse du broker invalide", "BAD_HOST")
    try:
        port = int(payload.get("port") or (8883 if payload.get("tls") else 1883))
        keepalive = int(payload.get("keepalive") or 60)
    except (TypeError, ValueError):
        return _err("Port ou keepalive invalide", "BAD_PORT")
    if not 1 <= port <= 65535 or not 5 <= keepalive <= 3600:
        return _err("Port (1-65535) ou keepalive (5-3600 s) hors limites", "BAD_PORT")
    client_id = str(payload.get("client_id") or "").strip() or f"ledroide-{uuid.uuid4().hex[:8]}"
    timeout = max(1.0, min(60.0, float(payload.get("timeout") or 10)))

    await _mqtt_close()
    loop = asyncio.get_running_loop()
    try:
        client = _make_client(mqtt, payload, client_id)
    except ValueError as exc:
        return _err(f"Paramètres MQTT invalides : {exc}", "BAD_REQUEST")
    info = {"host": host, "port": port, "client_id": client_id, "tls": bool(payload.get("tls")),
            "version": "5" if str(payload.get("version")) == "5" else "3.1.1"}
    sess = MqttSession(client, loop, info)
    if payload.get("username"):
        client.username_pw_set(str(payload["username"]), str(payload.get("password") or "") or None)
    if payload.get("tls"):
        ctx = ssl.create_default_context()
        if payload.get("verify_ssl") is False:
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        client.tls_set_context(ctx)
        if payload.get("verify_ssl") is False:
            client.tls_insecure_set(True)
    client.connect_timeout = timeout
    client.reconnect_delay_set(1, 30)

    def on_connect(_c, _u, _flags, rc, _props):
        failed = _rc_failed(rc)
        first = not sess.connect_future.done()
        if not failed:
            sess.connected = True

        def settle():
            if not sess.connect_future.done():
                sess.connect_future.set_result(rc)
        loop.call_soon_threadsafe(settle)
        if failed:
            return
        for topic, qos in list(sess.subs.items()):
            client.subscribe(topic, qos)
        if not first:
            sess.emit(_record("mqtt", "sys", "Reconnecté au broker", event="open"))
            sess.emit(_status_changed())

    def on_disconnect(_c, _u, _flags, rc, _props):
        was = sess.connected
        sess.connected = False
        if sess.closing or not was:
            return
        sess.emit(_record("mqtt", "sys", f"Déconnecté du broker ({rc}) — reconnexion automatique…", event="closed"))
        sess.emit(_status_changed())

    def on_message(_c, _u, msg):
        text, binary = _payload_text(msg.payload)
        sess.emit(_record("mqtt", "in", text, topic=msg.topic, qos=msg.qos, retain=bool(msg.retain),
                          binary=binary, size=len(msg.payload)))

    def on_subscribe(_c, _u, mid, rcs, _props):
        sess.resolve(mid, [int(getattr(r, "value", r)) for r in rcs])

    def on_unsubscribe(_c, _u, mid, _rcs, _props):
        sess.resolve(mid, True)

    client.on_connect = on_connect
    client.on_disconnect = on_disconnect
    client.on_message = on_message
    client.on_subscribe = on_subscribe
    client.on_unsubscribe = on_unsubscribe

    try:
        await asyncio.to_thread(client.connect, host, port, keepalive)
    except ssl.SSLCertVerificationError as exc:
        return _err(f"Certificat TLS invalide : {exc.verify_message}", "SSL_ERROR")
    except (TimeoutError, OSError) as exc:
        reason = "délai dépassé" if isinstance(exc, TimeoutError) else (exc.strerror or str(exc))
        return _err(f"Broker injoignable ({host}:{port}) : {reason}", "CONNECT_FAILED")
    except Exception as exc:
        return _err(f"Connexion MQTT impossible : {exc}", "CONNECT_FAILED")
    client.loop_start()
    try:
        rc = await asyncio.wait_for(sess.connect_future, timeout)
    except asyncio.TimeoutError:
        sess.closing = True
        await asyncio.to_thread(_stop_client, client)
        return _err("Le broker n'a pas répondu à la demande de connexion (CONNACK)", "TIMEOUT")
    if _rc_failed(rc):
        sess.closing = True
        await asyncio.to_thread(_stop_client, client)
        return _err(f"Connexion refusée par le broker : {rc}", "CONNECT_REFUSED")
    _mqtt = sess
    log.info("mqtt connected to %s:%s", host, port)
    await _record("mqtt", "sys", f"Connecté à {host}:{port} (client « {client_id} », MQTT {info['version']})", event="open")
    await _status_changed()
    return await status({})


def _stop_client(client):
    try:
        client.disconnect()
    except Exception:
        pass
    try:
        client.loop_stop()
    except Exception:
        pass


async def _mqtt_close():
    global _mqtt
    sess = _mqtt
    if sess is None:
        return
    _mqtt = None
    sess.closing = True
    await asyncio.to_thread(_stop_client, sess.client)
    await _record("mqtt", "sys", "Déconnecté du broker", event="closed")
    await _status_changed()


def _need_mqtt() -> MqttSession:
    if _mqtt is None:
        raise ConnectionError("Aucune connexion MQTT")
    return _mqtt


@module.on("mqtt_disconnect")
async def mqtt_disconnect(payload: dict):
    if _mqtt is None:
        return _err("Aucune connexion MQTT", "NOT_CONNECTED")
    await _mqtt_close()
    return {"ok": True}


@module.on("mqtt_subscribe")
async def mqtt_subscribe(payload: dict):
    try:
        sess = _need_mqtt()
        topic = validate_topic(str(payload.get("topic", "")), True)
    except ConnectionError as exc:
        return _err(str(exc), "NOT_CONNECTED")
    except ValueError as exc:
        return _err(str(exc), "BAD_TOPIC")
    qos = int(payload.get("qos") or 0)
    if qos not in (0, 1, 2):
        return _err("QoS invalide (0, 1 ou 2)", "BAD_QOS")
    rc, mid = sess.client.subscribe(topic, qos)
    if rc != 0:
        return _err(f"Abonnement impossible (code {rc})", "SUBSCRIBE_FAILED")
    try:
        granted = await sess.wait(mid, 10)
    except asyncio.TimeoutError:
        return _err("Le broker n'a pas confirmé l'abonnement (SUBACK)", "TIMEOUT")
    if granted and granted[0] >= 0x80:
        return _err(f"Abonnement refusé par le broker (code {granted[0]})", "SUBSCRIBE_REFUSED")
    sess.subs[topic] = qos
    await _record("mqtt", "sys", f"Abonné à « {topic} » (QoS accordé : {granted[0] if granted else qos})", event="sub")
    await _status_changed()
    return {"ok": True, "topic": topic, "granted_qos": granted[0] if granted else qos}


@module.on("mqtt_unsubscribe")
async def mqtt_unsubscribe(payload: dict):
    try:
        sess = _need_mqtt()
    except ConnectionError as exc:
        return _err(str(exc), "NOT_CONNECTED")
    topic = str(payload.get("topic", "")).strip()
    if topic not in sess.subs:
        return _err("Abonnement inconnu", "NOT_FOUND")
    rc, mid = sess.client.unsubscribe(topic)
    sess.subs.pop(topic, None)
    if rc == 0:
        try:
            await sess.wait(mid, 5)
        except asyncio.TimeoutError:
            pass
    await _record("mqtt", "sys", f"Désabonné de « {topic} »", event="unsub")
    await _status_changed()
    return {"ok": True}


@module.on("mqtt_publish")
async def mqtt_publish(payload: dict):
    try:
        sess = _need_mqtt()
        topic = validate_topic(str(payload.get("topic", "")), False)
    except ConnectionError as exc:
        return _err(str(exc), "NOT_CONNECTED")
    except ValueError as exc:
        return _err(str(exc), "BAD_TOPIC")
    qos = int(payload.get("qos") or 0)
    if qos not in (0, 1, 2):
        return _err("QoS invalide (0, 1 ou 2)", "BAD_QOS")
    text = str(payload.get("payload", ""))
    if payload.get("mode") == "json":
        try:
            json.loads(text)
        except json.JSONDecodeError as exc:
            return _err(f"JSON invalide (ligne {exc.lineno}, colonne {exc.colno}) : {exc.msg}", "BAD_JSON")
    retain = bool(payload.get("retain"))
    info = sess.client.publish(topic, text.encode("utf-8"), qos=qos, retain=retain)
    if info.rc != 0:
        return _err(f"Publication impossible (code {info.rc})", "PUBLISH_FAILED")
    if qos > 0:
        try:
            await asyncio.to_thread(info.wait_for_publish, 10)
        except (RuntimeError, ValueError) as exc:
            return _err(f"Publication non confirmée : {exc}", "PUBLISH_FAILED")
        if not info.is_published():
            return _err("Le broker n'a pas confirmé la publication (10 s)", "TIMEOUT")
    entry = await _record("mqtt", "out", text, topic=topic, qos=qos, retain=retain, size=len(text.encode("utf-8")))
    return {"ok": True, "mid": info.mid, "id": entry["id"]}


# ── Status / log / saved ─────────────────────────────────────────────────────

@module.on("status")
async def status(payload: dict):
    ws = None
    if _ws is not None:
        ws = {"url": _ws.url, "since": _ws.since, "subprotocol": _ws.ws.subprotocol}
    mq = None
    if _mqtt is not None:
        mq = {**_mqtt.info, "connected": _mqtt.connected, "subs": [{"topic": t, "qos": q} for t, q in _mqtt.subs.items()]}
    try:
        import paho.mqtt  # noqa: F401
        paho_ok = True
    except ImportError:
        paho_ok = False
    return {"ws": ws, "mqtt": mq, "paho": paho_ok}


@module.on("log_get")
async def log_get(payload: dict):
    kind = payload.get("kind")
    return {"items": [e for e in _log if not kind or e["kind"] == kind]}


@module.on("log_clear")
async def log_clear(payload: dict):
    kind = payload.get("kind")
    keep = [e for e in _log if kind and e["kind"] != kind]
    _log[:] = keep
    return {"ok": True}


@module.on("saved_list")
async def saved_list(payload: dict):
    items = await module.db.query("saved", {"kind": payload["kind"]} if payload.get("kind") else None)
    items.sort(key=lambda d: (d.get("name") or "").lower())
    return {"items": items}


@module.on("saved_save")
async def saved_save(payload: dict):
    name = str(payload.get("name") or "").strip()
    if not name:
        return _err("Le nom est obligatoire", "NAME_REQUIRED")
    kind = payload.get("kind")
    if kind not in ("ws", "mqtt"):
        return _err("Type de message invalide", "BAD_REQUEST")
    doc = {"name": name[:80], "kind": kind, "text": str(payload.get("text", "")),
           "mode": payload.get("mode") if payload.get("mode") in ("text", "json", "hex") else "text"}
    if kind == "mqtt":
        doc.update(topic=str(payload.get("topic", ""))[:500], qos=int(payload.get("qos") or 0) % 3,
                   retain=bool(payload.get("retain")))
    if payload.get("id"):
        doc["_id"] = str(payload["id"])
    return await module.db.save("saved", doc)


@module.on("saved_delete")
async def saved_delete(payload: dict):
    return await module.db.delete("saved", str(payload.get("id", "")))


SECRET_KEYS = {"password"}


@module.on("settings_get")
async def settings_get(payload: dict):
    return await module.db.get("settings", "last") or {"_id": "last", "ws": {}, "mqtt": {}}


@module.on("settings_save")
async def settings_save(payload: dict):
    def clean(d):
        return {k: v for k, v in (d or {}).items() if k not in SECRET_KEYS} if isinstance(d, dict) else {}
    return await module.db.save("settings", {"_id": "last", "ws": clean(payload.get("ws")), "mqtt": clean(payload.get("mqtt"))})


@module.on_shutdown
async def shutdown():
    await _ws_close(1001, "Fermeture du module")
    await _mqtt_close()
