import hmac
import json
import logging
import re
from urllib.parse import parse_qs

from core import settings

log = logging.getLogger("ledroide.security")

# Served to module iframes (same origin as the backend) and to <img>/<iframe> loads, which carry no token.
PUBLIC_PATH = re.compile(
    r"^/(ledroide-bridge\.js|system/health"
    r"|modules/[a-z0-9][a-z0-9_-]*/(api|frontend)(/.*)?"
    r"|modules/[a-z0-9][a-z0-9_-]*/icon)$"
)


def _headers(scope) -> dict[str, str]:
    return {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}


def _token_ok(candidate: str | None) -> bool:
    if not settings.TOKEN:
        return True
    return bool(candidate) and hmac.compare_digest(candidate, settings.TOKEN)


class SecurityMiddleware:
    """Blocks DNS rebinding (Host), cross-site calls (Origin) and requires the
    Electron-issued token on management endpoints and the WebSocket."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket"):
            return await self.app(scope, receive, send)

        headers = _headers(scope)
        path = scope.get("path", "")
        reason = self._check(scope, headers, path)
        if reason is None:
            return await self.app(scope, receive, send)

        log.warning("rejected %s %s: %s", scope["type"], path, reason)
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 4403})
            return
        body = json.dumps({"error": reason, "code": "FORBIDDEN"}).encode()
        await send({"type": "http.response.start", "status": 403,
                    "headers": [(b"content-type", b"application/json")]})
        await send({"type": "http.response.body", "body": body})

    def _check(self, scope, headers, path) -> str | None:
        if headers.get("host") not in settings.ALLOWED_HOSTS:
            return "Host non autorisé"
        if scope["type"] == "http" and scope.get("method") == "OPTIONS":
            return None

        origin = headers.get("origin")
        token = headers.get("x-ledroide-token")
        if scope["type"] == "websocket":
            token = parse_qs(scope.get("query_string", b"").decode()).get("token", [None])[0]

        if PUBLIC_PATH.match(path) and scope["type"] == "http":
            if origin is None or origin in settings.SELF_ORIGINS or _token_ok(token):
                return None
            return "Origine non autorisée"

        if not _token_ok(token):
            return "Jeton invalide"
        if origin and origin not in settings.SELF_ORIGINS and origin not in settings.RENDERER_ORIGINS:
            return "Origine non autorisée"
        return None
