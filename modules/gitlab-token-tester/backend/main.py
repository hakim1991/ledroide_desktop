import asyncio
import base64
import hashlib
import json
import logging
import re
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any

from ledroide_sdk import Module

log = logging.getLogger("ledroide.gitlab-token-tester")

module = Module(__file__)
router = module.router

MAX_BODY = 512 * 1024
MAX_BATCH = 200
BATCH_CONCURRENCY = 4
USER_AGENT = "LedroideDesktop-GitLabTokenTester/1.0"

TOKEN_TYPES: dict[str, dict] = {
    "pat": {"label": "Jeton d'accès (personnel, projet ou groupe)", "testable": True},
    "oauth": {"label": "Jeton d'accès OAuth 2", "testable": True},
    "job": {"label": "Jeton de job CI/CD", "testable": True},
    "runner": {"label": "Jeton d'authentification de runner", "testable": True},
    "deploy": {"label": "Jeton de déploiement", "testable": True},
    "feed": {"label": "Jeton de flux (RSS/Atom)", "testable": True},
    "trigger": {"label": "Jeton de déclenchement de pipeline", "testable": False,
                "why": "Le seul moyen de le vérifier est de déclencher un pipeline : test non effectué pour éviter tout effet de bord."},
    "runner_registration": {"label": "Jeton d'enregistrement de runner (obsolète)", "testable": False,
                            "why": "Le vérifier reviendrait à enregistrer un runner : test non effectué."},
    "oauth_secret": {"label": "Secret d'application OAuth", "testable": False,
                     "why": "Un secret d'application ne donne pas accès à l'API ; il sert à échanger un code contre un jeton OAuth."},
    "incoming_mail": {"label": "Jeton d'e-mail entrant", "testable": False,
                      "why": "Utilisé uniquement dans les adresses de création de tickets par e-mail."},
    "agent": {"label": "Jeton d'agent Kubernetes (KAS)", "testable": False,
              "why": "S'utilise uniquement contre le serveur KAS, pas contre l'API REST."},
    "scim": {"label": "Jeton OAuth SCIM", "testable": False,
             "why": "Réservé à l'API SCIM de provisionnement : test non effectué."},
    "feature_flags": {"label": "Jeton client des feature flags", "testable": False,
                      "why": "S'utilise avec l'API Unleash d'un projet précis : test non effectué."},
}

PREFIXES = [
    ("glpat-", "pat"), ("gloas-", "oauth_secret"), ("gldt-", "deploy"), ("glrtr-", "runner_registration"),
    ("glrt-", "runner"), ("GR1348941", "runner_registration"), ("glcbt-", "job"), ("glptt-", "trigger"),
    ("glft-", "feed"), ("glimt-", "incoming_mail"), ("glagent-", "agent"), ("glsoat-", "scim"),
    ("glffct-", "feature_flags"),
]

SCOPES = {
    "api": "Accès complet en lecture/écriture à l'API, y compris registres et dépôts",
    "read_api": "Lecture seule de l'API",
    "read_user": "Lecture du profil de l'utilisateur",
    "create_runner": "Création de runners",
    "manage_runner": "Gestion des runners",
    "k8s_proxy": "Appels à l'API Kubernetes via l'agent",
    "read_repository": "Lecture (clone/pull) des dépôts git",
    "write_repository": "Écriture (push) dans les dépôts git",
    "read_registry": "Lecture (pull) des images du registre de conteneurs",
    "write_registry": "Écriture (push) d'images dans le registre de conteneurs",
    "read_virtual_registry": "Lecture des registres virtuels",
    "write_virtual_registry": "Écriture dans les registres virtuels",
    "read_package_registry": "Lecture du registre de paquets",
    "write_package_registry": "Écriture dans le registre de paquets",
    "sudo": "Agir en tant que n'importe quel utilisateur (administrateur)",
    "admin_mode": "Opérations d'administration en mode admin",
    "read_service_ping": "Lecture du Service Ping (administrateur)",
    "ai_features": "Accès aux fonctionnalités IA (GitLab Duo)",
    "self_rotate": "Rotation du jeton par lui-même",
    "read_observability": "Lecture des données d'observabilité",
    "write_observability": "Écriture des données d'observabilité",
    "openid": "Authentification OpenID Connect",
    "profile": "Lecture du profil (OpenID)",
    "email": "Lecture de l'e-mail principal (OpenID)",
}
BROAD_SCOPES = {"api", "sudo", "admin_mode", "write_repository", "write_registry"}

ACCESS_LEVELS = {0: "Aucun", 5: "Accès minimal", 10: "Guest", 15: "Planner", 20: "Reporter",
                 30: "Developer", 40: "Maintainer", 50: "Owner", 60: "Admin"}

STATUS_LABELS = {
    "valid": "Valide", "limited": "Valide (portées limitées)", "expired": "Expiré", "revoked": "Révoqué",
    "inactive": "Inactif", "invalid": "Invalide", "error": "Erreur", "untested": "Non vérifiable",
    "identified": "Identifié (non testé)",
}


class InputError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


# ── Helpers ──────────────────────────────────────────────────────────────────

def clean_token(raw: Any) -> str:
    token = str(raw or "").strip().strip("'\"")
    for prefix in ("bearer ", "private-token:", "job-token:"):
        if token.lower().startswith(prefix):
            token = token[len(prefix):].strip()
    return "".join(token.split())


def detect_type(token: str) -> str:
    for prefix, kind in PREFIXES:
        if token.startswith(prefix):
            return kind
    if re.fullmatch(r"[0-9a-f]{64}", token):
        return "oauth"
    return "pat"


def mask(token: str) -> str:
    if len(token) <= 10:
        return "•" * len(token)
    prefix = next((p for p, _ in PREFIXES if token.startswith(p)), token[:3])
    return f"{prefix}…{token[-4:]}"


def fingerprint(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()[:16]


def normalize_url(raw: Any) -> str:
    url = str(raw or "").strip()
    if not url:
        raise InputError("URL_REQUIRED", "L'URL de l'instance GitLab est obligatoire.")
    if "://" not in url:
        url = "https://" + url
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise InputError("BAD_URL", f"URL invalide : {raw}")
    path = parts.path.rstrip("/")
    path = re.sub(r"/api/v4$", "", path)
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, path, "", ""))


def normalize_project(raw: Any) -> str:
    project = str(raw or "").strip().strip("/")
    if not project:
        return ""
    if "://" in project:
        project = urllib.parse.urlsplit(project).path.strip("/")
    project = project.split("/-/")[0]
    if project.endswith(".git"):
        project = project[:-4]
    return project


def days_until(value: str | None) -> int | None:
    if not value:
        return None
    try:
        return (date.fromisoformat(str(value)[:10]) - date.today()).days
    except ValueError:
        return None


def access_label(level: Any) -> str:
    try:
        return ACCESS_LEVELS.get(int(level), str(level))
    except (TypeError, ValueError):
        return "—"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _http(method: str, url: str, headers: dict, data: bytes | None, verify: bool, timeout: float) -> dict:
    ctx = ssl.create_default_context()
    if not verify:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    opener = urllib.request.build_opener(urllib.request.HTTPSHandler(context=ctx), _NoRedirect())
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"User-Agent": USER_AGENT, "Accept": "application/json", **headers})
    started = time.perf_counter()
    try:
        try:
            resp = opener.open(req, timeout=timeout)
        except urllib.error.HTTPError as exc:
            resp = exc
        with resp:
            raw = resp.read(MAX_BODY)
            status = resp.status
            resp_headers = {k.lower(): v for k, v in resp.headers.items()}
    except urllib.error.URLError as exc:
        reason = exc.reason
        if isinstance(reason, ssl.SSLCertVerificationError):
            return {"error": f"Certificat SSL invalide : {reason.verify_message}", "code": "SSL_ERROR"}
        if isinstance(reason, TimeoutError):
            return {"error": f"Délai dépassé ({timeout:g} s)", "code": "TIMEOUT"}
        return {"error": f"Connexion impossible : {reason}", "code": "NETWORK_ERROR"}
    except TimeoutError:
        return {"error": f"Délai dépassé ({timeout:g} s)", "code": "TIMEOUT"}
    except (ConnectionError, OSError) as exc:
        return {"error": f"Erreur réseau : {exc}", "code": "NETWORK_ERROR"}
    except ValueError as exc:
        return {"error": f"Requête invalide : {exc}", "code": "BAD_REQUEST"}

    text = raw.decode("utf-8", "replace")
    body = None
    if "json" in resp_headers.get("content-type", ""):
        try:
            body = json.loads(text)
        except ValueError:
            body = None
    return {"status": status, "ms": round((time.perf_counter() - started) * 1000), "headers": resp_headers,
            "json": body, "text": text}


def _error_text(resp: dict) -> str:
    body = resp.get("json")
    if isinstance(body, dict):
        for key in ("error_description", "message", "error"):
            value = body.get(key)
            if value:
                return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    return (resp.get("text") or "").strip()[:200]


def _classify_401(resp: dict) -> tuple[str, str]:
    text = _error_text(resp)
    low = text.lower()
    if "expired" in low:
        return "expired", "Le jeton a expiré."
    if "revoked" in low:
        return "revoked", "Le jeton a été révoqué."
    return "invalid", f"Jeton refusé par GitLab ({text or '401 Unauthorized'})."


# ── Test context ─────────────────────────────────────────────────────────────

@dataclass
class Ctx:
    base: str
    token: str
    verify: bool
    timeout: float
    project: str = ""
    username: str = ""
    steps: list = field(default_factory=list)
    warnings: list = field(default_factory=list)

    async def request(self, label: str, method: str, path: str, headers: dict | None = None,
                      data: bytes | None = None, shown: str | None = None) -> dict:
        url = path if path.startswith("http") else f"{self.base}/api/v4{path}"
        resp = await asyncio.to_thread(_http, method, url, headers or {}, data, self.verify, self.timeout)
        step = {"label": label, "method": method, "url": shown or url}
        if "error" in resp:
            step.update(ok=False, status=None, ms=None, note=resp["error"])
        else:
            status = resp["status"]
            note = ""
            if status in (401, 403, 404) or status >= 500:
                note = _error_text(resp)
            elif 300 <= status < 400:
                note = f"Redirection vers {resp['headers'].get('location', '?')}"
            step.update(ok=200 <= status < 300, status=status, ms=resp["ms"], note=note)
        self.steps.append(step)
        return resp

    def basic(self, user: str) -> dict:
        raw = f"{user}:{self.token}".encode()
        return {"Authorization": "Basic " + base64.b64encode(raw).decode()}


def _api_headers(kind: str, token: str) -> dict:
    if kind == "oauth":
        return {"Authorization": f"Bearer {token}"}
    if kind == "job":
        return {"JOB-TOKEN": token}
    return {"PRIVATE-TOKEN": token}


def _ok(resp: dict) -> bool:
    return resp.get("status") is not None and 200 <= resp["status"] < 300


def _json(resp: dict) -> dict:
    body = resp.get("json") if _ok(resp) else None
    return body if isinstance(body, dict) else {}


def _total(resp: dict) -> int | None:
    value = resp.get("headers", {}).get("x-total") if _ok(resp) else None
    if value is None and _ok(resp) and isinstance(resp.get("json"), list):
        return None if resp["json"] else 0
    try:
        return int(value) if value is not None else None
    except ValueError:
        return None


def _scopes(names: Any) -> list[dict]:
    if isinstance(names, str):
        names = names.split()
    return [{"scope": s, "desc": SCOPES.get(s, ""), "broad": s in BROAD_SCOPES} for s in (names or [])]


def _user_info(body: dict) -> dict | None:
    if not body:
        return None
    username = body.get("username") or ""
    kind = "Utilisateur"
    if body.get("bot"):
        kind = "Bot de projet" if username.startswith("project_") else "Bot de groupe" if username.startswith("group_") else "Bot"
    return {
        "id": body.get("id"), "username": username, "name": body.get("name"), "email": body.get("email"),
        "state": body.get("state"), "is_admin": body.get("is_admin"), "bot": bool(body.get("bot")), "kind": kind,
        "web_url": body.get("web_url"), "created_at": body.get("created_at"),
        "last_activity_on": body.get("last_activity_on"), "two_factor_enabled": body.get("two_factor_enabled"),
        "can_create_project": body.get("can_create_project"), "can_create_group": body.get("can_create_group"),
    }


async def _instance(ctx: Ctx, headers: dict) -> dict | None:
    resp = await ctx.request("Instance (métadonnées)", "GET", "/metadata", headers)
    if resp.get("status") == 404:
        resp = await ctx.request("Instance (version)", "GET", "/version", headers)
    body = _json(resp)
    if not body:
        return None
    return {"version": body.get("version"), "revision": body.get("revision"),
            "enterprise": body.get("enterprise"), "kas": (body.get("kas") or {}).get("enabled")}


async def _project_checks(ctx: Ctx, api_headers: dict | None, git_user: str) -> dict:
    project = {"path": ctx.project}
    enc = urllib.parse.quote(ctx.project, safe="")
    path = ctx.project
    if api_headers is not None:
        resp = await ctx.request("Projet", "GET", f"/projects/{enc}", api_headers)
        body = _json(resp)
        project["api_status"] = resp.get("status")
        if body:
            perms = body.get("permissions") or {}
            levels = [(perms.get(k) or {}).get("access_level") or 0 for k in ("project_access", "group_access")]
            level = max(levels) if any(levels) else None
            path = body.get("path_with_namespace") or path
            project.update(
                name=body.get("name_with_namespace"), path=path, web_url=body.get("web_url"),
                visibility=body.get("visibility"), archived=body.get("archived"),
                default_branch=body.get("default_branch"),
                access_level=level, access_label=access_label(level) if level else "Aucun accès direct (projet public ou admin)",
            )
            branches = await ctx.request("Branches du projet", "GET", f"/projects/{enc}/repository/branches?per_page=1",
                                         api_headers)
            project["branches_ok"] = _ok(branches)
    if not re.fullmatch(r"\d+", path):
        auth = ctx.basic(git_user)
        git_base = f"{ctx.base}/{urllib.parse.quote(path)}.git/info/refs"
        pull = await ctx.request("Git : lecture (clone/pull)", "GET", f"{git_base}?service=git-upload-pack", auth)
        push = await ctx.request("Git : écriture (push, sans rien envoyer)", "GET",
                                 f"{git_base}?service=git-receive-pack", auth)
        project["git_read"] = _ok(pull)
        project["git_write"] = _ok(push)
        project["git_read_status"] = pull.get("status")
        project["git_write_status"] = push.get("status")
    else:
        project["git_note"] = "Indiquez le chemin du projet (groupe/projet) plutôt que son ID pour tester l'accès git."
    return project


# ── Testers by token type ────────────────────────────────────────────────────

async def _test_api_token(ctx: Ctx, kind: str, result: dict):
    headers = _api_headers(kind, ctx.token)
    if kind == "pat":
        meta_task = ctx.request("Métadonnées du jeton", "GET", "/personal_access_tokens/self", headers)
    else:
        meta_task = ctx.request("Informations du jeton OAuth", "GET", f"{ctx.base}/oauth/token/info", headers)
    meta, user, instance = await asyncio.gather(meta_task, ctx.request("Utilisateur", "GET", "/user", headers),
                                                _instance(ctx, headers))
    responses = [meta, user]
    if all("error" in r for r in responses):
        result.update(status="error", message=meta["error"])
        return

    info = _json(meta)
    if info:
        if kind == "pat":
            result["token"] = {
                "id": info.get("id"), "name": info.get("name"), "description": info.get("description"),
                "scopes": _scopes(info.get("scopes")), "created_at": info.get("created_at"),
                "expires_at": info.get("expires_at"), "days_left": days_until(info.get("expires_at")),
                "last_used_at": info.get("last_used_at"), "last_used_ips": info.get("last_used_ips"),
                "active": info.get("active"), "revoked": info.get("revoked"), "user_id": info.get("user_id"),
            }
        else:
            expires_in = info.get("expires_in") or info.get("expires_in_seconds")
            created = info.get("created_at")
            result["token"] = {
                "scopes": _scopes(info.get("scope") or info.get("scopes")),
                "application": (info.get("application") or {}).get("uid"),
                "created_at": datetime.fromtimestamp(created, timezone.utc).isoformat() if isinstance(created, (int, float)) else created,
                "expires_in_s": expires_in,
                "expires_at": datetime.fromtimestamp(time.time() + expires_in, timezone.utc).isoformat() if isinstance(expires_in, (int, float)) else None,
                "active": True,
            }
    result["user"] = _user_info(_json(user))
    result["instance"] = instance
    if _ok(user):
        rate = {k: user["headers"].get(f"ratelimit-{k}") for k in ("limit", "remaining", "reset")}
        if rate["limit"]:
            result["rate_limit"] = rate

    if _ok(meta) or _ok(user):
        token = result.get("token") or {}
        if token.get("revoked"):
            result.update(status="revoked", message="Le jeton a été révoqué.")
        elif token.get("active") is False:
            expired = (token.get("days_left") or 0) < 0
            result.update(status="expired" if expired else "inactive",
                          message="Le jeton a expiré." if expired else "Le jeton n'est plus actif.")
        else:
            result.update(status="valid", message="Le jeton est valide.")
    elif any(r.get("status") == 403 for r in responses):
        result.update(status="limited",
                      message="Le jeton est reconnu mais ses portées ne permettent pas de lire l'API (ex. read_repository ou registre uniquement).")
    elif any(r.get("status") == 401 for r in responses):
        status, message = _classify_401(next(r for r in responses if r.get("status") == 401))
        result.update(status=status, message=message)
        return
    else:
        first = next((r for r in responses if "status" in r), meta)
        result.update(status="error", message=f"Réponse inattendue de GitLab (HTTP {first.get('status')}). Vérifiez l'URL de l'instance.")
        return

    if result["status"] in ("valid", "limited"):
        if result["status"] == "valid":
            projects, groups = await asyncio.gather(
                ctx.request("Projets accessibles (membre)", "GET", "/projects?membership=true&simple=true&per_page=1", headers),
                ctx.request("Groupes accessibles", "GET", "/groups?min_access_level=10&per_page=1", headers),
            )
            result["counts"] = {"projects": _total(projects), "groups": _total(groups),
                                "projects_ok": _ok(projects), "groups_ok": _ok(groups)}
        if ctx.project:
            user_name = "oauth2" if kind == "oauth" else ((result.get("user") or {}).get("username") or "oauth2")
            result["project"] = await _project_checks(ctx, headers, user_name)
            if result["status"] == "limited" and result["project"].get("git_read"):
                result["message"] += " Accès git en lecture confirmé sur le projet."


async def _test_job(ctx: Ctx, result: dict):
    headers = {"JOB-TOKEN": ctx.token}
    resp = await ctx.request("Job associé", "GET", "/job", headers)
    if "error" in resp:
        result.update(status="error", message=resp["error"])
        return
    if resp["status"] == 401:
        result.update(status="invalid", message="Jeton refusé : un jeton de job n'est valable que pendant l'exécution du job.")
        return
    body = _json(resp)
    if not body:
        result.update(status="error", message=f"Réponse inattendue (HTTP {resp['status']}).")
        return
    pipeline = body.get("pipeline") or {}
    result["job"] = {"id": body.get("id"), "name": body.get("name"), "stage": body.get("stage"),
                     "status": body.get("status"), "ref": body.get("ref"), "web_url": body.get("web_url"),
                     "pipeline_id": pipeline.get("id"), "project_id": pipeline.get("project_id"),
                     "started_at": body.get("started_at")}
    result["user"] = _user_info(body.get("user") or {})
    result.update(status="valid", message="Le jeton de job est valide (job en cours d'exécution).")
    if ctx.project:
        result["project"] = await _project_checks(ctx, None, "gitlab-ci-token")


async def _test_runner(ctx: Ctx, result: dict):
    data = urllib.parse.urlencode({"token": ctx.token}).encode()
    resp = await ctx.request("Vérification du runner", "POST", "/runners/verify",
                             {"Content-Type": "application/x-www-form-urlencoded"}, data)
    if "error" in resp:
        result.update(status="error", message=resp["error"])
        return
    if resp["status"] in (200, 201):
        body = _json(resp)
        result["runner"] = {"id": body.get("id"), "token_expires_at": body.get("token_expires_at")}
        result["token"] = {"expires_at": body.get("token_expires_at"), "days_left": days_until(body.get("token_expires_at"))}
        result.update(status="valid", message="Le jeton de runner est valide.")
    elif resp["status"] in (401, 403):
        result.update(status="invalid", message="Jeton de runner inconnu ou expiré.")
    else:
        result.update(status="error", message=f"Réponse inattendue (HTTP {resp['status']}).")


async def _test_deploy(ctx: Ctx, result: dict):
    if not ctx.project:
        result.update(status="untested",
                      message="Un jeton de déploiement ne donne pas accès à l'API : indiquez un projet (et le nom d'utilisateur du jeton) pour tester l'accès git.")
        return
    if not ctx.username:
        ctx.warnings.append("Sans nom d'utilisateur, GitLab peut refuser le jeton : renseignez celui affiché à sa création (ex. gitlab+deploy-token-42).")
    result["project"] = await _project_checks(ctx, None, ctx.username or "gitlab+deploy-token")
    project = result["project"]
    if project.get("git_read") or project.get("git_write"):
        result.update(status="valid", message="Le jeton de déploiement donne accès au dépôt.")
    elif project.get("git_read_status") is None:
        result.update(status="error", message=next((s["note"] for s in ctx.steps if s.get("note")), "Erreur réseau."))
    else:
        result.update(status="invalid",
                      message="Accès git refusé : jeton invalide, expiré, nom d'utilisateur incorrect ou projet différent.")


async def _test_feed(ctx: Ctx, result: dict):
    query = urllib.parse.urlencode({"feed_token": ctx.token})
    url = f"{ctx.base}/dashboard/projects.atom?{query}"
    shown = f"{ctx.base}/dashboard/projects.atom?feed_token={mask(ctx.token)}"
    resp = await ctx.request("Flux Atom du tableau de bord", "GET", url, {"Accept": "application/atom+xml"}, shown=shown)
    if "error" in resp:
        result.update(status="error", message=resp["error"])
    elif resp["status"] == 200 and "<feed" in resp.get("text", ""):
        match = re.search(r"<author>\s*<name>([^<]*)</name>", resp["text"])
        if match:
            result["user"] = {"name": match.group(1), "kind": "Utilisateur"}
        result.update(status="valid", message="Le jeton de flux est valide.")
    else:
        result.update(status="invalid", message="Jeton de flux refusé (redirection vers la connexion).")


# ── Orchestration ────────────────────────────────────────────────────────────

def _warnings(ctx: Ctx, result: dict):
    if ctx.base.startswith("http://"):
        ctx.warnings.append("Connexion non chiffrée (http) : le jeton a circulé en clair.")
    if not ctx.verify:
        ctx.warnings.append("Vérification du certificat SSL désactivée.")
    if result["status"] not in ("valid", "limited"):
        return
    token = result.get("token") or {}
    days = token.get("days_left")
    if "days_left" in token and token.get("expires_at") is None and result["type"] == "pat":
        ctx.warnings.append("Ce jeton n'a pas de date d'expiration.")
    elif days is not None and 0 <= days <= 30:
        ctx.warnings.append(f"Le jeton expire dans {days} jour{'s' if days > 1 else ''}.")
    broad = [s["scope"] for s in token.get("scopes", []) if s["broad"]]
    if broad:
        ctx.warnings.append("Portées étendues : " + ", ".join(broad) + ". Préférez des portées minimales (read_api, read_repository…).")
    user = result.get("user") or {}
    if user.get("is_admin"):
        ctx.warnings.append("Le jeton appartient à un administrateur de l'instance.")
    if user.get("state") and user["state"] != "active":
        ctx.warnings.append(f"Le compte associé est dans l'état « {user['state']} ».")


async def run_test(payload: dict) -> dict:
    token = clean_token(payload.get("token"))
    if not token:
        raise InputError("TOKEN_REQUIRED", "Aucun jeton fourni.")
    base = normalize_url(payload.get("url") or module.config.get("default_url"))
    detected = detect_type(token)
    kind = payload.get("type") or "auto"
    kind = detected if kind == "auto" else kind
    if kind not in TOKEN_TYPES:
        raise InputError("BAD_TYPE", f"Type de jeton inconnu : {kind}")

    ctx = Ctx(base=base, token=token, verify=bool(payload.get("verify_ssl", True)),
              timeout=float(module.config.get("timeout_s", 15)),
              project=normalize_project(payload.get("project")), username=str(payload.get("username") or "").strip())
    result: dict = {"type": kind, "type_label": TOKEN_TYPES[kind]["label"], "detected_type": detected,
                    "masked": mask(token), "fingerprint": fingerprint(token), "base_url": base,
                    "project_input": ctx.project, "tested_at": now_iso()}
    started = time.perf_counter()

    if not TOKEN_TYPES[kind]["testable"]:
        result.update(status="identified", message=TOKEN_TYPES[kind]["why"])
    elif kind in ("pat", "oauth"):
        await _test_api_token(ctx, kind, result)
    elif kind == "job":
        await _test_job(ctx, result)
    elif kind == "runner":
        await _test_runner(ctx, result)
    elif kind == "deploy":
        await _test_deploy(ctx, result)
    elif kind == "feed":
        await _test_feed(ctx, result)

    _warnings(ctx, result)
    result["status_label"] = STATUS_LABELS.get(result["status"], result["status"])
    result["steps"] = ctx.steps
    result["warnings"] = ctx.warnings
    result["elapsed_ms"] = round((time.perf_counter() - started) * 1000)
    log.info("test %s %s → %s", kind, base, result["status"])
    return result


async def _save_history(result: dict, source: str):
    token = result.get("token") or {}
    user = result.get("user") or {}
    await module.db.save("history", {
        "tested_at": result["tested_at"], "source": source, "base_url": result["base_url"],
        "masked": result["masked"], "fingerprint": result["fingerprint"], "type": result["type"],
        "type_label": result["type_label"], "status": result["status"], "status_label": result["status_label"],
        "message": result.get("message"), "name": token.get("name"), "username": user.get("username"),
        "scopes": [s["scope"] for s in token.get("scopes", [])], "expires_at": token.get("expires_at"),
        "project": result.get("project_input") or None,
    })
    keep = int(module.config.get("history_size", 100))
    for old in (await _history())[keep:]:
        await module.db.delete("history", old["_id"])


# ── Handlers ─────────────────────────────────────────────────────────────────

@module.on("init")
async def init(payload: dict):
    urls: list[str] = []
    for item in await _history():
        if item.get("base_url") and item["base_url"] not in urls:
            urls.append(item["base_url"])
    return {
        "default_url": module.config.get("default_url"),
        "types": [{"id": k, "label": v["label"], "testable": v["testable"]} for k, v in TOKEN_TYPES.items()],
        "urls": urls[:20],
    }


@module.on("detect")
async def detect(payload: dict):
    token = clean_token(payload.get("token"))
    if not token:
        return {"type": None}
    kind = detect_type(token)
    return {"type": kind, "label": TOKEN_TYPES[kind]["label"], "masked": mask(token)}


@module.on("test")
async def test(payload: dict):
    try:
        result = await run_test(payload)
    except InputError as exc:
        return {"error": exc.message, "code": exc.code}
    await _save_history(result, "unitaire")
    return result


@module.job("batch")
async def batch(payload: dict, job):
    seen, tokens = set(), []
    for line in str(payload.get("tokens") or "").splitlines():
        token = clean_token(line)
        if token and not token.startswith("#") and token not in seen:
            seen.add(token)
            tokens.append(token)
    if not tokens:
        raise ValueError("Aucun jeton à tester (un jeton par ligne).")
    if len(tokens) > MAX_BATCH:
        raise ValueError(f"Trop de jetons : {len(tokens)} (maximum {MAX_BATCH}).")
    normalize_url(payload.get("url") or module.config.get("default_url"))

    results: list[dict | None] = [None] * len(tokens)
    done = 0
    sem = asyncio.Semaphore(BATCH_CONCURRENCY)

    async def one(i: int, token: str):
        nonlocal done
        async with sem:
            try:
                res = await run_test({**payload, "token": token})
                await _save_history(res, "lot")
            except InputError as exc:
                res = {"masked": mask(token), "fingerprint": fingerprint(token), "status": "error",
                       "status_label": STATUS_LABELS["error"], "message": exc.message}
            results[i] = res
            done += 1
            await job.progress(round(done * 100 / len(tokens)), f"{done}/{len(tokens)} jetons testés")

    await job.progress(0, f"0/{len(tokens)} jetons testés")
    await asyncio.gather(*(one(i, t) for i, t in enumerate(tokens)))
    summary: dict[str, int] = {}
    for res in results:
        summary[res["status"]] = summary.get(res["status"], 0) + 1
    return {"results": results, "summary": summary}


async def _history() -> list[dict]:
    items = await module.db.query("history")
    items.sort(key=lambda d: d.get("tested_at") or "", reverse=True)
    return items


@module.on("history_list")
async def history_list(payload: dict):
    return {"items": await _history()}


@module.on("history_delete")
async def history_delete(payload: dict):
    return await module.db.delete("history", str(payload.get("id", "")))


@module.on("history_clear")
async def history_clear(payload: dict):
    for item in await module.db.query("history"):
        await module.db.delete("history", item["_id"])
    return {"ok": True}
