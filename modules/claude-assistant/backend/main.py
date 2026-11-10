import asyncio
import importlib.util
import logging
import re
import time
from datetime import datetime, timezone
from typing import Any, Callable, Literal

from pydantic import BaseModel, Field, ValidationError

from ledroide_sdk import Module

log = logging.getLogger("ledroide.claude-assistant")

module = Module(__file__)
router = module.router

KEY_NAME = "ANTHROPIC_API_KEY"
COLLECTION = "conversations"
DEFAULT_TITLE = "Nouvelle conversation"
MAX_INPUT_CHARS = 500_000
FLUSH_INTERVAL_S = 0.05
FALLBACK_BETA = "server-side-fallback-2026-07-01"

MODELS = [
    {"id": "claude-opus-5-5", "label": "Claude Opus 5.5", "hint": "Le plus polyvalent (recommandé)", "fallbacks": True},
    {"id": "claude-sonnet-5-5", "label": "Claude Sonnet 5.5", "hint": "Rapide et économique", "fallbacks": True},
    {"id": "claude-haiku-5-5", "label": "Claude Haiku 5.5", "hint": "Très rapide, très bon marché", "fallbacks": False},
    {"id": "claude-fable-5-1", "label": "Claude Fable 5.1", "hint": "Le plus puissant, le plus cher", "fallbacks": True},
]
MODEL_IDS = [m["id"] for m in MODELS]
EFFORTS = ("low", "medium", "high", "xhigh", "max")

TONES = {
    "formel": "formel et professionnel",
    "neutre": "neutre et clair",
    "amical": "amical et chaleureux",
}

QUICK_ACTIONS = {
    "summarize": {
        "label": "Résumer",
        "system": "Tu résumes des textes. Produis un résumé fidèle, clair et concis, en français, "
                  "avec une phrase d'introduction puis les points clés sous forme de liste. "
                  "N'ajoute aucune information absente du texte.",
    },
    "translate": {
        "label": "Traduire",
        "system": "Tu es un traducteur professionnel. Traduis le texte fourni en {lang}, en conservant "
                  "le sens, le ton et la mise en forme (Markdown, listes, code). Réponds uniquement par la traduction.",
    },
    "correct": {
        "label": "Corriger",
        "system": "Tu corriges l'orthographe, la grammaire, la conjugaison et la ponctuation du texte fourni, "
                  "dans sa langue d'origine, sans en changer le sens ni le style. Réponds uniquement par le texte corrigé.",
    },
    "rephrase": {
        "label": "Reformuler",
        "system": "Tu reformules le texte fourni dans sa langue d'origine avec un ton {tone}, en conservant "
                  "toutes les informations. Réponds uniquement par le texte reformulé.",
    },
    "explain_code": {
        "label": "Expliquer du code",
        "system": "Tu es un développeur expérimenté et pédagogue. Explique en français ce que fait le code fourni : "
                  "rôle général, puis fonctionnement étape par étape, et signale les problèmes ou améliorations "
                  "éventuels. Utilise du Markdown et des blocs de code si utile.",
    },
    "commit": {
        "label": "Message de commit",
        "system": "Tu rédiges des messages de commit Git à partir d'un diff, au format Conventional Commits, en {lang} : "
                  "une ligne de résumé de 72 caractères maximum (type(portée): description), une ligne vide, "
                  "puis un corps court en liste à puces expliquant le quoi et le pourquoi. "
                  "Réponds uniquement par le message de commit, sans bloc de code.",
    },
}

REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")

_client_factory: Callable[[str], Any] | None = None
_active: dict[str, dict] = {}
_locks: dict[str, asyncio.Lock] = {}


class AssistantError(Exception):
    def __init__(self, code: str, message: str, **extra):
        super().__init__(message)
        self.code = code
        self.message = message
        self.extra = extra


def _err(code: str, message: str, **extra) -> dict:
    return {"error": message, "code": code, **extra}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _first_error(exc: ValidationError) -> str:
    e = exc.errors()[0]
    loc = ".".join(str(p) for p in e.get("loc", ()))
    return f"Paramètre invalide ({loc}) : {e.get('msg')}" if loc else f"Paramètre invalide : {e.get('msg')}"


# ── Client & settings ────────────────────────────────────────────────────────

def _api_key() -> str | None:
    key = (module.env(KEY_NAME) or "").strip()
    return key or None


def _sdk_available() -> bool:
    return _client_factory is not None or importlib.util.find_spec("anthropic") is not None


def _make_client(api_key: str):
    if _client_factory is not None:
        return _client_factory(api_key)
    try:
        import anthropic
    except ImportError:
        raise AssistantError("DEPENDENCY_MISSING",
                             "La bibliothèque Python « anthropic » n'est pas installée. Désactivez puis réactivez "
                             "le module, ou installez-la depuis le menu « Bibliothèques ».")
    return anthropic.AsyncAnthropic(api_key=api_key)


def _settings() -> dict:
    model = module.config.get("default_model", MODEL_IDS[0])
    if model not in MODEL_IDS:
        model = MODEL_IDS[0]
    try:
        max_tokens = int(module.config.get("max_tokens", 64000))
    except (TypeError, ValueError):
        max_tokens = 64000
    effort = module.config.get("effort", "medium")
    if effort not in EFFORTS:
        effort = "medium"
    return {"model": model, "max_tokens": max(1024, min(max_tokens, 128000)), "effort": effort}


def _model_info(model_id: str) -> dict:
    return next((m for m in MODELS if m["id"] == model_id), MODELS[0])


# ── Error mapping ────────────────────────────────────────────────────────────

def _is_context_error(exc) -> bool:
    if getattr(exc, "status_code", None) == 413:
        return True
    code = (getattr(exc, "error_code", None) or "").lower()
    if code in ("prompt_too_long", "context_length_exceeded", "request_too_large"):
        return True
    msg = (getattr(exc, "api_message", None) or getattr(exc, "message", None) or "").lower()
    return "too long" in msg or "context window" in msg or "context length" in msg


def _map_api_error(exc: Exception) -> AssistantError:
    if isinstance(exc, AssistantError):
        return exc
    try:
        import anthropic
    except ImportError:
        return AssistantError("API_ERROR", "Erreur inattendue lors de l'appel à l'API.")

    if isinstance(exc, anthropic.AuthenticationError):
        return AssistantError("INVALID_KEY", "Clé d'API refusée (401). Vérifiez la variable ANTHROPIC_API_KEY "
                                             "dans le menu « Variables » de l'application.")
    if isinstance(exc, anthropic.PermissionDeniedError):
        return AssistantError("PERMISSION_DENIED", "Accès refusé (403) : cette clé n'a pas le droit d'utiliser "
                                                   "ce modèle ou cette fonctionnalité.")
    if isinstance(exc, anthropic.NotFoundError):
        return AssistantError("MODEL_NOT_FOUND", "Modèle introuvable ou non disponible pour votre organisation (404).")
    if isinstance(exc, anthropic.RateLimitError):
        retry = None
        try:
            raw = exc.response.headers.get("retry-after")
            retry = int(float(raw)) if raw else None
        except (TypeError, ValueError, AttributeError):
            retry = None
        wait = f" Réessayez dans {retry} s." if retry else " Réessayez dans quelques instants."
        return AssistantError("RATE_LIMITED", "Limite de débit ou quota atteint (429)." + wait, retry_after=retry)
    overloaded = getattr(anthropic, "OverloadedError", None)
    if (overloaded and isinstance(exc, overloaded)) or getattr(exc, "status_code", None) == 529:
        return AssistantError("OVERLOADED", "L'API Anthropic est momentanément surchargée (529). Réessayez dans un instant.")
    if isinstance(exc, anthropic.APIStatusError) and _is_context_error(exc):
        return AssistantError("CONTEXT_TOO_LONG", "La conversation ou le texte est trop long pour le contexte du modèle. "
                                                  "Démarrez une nouvelle conversation ou raccourcissez le texte.")
    if isinstance(exc, anthropic.BadRequestError):
        detail = getattr(exc, "api_message", None) or ""
        return AssistantError("BAD_REQUEST", "Requête refusée par l'API (400)." + (f" Détail : {detail}" if detail else ""))
    if isinstance(exc, anthropic.APIStatusError):
        if exc.status_code == 402:
            return AssistantError("BILLING", "Problème de facturation sur le compte Anthropic (402) : vérifiez vos crédits.")
        if exc.status_code >= 500:
            return AssistantError("SERVER_ERROR", f"Erreur du service Anthropic ({exc.status_code}). Réessayez plus tard.")
        return AssistantError("API_ERROR", f"Erreur de l'API ({exc.status_code}).")
    if isinstance(exc, anthropic.APITimeoutError):
        return AssistantError("TIMEOUT", "Délai dépassé : l'API n'a pas répondu à temps.")
    if isinstance(exc, anthropic.APIConnectionError):
        return AssistantError("NETWORK", "Impossible de joindre l'API Anthropic. Vérifiez votre connexion Internet.")
    return AssistantError("API_ERROR", "Erreur inattendue lors de l'appel à l'API.")


# ── Streaming core ───────────────────────────────────────────────────────────

async def _stream_reply(request_id: str, scope: str, model: str, system: str | None,
                        messages: list[dict], on_text: Callable[[str], None]) -> dict:
    key = _api_key()
    if not key:
        raise AssistantError("API_KEY_MISSING", "Clé d'API absente : définissez ANTHROPIC_API_KEY dans le menu « Variables ».")
    settings = _settings()
    client = _make_client(key)
    params: dict[str, Any] = {
        "model": model,
        "max_tokens": settings["max_tokens"],
        "messages": messages,
        "output_config": {"effort": settings["effort"]},
    }
    if system:
        params["system"] = system
    if _model_info(model)["fallbacks"]:
        params["fallbacks"] = "default"
        params["betas"] = [FALLBACK_BETA]

    pending: list[str] = []
    last_flush = time.monotonic()

    async def flush():
        nonlocal last_flush
        if pending:
            chunk = "".join(pending)
            pending.clear()
            await module.events.emit("stream:delta", {"request_id": request_id, "scope": scope, "text": chunk})
        last_flush = time.monotonic()

    try:
        async with client.beta.messages.stream(**params) as stream:
            async for event in stream:
                etype = getattr(event, "type", None)
                if etype == "text":
                    on_text(event.text)
                    pending.append(event.text)
                    if time.monotonic() - last_flush >= FLUSH_INTERVAL_S:
                        await flush()
                elif etype == "content_block_start" and getattr(event.content_block, "type", None) == "thinking":
                    await module.events.emit("stream:status", {"request_id": request_id, "scope": scope, "phase": "thinking"})
            await flush()
            final = await stream.get_final_message()
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        mapped = _map_api_error(exc)
        log.warning("API call failed (%s, %s)", mapped.code, type(exc).__name__)
        raise mapped

    if final.stop_reason == "refusal":
        raise AssistantError("REFUSED", "Le modèle a refusé de répondre à cette demande (filtre de sécurité). "
                                        "Reformulez la demande ou essayez un autre modèle.")
    text = "".join(b.text for b in final.content if getattr(b, "type", None) == "text")
    usage = final.usage
    return {
        "text": text,
        "model": getattr(final, "model", model) or model,
        "stop_reason": final.stop_reason,
        "usage": {
            "input": int(getattr(usage, "input_tokens", 0) or 0),
            "output": int(getattr(usage, "output_tokens", 0) or 0),
            "cache_read": int(getattr(usage, "cache_read_input_tokens", 0) or 0),
            "cache_write": int(getattr(usage, "cache_creation_input_tokens", 0) or 0),
        },
    }


def _start(request_id: str, conv_id: str | None, coro_factory) -> None:
    entry = {"conv_id": conv_id, "text": "", "started": time.time()}
    _active[request_id] = entry
    task = asyncio.create_task(coro_factory(entry))
    entry["task"] = task
    task.add_done_callback(lambda _t, rid=request_id: _active.pop(rid, None))


def _check_request_id(request_id: str) -> str | None:
    if not REQUEST_ID_RE.match(request_id or ""):
        return "Identifiant de requête invalide"
    if request_id in _active:
        return "Identifiant de requête déjà utilisé"
    return None


def _busy_request(conv_id: str) -> str | None:
    return next((rid for rid, e in _active.items() if e["conv_id"] == conv_id), None)


# ── Conversations ────────────────────────────────────────────────────────────

def _lock(conv_id: str) -> asyncio.Lock:
    return _locks.setdefault(conv_id, asyncio.Lock())


async def _get_conv(conv_id: str) -> dict | None:
    if not isinstance(conv_id, str) or not conv_id:
        return None
    return await module.db.get(COLLECTION, conv_id)


async def _mutate(conv_id: str, fn: Callable[[dict], None]) -> dict | None:
    async with _lock(conv_id):
        doc = await _get_conv(conv_id)
        if doc is None:
            return None
        fn(doc)
        doc["updated"] = _now()
        return await module.db.save(COLLECTION, doc)


def _summary(doc: dict) -> dict:
    msgs = doc.get("messages") or []
    return {
        "_id": doc["_id"],
        "title": doc.get("title") or DEFAULT_TITLE,
        "model": doc.get("model"),
        "updated": doc.get("updated"),
        "count": len(msgs),
        "busy": _busy_request(doc["_id"]) is not None,
    }


def _public(doc: dict) -> dict:
    rid = _busy_request(doc["_id"])
    pending = {"request_id": rid, "text": _active[rid]["text"]} if rid else None
    return {**doc, "pending": pending}


def _api_messages(doc: dict) -> list[dict]:
    out = []
    for m in doc.get("messages") or []:
        content = (m.get("content") or "").strip()
        if m.get("role") in ("user", "assistant") and content:
            out.append({"role": m["role"], "content": m["content"]})
    while out and out[0]["role"] != "user":
        out.pop(0)
    return out


def _auto_title(text: str) -> str:
    line = " ".join(text.split())
    return (line[:60] + "…") if len(line) > 60 else (line or DEFAULT_TITLE)


async def _run_chat(request_id: str, conv_id: str, entry: dict) -> None:
    doc = await _get_conv(conv_id)
    if doc is None:
        await module.events.emit("stream:error", {"request_id": request_id, "scope": "chat", "conv_id": conv_id,
                                           "error": "Conversation introuvable", "code": "NOT_FOUND"})
        return
    model = doc.get("model") if doc.get("model") in MODEL_IDS else _settings()["model"]

    def on_text(t: str):
        entry["text"] += t

    try:
        result = await _stream_reply(request_id, "chat", model, (doc.get("system") or "").strip() or None,
                                     _api_messages(doc), on_text)
    except asyncio.CancelledError:
        partial = entry["text"]
        if partial.strip():
            msg = {"role": "assistant", "content": partial, "ts": _now(), "model": model, "stopped": True}
            await _mutate(conv_id, lambda d: d.setdefault("messages", []).append(msg))
        await module.events.emit("stream:done", {"request_id": request_id, "scope": "chat", "conv_id": conv_id,
                                          "stopped": True, "text": partial})
        raise
    except AssistantError as exc:
        await module.events.emit("stream:error", {"request_id": request_id, "scope": "chat", "conv_id": conv_id,
                                           "error": exc.message, "code": exc.code, **exc.extra})
        return
    except Exception:
        log.exception("chat request failed")
        await module.events.emit("stream:error", {"request_id": request_id, "scope": "chat", "conv_id": conv_id,
                                           "error": "Erreur interne inattendue", "code": "INTERNAL"})
        return

    msg = {"role": "assistant", "content": result["text"], "ts": _now(), "model": result["model"],
           "usage": result["usage"], "truncated": result["stop_reason"] == "max_tokens"}

    def apply(d: dict):
        d.setdefault("messages", []).append(msg)
        tot = d.setdefault("usage_total", {"input": 0, "output": 0})
        tot["input"] = tot.get("input", 0) + result["usage"]["input"]
        tot["output"] = tot.get("output", 0) + result["usage"]["output"]

    saved = await _mutate(conv_id, apply)
    await module.events.emit("stream:done", {"request_id": request_id, "scope": "chat", "conv_id": conv_id,
                                      "message": msg, "usage_total": (saved or {}).get("usage_total")})


async def _launch_chat(conv_id: str, request_id: str, prepare: Callable[[dict], str | None]) -> dict:
    if not _api_key():
        return _err("API_KEY_MISSING", "Clé d'API absente : définissez ANTHROPIC_API_KEY dans le menu « Variables ».")
    if bad := _check_request_id(request_id):
        return _err("INVALID_PAYLOAD", bad)
    if _busy_request(conv_id):
        return _err("BUSY", "Une réponse est déjà en cours dans cette conversation")
    async with _lock(conv_id):
        doc = await _get_conv(conv_id)
        if doc is None:
            return _err("NOT_FOUND", "Conversation introuvable")
        problem = prepare(doc)
        if problem:
            return _err("INVALID_STATE", problem)
        doc["updated"] = _now()
        doc = await module.db.save(COLLECTION, doc)
        _start(request_id, conv_id, lambda entry: _run_chat(request_id, conv_id, entry))
    return {"request_id": request_id, "conversation": _public(doc)}


# ── Payloads ─────────────────────────────────────────────────────────────────

class CreatePayload(BaseModel):
    title: str = Field(DEFAULT_TITLE, max_length=200)
    model: str | None = None
    system: str = Field("", max_length=50_000)


class UpdatePayload(BaseModel):
    id: str
    title: str | None = Field(None, max_length=200)
    model: str | None = None
    system: str | None = Field(None, max_length=50_000)


class SendPayload(BaseModel):
    conv_id: str
    request_id: str
    text: str = Field(..., max_length=MAX_INPUT_CHARS)


class RegenPayload(BaseModel):
    conv_id: str
    request_id: str


class QuickPayload(BaseModel):
    action: Literal["summarize", "translate", "correct", "rephrase", "explain_code", "commit"]
    request_id: str
    text: str = Field(..., max_length=MAX_INPUT_CHARS)
    model: str | None = None
    lang: str = Field("anglais", max_length=40)
    tone: Literal["formel", "neutre", "amical"] = "neutre"


# ── Handlers ─────────────────────────────────────────────────────────────────

@module.on("status")
async def status(payload: dict):
    return {
        "has_key": _api_key() is not None,
        "sdk_available": _sdk_available(),
        "key_name": KEY_NAME,
        "models": MODELS,
        "settings": _settings(),
        "quick_actions": [{"id": k, "label": v["label"]} for k, v in QUICK_ACTIONS.items()],
    }


@module.on("conv:list")
async def conv_list(payload: dict):
    docs = await module.db.query(COLLECTION)
    docs.sort(key=lambda d: d.get("updated") or "", reverse=True)
    return {"items": [_summary(d) for d in docs]}


@module.on("conv:get")
async def conv_get(payload: dict):
    doc = await _get_conv(payload.get("id"))
    if doc is None:
        return _err("NOT_FOUND", "Conversation introuvable")
    return {"conversation": _public(doc)}


@module.on("conv:create")
async def conv_create(payload: dict):
    try:
        p = CreatePayload(**(payload or {}))
    except ValidationError as exc:
        return _err("INVALID_PAYLOAD", _first_error(exc))
    model = p.model if p.model in MODEL_IDS else _settings()["model"]
    now = _now()
    doc = await module.db.save(COLLECTION, {
        "title": p.title.strip() or DEFAULT_TITLE, "model": model, "system": p.system,
        "messages": [], "usage_total": {"input": 0, "output": 0}, "created": now, "updated": now,
    })
    return {"conversation": _public(doc)}


@module.on("conv:update")
async def conv_update(payload: dict):
    try:
        p = UpdatePayload(**(payload or {}))
    except ValidationError as exc:
        return _err("INVALID_PAYLOAD", _first_error(exc))
    if p.model is not None and p.model not in MODEL_IDS:
        return _err("INVALID_PAYLOAD", "Modèle inconnu")

    def apply(d: dict):
        if p.title is not None:
            d["title"] = p.title.strip() or DEFAULT_TITLE
        if p.model is not None:
            d["model"] = p.model
        if p.system is not None:
            d["system"] = p.system

    doc = await _mutate(p.id, apply)
    if doc is None:
        return _err("NOT_FOUND", "Conversation introuvable")
    return {"conversation": _public(doc)}


@module.on("conv:delete")
async def conv_delete(payload: dict):
    conv_id = payload.get("id")
    rid = _busy_request(conv_id) if conv_id else None
    if rid:
        task = _active[rid].get("task")
        if task:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    if await _get_conv(conv_id) is None:
        return _err("NOT_FOUND", "Conversation introuvable")
    await module.db.delete(COLLECTION, conv_id)
    _locks.pop(conv_id, None)
    return {"ok": True}


@module.on("chat:send")
async def chat_send(payload: dict):
    try:
        p = SendPayload(**(payload or {}))
    except ValidationError as exc:
        return _err("INVALID_PAYLOAD", _first_error(exc))
    text = p.text.strip()
    if not text:
        return _err("INVALID_PAYLOAD", "Le message est vide")

    def prepare(d: dict):
        msgs = d.setdefault("messages", [])
        if not msgs and (d.get("title") or DEFAULT_TITLE) == DEFAULT_TITLE:
            d["title"] = _auto_title(text)
        msgs.append({"role": "user", "content": text, "ts": _now()})

    return await _launch_chat(p.conv_id, p.request_id, prepare)


@module.on("chat:regenerate")
async def chat_regenerate(payload: dict):
    try:
        p = RegenPayload(**(payload or {}))
    except ValidationError as exc:
        return _err("INVALID_PAYLOAD", _first_error(exc))

    def prepare(d: dict):
        msgs = d.setdefault("messages", [])
        while msgs and msgs[-1].get("role") == "assistant":
            msgs.pop()
        if not msgs:
            return "Aucun message à régénérer"
        return None

    return await _launch_chat(p.conv_id, p.request_id, prepare)


@module.on("chat:edit_last")
async def chat_edit_last(payload: dict):
    try:
        p = SendPayload(**(payload or {}))
    except ValidationError as exc:
        return _err("INVALID_PAYLOAD", _first_error(exc))
    text = p.text.strip()
    if not text:
        return _err("INVALID_PAYLOAD", "Le message est vide")

    def prepare(d: dict):
        msgs = d.setdefault("messages", [])
        idx = next((i for i in range(len(msgs) - 1, -1, -1) if msgs[i].get("role") == "user"), None)
        if idx is None:
            return "Aucun message à modifier"
        del msgs[idx:]
        msgs.append({"role": "user", "content": text, "ts": _now(), "edited": True})
        return None

    return await _launch_chat(p.conv_id, p.request_id, prepare)


@module.on("chat:stop")
async def chat_stop(payload: dict):
    entry = _active.get(payload.get("request_id") or "")
    if entry is None or entry.get("task") is None:
        return _err("NOT_FOUND", "Aucune réponse en cours avec cet identifiant")
    entry["task"].cancel()
    return {"ok": True}


@module.on("quick:run")
async def quick_run(payload: dict):
    try:
        p = QuickPayload(**(payload or {}))
    except ValidationError as exc:
        return _err("INVALID_PAYLOAD", _first_error(exc))
    if not p.text.strip():
        return _err("INVALID_PAYLOAD", "Collez un texte à traiter")
    if not _api_key():
        return _err("API_KEY_MISSING", "Clé d'API absente : définissez ANTHROPIC_API_KEY dans le menu « Variables ».")
    if bad := _check_request_id(p.request_id):
        return _err("INVALID_PAYLOAD", bad)
    model = p.model if p.model in MODEL_IDS else _settings()["model"]
    lang = " ".join(p.lang.split()) or "anglais"
    system = QUICK_ACTIONS[p.action]["system"].format(lang=lang, tone=TONES[p.tone])
    rid = p.request_id
    text = p.text

    async def run(entry: dict):
        def on_text(t: str):
            entry["text"] += t
        try:
            result = await _stream_reply(rid, "quick", model, system, [{"role": "user", "content": text}], on_text)
        except asyncio.CancelledError:
            await module.events.emit("stream:done", {"request_id": rid, "scope": "quick", "stopped": True, "text": entry["text"]})
            raise
        except AssistantError as exc:
            await module.events.emit("stream:error", {"request_id": rid, "scope": "quick", "error": exc.message,
                                               "code": exc.code, **exc.extra})
            return
        except Exception:
            log.exception("quick action failed")
            await module.events.emit("stream:error", {"request_id": rid, "scope": "quick",
                                               "error": "Erreur interne inattendue", "code": "INTERNAL"})
            return
        await module.events.emit("stream:done", {"request_id": rid, "scope": "quick", "message": {
            "content": result["text"], "model": result["model"], "usage": result["usage"],
            "truncated": result["stop_reason"] == "max_tokens"}})

    _start(rid, None, run)
    return {"request_id": rid, "model": model}


@module.on_shutdown
async def cleanup():
    tasks = [e["task"] for e in _active.values() if e.get("task")]
    for t in tasks:
        t.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
