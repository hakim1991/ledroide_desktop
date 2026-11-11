import asyncio
import base64
import importlib.util
import json
import logging
import platform
import re
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Literal

from pydantic import BaseModel, Field, ValidationError

from ledroide_sdk import Module

import la_mcp
import la_skills
import la_tools

log = logging.getLogger("ledroide.litellm-agent")

module = Module(__file__)
router = module.router

URL_KEY = "LITELLM_BASE_URL"
API_KEY = "LITELLM_API_KEY"
CONVS = "conversations"
PROFILES = "profiles"
SERVERS = "mcp_servers"
SETTINGS = "settings"
DEFAULT_TITLE = "Nouvelle conversation"
MAX_INPUT_CHARS = 500_000
FLUSH_INTERVAL_S = 0.05
MODELS_TTL_S = 60
MODES = ("chat", "approve", "auto")
POLICIES = ("allow", "ask", "deny")
REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
TOOL_NAME_RE = re.compile(r"[^A-Za-z0-9_-]+")

_client_factory: Callable[[str, str], Any] | None = None
_clients: dict[tuple, Any] = {}
_models_cache: dict[str, Any] = {"at": 0.0, "items": None}
_active: dict[str, dict] = {}
_locks: dict[str, asyncio.Lock] = {}


class AgentError(Exception):
    def __init__(self, code: str, message: str, **extra):
        super().__init__(message)
        self.code = code
        self.message = message
        self.extra = extra


def _err(code: str, message: str, **extra) -> dict:
    return {"error": message, "code": code, **extra}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _mid() -> str:
    return uuid.uuid4().hex[:12]


def _first_error(exc: ValidationError) -> str:
    e = exc.errors()[0]
    loc = ".".join(str(p) for p in e.get("loc", ()))
    return f"Paramètre invalide ({loc}) : {e.get('msg')}" if loc else f"Paramètre invalide : {e.get('msg')}"


def _skills_root() -> Path:
    root = module.data_dir / "skills"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _default_workspace() -> Path:
    ws = module.data_dir / "workspace"
    ws.mkdir(parents=True, exist_ok=True)
    return ws


async def _emit_mcp_status(snapshot: dict):
    await module.events.emit("mcp:status", snapshot)


mcp_manager = la_mcp.McpManager(module.data_dir / "mcp-logs", _emit_mcp_status)


# ── Client & settings ────────────────────────────────────────────────────────

def _base_url() -> str | None:
    url = (module.env(URL_KEY) or "").strip().rstrip("/")
    return url or None


def _api_key() -> str:
    return (module.env(API_KEY) or "").strip()


def _sdk_available() -> bool:
    return _client_factory is not None or importlib.util.find_spec("openai") is not None


def _int_setting(key: str, default: int, lo: int, hi: int) -> int:
    try:
        v = int(module.config.get(key, default))
    except (TypeError, ValueError):
        v = default
    return max(lo, min(v, hi))


def _settings() -> dict:
    return {
        "default_model": (module.config.get("default_model") or "").strip(),
        "max_iterations": _int_setting("max_iterations", 25, 1, 200),
        "context_budget": _int_setting("context_budget", 100_000, 4_000, 2_000_000),
        "max_tokens": _int_setting("max_tokens", 0, 0, 1_000_000),
        "request_timeout": _int_setting("request_timeout", 600, 10, 3600),
        "tool_timeout": _int_setting("tool_timeout", 120, 5, 3600),
    }


def _client():
    url = _base_url()
    if not url:
        raise AgentError("CONFIG_MISSING", f"URL du proxy absente : définissez {URL_KEY} dans le menu « Variables ».")
    key = _api_key() or "sk-none"
    if _client_factory is not None:
        return _client_factory(url, key)
    timeout = _settings()["request_timeout"]
    ck = (url, key, timeout)
    if ck not in _clients:
        try:
            import openai
        except ImportError:
            raise AgentError("DEPENDENCY_MISSING", "La bibliothèque Python « openai » n'est pas installée. Désactivez puis "
                                                   "réactivez le module, ou installez-la depuis le menu « Bibliothèques ».")
        _clients.clear()
        _clients[ck] = openai.AsyncOpenAI(base_url=url, api_key=key, timeout=timeout, max_retries=2)
    return _clients[ck]


def _map_api_error(exc: Exception) -> AgentError:
    if isinstance(exc, AgentError):
        return exc
    try:
        import openai
    except ImportError:
        return AgentError("API_ERROR", f"Erreur inattendue : {type(exc).__name__}")
    detail = (getattr(exc, "message", None) or str(exc) or "").strip()
    short = (" Détail : " + detail[:500]) if detail else ""
    if isinstance(exc, openai.AuthenticationError):
        return AgentError("INVALID_KEY", f"Clé refusée par le proxy (401). Vérifiez {API_KEY} dans « Variables ».")
    if isinstance(exc, openai.PermissionDeniedError):
        return AgentError("PERMISSION_DENIED", "Accès refusé (403) : cette clé n'a pas accès à ce modèle." + short)
    if isinstance(exc, openai.NotFoundError):
        return AgentError("NOT_FOUND", f"Introuvable (404) : modèle inconnu du proxy ou URL incorrecte ({URL_KEY})." + short)
    if isinstance(exc, openai.RateLimitError):
        return AgentError("RATE_LIMITED", "Limite de débit ou budget atteint (429). Réessayez plus tard." + short)
    low = detail.lower()
    if isinstance(exc, openai.APIStatusError) and (
            "contextwindowexceeded" in low or "context length" in low or "context window" in low
            or "too long" in low or "maximum context" in low):
        return AgentError("CONTEXT_TOO_LONG", "La conversation dépasse le contexte du modèle. Utilisez « Compresser » "
                                              "ou démarrez une nouvelle conversation.")
    if isinstance(exc, openai.BadRequestError):
        return AgentError("BAD_REQUEST", "Requête refusée (400)." + short)
    if isinstance(exc, openai.APIStatusError):
        if exc.status_code >= 500:
            return AgentError("SERVER_ERROR", f"Erreur du proxy ou du fournisseur ({exc.status_code})." + short)
        return AgentError("API_ERROR", f"Erreur de l'API ({exc.status_code})." + short)
    if isinstance(exc, openai.APITimeoutError):
        return AgentError("TIMEOUT", "Délai dépassé : le proxy n'a pas répondu à temps.")
    if isinstance(exc, openai.APIConnectionError):
        return AgentError("NETWORK", f"Impossible de joindre le proxy LiteLLM ({_base_url()}).")
    return AgentError("API_ERROR", f"Erreur inattendue : {type(exc).__name__}")


async def _list_models(force: bool = False) -> list[str]:
    if not force and _models_cache["items"] is not None and time.time() - _models_cache["at"] < MODELS_TTL_S:
        return _models_cache["items"]
    try:
        page = await _client().models.list()
        ids = sorted({m.id for m in getattr(page, "data", None) or [] if getattr(m, "id", None)})
    except Exception as exc:
        raise _map_api_error(exc)
    _models_cache.update(at=time.time(), items=ids)
    return ids


# ── Settings documents (policies, seeds) ─────────────────────────────────────

async def _get_setting(key: str, default: Any) -> Any:
    doc = await module.db.get(SETTINGS, key)
    return doc.get("value", default) if doc else default


async def _set_setting(key: str, value: Any):
    await module.db.save(SETTINGS, {"_id": key, "value": value})


async def _policies() -> dict:
    val = await _get_setting("policies", {})
    return val if isinstance(val, dict) else {}


DEFAULT_PROFILES = [
    {"name": "Assistant", "icon": "💬", "mode": "chat", "tools": [], "skills": [], "mcp": [],
     "system": "Tu es un assistant utile, précis et concis. Réponds en français sauf demande contraire."},
    {"name": "Agent", "icon": "🤖", "mode": "approve", "tools": ["files_read", "todo", "http"], "skills": [], "mcp": [],
     "system": "Tu es un agent autonome. Décompose les tâches complexes, utilise les outils disponibles pour "
               "vérifier plutôt que supposer, et termine par un compte rendu clair de ce qui a été fait."},
    {"name": "Développeur", "icon": "🛠️", "mode": "approve",
     "tools": ["files_read", "files_write", "shell", "todo"], "skills": [], "mcp": [],
     "system": "Tu es un développeur expérimenté qui travaille dans le dossier de travail. Lis le code avant de le "
               "modifier, fais des changements minimaux et ciblés, vérifie ton travail (tests, exécution) et "
               "résume les modifications à la fin."},
]


async def _seed():
    if await _get_setting("seeded", False):
        return
    if not await module.db.query(PROFILES):
        base = datetime.now(timezone.utc)
        for i, p in enumerate(DEFAULT_PROFILES):
            ts = base.replace(microsecond=i).isoformat()
            await module.db.save(PROFILES, {**p, "model": "", "workspace": "", "max_iterations": 0,
                                            "created": ts, "updated": ts})
    await _set_setting("seeded", True)


# ── Conversations storage ────────────────────────────────────────────────────

def _lock(conv_id: str) -> asyncio.Lock:
    return _locks.setdefault(conv_id, asyncio.Lock())


async def _get_conv(conv_id: Any) -> dict | None:
    if not isinstance(conv_id, str) or not conv_id:
        return None
    return await module.db.get(CONVS, conv_id)


async def _mutate(conv_id: str, fn: Callable[[dict], Any]) -> dict | None:
    async with _lock(conv_id):
        doc = await _get_conv(conv_id)
        if doc is None:
            return None
        fn(doc)
        doc["updated"] = _now()
        return await module.db.save(CONVS, doc)


def _busy_request(conv_id: str) -> str | None:
    return next((rid for rid, e in _active.items() if e["conv_id"] == conv_id), None)


def _pending(rid: str) -> dict:
    e = _active[rid]
    return {"request_id": rid, "text": e["text"], "reasoning": e["reasoning"], "iteration": e["iteration"],
            "max_iterations": e["max_iterations"], "phase": e["phase"],
            "approvals": [a["info"] for a in e["approvals"].values()],
            "running": list(e["running"].values())}


def _summary(doc: dict) -> dict:
    return {"_id": doc["_id"], "title": doc.get("title") or DEFAULT_TITLE, "model": doc.get("model"),
            "mode": doc.get("mode"), "icon": doc.get("icon") or "", "updated": doc.get("updated"),
            "count": len(doc.get("messages") or []), "busy": _busy_request(doc["_id"]) is not None,
            "awaiting": any(_active[r]["approvals"] for r in _active if _active[r]["conv_id"] == doc["_id"])}


def _public(doc: dict) -> dict:
    rid = _busy_request(doc["_id"])
    return {**doc, "pending": _pending(rid) if rid else None}


def _auto_title(text: str) -> str:
    line = " ".join(text.split())
    return (line[:60] + "…") if len(line) > 60 else (line or DEFAULT_TITLE)


def _api_messages(msgs: list[dict]) -> list[dict]:
    out: list[dict] = []
    i = 0
    while i < len(msgs):
        m = msgs[i]
        role = m.get("role")
        if role == "user" and (m.get("content") or "").strip():
            out.append({"role": "user", "content": m["content"]})
        elif role == "assistant":
            calls = m.get("tool_calls") or []
            content = m.get("content") or ""
            if calls:
                out.append({"role": "assistant", "content": content or None, "tool_calls": [
                    {"id": c["id"], "type": "function", "function": {"name": c["name"], "arguments": c.get("arguments") or "{}"}}
                    for c in calls]})
                results = {}
                j = i + 1
                while j < len(msgs) and msgs[j].get("role") == "tool":
                    results[msgs[j].get("tool_call_id")] = msgs[j].get("content") or ""
                    j += 1
                for c in calls:
                    out.append({"role": "tool", "tool_call_id": c["id"],
                                "content": results.get(c["id"], "[interrompu : aucun résultat]")})
                i = j
                continue
            if content.strip():
                out.append({"role": "assistant", "content": content})
        i += 1
    while out and out[0]["role"] != "user":
        out.pop(0)
    return out


def _estimate_tokens(messages: list[dict], extra: str = "") -> int:
    chars = len(extra)
    for m in messages:
        chars += len(m.get("content") or "")
        for c in m.get("tool_calls") or []:
            chars += len(c["function"]["name"]) + len(c["function"].get("arguments") or "")
    return chars // 3


# ── Tools assembly ───────────────────────────────────────────────────────────

def _slug(s: str, n: int) -> str:
    return TOOL_NAME_RE.sub("_", s).strip("_")[:n] or "x"


def _resolve_workspace(doc: dict) -> Path:
    raw = (doc.get("workspace") or "").strip()
    if raw:
        p = Path(raw)
        if p.is_dir():
            return p
    return _default_workspace()


async def _build_tools(rid: str, doc: dict) -> tuple[list[la_tools.ToolSpec], list[str]]:
    if doc.get("mode", "chat") == "chat":
        return [], []
    conv_id = doc["_id"]
    warnings: list[str] = []

    async def on_todo(items: list):
        await _mutate(conv_id, lambda d: d.__setitem__("todo", items))
        await module.events.emit("agent:todo", {"conv_id": conv_id, "todo": items})

    ctx = la_tools.ToolContext(workspace=_resolve_workspace(doc), on_todo=on_todo)
    specs = la_tools.builtin_tools(ctx, [g for g in doc.get("tools") or [] if g in la_tools.GROUP_IDS])

    root = _skills_root()
    enabled = [s for s in doc.get("skills") or [] if la_skills.NAME_RE.match(s) and (root / s / la_skills.SKILL_FILE).is_file()]
    if enabled:
        async def load_skill(a: dict) -> str:
            name = a.get("name", "")
            if name not in enabled:
                raise la_tools.ToolError(f"Skill inconnu ou non activé : {name}. Disponibles : {', '.join(enabled)}")
            return await asyncio.to_thread(la_skills.read_skill, root, name)

        async def read_skill_file(a: dict) -> str:
            name = a.get("name", "")
            if name not in enabled:
                raise la_tools.ToolError(f"Skill inconnu ou non activé : {name}")
            try:
                return await asyncio.to_thread(la_skills.read_skill_file, root, name, a.get("path", ""))
            except la_skills.SkillError as e:
                raise la_tools.ToolError(e.message)

        specs += [
            la_tools.ToolSpec("load_skill", "Charge les instructions complètes (SKILL.md) d'un skill de la liste « Skills "
                                            "disponibles » du prompt système. À appeler avant d'appliquer un skill.",
                              {"type": "object", "properties": {"name": {"type": "string", "enum": enabled}},
                               "required": ["name"]}, load_skill, source="skill", group="skills"),
            la_tools.ToolSpec("read_skill_file", "Lit un fichier annexe d'un skill (référencé dans son SKILL.md).",
                              {"type": "object", "properties": {"name": {"type": "string", "enum": enabled},
                                                                "path": {"type": "string"}},
                               "required": ["name", "path"]}, read_skill_file, source="skill", group="skills"),
        ]

    used = {s.name for s in specs}
    for sid in doc.get("mcp") or []:
        cfg = await module.db.get(SERVERS, sid)
        if not cfg or not cfg.get("enabled", True):
            continue
        await module.events.emit("agent:status", {"request_id": rid, "conv_id": conv_id, "phase": "mcp",
                                                  "detail": cfg.get("name")})
        try:
            conn = await mcp_manager.ensure(cfg)
        except la_mcp.McpError as e:
            warnings.append(e.message)
            continue
        prefix = "mcp_" + _slug(cfg.get("name") or sid, 20) + "__"
        timeout = max(5, int(cfg.get("tool_timeout") or _settings()["tool_timeout"]))
        for t in conn.tools:
            name = prefix + _slug(t.name, 64 - len(prefix))
            k = 2
            while name in used:
                name = f"{prefix}{_slug(t.name, 60 - len(prefix))}_{k}"
                k += 1
            used.add(name)

            async def run(a: dict, _conn=conn, _tool=t.name, _to=timeout) -> str:
                try:
                    text, is_error = await _conn.call(_tool, a, _to)
                except la_mcp.McpError as e:
                    raise la_tools.ToolError(e.message)
                if is_error:
                    raise la_tools.ToolError(text)
                return la_tools.truncate(text)

            schema = t.inputSchema if isinstance(getattr(t, "inputSchema", None), dict) else {}
            if schema.get("type") != "object":
                schema = {"type": "object", "properties": {}}
            specs.append(la_tools.ToolSpec(name, (t.description or t.name), schema, run,
                                           source="mcp", group="mcp:" + sid, default_policy="ask",
                                           timeout=timeout + 10,
                                           meta={"server": cfg.get("name"), "tool": t.name}))
    return specs, warnings


def _system_prompt(doc: dict, specs: list[la_tools.ToolSpec]) -> str:
    parts = [(doc.get("system") or "").strip()]
    if specs:
        env = [f"- Date : {datetime.now().strftime('%Y-%m-%d %H:%M')}",
               f"- Système : {platform.system()} {platform.release()}"]
        if any(s.group in ("files_read", "files_write", "shell") for s in specs):
            env.append(f"- Dossier de travail : {_resolve_workspace(doc)}")
        parts.append("## Environnement\n" + "\n".join(env))
    if any(s.name == "load_skill" for s in specs):
        root = _skills_root()
        lines = []
        for n in doc.get("skills") or []:
            i = la_skills.info(root, n) if la_skills.NAME_RE.match(n) else None
            if i:
                lines.append(f"- **{n}** : {i['description']}")
        parts.append("## Skills disponibles\nQuand une demande correspond à un skill ci-dessous, charge d'abord ses "
                     "instructions avec l'outil `load_skill`, puis suis-les.\n" + "\n".join(lines))
    if doc.get("summary"):
        parts.append("## Résumé de la partie antérieure de la conversation\n" + doc["summary"])
    return "\n\n".join(p for p in parts if p)


# ── LLM streaming ────────────────────────────────────────────────────────────

async def _stream_completion(rid: str, conv_id: str, model: str, messages: list[dict],
                             tools: list[dict], entry: dict) -> dict:
    client = _client()
    params: dict[str, Any] = {"model": model, "messages": messages, "stream": True,
                              "stream_options": {"include_usage": True}}
    if tools:
        params["tools"] = tools
    max_tokens = _settings()["max_tokens"]
    if max_tokens:
        params["max_tokens"] = max_tokens

    pending_text: list[str] = []
    pending_reason: list[str] = []
    last_flush = time.monotonic()

    async def flush():
        nonlocal last_flush
        if pending_text or pending_reason:
            payload = {"request_id": rid, "conv_id": conv_id}
            if pending_text:
                payload["text"] = "".join(pending_text)
            if pending_reason:
                payload["reasoning"] = "".join(pending_reason)
            pending_text.clear()
            pending_reason.clear()
            await module.events.emit("agent:delta", payload)
        last_flush = time.monotonic()

    calls: dict[int, dict] = {}
    usage = {"input": 0, "output": 0}
    finish = None
    resolved_model = model
    try:
        stream = await client.chat.completions.create(**params)
        async for chunk in stream:
            if getattr(chunk, "model", None):
                resolved_model = chunk.model
            u = getattr(chunk, "usage", None)
            if u:
                usage = {"input": int(getattr(u, "prompt_tokens", 0) or 0),
                         "output": int(getattr(u, "completion_tokens", 0) or 0)}
            if not chunk.choices:
                continue
            ch = chunk.choices[0]
            d = ch.delta
            if d is not None:
                if d.content:
                    entry["text"] += d.content
                    pending_text.append(d.content)
                extra = getattr(d, "model_extra", None) or {}
                rc = getattr(d, "reasoning_content", None) or extra.get("reasoning_content") or extra.get("reasoning")
                if isinstance(rc, str) and rc:
                    entry["reasoning"] += rc
                    pending_reason.append(rc)
                for pos, tc in enumerate(d.tool_calls or []):
                    idx = tc.index if tc.index is not None else pos
                    slot = calls.get(idx)
                    if slot is None:
                        slot = calls[idx] = {"id": "", "name": "", "arguments": ""}
                        await flush()
                    if tc.id:
                        slot["id"] = tc.id
                    fn = tc.function
                    if fn is not None:
                        if fn.name and fn.name != slot["name"]:
                            slot["name"] = fn.name if not slot["name"] else slot["name"] + fn.name
                            await module.events.emit("agent:status", {"request_id": rid, "conv_id": conv_id,
                                                                      "phase": "tool_call", "detail": slot["name"]})
                        if fn.arguments:
                            slot["arguments"] += fn.arguments
            if ch.finish_reason:
                finish = ch.finish_reason
            if time.monotonic() - last_flush >= FLUSH_INTERVAL_S:
                await flush()
        await flush()
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        mapped = _map_api_error(exc)
        log.warning("LLM call failed (%s, %s)", mapped.code, type(exc).__name__)
        raise mapped

    tool_calls = []
    for idx in sorted(calls):
        c = calls[idx]
        if not c["name"]:
            continue
        tool_calls.append({"id": c["id"] or ("call_" + uuid.uuid4().hex[:20]), "name": c["name"],
                           "arguments": c["arguments"] or "{}"})
    return {"text": entry["text"], "reasoning": entry["reasoning"], "tool_calls": tool_calls,
            "finish": finish, "usage": usage, "model": resolved_model}


async def _complete_text(model: str, system: str, user: str) -> str:
    try:
        resp = await _client().chat.completions.create(
            model=model, messages=[{"role": "system", "content": system}, {"role": "user", "content": user}])
    except Exception as exc:
        raise _map_api_error(exc)
    return (resp.choices[0].message.content or "").strip() if resp.choices else ""


# ── Context compaction ───────────────────────────────────────────────────────

def _transcript(msgs: list[dict]) -> str:
    lines = []
    for m in msgs:
        role = m.get("role")
        if role == "user":
            lines.append("UTILISATEUR : " + (m.get("content") or ""))
        elif role == "assistant":
            if (m.get("content") or "").strip():
                lines.append("ASSISTANT : " + m["content"])
            for c in m.get("tool_calls") or []:
                lines.append(f"ASSISTANT → outil {c['name']}({(c.get('arguments') or '')[:500]})")
        elif role == "tool":
            content = m.get("content") or ""
            lines.append(f"RÉSULTAT {m.get('name', '')} : " + (content[:1500] + " […]" if len(content) > 1500 else content))
    return "\n\n".join(lines)


COMPACT_SYSTEM = ("Tu condenses l'historique d'une conversation entre un utilisateur et un assistant-agent pour qu'elle "
                  "puisse continuer sans l'historique complet. Produis un résumé factuel et structuré en français : "
                  "objectifs de l'utilisateur, décisions prises, informations et résultats importants (chemins, valeurs, "
                  "commandes, erreurs), travail effectué et ce qui reste à faire. Intègre le résumé précédent s'il existe. "
                  "Pas de préambule.")


async def _compact(rid: str | None, conv_id: str, model: str, force: bool) -> bool:
    doc = await _get_conv(conv_id)
    if doc is None:
        return False
    msgs = doc.get("messages") or []
    upto = int(doc.get("summary_upto") or 0)
    users = [i for i, m in enumerate(msgs) if m.get("role") == "user" and i > upto]
    if not users:
        return False
    budget = _settings()["context_budget"]
    if force:
        cut = users[-1]
    else:
        total = _estimate_tokens(_api_messages(msgs[upto:]), _system_prompt(doc, []))
        if total <= budget:
            return False
        cut = next((i for i in users if _estimate_tokens(_api_messages(msgs[i:])) <= budget // 2), users[-1])
    if cut <= upto:
        return False
    if rid:
        await module.events.emit("agent:status", {"request_id": rid, "conv_id": conv_id, "phase": "compacting"})
    prev = doc.get("summary") or ""
    text = (f"Résumé précédent :\n{prev}\n\n" if prev else "") + "Suite de la conversation :\n\n" + _transcript(msgs[upto:cut])
    summary = await _complete_text(model, COMPACT_SYSTEM, la_tools.truncate(text, 400_000))
    if not summary:
        return False

    def apply(d: dict):
        d["summary"] = summary
        d["summary_upto"] = cut
        d.setdefault("messages", []).insert(cut, {"id": _mid(), "role": "event", "kind": "compacted",
                                                  "content": summary, "ts": _now()})
        d["summary_upto"] = cut + 1

    await _mutate(conv_id, apply)
    await module.events.emit("agent:compacted", {"conv_id": conv_id})
    return True


# ── Agent loop ───────────────────────────────────────────────────────────────

async def _append(conv_id: str, items: list[dict], usage: dict | None = None):
    def apply(d: dict):
        d.setdefault("messages", []).extend(items)
        if usage:
            tot = d.setdefault("usage_total", {"input": 0, "output": 0})
            tot["input"] = tot.get("input", 0) + usage.get("input", 0)
            tot["output"] = tot.get("output", 0) + usage.get("output", 0)
    return await _mutate(conv_id, apply)


async def _decide(rid: str, conv_id: str, mode: str, spec: la_tools.ToolSpec, call: dict, args: dict,
                  entry: dict) -> tuple[str, str]:
    policies = await _policies()
    policy = policies.get(spec.name) or spec.default_policy
    if policy == "deny":
        return "deny", "Outil interdit par les réglages de l'utilisateur."
    if policy == "allow" or mode == "auto":
        return "allow", ""
    fut = asyncio.get_running_loop().create_future()
    info = {"call_id": call["id"], "name": spec.name, "arguments": args, "source": spec.source,
            "label": spec.meta.get("tool") or spec.name, "server": spec.meta.get("server")}
    entry["approvals"][call["id"]] = {"future": fut, "info": info}
    await module.events.emit("agent:approval", {"request_id": rid, "conv_id": conv_id, **info})
    try:
        decision, reason = await fut
    finally:
        entry["approvals"].pop(call["id"], None)
    if decision == "allow_always":
        policies = await _policies()
        policies[spec.name] = "allow"
        await _set_setting("policies", policies)
        return "allow", ""
    if decision == "deny":
        return "deny", "L'utilisateur a refusé cet appel." + (f" Motif : {reason}" if reason else "")
    return "allow", ""


async def _handle_call(rid: str, conv_id: str, mode: str, call: dict, specs: dict[str, la_tools.ToolSpec],
                       entry: dict) -> dict:
    base = {"id": _mid(), "role": "tool", "tool_call_id": call["id"], "name": call["name"], "ts": _now()}
    spec = specs.get(call["name"])
    if spec is None:
        return {**base, "status": "error", "content": f"Erreur : outil inconnu « {call['name']} »."}
    try:
        args = la_tools.parse_args(call.get("arguments"))
    except la_tools.ToolError as e:
        return {**base, "status": "error", "content": f"Erreur : {e}"}
    decision, reason = await _decide(rid, conv_id, mode, spec, call, args, entry)
    if decision == "deny":
        msg = {**base, "status": "denied", "content": reason}
        await module.events.emit("agent:tool", {"request_id": rid, "conv_id": conv_id, "call_id": call["id"],
                                                "status": "denied", "message": msg})
        return msg
    entry["running"][call["id"]] = {"call_id": call["id"], "name": spec.name, "started": time.time()}
    await module.events.emit("agent:tool", {"request_id": rid, "conv_id": conv_id, "call_id": call["id"],
                                            "status": "running", "name": spec.name})
    t0 = time.monotonic()
    timeout = spec.timeout or _settings()["tool_timeout"]
    try:
        content = await asyncio.wait_for(spec.run(args), timeout)
        status = "ok"
    except la_tools.ToolError as e:
        content, status = f"Erreur : {e}", "error"
    except asyncio.TimeoutError:
        content, status = f"Erreur : délai dépassé ({timeout} s).", "error"
    except asyncio.CancelledError:
        raise
    except (KeyError, TypeError, ValueError) as e:
        content, status = f"Erreur : arguments invalides ({type(e).__name__}: {e}).", "error"
    except Exception as e:
        log.warning("tool %s failed: %s", spec.name, type(e).__name__)
        content, status = f"Erreur interne de l'outil : {type(e).__name__}: {e}", "error"
    finally:
        entry["running"].pop(call["id"], None)
    msg = {**base, "status": status, "content": la_tools.truncate(str(content)),
           "duration_ms": int((time.monotonic() - t0) * 1000)}
    await module.events.emit("agent:tool", {"request_id": rid, "conv_id": conv_id, "call_id": call["id"],
                                            "status": status, "message": msg})
    return msg


async def _run_agent(rid: str, conv_id: str, entry: dict) -> None:
    ev = {"request_id": rid, "conv_id": conv_id}
    current_calls: list[dict] = []
    done_ids: set[str] = set()
    try:
        doc = await _get_conv(conv_id)
        if doc is None:
            raise AgentError("NOT_FOUND", "Conversation introuvable")
        model = (doc.get("model") or _settings()["default_model"]).strip()
        if not model:
            raise AgentError("NO_MODEL", "Aucun modèle choisi : sélectionnez un modèle pour cette conversation.")
        mode = doc.get("mode") if doc.get("mode") in MODES else "chat"
        specs, warnings = await _build_tools(rid, doc)
        for w in warnings:
            await module.events.emit("agent:notice", {**ev, "level": "warn", "text": w})
        spec_map = {s.name: s for s in specs}
        tool_defs = [s.openai() for s in specs]
        max_it = int(doc.get("max_iterations") or 0) or _settings()["max_iterations"]
        entry["max_iterations"] = max_it if tool_defs else 1
        reason = "done"

        for it in range(1, max_it + 1):
            entry["iteration"] = it
            entry["phase"] = "thinking"
            entry["text"] = ""
            entry["reasoning"] = ""
            await module.events.emit("agent:step", {**ev, "iteration": it, "max": entry["max_iterations"]})
            await _compact(rid, conv_id, model, force=False)
            doc = await _get_conv(conv_id)
            upto = int(doc.get("summary_upto") or 0)
            messages = _api_messages((doc.get("messages") or [])[upto:])
            system = _system_prompt(doc, specs)
            if system:
                messages = [{"role": "system", "content": system}] + messages
            entry["phase"] = "streaming"
            result = await _stream_completion(rid, conv_id, model, messages, tool_defs, entry)
            amsg = {"id": _mid(), "role": "assistant", "content": result["text"], "ts": _now(),
                    "model": result["model"], "usage": result["usage"]}
            if result["reasoning"]:
                amsg["reasoning"] = result["reasoning"]
            if result["tool_calls"]:
                amsg["tool_calls"] = result["tool_calls"]
            if result["finish"] == "length":
                amsg["truncated"] = True
            current_calls = result["tool_calls"]
            done_ids = set()
            entry["text"] = ""
            entry["reasoning"] = ""
            await _append(conv_id, [amsg], result["usage"])
            await module.events.emit("agent:message", {**ev, "message": amsg})
            if not result["tool_calls"]:
                break
            entry["phase"] = "tools"

            async def one(c: dict) -> dict:
                m = await _handle_call(rid, conv_id, mode, c, spec_map, entry)
                done_ids.add(c["id"])
                return m

            tool_msgs = await asyncio.gather(*(one(c) for c in result["tool_calls"]))
            current_calls = []
            await _append(conv_id, list(tool_msgs))
        else:
            reason = "max_iterations"
            note = {"id": _mid(), "role": "event", "kind": "limit", "ts": _now(),
                    "content": f"Limite de {max_it} itérations atteinte : l'agent s'est arrêté. Envoyez « continue » pour reprendre."}
            await _append(conv_id, [note])
            await module.events.emit("agent:message", {**ev, "message": note})
        saved = await _get_conv(conv_id)
        await module.events.emit("agent:done", {**ev, "reason": reason,
                                                "usage_total": (saved or {}).get("usage_total")})
    except asyncio.CancelledError:
        items = []
        if entry["text"].strip() or entry["reasoning"].strip():
            items.append({"id": _mid(), "role": "assistant", "content": entry["text"], "reasoning": entry["reasoning"] or None,
                          "ts": _now(), "stopped": True})
        for c in current_calls:
            if c["id"] not in done_ids:
                items.append({"id": _mid(), "role": "tool", "tool_call_id": c["id"], "name": c["name"], "ts": _now(),
                              "status": "cancelled", "content": "Appel annulé par l'utilisateur."})
        if items:
            await asyncio.shield(_append(conv_id, items))
        await module.events.emit("agent:done", {**ev, "reason": "stopped"})
        raise
    except AgentError as exc:
        await module.events.emit("agent:error", {**ev, "error": exc.message, "code": exc.code, **exc.extra})
    except Exception:
        log.exception("agent run failed")
        await module.events.emit("agent:error", {**ev, "error": "Erreur interne inattendue", "code": "INTERNAL"})


def _start(rid: str, conv_id: str) -> None:
    entry = {"conv_id": conv_id, "text": "", "reasoning": "", "iteration": 0, "max_iterations": 0,
             "phase": "starting", "approvals": {}, "running": {}, "started": time.time()}
    _active[rid] = entry
    task = asyncio.create_task(_run_agent(rid, conv_id, entry))
    entry["task"] = task
    task.add_done_callback(lambda _t, r=rid: _active.pop(r, None))


async def _launch(conv_id: str, rid: str, prepare: Callable[[dict], str | None]) -> dict:
    if not _base_url():
        return _err("CONFIG_MISSING", f"URL du proxy absente : définissez {URL_KEY} dans le menu « Variables ».")
    if not REQUEST_ID_RE.match(rid or "") or rid in _active:
        return _err("INVALID_PAYLOAD", "Identifiant de requête invalide")
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
        doc = await module.db.save(CONVS, doc)
        _start(rid, conv_id)
    return {"request_id": rid, "conversation": _public(doc)}


# ── Payloads ─────────────────────────────────────────────────────────────────

class AgentSettings(BaseModel):
    model: str | None = Field(None, max_length=200)
    system: str | None = Field(None, max_length=100_000)
    mode: Literal["chat", "approve", "auto"] | None = None
    tools: list[str] | None = None
    skills: list[str] | None = None
    mcp: list[str] | None = None
    workspace: str | None = Field(None, max_length=1000)
    max_iterations: int | None = Field(None, ge=0, le=200)


class ConvCreate(AgentSettings):
    title: str = Field(DEFAULT_TITLE, max_length=200)
    profile_id: str | None = None


class ConvUpdate(AgentSettings):
    id: str
    title: str | None = Field(None, max_length=200)


class ProfilePayload(AgentSettings):
    id: str | None = None
    name: str = Field(..., min_length=1, max_length=80)
    icon: str = Field("", max_length=8)


class SendPayload(BaseModel):
    conv_id: str
    request_id: str
    text: str = Field(..., max_length=MAX_INPUT_CHARS)


class RegenPayload(BaseModel):
    conv_id: str
    request_id: str


class ApprovePayload(BaseModel):
    request_id: str
    call_id: str
    decision: Literal["allow", "allow_always", "deny"]
    reason: str = Field("", max_length=2000)


class McpPayload(BaseModel):
    id: str | None = None
    name: str = Field(..., min_length=1, max_length=60)
    transport: Literal["stdio", "http", "sse"] = "stdio"
    command: str = Field("", max_length=1000)
    args: list[str] = Field(default_factory=list, max_length=100)
    env: dict[str, str] = Field(default_factory=dict)
    cwd: str = Field("", max_length=1000)
    url: str = Field("", max_length=2000)
    headers: dict[str, str] = Field(default_factory=dict)
    enabled: bool = True
    timeout: int = Field(60, ge=5, le=600)
    tool_timeout: int = Field(0, ge=0, le=3600)


def _apply_settings(target: dict, p: AgentSettings) -> str | None:
    if p.workspace is not None:
        ws = p.workspace.strip()
        if ws and not Path(ws).is_dir():
            return f"Dossier de travail introuvable : {ws}"
        target["workspace"] = ws
    if p.model is not None:
        target["model"] = p.model.strip()
    if p.system is not None:
        target["system"] = p.system
    if p.mode is not None:
        target["mode"] = p.mode
    if p.tools is not None:
        target["tools"] = [t for t in dict.fromkeys(p.tools) if t in la_tools.GROUP_IDS]
    if p.skills is not None:
        target["skills"] = [s for s in dict.fromkeys(p.skills) if la_skills.NAME_RE.match(s)]
    if p.mcp is not None:
        target["mcp"] = [s for s in dict.fromkeys(p.mcp) if isinstance(s, str) and s][:50]
    if p.max_iterations is not None:
        target["max_iterations"] = p.max_iterations
    return None


def _parse(model: type[BaseModel], payload: dict) -> tuple[Any, dict | None]:
    try:
        return model(**(payload or {})), None
    except ValidationError as exc:
        return None, _err("INVALID_PAYLOAD", _first_error(exc))


# ── Handlers : status & models ───────────────────────────────────────────────

@module.on("status")
async def status(payload: dict):
    await _seed()
    return {
        "has_url": _base_url() is not None,
        "base_url": _base_url(),
        "has_key": bool(_api_key()),
        "url_key": URL_KEY,
        "api_key_name": API_KEY,
        "sdk_available": _sdk_available(),
        "mcp_available": la_mcp.mcp_available(),
        "settings": _settings(),
        "tool_groups": la_tools.GROUPS,
        "default_workspace": str(_default_workspace()),
        "platform": platform.system(),
    }


@module.on("models:list")
async def models_list(payload: dict):
    try:
        return {"items": await _list_models(bool((payload or {}).get("refresh")))}
    except AgentError as e:
        return _err(e.code, e.message)


# ── Handlers : conversations ─────────────────────────────────────────────────

@module.on("conv:list")
async def conv_list(payload: dict):
    docs = await module.db.query(CONVS)
    docs.sort(key=lambda d: d.get("updated") or "", reverse=True)
    return {"items": [_summary(d) for d in docs]}


@module.on("conv:get")
async def conv_get(payload: dict):
    doc = await _get_conv((payload or {}).get("id"))
    if doc is None:
        return _err("NOT_FOUND", "Conversation introuvable")
    return {"conversation": _public(doc)}


@module.on("conv:create")
async def conv_create(payload: dict):
    p, bad = _parse(ConvCreate, payload)
    if bad:
        return bad
    base: dict[str, Any] = {"model": _settings()["default_model"], "system": "", "mode": "chat", "tools": [],
                            "skills": [], "mcp": [], "workspace": "", "max_iterations": 0, "icon": ""}
    if p.profile_id:
        prof = await module.db.get(PROFILES, p.profile_id)
        if prof is None:
            return _err("NOT_FOUND", "Profil introuvable")
        for k in ("system", "mode", "tools", "skills", "mcp", "workspace", "max_iterations", "icon"):
            if k in prof:
                base[k] = prof[k]
        if prof.get("model"):
            base["model"] = prof["model"]
        base["profile_id"] = prof["_id"]
    if problem := _apply_settings(base, p):
        return _err("INVALID_PAYLOAD", problem)
    now = _now()
    doc = await module.db.save(CONVS, {**base, "title": p.title.strip() or DEFAULT_TITLE, "messages": [], "todo": [],
                                       "summary": "", "summary_upto": 0, "usage_total": {"input": 0, "output": 0},
                                       "created": now, "updated": now})
    return {"conversation": _public(doc)}


@module.on("conv:update")
async def conv_update(payload: dict):
    p, bad = _parse(ConvUpdate, payload)
    if bad:
        return bad
    problem: list[str] = []

    def apply(d: dict):
        if p.title is not None:
            d["title"] = p.title.strip() or DEFAULT_TITLE
        if msg := _apply_settings(d, p):
            problem.append(msg)

    if p.workspace and not Path(p.workspace.strip()).is_dir():
        return _err("INVALID_PAYLOAD", f"Dossier de travail introuvable : {p.workspace.strip()}")
    doc = await _mutate(p.id, apply)
    if doc is None:
        return _err("NOT_FOUND", "Conversation introuvable")
    if problem:
        return _err("INVALID_PAYLOAD", problem[0])
    return {"conversation": _public(doc)}


async def _cancel_conv(conv_id: str):
    rid = _busy_request(conv_id)
    if rid:
        task = _active[rid].get("task")
        if task:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


@module.on("conv:delete")
async def conv_delete(payload: dict):
    conv_id = (payload or {}).get("id")
    if await _get_conv(conv_id) is None:
        return _err("NOT_FOUND", "Conversation introuvable")
    await _cancel_conv(conv_id)
    await module.db.delete(CONVS, conv_id)
    _locks.pop(conv_id, None)
    return {"ok": True}


@module.on("conv:clear")
async def conv_clear(payload: dict):
    conv_id = (payload or {}).get("id")
    if _busy_request(conv_id or ""):
        return _err("BUSY", "Arrêtez d'abord la réponse en cours")

    def apply(d: dict):
        d.update(messages=[], todo=[], summary="", summary_upto=0, usage_total={"input": 0, "output": 0})
    doc = await _mutate(conv_id, apply)
    if doc is None:
        return _err("NOT_FOUND", "Conversation introuvable")
    return {"conversation": _public(doc)}


@module.on("conv:compact")
async def conv_compact(payload: dict):
    conv_id = (payload or {}).get("id")
    doc = await _get_conv(conv_id)
    if doc is None:
        return _err("NOT_FOUND", "Conversation introuvable")
    if _busy_request(conv_id):
        return _err("BUSY", "Arrêtez d'abord la réponse en cours")
    model = (doc.get("model") or _settings()["default_model"]).strip()
    if not model:
        return _err("NO_MODEL", "Aucun modèle choisi")
    try:
        done = await _compact(None, conv_id, model, force=True)
    except AgentError as e:
        return _err(e.code, e.message)
    if not done:
        return _err("NOTHING_TO_DO", "Pas assez d'historique à compresser")
    return {"conversation": _public(await _get_conv(conv_id))}


@module.on("conv:export")
async def conv_export(payload: dict):
    doc = await _get_conv((payload or {}).get("id"))
    if doc is None:
        return _err("NOT_FOUND", "Conversation introuvable")
    lines = [f"# {doc.get('title') or DEFAULT_TITLE}", ""]
    if (doc.get("system") or "").strip():
        lines += ["> **Prompt système** : " + doc["system"].strip().replace("\n", "\n> "), ""]
    for m in doc.get("messages") or []:
        role = m.get("role")
        if role == "user":
            lines += ["## 👤 Utilisateur", "", m.get("content") or "", ""]
        elif role == "assistant":
            if (m.get("content") or "").strip():
                lines += ["## 🤖 Assistant", "", m["content"], ""]
            for c in m.get("tool_calls") or []:
                lines += [f"**🔧 {c['name']}**", "", "```json", c.get("arguments") or "{}", "```", ""]
        elif role == "tool":
            lines += [f"<details><summary>Résultat {m.get('name')} ({m.get('status')})</summary>", "", "```",
                      (m.get("content") or "")[:5000], "```", "</details>", ""]
        elif role == "event":
            lines += [f"_{m.get('kind')}_ : {m.get('content') or ''}", ""]
    return {"markdown": "\n".join(lines), "filename": re.sub(r"[^\w\- ]+", "", doc.get("title") or "conversation")[:60] + ".md"}


# ── Handlers : chat ──────────────────────────────────────────────────────────

@module.on("chat:send")
async def chat_send(payload: dict):
    p, bad = _parse(SendPayload, payload)
    if bad:
        return bad
    text = p.text.strip()
    if not text:
        return _err("INVALID_PAYLOAD", "Le message est vide")

    def prepare(d: dict):
        msgs = d.setdefault("messages", [])
        if not any(m.get("role") == "user" for m in msgs) and (d.get("title") or DEFAULT_TITLE) == DEFAULT_TITLE:
            d["title"] = _auto_title(text)
        msgs.append({"id": _mid(), "role": "user", "content": text, "ts": _now()})

    return await _launch(p.conv_id, p.request_id, prepare)


def _truncate_to_last_user(d: dict) -> str | None:
    msgs = d.setdefault("messages", [])
    idx = next((i for i in range(len(msgs) - 1, -1, -1) if msgs[i].get("role") == "user"), None)
    if idx is None:
        return "Aucun message utilisateur"
    if idx < int(d.get("summary_upto") or 0):
        return "Ce message fait partie de l'historique compressé"
    del msgs[idx + 1:]
    return None


@module.on("chat:regenerate")
async def chat_regenerate(payload: dict):
    p, bad = _parse(RegenPayload, payload)
    if bad:
        return bad
    return await _launch(p.conv_id, p.request_id, _truncate_to_last_user)


@module.on("chat:edit_last")
async def chat_edit_last(payload: dict):
    p, bad = _parse(SendPayload, payload)
    if bad:
        return bad
    text = p.text.strip()
    if not text:
        return _err("INVALID_PAYLOAD", "Le message est vide")

    def prepare(d: dict):
        if problem := _truncate_to_last_user(d):
            return problem
        last = d["messages"][-1]
        last.update(content=text, edited=True, ts=_now())
        return None

    return await _launch(p.conv_id, p.request_id, prepare)


@module.on("chat:stop")
async def chat_stop(payload: dict):
    entry = _active.get((payload or {}).get("request_id") or "")
    if entry is None or entry.get("task") is None:
        return _err("NOT_FOUND", "Aucune réponse en cours avec cet identifiant")
    entry["task"].cancel()
    return {"ok": True}


@module.on("chat:approve")
async def chat_approve(payload: dict):
    p, bad = _parse(ApprovePayload, payload)
    if bad:
        return bad
    entry = _active.get(p.request_id)
    pending = entry["approvals"].get(p.call_id) if entry else None
    if pending is None or pending["future"].done():
        return _err("NOT_FOUND", "Aucune validation en attente pour cet appel")
    pending["future"].set_result((p.decision, p.reason.strip()))
    return {"ok": True}


# ── Handlers : profiles ──────────────────────────────────────────────────────

@module.on("profiles:list")
async def profiles_list(payload: dict):
    await _seed()
    docs = await module.db.query(PROFILES)
    docs.sort(key=lambda d: (d.get("created") or "", d.get("name") or ""))
    return {"items": docs}


@module.on("profiles:save")
async def profiles_save(payload: dict):
    p, bad = _parse(ProfilePayload, payload)
    if bad:
        return bad
    now = _now()
    if p.id:
        doc = await module.db.get(PROFILES, p.id)
        if doc is None:
            return _err("NOT_FOUND", "Profil introuvable")
    else:
        doc = {"model": "", "system": "", "mode": "chat", "tools": [], "skills": [], "mcp": [], "workspace": "",
               "max_iterations": 0, "created": now}
    doc.update(name=p.name.strip(), icon=p.icon.strip(), updated=now)
    if problem := _apply_settings(doc, p):
        return _err("INVALID_PAYLOAD", problem)
    return {"profile": await module.db.save(PROFILES, doc)}


@module.on("profiles:delete")
async def profiles_delete(payload: dict):
    res = await module.db.delete(PROFILES, (payload or {}).get("id") or "")
    return {"ok": True} if res["ok"] else _err("NOT_FOUND", "Profil introuvable")


# ── Handlers : tools & policies ──────────────────────────────────────────────

@module.on("tools:catalog")
async def tools_catalog(payload: dict):
    ctx = la_tools.ToolContext(workspace=_default_workspace())
    builtins = [{"name": s.name, "group": s.group, "description": s.description, "default_policy": s.default_policy}
                for s in la_tools.builtin_tools(ctx, la_tools.GROUP_IDS)]
    return {"groups": la_tools.GROUPS, "builtins": builtins, "policies": await _policies()}


@module.on("policies:set")
async def policies_set(payload: dict):
    name = (payload or {}).get("name")
    policy = (payload or {}).get("policy")
    if not isinstance(name, str) or not name or len(name) > 128:
        return _err("INVALID_PAYLOAD", "Nom d'outil invalide")
    pol = await _policies()
    if policy in (None, "", "default"):
        pol.pop(name, None)
    elif policy in POLICIES:
        pol[name] = policy
    else:
        return _err("INVALID_PAYLOAD", "Politique invalide")
    await _set_setting("policies", pol)
    return {"policies": pol}


# ── Handlers : MCP ───────────────────────────────────────────────────────────

def _server_view(doc: dict) -> dict:
    conn = mcp_manager.get(doc["_id"])
    snap = conn.snapshot() if conn else {"status": "stopped", "error": None, "tools": []}
    prefix = "mcp_" + _slug(doc.get("name") or doc["_id"], 20) + "__"
    for t in snap["tools"]:
        t["exposed"] = prefix + _slug(t["name"], 64 - len(prefix))
    return {**doc, "status": snap["status"], "error": snap["error"], "tools": snap["tools"]}


@module.on("mcp:list")
async def mcp_list(payload: dict):
    docs = await module.db.query(SERVERS)
    docs.sort(key=lambda d: (d.get("name") or "").lower())
    return {"items": [_server_view(d) for d in docs], "available": la_mcp.mcp_available()}


@module.on("mcp:save")
async def mcp_save(payload: dict):
    p, bad = _parse(McpPayload, payload)
    if bad:
        return bad
    if p.transport == "stdio" and not p.command.strip():
        return _err("INVALID_PAYLOAD", "Indiquez la commande à lancer (ex. npx, uvx, python)")
    if p.transport != "stdio" and not re.match(r"^https?://", p.url.strip()):
        return _err("INVALID_PAYLOAD", "URL invalide (http:// ou https://)")
    if p.id:
        doc = await module.db.get(SERVERS, p.id)
        if doc is None:
            return _err("NOT_FOUND", "Serveur introuvable")
    else:
        doc = {"created": _now()}
    doc.update(p.model_dump(exclude={"id"}))
    doc["name"] = p.name.strip()
    doc["command"] = p.command.strip()
    doc["url"] = p.url.strip()
    doc = await module.db.save(SERVERS, doc)
    conn = mcp_manager.get(doc["_id"])
    if conn and (not doc["enabled"] or conn.fingerprint != la_mcp._fingerprint(doc)):
        await mcp_manager.stop(doc["_id"])
    return {"server": _server_view(doc)}


@module.on("mcp:delete")
async def mcp_delete(payload: dict):
    sid = (payload or {}).get("id") or ""
    await mcp_manager.stop(sid)
    res = await module.db.delete(SERVERS, sid)
    return {"ok": True} if res["ok"] else _err("NOT_FOUND", "Serveur introuvable")


@module.on("mcp:connect")
async def mcp_connect(payload: dict):
    sid = (payload or {}).get("id") or ""
    doc = await module.db.get(SERVERS, sid)
    if doc is None:
        return _err("NOT_FOUND", "Serveur introuvable")
    try:
        await mcp_manager.ensure(doc, restart=bool((payload or {}).get("restart", True)))
    except la_mcp.McpError as e:
        conn = mcp_manager.get(sid)
        return _err(e.code, e.message, log=conn.log_tail() if conn else "", server=_server_view(doc))
    return {"server": _server_view(doc)}


@module.on("mcp:log")
async def mcp_log(payload: dict):
    conn = mcp_manager.get((payload or {}).get("id") or "")
    return {"log": conn.log_tail() if conn else ""}


@module.on("mcp:disconnect")
async def mcp_disconnect(payload: dict):
    sid = (payload or {}).get("id") or ""
    await mcp_manager.stop(sid)
    doc = await module.db.get(SERVERS, sid)
    return {"server": _server_view(doc)} if doc else {"ok": True}


@module.on("mcp:import")
async def mcp_import(payload: dict):
    raw = (payload or {}).get("json") or ""
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        return _err("INVALID_PAYLOAD", f"JSON invalide : {e.msg}")
    servers = data.get("mcpServers") or data.get("servers") if isinstance(data, dict) else None
    if not isinstance(servers, dict) or not servers:
        return _err("INVALID_PAYLOAD", "Format attendu : { \"mcpServers\": { \"nom\": { \"command\": …, \"args\": […] } } }")
    existing = {(d.get("name") or "").lower() for d in await module.db.query(SERVERS)}
    added, skipped = [], []
    for name, cfg in servers.items():
        if not isinstance(cfg, dict) or str(name).lower() in existing:
            skipped.append(str(name))
            continue
        url = cfg.get("url") or cfg.get("serverUrl") or ""
        ctype = (cfg.get("type") or cfg.get("transport") or "").lower()
        transport = "stdio" if cfg.get("command") else ("sse" if ctype == "sse" else "http")
        try:
            p = McpPayload(name=str(name)[:60], transport=transport, command=str(cfg.get("command") or ""),
                           args=[str(a) for a in cfg.get("args") or []],
                           env={str(k): str(v) for k, v in (cfg.get("env") or {}).items()},
                           cwd=str(cfg.get("cwd") or ""), url=str(url),
                           headers={str(k): str(v) for k, v in (cfg.get("headers") or {}).items()},
                           enabled=not cfg.get("disabled", False))
        except ValidationError:
            skipped.append(str(name))
            continue
        await module.db.save(SERVERS, {**p.model_dump(exclude={"id"}), "created": _now()})
        added.append(p.name)
    return {"added": added, "skipped": skipped}


# ── Handlers : skills ────────────────────────────────────────────────────────

def _skill_err(e: la_skills.SkillError) -> dict:
    return _err(e.code, e.message)


@module.on("skills:list")
async def skills_list(payload: dict):
    return {"items": await asyncio.to_thread(la_skills.list_skills, _skills_root()), "root": str(_skills_root())}


@module.on("skills:get")
async def skills_get(payload: dict):
    name = (payload or {}).get("name") or ""
    try:
        content = await asyncio.to_thread(la_skills.read_skill, _skills_root(), name)
        info = await asyncio.to_thread(la_skills.info, _skills_root(), name)
    except la_skills.SkillError as e:
        return _skill_err(e)
    return {"skill": info, "content": content}


@module.on("skills:save")
async def skills_save(payload: dict):
    payload = payload or {}
    name = la_skills.slugify(payload.get("name") or "")
    content = payload.get("content")
    if not isinstance(content, str) or not content.strip():
        return _err("INVALID_PAYLOAD", "Le contenu du SKILL.md est vide")
    original = payload.get("original") or ""
    try:
        if not original and (_skills_root() / name).exists():
            return _err("EXISTS", f"Le skill « {name} » existe déjà")
        if original and original != name:
            if (_skills_root() / name).exists():
                return _err("EXISTS", f"Le skill « {name} » existe déjà")
            src = _skills_root() / original
            if src.is_dir() and la_skills.NAME_RE.match(original):
                await asyncio.to_thread(src.rename, _skills_root() / name)
        info = await asyncio.to_thread(la_skills.save_skill, _skills_root(), name, content)
    except la_skills.SkillError as e:
        return _skill_err(e)
    if original and original != name:
        for coll in (CONVS, PROFILES):
            for d in await module.db.query(coll):
                if original in (d.get("skills") or []):
                    d["skills"] = [name if s == original else s for s in d["skills"]]
                    await module.db.save(coll, d)
    return {"skill": info}


@module.on("skills:delete")
async def skills_delete(payload: dict):
    try:
        ok = await asyncio.to_thread(la_skills.delete_skill, _skills_root(), (payload or {}).get("name") or "")
    except la_skills.SkillError as e:
        return _skill_err(e)
    return {"ok": True} if ok else _err("NOT_FOUND", "Skill introuvable")


@module.on("skills:import")
async def skills_import(payload: dict):
    raw = (payload or {}).get("data") or ""
    try:
        data = base64.b64decode(raw.split(",", 1)[-1], validate=True)
    except (ValueError, TypeError):
        return _err("INVALID_PAYLOAD", "Données invalides")
    try:
        names = await asyncio.to_thread(la_skills.import_zip, _skills_root(), data, bool(payload.get("overwrite")))
    except la_skills.SkillError as e:
        return _skill_err(e)
    return {"imported": names}


@module.on("skills:open_folder")
async def skills_open_folder(payload: dict):
    import os
    import subprocess
    import sys
    root = str(_skills_root())
    try:
        if sys.platform == "win32":
            os.startfile(root)
        elif sys.platform == "darwin":
            subprocess.Popen(["open", root])
        else:
            subprocess.Popen(["xdg-open", root])
    except OSError as e:
        return _err("OPEN_FAILED", str(e))
    return {"ok": True}


@module.on_shutdown
async def cleanup():
    tasks = [e["task"] for e in _active.values() if e.get("task")]
    for t in tasks:
        t.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    await mcp_manager.stop_all()
