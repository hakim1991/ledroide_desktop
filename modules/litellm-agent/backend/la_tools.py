import asyncio
import fnmatch
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

IS_WINDOWS = sys.platform == "win32"
MAX_RESULT_CHARS = 30_000
MAX_READ_LINES = 2000
MAX_LIST_ENTRIES = 500
MAX_SEARCH_HITS = 200
MAX_WRITE_CHARS = 2_000_000
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", ".idea", ".vs", "dist", "build"}


class ToolError(Exception):
    pass


@dataclass
class ToolSpec:
    name: str
    description: str
    parameters: dict
    run: Callable[[dict], Awaitable[str]]
    source: str = "builtin"
    group: str = ""
    default_policy: str = "allow"
    timeout: float | None = None
    meta: dict = field(default_factory=dict)

    def openai(self) -> dict:
        return {"type": "function", "function": {"name": self.name, "description": self.description[:1024],
                                                  "parameters": self.parameters}}


@dataclass
class ToolContext:
    workspace: Path
    on_todo: Callable[[list], Awaitable[None]] | None = None


GROUPS = [
    {"id": "files_read", "label": "Lecture de fichiers", "icon": "📖",
     "desc": "Lister, lire et rechercher dans le dossier de travail", "tools": ["fs_list", "fs_read", "fs_search"]},
    {"id": "files_write", "label": "Écriture de fichiers", "icon": "✏️",
     "desc": "Créer et modifier des fichiers du dossier de travail", "tools": ["fs_write", "fs_edit"]},
    {"id": "shell", "label": "Commandes shell", "icon": "🖥️",
     "desc": "PowerShell (Windows) ou sh, dans le dossier de travail", "tools": ["shell"]},
    {"id": "http", "label": "Requêtes HTTP", "icon": "🌐", "desc": "Appeler une URL (GET, POST…)", "tools": ["http_request"]},
    {"id": "todo", "label": "Plan de travail", "icon": "📋", "desc": "L'agent tient une liste de tâches visible",
     "tools": ["todo_write"]},
]
GROUP_IDS = [g["id"] for g in GROUPS]


def truncate(text: str, limit: int = MAX_RESULT_CHARS) -> str:
    if len(text) <= limit:
        return text
    head = text[: limit * 3 // 4]
    tail = text[-limit // 4:]
    return f"{head}\n\n[… {len(text) - limit} caractères omis …]\n\n{tail}"


def _resolve(ctx: ToolContext, path: str | None) -> Path:
    ws = ctx.workspace.resolve()
    raw = (path or ".").strip() or "."
    p = Path(raw)
    target = (p if p.is_absolute() else ws / p).resolve()
    if target != ws and ws not in target.parents:
        raise ToolError(f"Chemin hors du dossier de travail ({ws}) : {raw}")
    return target


def _rel(ctx: ToolContext, p: Path) -> str:
    try:
        return p.relative_to(ctx.workspace.resolve()).as_posix() or "."
    except ValueError:
        return str(p)


def _is_binary(data: bytes) -> bool:
    return b"\x00" in data[:8192]


# ── Files ─────────────────────────────────────────────────────────────────────

def _fs_list_sync(ctx: ToolContext, path: str, recursive: bool, pattern: str | None) -> str:
    root = _resolve(ctx, path)
    if not root.is_dir():
        raise ToolError(f"Dossier introuvable : {path}")
    lines = []
    total = 0

    def add(p: Path):
        nonlocal total
        total += 1
        if len(lines) >= MAX_LIST_ENTRIES:
            return
        if p.is_dir():
            lines.append(_rel(ctx, p) + "/")
        else:
            try:
                size = p.stat().st_size
            except OSError:
                size = 0
            lines.append(f"{_rel(ctx, p)}  ({size} o)")

    if recursive:
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
            for f in sorted(filenames):
                if not pattern or fnmatch.fnmatch(f, pattern):
                    add(Path(dirpath) / f)
            if total > MAX_LIST_ENTRIES * 4:
                break
    else:
        for p in sorted(root.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower())):
            if not pattern or fnmatch.fnmatch(p.name, pattern):
                add(p)
    if not lines:
        return "(dossier vide)"
    more = f"\n[… {total - len(lines)} entrées non affichées]" if total > len(lines) else ""
    return "\n".join(lines) + more


def _fs_read_sync(ctx: ToolContext, path: str, offset: int, limit: int) -> str:
    p = _resolve(ctx, path)
    if not p.is_file():
        raise ToolError(f"Fichier introuvable : {path}")
    data = p.read_bytes()
    if _is_binary(data):
        return f"[fichier binaire, {len(data)} octets]"
    lines = data.decode("utf-8", errors="replace").splitlines()
    offset = max(1, offset)
    limit = max(1, min(limit, MAX_READ_LINES))
    chunk = lines[offset - 1: offset - 1 + limit]
    width = len(str(offset + len(chunk)))
    body = "\n".join(f"{str(i).rjust(width)}│{l}" for i, l in enumerate(chunk, start=offset))
    info = f"[{_rel(ctx, p)} — lignes {offset}-{offset + len(chunk) - 1} sur {len(lines)}]"
    return truncate(info + "\n" + body)


def _fs_search_sync(ctx: ToolContext, pattern: str, path: str, glob: str | None, ignore_case: bool) -> str:
    try:
        rx = re.compile(pattern, re.IGNORECASE if ignore_case else 0)
    except re.error as e:
        raise ToolError(f"Expression régulière invalide : {e}")
    root = _resolve(ctx, path)
    files = [root] if root.is_file() else []
    if root.is_dir():
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
            files.extend(Path(dirpath) / f for f in filenames if not glob or fnmatch.fnmatch(f, glob))
            if len(files) > 20_000:
                break
    hits = []
    for f in files:
        try:
            if f.stat().st_size > 5_000_000:
                continue
            data = f.read_bytes()
        except OSError:
            continue
        if _is_binary(data):
            continue
        for n, line in enumerate(data.decode("utf-8", errors="replace").splitlines(), 1):
            if rx.search(line):
                hits.append(f"{_rel(ctx, f)}:{n}: {line.strip()[:300]}")
                if len(hits) >= MAX_SEARCH_HITS:
                    return "\n".join(hits) + "\n[… limite de résultats atteinte]"
    return "\n".join(hits) if hits else "Aucun résultat."


def _fs_write_sync(ctx: ToolContext, path: str, content: str) -> str:
    if len(content) > MAX_WRITE_CHARS:
        raise ToolError("Contenu trop volumineux")
    p = _resolve(ctx, path)
    if p.is_dir():
        raise ToolError(f"{path} est un dossier")
    existed = p.exists()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8", newline="")
    return f"{'Fichier remplacé' if existed else 'Fichier créé'} : {_rel(ctx, p)} ({len(content)} caractères)"


def _fs_edit_sync(ctx: ToolContext, path: str, old: str, new: str, replace_all: bool) -> str:
    p = _resolve(ctx, path)
    if not p.is_file():
        raise ToolError(f"Fichier introuvable : {path}")
    text = p.read_text(encoding="utf-8", errors="strict")
    if not old:
        raise ToolError("old_text est vide")
    count = text.count(old)
    if count == 0:
        raise ToolError("old_text introuvable dans le fichier (il doit correspondre exactement, espaces compris)")
    if count > 1 and not replace_all:
        raise ToolError(f"old_text apparaît {count} fois : ajoutez du contexte pour le rendre unique, ou replace_all=true")
    p.write_text(text.replace(old, new) if replace_all else text.replace(old, new, 1), encoding="utf-8", newline="")
    return f"Fichier modifié : {_rel(ctx, p)} ({count if replace_all else 1} remplacement(s))"


# ── Shell ─────────────────────────────────────────────────────────────────────

def _kill_tree(proc: asyncio.subprocess.Process):
    if proc.returncode is not None:
        return
    try:
        if IS_WINDOWS:
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True,
                           creationflags=subprocess.CREATE_NO_WINDOW, timeout=10)
        else:
            proc.kill()
    except Exception:
        pass


async def _shell(ctx: ToolContext, command: str, timeout: int) -> str:
    cwd = ctx.workspace.resolve()
    cwd.mkdir(parents=True, exist_ok=True)
    if IS_WINDOWS:
        script = "[Console]::OutputEncoding=[Text.Encoding]::UTF8; $ProgressPreference='SilentlyContinue'; " + command
        args = ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", script]
        kwargs = {"creationflags": subprocess.CREATE_NO_WINDOW}
    else:
        args = ["/bin/sh", "-c", command]
        kwargs = {"start_new_session": True}
    proc = await asyncio.create_subprocess_exec(*args, cwd=str(cwd), stdin=asyncio.subprocess.DEVNULL,
                                                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
                                                **kwargs)
    timed_out = False
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        timed_out = True
        _kill_tree(proc)
        out = b""
        try:
            rest = await asyncio.wait_for(proc.stdout.read(), 5) if proc.stdout else b""
            out = rest or b""
        except Exception:
            pass
    except asyncio.CancelledError:
        _kill_tree(proc)
        raise
    text = out.decode("utf-8", errors="replace").replace("\r\n", "\n")
    head = f"[délai dépassé après {timeout} s — processus arrêté]" if timed_out else f"[code de sortie : {proc.returncode}]"
    return truncate(f"{head}\n{text}" if text.strip() else f"{head}\n(aucune sortie)")


# ── HTTP ──────────────────────────────────────────────────────────────────────

async def _http(method: str, url: str, headers: dict | None, body: Any, timeout: int) -> str:
    import httpx
    if not re.match(r"^https?://", url or ""):
        raise ToolError("URL invalide (http:// ou https:// attendu)")
    kwargs: dict[str, Any] = {"headers": {str(k): str(v) for k, v in (headers or {}).items()}}
    if body is not None:
        if isinstance(body, (dict, list)):
            kwargs["json"] = body
        else:
            kwargs["content"] = str(body).encode("utf-8")
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
        try:
            r = await client.request(method.upper(), url, **kwargs)
        except httpx.HTTPError as e:
            raise ToolError(f"Échec de la requête : {type(e).__name__}: {e}")
    ctype = r.headers.get("content-type", "")
    if any(t in ctype for t in ("json", "text", "xml", "javascript", "html", "csv", "yaml")) or not r.content:
        content = r.text
    else:
        content = f"[contenu binaire {ctype}, {len(r.content)} octets]"
    hdrs = "\n".join(f"{k}: {v}" for k, v in r.headers.items()
                     if k.lower() in ("content-type", "content-length", "location", "retry-after", "date"))
    return truncate(f"HTTP {r.status_code} {r.reason_phrase}\n{hdrs}\n\n{content}")


# ── Registry ──────────────────────────────────────────────────────────────────

def _obj(props: dict, required: list[str]) -> dict:
    return {"type": "object", "properties": props, "required": required}


def builtin_tools(ctx: ToolContext, groups: list[str]) -> list[ToolSpec]:
    th = asyncio.to_thread
    os_name = "Windows (PowerShell)" if IS_WINDOWS else "Unix (sh)"
    specs: list[ToolSpec] = []
    g = set(groups)

    if "files_read" in g:
        specs += [
            ToolSpec("fs_list", "Liste le contenu d'un dossier du dossier de travail (chemins relatifs).",
                     _obj({"path": {"type": "string", "description": "Dossier, relatif au dossier de travail (défaut « . »)"},
                           "recursive": {"type": "boolean", "description": "Parcourir les sous-dossiers"},
                           "pattern": {"type": "string", "description": "Filtre glob sur le nom (ex. *.py)"}}, []),
                     lambda a: th(_fs_list_sync, ctx, a.get("path") or ".", bool(a.get("recursive")), a.get("pattern")),
                     group="files_read"),
            ToolSpec("fs_read", f"Lit un fichier texte (numéros de ligne inclus, {MAX_READ_LINES} lignes max par appel).",
                     _obj({"path": {"type": "string"},
                           "offset": {"type": "integer", "description": "Première ligne (à partir de 1)"},
                           "limit": {"type": "integer", "description": "Nombre de lignes"}}, ["path"]),
                     lambda a: th(_fs_read_sync, ctx, a["path"], int(a.get("offset") or 1),
                                  int(a.get("limit") or MAX_READ_LINES)),
                     group="files_read"),
            ToolSpec("fs_search", "Recherche une expression régulière dans les fichiers texte (style grep).",
                     _obj({"pattern": {"type": "string", "description": "Expression régulière Python"},
                           "path": {"type": "string", "description": "Dossier ou fichier (défaut « . »)"},
                           "glob": {"type": "string", "description": "Filtre sur le nom de fichier (ex. *.ts)"},
                           "ignore_case": {"type": "boolean"}}, ["pattern"]),
                     lambda a: th(_fs_search_sync, ctx, a["pattern"], a.get("path") or ".", a.get("glob"),
                                  bool(a.get("ignore_case"))),
                     group="files_read"),
        ]
    if "files_write" in g:
        specs += [
            ToolSpec("fs_write", "Crée ou remplace entièrement un fichier texte (UTF-8) du dossier de travail.",
                     _obj({"path": {"type": "string"}, "content": {"type": "string"}}, ["path", "content"]),
                     lambda a: th(_fs_write_sync, ctx, a["path"], a["content"]),
                     group="files_write", default_policy="ask"),
            ToolSpec("fs_edit", "Remplace un passage exact d'un fichier existant (old_text doit être unique sauf replace_all).",
                     _obj({"path": {"type": "string"}, "old_text": {"type": "string"}, "new_text": {"type": "string"},
                           "replace_all": {"type": "boolean"}}, ["path", "old_text", "new_text"]),
                     lambda a: th(_fs_edit_sync, ctx, a["path"], a["old_text"], a["new_text"], bool(a.get("replace_all"))),
                     group="files_write", default_policy="ask"),
        ]
    if "shell" in g:
        specs.append(ToolSpec(
            "shell", f"Exécute une commande shell dans le dossier de travail. Système : {os_name}. "
                     "Sortie standard et erreurs combinées, tronquées si trop longues. Pas d'entrée interactive.",
            _obj({"command": {"type": "string"},
                  "timeout": {"type": "integer", "description": "Délai en secondes (défaut 60, max 600)"}}, ["command"]),
            lambda a: _shell(ctx, a["command"], max(1, min(int(a.get("timeout") or 60), 600))),
            group="shell", default_policy="ask", timeout=610))
    if "http" in g:
        specs.append(ToolSpec(
            "http_request", "Effectue une requête HTTP et renvoie le statut, quelques en-têtes et le corps (texte).",
            _obj({"method": {"type": "string", "enum": ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD"]},
                  "url": {"type": "string"},
                  "headers": {"type": "object", "description": "En-têtes HTTP (nom → valeur)"},
                  "body": {"type": "string", "description": "Corps de la requête (texte ou JSON sérialisé)"}}, ["method", "url"]),
            lambda a: _http(a.get("method") or "GET", a["url"], a.get("headers"), a.get("body"), 60),
            group="http", default_policy="ask"))
    if "todo" in g:
        async def todo(a: dict) -> str:
            items = []
            for it in (a.get("items") or [])[:50]:
                if isinstance(it, dict) and str(it.get("text", "")).strip():
                    st = it.get("status") if it.get("status") in ("pending", "in_progress", "done") else "pending"
                    items.append({"text": str(it["text"])[:300], "status": st})
            if ctx.on_todo:
                await ctx.on_todo(items)
            done = sum(1 for i in items if i["status"] == "done")
            return f"Plan mis à jour : {done}/{len(items)} tâche(s) terminée(s)."
        specs.append(ToolSpec(
            "todo_write", "Remplace la liste des tâches du plan de travail (visible par l'utilisateur). "
                          "À utiliser pour les tâches en plusieurs étapes ; mets-la à jour au fil de l'avancement.",
            _obj({"items": {"type": "array", "items": _obj({
                "text": {"type": "string"},
                "status": {"type": "string", "enum": ["pending", "in_progress", "done"]}}, ["text", "status"])}},
                ["items"]),
            todo, group="todo"))
    return specs


def parse_args(raw: str | None) -> dict:
    if not raw or not raw.strip():
        return {}
    try:
        val = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ToolError(f"Arguments JSON invalides : {e.msg} (position {e.pos})")
    if not isinstance(val, dict):
        raise ToolError("Les arguments doivent être un objet JSON")
    return val
