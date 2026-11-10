import asyncio
import base64
import codecs
import ctypes
import logging
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, ValidationError, field_validator

from ledroide_sdk import Module

log = logging.getLogger("ledroide.command-launcher")

module = Module(__file__)
router = module.router

IS_WINDOWS = sys.platform == "win32"
NO_WINDOW = subprocess.CREATE_NO_WINDOW if IS_WINDOWS else 0
NEW_GROUP = subprocess.CREATE_NEW_PROCESS_GROUP if IS_WINDOWS else 0
PARAM_RE = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_\-]*)\s*(?:\|([^}]*))?\}\}")
MAX_LIVE_OUTPUT = 4 * 1024 * 1024
MAX_SAVED_OUTPUT = 256 * 1024
FLUSH_INTERVAL = 0.08
INTERPRETERS = ("cmd", "powershell", "python", "exe")
EXPORT_FORMAT = "ledroide-command-launcher"


class EnvVar(BaseModel):
    key: str = Field("", max_length=200)
    value: str = Field("", max_length=8000)


class Command(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    icon: str = Field("▶️", max_length=16)
    color: str = "#6366f1"
    command: str = Field(min_length=1, max_length=20000)
    interpreter: Literal["cmd", "powershell", "python", "exe"] = "cmd"
    cwd: str = Field("", max_length=1000)
    env: list[EnvVar] = []
    confirm: bool = False
    timeout: int = Field(0, ge=0, le=86400)
    group: str = Field("Général", max_length=40)
    description: str = Field("", max_length=300)

    @field_validator("name", "group")
    @classmethod
    def _strip(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("ne doit pas être vide")
        return v

    @field_validator("color")
    @classmethod
    def _color(cls, v: str) -> str:
        return v if re.fullmatch(r"#[0-9a-fA-F]{6}", v or "") else "#6366f1"


class LaunchError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _validation_message(exc: ValidationError) -> str:
    labels = {"name": "Nom", "command": "Commande", "group": "Groupe", "timeout": "Délai", "icon": "Icône"}
    e = exc.errors()[0]
    field = labels.get(str(e["loc"][0]), str(e["loc"][0])) if e["loc"] else "Commande"
    msgs = {"string_too_short": "obligatoire", "missing": "obligatoire", "string_too_long": "trop long",
            "literal_error": "valeur non autorisée", "greater_than_equal": "valeur trop petite",
            "less_than_equal": "valeur trop grande", "int_parsing": "nombre entier attendu"}
    return f"{field} : {msgs.get(e.get('type'), e['msg'])}"


# ── Parameters ────────────────────────────────────────────────────────────────

def params_of(cmd: dict) -> list[dict]:
    seen: dict[str, dict] = {}
    texts = [cmd.get("command", ""), cmd.get("cwd", "")] + [e.get("value", "") for e in cmd.get("env") or []]
    for text in texts:
        for m in PARAM_RE.finditer(text or ""):
            name = m.group(1)
            if name not in seen:
                seen[name] = {"name": name, "default": (m.group(2) or "").strip()}
            elif not seen[name]["default"] and m.group(2):
                seen[name]["default"] = m.group(2).strip()
    return list(seen.values())


def substitute(text: str, values: dict) -> str:
    def rep(m):
        name = m.group(1)
        if name in values:
            return str(values[name])
        if m.group(2) is not None:
            return m.group(2).strip()
        raise LaunchError("PARAM_MISSING", f"Paramètre manquant : {name}")
    return PARAM_RE.sub(rep, text or "")


def split_args(text: str) -> list[str]:
    """Windows-style split: whitespace separates, double quotes group, backslashes are literal."""
    args, cur, quoted, has = [], [], False, False
    for ch in text:
        if ch == '"':
            quoted = not quoted
            has = True
        elif ch.isspace() and not quoted:
            if cur or has:
                args.append("".join(cur))
            cur, has = [], False
        else:
            cur.append(ch)
    if quoted:
        raise LaunchError("BAD_COMMAND", "Guillemet non fermé dans la commande")
    if cur or has:
        args.append("".join(cur))
    return args


def _python_exe() -> str:
    configured = str(module.config.get("python_path") or "").strip().strip('"')
    if configured:
        found = shutil.which(configured) or (configured if Path(configured).is_file() else None)
        if not found:
            raise LaunchError("NOT_FOUND", f"Interpréteur Python introuvable : {configured}")
        return found
    return sys.executable


def build(cmd: dict, values: dict) -> dict:
    """Returns {args (list|str), cwd, env, display} for subprocess.Popen."""
    values = {k: str(v) for k, v in (values or {}).items() if isinstance(k, str)}
    interp = cmd.get("interpreter", "cmd")
    template = cmd.get("command", "")
    env = dict(os.environ)
    for p in params_of(cmd):
        env["LP_" + p["name"].upper().replace("-", "_")] = values.get(p["name"], p["default"])
    for e in cmd.get("env") or []:
        key = str(e.get("key", "")).strip()
        if key:
            env[key] = os.path.expandvars(substitute(str(e.get("value", "")), values))
    cwd_raw = substitute(cmd.get("cwd", ""), values).strip().strip('"')
    cwd = os.path.expanduser(os.path.expandvars(cwd_raw)) if cwd_raw else str(Path.home())
    if not Path(cwd).is_dir():
        raise LaunchError("CWD_NOT_FOUND", f"Dossier de travail introuvable : {cwd}")

    if interp == "exe":
        tokens = [substitute(t, values) for t in split_args(template)]
        if not tokens or not tokens[0]:
            raise LaunchError("BAD_COMMAND", "Commande vide")
        exe = os.path.expandvars(tokens[0])
        resolved = shutil.which(exe, path=env.get("PATH")) or (exe if Path(exe).is_file() else None)
        if not resolved:
            raise LaunchError("NOT_FOUND", f"Exécutable introuvable : {exe}")
        args = [resolved] + tokens[1:]
        display = subprocess.list2cmdline([exe] + tokens[1:])
    elif interp == "cmd":
        line = substitute(template, values).replace("\r\n", "\n")
        line = " & ".join(x for x in line.split("\n") if x.strip())
        if not line.strip():
            raise LaunchError("BAD_COMMAND", "Commande vide")
        comspec = env.get("ComSpec") or os.environ.get("ComSpec") or "cmd.exe"
        args = f'"{comspec}" /d /s /c "{line}"'
        display = line
    elif interp == "powershell":
        script = substitute(template, values)
        prelude = ("[Console]::OutputEncoding=[Text.Encoding]::UTF8;$OutputEncoding=[Text.Encoding]::UTF8;"
                   "$ProgressPreference='SilentlyContinue';\n")
        encoded = base64.b64encode((prelude + script).encode("utf-16-le")).decode()
        exe = shutil.which("powershell.exe") or "powershell.exe"
        args = [exe, "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-EncodedCommand", encoded]
        display = script
        env["PSModulePath"] = env.get("PSModulePath", "")
    else:
        code = substitute(template, values)
        args = [_python_exe(), "-u", "-c", code]
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUNBUFFERED"] = "1"
        display = code
    return {"args": args, "cwd": cwd, "env": env, "display": display}


# ── Output decoding ───────────────────────────────────────────────────────────

def _oem_codec() -> str:
    if IS_WINDOWS:
        try:
            cp = ctypes.windll.kernel32.GetOEMCP()
            codecs.lookup(f"cp{cp}")
            return f"cp{cp}"
        except Exception:
            pass
    return "latin-1"


OEM = _oem_codec()


class StreamDecoder:
    """UTF-8 until an invalid sequence shows up, then the console OEM code page."""

    def __init__(self):
        self.dec = codecs.getincrementaldecoder("utf-8")("strict")
        self.fallback = False
        self.pending_cr = False

    def decode(self, data: bytes, final: bool = False) -> str:
        if not self.fallback:
            try:
                text = self.dec.decode(data, final)
            except UnicodeDecodeError:
                buffered = self.dec.getstate()[0]
                self.fallback = True
                self.dec = codecs.getincrementaldecoder(OEM)("replace")
                text = self.dec.decode(buffered + data, final)
        else:
            text = self.dec.decode(data, final)
        if self.pending_cr:
            text = "\r" + text
            self.pending_cr = False
        if text.endswith("\r") and not final:
            text, self.pending_cr = text[:-1], True
        return text.replace("\r\n", "\n").replace("\r", "\n")


# ── Runs ──────────────────────────────────────────────────────────────────────

class Run:
    def __init__(self, cmd: dict, built: dict):
        self.id = uuid.uuid4().hex
        self.cmd = cmd
        self.built = built
        self.proc: subprocess.Popen | None = None
        self.started = time.time()
        self.ended: float | None = None
        self.status = "running"
        self.code: int | None = None
        self.output: list[list[str]] = []
        self.size = 0
        self.truncated = False
        self.pending: list[list[str]] = []
        self.lock = threading.Lock()
        self.stop_reason: str | None = None
        self.readers: list[threading.Thread] = []

    def push(self, stream: str, text: str):
        if not text:
            return
        with self.lock:
            self.pending.append([stream, text])
            if self.size < MAX_LIVE_OUTPUT:
                if self.output and self.output[-1][0] == stream:
                    self.output[-1][1] += text
                else:
                    self.output.append([stream, text])
                self.size += len(text)
            else:
                self.truncated = True

    def take_pending(self) -> list[list[str]]:
        with self.lock:
            out, self.pending = self.pending, []
        merged: list[list[str]] = []
        for s, t in out:
            if merged and merged[-1][0] == s:
                merged[-1][1] += t
            else:
                merged.append([s, t])
        return merged

    def meta(self) -> dict:
        end = self.ended or time.time()
        return {"run_id": self.id, "command_id": self.cmd.get("_id"), "name": self.cmd.get("name"),
                "icon": self.cmd.get("icon"), "color": self.cmd.get("color"), "interpreter": self.cmd.get("interpreter"),
                "display": self.built["display"][:2000], "cwd": self.built["cwd"], "status": self.status,
                "code": self.code, "started": datetime.fromtimestamp(self.started, timezone.utc).isoformat(),
                "duration_ms": round((end - self.started) * 1000), "pid": self.proc.pid if self.proc else None,
                "truncated": self.truncated}


_runs: dict[str, Run] = {}


def _reader(run: Run, pipe, stream: str):
    dec = StreamDecoder()
    try:
        while True:
            chunk = pipe.read1(65536) if hasattr(pipe, "read1") else pipe.read(4096)
            if not chunk:
                break
            run.push(stream, dec.decode(chunk))
        run.push(stream, dec.decode(b"", True))
    except (OSError, ValueError):
        pass
    finally:
        try:
            pipe.close()
        except OSError:
            pass


def _kill_tree(proc: subprocess.Popen):
    if proc.poll() is not None:
        return
    if IS_WINDOWS:
        try:
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True, timeout=15,
                           creationflags=NO_WINDOW)
        except (OSError, subprocess.TimeoutExpired):
            pass
    if proc.poll() is None:
        try:
            proc.kill()
        except OSError:
            pass


async def _supervise(run: Run, timeout: int):
    deadline = run.started + timeout if timeout else None
    exited_at = None
    try:
        while True:
            await asyncio.sleep(FLUSH_INTERVAL)
            chunks = run.take_pending()
            if chunks:
                await module.events.emit("run:output", {"run_id": run.id, "chunks": chunks})
            if run.proc.poll() is not None:
                exited_at = exited_at or time.time()
                # A detached grandchild may keep the pipes open: stop waiting after a grace period.
                if not any(t.is_alive() for t in run.readers) or time.time() - exited_at > 3:
                    break
                continue
            if deadline and time.time() > deadline and not run.stop_reason:
                run.stop_reason = "timeout"
                run.push("sys", f"\n⏱ Délai de {timeout} s dépassé : arrêt du processus.\n")
                await asyncio.to_thread(_kill_tree, run.proc)
        chunks = run.take_pending()
        if chunks:
            await module.events.emit("run:output", {"run_id": run.id, "chunks": chunks})
    except asyncio.CancelledError:
        run.stop_reason = run.stop_reason or "stopped"
        await asyncio.to_thread(_kill_tree, run.proc)
        raise
    finally:
        run.ended = time.time()
        run.code = run.proc.poll()
        run.status = run.stop_reason or ("ok" if run.code == 0 else "error")
        meta = run.meta()
        log.info("run %s (%s) -> %s code=%s %d ms", run.cmd.get("name"), run.cmd.get("interpreter"), run.status,
                 run.code, meta["duration_ms"])
        try:
            await module.events.emit("run:end", meta)
            await _save_history(run, meta)
        except Exception:
            log.exception("could not save run history")
        asyncio.get_running_loop().call_later(30, _runs.pop, run.id, None)


async def _save_history(run: Run, meta: dict):
    keep = int(module.config.get("history_size", 100))
    if keep <= 0:
        return
    out, total = [], 0
    for stream, text in reversed(run.output):
        if total >= MAX_SAVED_OUTPUT:
            break
        part = text[-(MAX_SAVED_OUTPUT - total):]
        out.insert(0, [stream, part])
        total += len(part)
    await module.db.save("runs", {**meta, "_id": run.id, "saved_truncated": run.truncated or total >= MAX_SAVED_OUTPUT})
    await module.db.save("run_output", {"_id": run.id, "output": out})
    runs = await module.db.query("runs")
    for old in runs[keep:]:
        await module.db.delete("runs", old["_id"])
        await module.db.delete("run_output", old["_id"])


def _spawn(built: dict) -> subprocess.Popen:
    return subprocess.Popen(built["args"], cwd=built["cwd"], env=built["env"], stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=NO_WINDOW | NEW_GROUP)


async def start_run(cmd: dict, values: dict) -> Run:
    built = await asyncio.to_thread(build, cmd, values)
    run = Run(cmd, built)
    try:
        run.proc = await asyncio.to_thread(_spawn, built)
    except FileNotFoundError:
        raise LaunchError("NOT_FOUND", "Interpréteur ou exécutable introuvable")
    except OSError as exc:
        raise LaunchError("START_FAILED", f"Impossible de démarrer la commande : {exc.strerror or exc}")
    for pipe, stream in ((run.proc.stdout, "out"), (run.proc.stderr, "err")):
        t = threading.Thread(target=_reader, args=(run, pipe, stream), daemon=True, name=f"cl-{stream}-{run.id[:6]}")
        t.start()
        run.readers.append(t)
    _runs[run.id] = run
    timeout = int(cmd.get("timeout") or 0) or int(module.config.get("default_timeout", 0) or 0)
    await module.events.emit("run:start", run.meta())
    run.task = asyncio.create_task(_supervise(run, timeout))
    return run


# ── Commands storage ──────────────────────────────────────────────────────────

EXAMPLES = [
    {"name": "Configuration IP", "icon": "🌐", "color": "#38bdf8", "interpreter": "cmd", "command": "ipconfig /all",
     "group": "Réseau", "description": "Adresses, passerelles et DNS de toutes les cartes réseau"},
    {"name": "Ping", "icon": "📶", "color": "#22c55e", "interpreter": "exe", "command": "ping -n 4 {{hote|127.0.0.1}}",
     "group": "Réseau", "description": "4 pings vers l'hôte demandé"},
    {"name": "Lister les fichiers", "icon": "📂", "color": "#f59e0b", "interpreter": "cmd",
     "command": 'dir /a "{{dossier|%USERPROFILE%}}"', "group": "Fichiers", "description": "Contenu d'un dossier"},
    {"name": "Git status", "icon": "🌿", "color": "#f97316", "interpreter": "exe", "command": "git status",
     "cwd": "{{depot}}", "group": "Développement", "description": "État d'un dépôt Git (dossier demandé)"},
    {"name": "Processus gourmands", "icon": "📊", "color": "#a855f7", "interpreter": "powershell",
     "command": "Get-Process | Sort-Object CPU -Descending | Select-Object -First 10 Name, Id, CPU, "
                "@{n='Mémoire (Mo)';e={[math]::Round($_.WorkingSet64/1MB)}} | Format-Table -AutoSize",
     "group": "Système", "description": "Top 10 des processus par temps CPU"},
    {"name": "Espace disque", "icon": "💾", "color": "#6366f1", "interpreter": "powershell",
     "command": "Get-PSDrive -PSProvider FileSystem | Format-Table Name, "
                "@{n='Utilisé (Go)';e={[math]::Round($_.Used/1GB,1)}}, @{n='Libre (Go)';e={[math]::Round($_.Free/1GB,1)}} -AutoSize",
     "group": "Système", "description": "Espace utilisé et libre par lecteur"},
    {"name": "Infos Python", "icon": "🐍", "color": "#eab308", "interpreter": "python",
     "command": "import platform, sys\nprint(platform.platform())\nprint(sys.version)", "group": "Système",
     "description": "Version de Python et du système"},
]


async def _ensure_seeded():
    flag = await module.db.get("settings", "seeded")
    if flag:
        return
    existing = await module.db.query("commands")
    if not existing:
        for i, ex in enumerate(EXAMPLES):
            await module.db.save("commands", {**Command.model_validate(ex).model_dump(), "order": i})
    await module.db.save("settings", {"_id": "seeded", "at": _now()})
    groups = []
    for ex in EXAMPLES:
        if ex["group"] not in groups:
            groups.append(ex["group"])
    if not await module.db.get("settings", "groups"):
        await module.db.save("settings", {"_id": "groups", "items": groups})


async def _commands() -> list[dict]:
    items = await module.db.query("commands")
    items.sort(key=lambda c: (c.get("order", 0), (c.get("name") or "").lower()))
    for c in items:
        c["params"] = params_of(c)
    return items


async def _groups(commands: list[dict]) -> list[str]:
    doc = await module.db.get("settings", "groups")
    groups = [g for g in (doc or {}).get("items", []) if isinstance(g, str)]
    for c in commands:
        if c.get("group") and c["group"] not in groups:
            groups.append(c["group"])
    return groups


@module.on("list")
async def list_commands(payload: dict):
    await _ensure_seeded()
    commands = await _commands()
    return {"commands": commands, "groups": await _groups(commands), "platform": sys.platform,
            "default_timeout": int(module.config.get("default_timeout", 0) or 0)}


@module.on("save")
async def save_command(payload: dict):
    try:
        cmd = Command.model_validate(payload.get("command") or {})
    except ValidationError as exc:
        return {"error": _validation_message(exc), "code": "BAD_REQUEST"}
    doc = cmd.model_dump()
    cid = str(payload.get("id") or "")
    if cid:
        old = await module.db.get("commands", cid)
        if not old:
            return {"error": "Commande introuvable", "code": "NOT_FOUND"}
        doc["_id"] = cid
        doc["order"] = old.get("order", 0)
    else:
        doc["order"] = max([c.get("order", 0) for c in await module.db.query("commands")] or [-1]) + 1
    saved = await module.db.save("commands", doc)
    groups_doc = await module.db.get("settings", "groups") or {"_id": "groups", "items": []}
    if saved["group"] not in groups_doc["items"]:
        groups_doc["items"].append(saved["group"])
        await module.db.save("settings", groups_doc)
    saved["params"] = params_of(saved)
    return saved


@module.on("delete")
async def delete_command(payload: dict):
    return await module.db.delete("commands", str(payload.get("id") or ""))


@module.on("reorder")
async def reorder(payload: dict):
    ids = [str(x) for x in payload.get("ids") or []]
    by_id = {c["_id"]: c for c in await module.db.query("commands")}
    for i, cid in enumerate(ids):
        c = by_id.get(cid)
        if c and (c.get("order") != i or (payload.get("group") and c.get("group") != payload["group"])):
            c["order"] = i
            if payload.get("group"):
                c["group"] = str(payload["group"])[:40]
            await module.db.save("commands", c)
    return {"ok": True}


@module.on("move")
async def move(payload: dict):
    c = await module.db.get("commands", str(payload.get("id") or ""))
    group = str(payload.get("group") or "").strip()[:40]
    if not c or not group:
        return {"error": "Commande ou groupe invalide", "code": "BAD_REQUEST"}
    c["group"] = group
    return await module.db.save("commands", c)


@module.on("groups_save")
async def groups_save(payload: dict):
    items = []
    for g in payload.get("groups") or []:
        g = str(g).strip()[:40]
        if g and g not in items:
            items.append(g)
    renames = payload.get("renames") or {}
    if renames:
        for c in await module.db.query("commands"):
            new = renames.get(c.get("group"))
            if isinstance(new, str) and new.strip():
                c["group"] = new.strip()[:40]
                await module.db.save("commands", c)
    commands = await module.db.query("commands")
    used = {c.get("group") for c in commands}
    if not items:
        items = ["Général"]
    fallback = items[0]
    for c in commands:
        if c.get("group") not in items:
            c["group"] = fallback
            await module.db.save("commands", c)
    await module.db.save("settings", {"_id": "groups", "items": items})
    return {"groups": items, "moved": len(used - set(items) - {None})}


@module.on("export")
async def export(payload: dict):
    commands = await _commands()
    keys = list(Command.model_fields)
    return {"format": EXPORT_FORMAT, "version": 1, "exported": _now(), "groups": await _groups(commands),
            "commands": [{k: c.get(k) for k in keys if k in c} for c in commands]}


@module.on("import")
async def import_commands(payload: dict):
    data = payload.get("data")
    if isinstance(data, list):
        data = {"commands": data}
    if not isinstance(data, dict) or not isinstance(data.get("commands"), list):
        return {"error": "Fichier invalide : liste « commands » attendue", "code": "BAD_FORMAT"}
    valid, errors = [], []
    for i, raw in enumerate(data["commands"][:1000]):
        try:
            valid.append(Command.model_validate(raw).model_dump())
        except ValidationError as exc:
            errors.append(f"#{i + 1} : {_validation_message(exc)}")
    if not valid:
        return {"error": "Aucune commande valide dans le fichier" + (f" ({errors[0]})" if errors else ""), "code": "BAD_FORMAT"}
    existing = await module.db.query("commands")
    if payload.get("mode") == "replace":
        for c in existing:
            await module.db.delete("commands", c["_id"])
        existing = []
    start = max([c.get("order", 0) for c in existing] or [-1]) + 1
    for i, c in enumerate(valid):
        await module.db.save("commands", {**c, "order": start + i})
    groups_doc = await module.db.get("settings", "groups") or {"_id": "groups", "items": []}
    if payload.get("mode") == "replace":
        groups_doc["items"] = []
    for g in list(data.get("groups") or []) + [c["group"] for c in valid]:
        if isinstance(g, str) and g.strip() and g.strip()[:40] not in groups_doc["items"]:
            groups_doc["items"].append(g.strip()[:40])
    await module.db.save("settings", groups_doc)
    return {"imported": len(valid), "skipped": len(errors), "errors": errors[:10]}


# ── Execution API ─────────────────────────────────────────────────────────────

@module.on("run")
async def run_command(payload: dict):
    if payload.get("id"):
        cmd = await module.db.get("commands", str(payload["id"]))
        if not cmd:
            return {"error": "Commande introuvable", "code": "NOT_FOUND"}
    else:
        try:
            cmd = Command.model_validate(payload.get("command") or {}).model_dump()
        except ValidationError as exc:
            return {"error": _validation_message(exc), "code": "BAD_REQUEST"}
        cmd["_id"] = None
    params = payload.get("params") if isinstance(payload.get("params"), dict) else {}
    try:
        run = await start_run(cmd, params)
    except LaunchError as exc:
        return {"error": exc.message, "code": exc.code}
    return run.meta()


@module.on("stop")
async def stop(payload: dict):
    run = _runs.get(str(payload.get("run_id") or ""))
    if not run or run.status != "running" or run.proc.poll() is not None:
        return {"error": "Aucune exécution en cours avec cet identifiant", "code": "NOT_RUNNING"}
    run.stop_reason = "stopped"
    run.push("sys", "\n■ Arrêt demandé : processus et sous-processus terminés.\n")
    await asyncio.to_thread(_kill_tree, run.proc)
    return {"ok": True}


@module.on("active")
async def active(payload: dict):
    out = []
    for run in list(_runs.values()):
        with run.lock:
            output = [list(x) for x in run.output]
        out.append({**run.meta(), "output": output})
    return {"runs": out}


@module.on("history")
async def history(payload: dict):
    items = await module.db.query("runs")
    cid = payload.get("command_id")
    if cid:
        items = [r for r in items if r.get("command_id") == cid]
    return {"items": items[:int(payload.get("limit") or 500)]}


@module.on("history_get")
async def history_get(payload: dict):
    rid = str(payload.get("run_id") or "")
    run = _runs.get(rid)
    if run:
        with run.lock:
            return {**run.meta(), "output": [list(x) for x in run.output]}
    meta = await module.db.get("runs", rid)
    if not meta:
        return {"error": "Exécution introuvable", "code": "NOT_FOUND"}
    out = await module.db.get("run_output", rid) or {}
    return {**meta, "output": out.get("output", [])}


@module.on("history_clear")
async def history_clear(payload: dict):
    for r in await module.db.query("runs"):
        await module.db.delete("runs", r["_id"])
        await module.db.delete("run_output", r["_id"])
    return {"ok": True}


@module.on_shutdown
async def _shutdown():
    for run in list(_runs.values()):
        if run.proc and run.proc.poll() is None:
            run.stop_reason = "stopped"
            await asyncio.to_thread(_kill_tree, run.proc)
        task = getattr(run, "task", None)
        if task and not task.done():
            try:
                await asyncio.wait_for(task, 5)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                pass
