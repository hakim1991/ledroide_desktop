import asyncio
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path

from ledroide_sdk import Module

log = logging.getLogger("ledroide.docker-manager")

module = Module(__file__)
router = module.router

ID_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}$")
IMAGE_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._/:@-]{0,254}$")
PROJECT_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}$")
BUILTIN_NETWORKS = {"bridge", "host", "none"}
COMPOSE_EXT = {".yml", ".yaml"}
MAX_STREAMS = 6
MAX_LOG_LINE = 8000
UNITS = {"b": 1, "kb": 1e3, "mb": 1e6, "gb": 1e9, "tb": 1e12, "kib": 1024, "mib": 1024 ** 2,
         "gib": 1024 ** 3, "tib": 1024 ** 4}

_streams: dict[str, dict] = {}


class DockerError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _err(message: str, code: str = "BAD_REQUEST") -> dict:
    return {"error": message, "code": code}


def _fail(exc: DockerError) -> dict:
    return _err(exc.message, exc.code)


# ── Parsing ──────────────────────────────────────────────────────────────────

def parse_json_lines(text: str) -> list[dict]:
    out = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            out.append(obj)
        elif isinstance(obj, list):
            out.extend(o for o in obj if isinstance(o, dict))
    return out


def parse_labels(raw) -> dict[str, str]:
    if isinstance(raw, dict):
        return {str(k): str(v) for k, v in raw.items()}
    if not raw:
        return {}
    labels = {}
    for part in re.split(r",(?=[^,=]+=)", str(raw)):
        key, sep, value = part.partition("=")
        if key.strip():
            labels[key.strip()] = value if sep else ""
    return labels


def parse_size(raw) -> int:
    if raw is None:
        return 0
    text = str(raw).strip().split(" (")[0].replace(",", ".")
    m = re.match(r"^([\d.]+)\s*([a-zA-Z]*)$", text)
    if not m:
        return 0
    try:
        value = float(m.group(1))
    except ValueError:
        return 0
    return int(value * UNITS.get(m.group(2).lower() or "b", 1))


def parse_percent(raw) -> float:
    try:
        return float(str(raw).strip().rstrip("%").replace(",", "."))
    except ValueError:
        return 0.0


def parse_pair(raw) -> tuple[int, int]:
    parts = [p.strip() for p in str(raw or "").split("/")]
    if len(parts) != 2:
        return 0, 0
    return parse_size(parts[0]), parse_size(parts[1])


def parse_ports(raw) -> list[dict]:
    ports, seen = [], set()
    for chunk in str(raw or "").split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        m = re.match(r"^(?:(?P<ip>\[?[0-9a-fA-F:.]*\]?):(?P<host>\d+(?:-\d+)?)->)?(?P<cport>\d+(?:-\d+)?)/(?P<proto>\w+)$", chunk)
        if not m:
            ports.append({"raw": chunk})
            continue
        ip = (m.group("ip") or "").strip("[]")
        entry = {"ip": ip, "host_port": m.group("host") or "", "container_port": m.group("cport"),
                 "proto": m.group("proto")}
        key = (entry["host_port"], entry["container_port"], entry["proto"])
        if entry["host_port"] and key in seen:
            continue
        seen.add(key)
        ports.append(entry)
    return ports


def normalize_container(row: dict) -> dict:
    labels = parse_labels(row.get("Labels"))
    names = str(row.get("Names") or "").split(",")
    cid = str(row.get("ID") or "")
    state = str(row.get("State") or "").lower()
    if not state:
        status = str(row.get("Status") or "").lower()
        state = "running" if status.startswith("up") else "exited" if status.startswith("exited") else status.split(" ")[0]
    return {
        "id": cid,
        "short_id": cid[:12],
        "name": names[0].lstrip("/") if names else "",
        "image": row.get("Image") or "",
        "command": str(row.get("Command") or "").strip('"'),
        "state": state,
        "status": row.get("Status") or "",
        "created": row.get("CreatedAt") or "",
        "running_for": row.get("RunningFor") or "",
        "ports": parse_ports(row.get("Ports")),
        "networks": [n for n in str(row.get("Networks") or "").split(",") if n],
        "mounts": [m for m in str(row.get("Mounts") or "").split(",") if m],
        "labels": labels,
        "compose_project": labels.get("com.docker.compose.project", ""),
        "compose_service": labels.get("com.docker.compose.service", ""),
    }


def normalize_stats(row: dict) -> dict:
    mem_used, mem_limit = parse_pair(row.get("MemUsage"))
    net_rx, net_tx = parse_pair(row.get("NetIO"))
    blk_r, blk_w = parse_pair(row.get("BlockIO"))
    try:
        pids = int(str(row.get("PIDs") or "0").strip())
    except ValueError:
        pids = 0
    return {
        "id": str(row.get("ID") or row.get("Container") or ""),
        "name": str(row.get("Name") or "").lstrip("/"),
        "cpu": parse_percent(row.get("CPUPerc")),
        "mem_percent": parse_percent(row.get("MemPerc")),
        "mem_used": mem_used, "mem_limit": mem_limit,
        "net_rx": net_rx, "net_tx": net_tx, "block_read": blk_r, "block_write": blk_w,
        "pids": pids,
    }


def normalize_image(row: dict) -> dict:
    repo, tag = row.get("Repository") or "<none>", row.get("Tag") or "<none>"
    iid = str(row.get("ID") or "")
    return {
        "id": iid,
        "short_id": iid.removeprefix("sha256:")[:12],
        "repository": repo, "tag": tag,
        "ref": f"{repo}:{tag}" if repo != "<none>" and tag != "<none>" else iid.removeprefix("sha256:")[:12],
        "dangling": repo == "<none>",
        "size": parse_size(row.get("Size") or row.get("VirtualSize")),
        "size_text": row.get("Size") or row.get("VirtualSize") or "",
        "created": row.get("CreatedAt") or "",
        "created_since": row.get("CreatedSince") or "",
        "containers": row.get("Containers") if row.get("Containers") not in (None, "N/A") else "",
    }


def normalize_volume(row: dict) -> dict:
    labels = parse_labels(row.get("Labels"))
    return {"name": row.get("Name") or "", "driver": row.get("Driver") or "", "scope": row.get("Scope") or "",
            "mountpoint": row.get("Mountpoint") or "", "size": row.get("Size") if row.get("Size") not in (None, "N/A") else "",
            "labels": labels, "compose_project": labels.get("com.docker.compose.project", "")}


def normalize_network(row: dict) -> dict:
    labels = parse_labels(row.get("Labels"))
    name = row.get("Name") or ""
    return {"id": str(row.get("ID") or "")[:12], "name": name, "driver": row.get("Driver") or "",
            "scope": row.get("Scope") or "", "internal": str(row.get("Internal")).lower() == "true",
            "ipv6": str(row.get("IPv6")).lower() == "true", "created": row.get("CreatedAt") or "",
            "builtin": name in BUILTIN_NETWORKS, "compose_project": labels.get("com.docker.compose.project", "")}


def compose_projects(containers: list[dict]) -> list[dict]:
    projects: dict[str, dict] = {}
    for c in containers:
        name = c.get("compose_project")
        if not name:
            continue
        labels = c.get("labels") or {}
        p = projects.setdefault(name, {"name": name, "working_dir": "", "config_files": [], "services": [],
                                       "running": 0, "total": 0, "containers": []})
        p["working_dir"] = p["working_dir"] or labels.get("com.docker.compose.project.working_dir", "")
        for f in labels.get("com.docker.compose.project.config_files", "").split(","):
            f = f.strip()
            if f and f not in p["config_files"]:
                p["config_files"].append(f)
        svc = c.get("compose_service")
        if svc and svc not in p["services"]:
            p["services"].append(svc)
        p["total"] += 1
        p["running"] += c.get("state") == "running"
        p["containers"].append({"id": c["id"], "name": c["name"], "service": svc, "state": c["state"]})
    for p in projects.values():
        p["services"].sort()
        p["status"] = "running" if p["running"] == p["total"] else "stopped" if p["running"] == 0 else "partial"
    return sorted(projects.values(), key=lambda p: p["name"])


def parse_pull_line(line: str, layers: dict[str, str]) -> None:
    m = re.match(r"^([0-9a-f]{12}):\s*(.+)$", line.strip())
    if not m:
        return
    status = m.group(2).lower()
    current = layers.get(m.group(1), "")
    if "pull complete" in status or "already exists" in status:
        layers[m.group(1)] = "done"
    elif current != "done" and ("download complete" in status or "extracting" in status or "verifying" in status):
        layers[m.group(1)] = "downloaded"
    elif m.group(1) not in layers:
        layers[m.group(1)] = "pending"


def pull_percent(layers: dict[str, str]) -> float | None:
    if not layers:
        return None
    score = sum(1.0 if s == "done" else 0.6 if s == "downloaded" else 0.0 for s in layers.values())
    return round(score / len(layers) * 100, 1)


def daemon_error_message(stderr: str) -> str:
    low = stderr.lower()
    if "permission denied" in low:
        return "Accès refusé au démon Docker (ajoutez votre utilisateur au groupe « docker »)."
    if any(k in low for k in ("cannot connect", "error during connect", "is the docker daemon running",
                              "pipe/docker", "docker_engine", "dockerdesktoplinuxengine", "connection refused")):
        return "Le démon Docker ne répond pas. Démarrez Docker Desktop (ou le service Docker) puis réessayez."
    return stderr.strip().splitlines()[-1][:400] if stderr.strip() else "Erreur Docker inconnue"


# ── CLI ──────────────────────────────────────────────────────────────────────

def _docker_exe() -> str:
    exe = shutil.which("docker")
    if exe:
        return exe
    if sys.platform == "win32":
        for base in (os.environ.get("ProgramFiles", r"C:\Program Files"), os.environ.get("ProgramW6432", "")):
            cand = Path(base or "") / "Docker" / "Docker" / "resources" / "bin" / "docker.exe"
            if base and cand.is_file():
                return str(cand)
    raise DockerError("DOCKER_NOT_FOUND", "Docker est introuvable : installez Docker Desktop (ou le CLI docker) et vérifiez qu'il est dans le PATH.")


def _flags() -> int:
    return subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0


async def _spawn(args: list[str], cwd: str | None = None, merge: bool = False):
    try:
        return await asyncio.create_subprocess_exec(
            _docker_exe(), *args, cwd=cwd, stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT if merge else asyncio.subprocess.PIPE,
            creationflags=_flags())
    except FileNotFoundError:
        raise DockerError("DOCKER_NOT_FOUND", "Docker est introuvable.")
    except OSError as exc:
        raise DockerError("DOCKER_FAILED", f"Impossible de lancer docker : {exc}")


async def docker(args: list[str], timeout: float = 30, check: bool = True) -> tuple[int, str, str]:
    proc = await _spawn(args)
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise DockerError("DOCKER_TIMEOUT", "Docker ne répond pas (délai dépassé). Le démon est-il démarré ?")
    stdout = out.decode("utf-8", "replace")
    stderr = err.decode("utf-8", "replace")
    if check and proc.returncode != 0:
        raise DockerError("DAEMON_DOWN" if _is_daemon_error(stderr) else "DOCKER_ERROR", daemon_error_message(stderr))
    return proc.returncode, stdout, stderr


def _is_daemon_error(stderr: str) -> bool:
    return daemon_error_message(stderr).startswith(("Le démon", "Accès refusé"))


async def docker_json(args: list[str], timeout: float = 30) -> list[dict]:
    _, out, _ = await docker([*args, "--format", "{{json .}}"], timeout)
    return parse_json_lines(out)


def _cid(payload: dict, key: str = "id") -> str:
    value = str(payload.get(key) or "").strip()
    if not ID_RE.match(value):
        raise DockerError("BAD_ID", "Identifiant invalide")
    return value


def _image_ref(value) -> str:
    value = str(value or "").strip()
    if not IMAGE_RE.match(value):
        raise DockerError("BAD_IMAGE", "Nom d'image invalide (ex. nginx:latest)")
    return value


def _compose_file(raw) -> Path:
    text = str(raw or "").strip().strip('"')
    if not text:
        raise DockerError("BAD_PATH", "Chemin du fichier Compose requis")
    path = Path(os.path.expandvars(os.path.expanduser(text)))
    if not path.is_absolute():
        raise DockerError("BAD_PATH", "Indiquez un chemin absolu")
    if not path.is_file():
        raise DockerError("NOT_FOUND", f"Fichier introuvable : {path}")
    if path.suffix.lower() not in COMPOSE_EXT:
        raise DockerError("BAD_PATH", "Le fichier doit être un .yml ou .yaml")
    return path.resolve()


# ── Handlers ─────────────────────────────────────────────────────────────────

@module.on("status")
async def status(payload: dict):
    try:
        exe = _docker_exe()
    except DockerError as exc:
        return {"available": False, "daemon": False, "code": exc.code, "message": exc.message}
    try:
        code, out, err = await docker(["version", "--format", "{{json .}}"], timeout=15, check=False)
    except DockerError as exc:
        return {"available": True, "daemon": False, "exe": exe, "code": exc.code, "message": exc.message}
    info = (parse_json_lines(out) or [{}])[0]
    client = info.get("Client") or {}
    server = info.get("Server") or {}
    if code != 0 or not server:
        return {"available": True, "daemon": False, "exe": exe, "client_version": client.get("Version", ""),
                "code": "DAEMON_DOWN", "message": daemon_error_message(err),
                "detail": " ".join(err.strip().splitlines()[-3:])[:600]}
    return {"available": True, "daemon": True, "exe": exe, "client_version": client.get("Version", ""),
            "server_version": server.get("Version", ""), "os": server.get("Os", ""), "arch": server.get("Arch", ""),
            "platform": (server.get("Platform") or {}).get("Name", "")}


@module.on("containers.list")
async def containers_list(payload: dict):
    try:
        rows = await docker_json(["ps", "-a", "--no-trunc"])
    except DockerError as exc:
        return _fail(exc)
    containers = sorted((normalize_container(r) for r in rows), key=lambda c: (c["state"] != "running", c["name"]))
    return {"containers": containers}


@module.on("containers.action")
async def containers_action(payload: dict):
    action = payload.get("action")
    commands = {"start": ["start"], "stop": ["stop"], "restart": ["restart"], "pause": ["pause"],
                "unpause": ["unpause"], "kill": ["kill"], "remove": ["rm", "-f"] if payload.get("force") else ["rm"]}
    if action not in commands:
        return _err("Action inconnue", "BAD_ACTION")
    try:
        cid = _cid(payload)
        await docker([*commands[action], cid], timeout=90)
    except DockerError as exc:
        return _fail(exc)
    log.info("container %s: %s", cid[:12], action)
    return {"ok": True}


@module.on("containers.stats")
async def containers_stats(payload: dict):
    try:
        rows = await docker_json(["stats", "--no-stream", "--no-trunc"], timeout=30)
    except DockerError as exc:
        return _fail(exc)
    return {"stats": [normalize_stats(r) for r in rows]}


@module.on("containers.inspect")
async def containers_inspect(payload: dict):
    kind = payload.get("kind", "container")
    if kind not in ("container", "image", "volume", "network"):
        return _err("Type invalide")
    try:
        target = _image_ref(payload.get("id")) if kind == "image" else _cid(payload)
        args = ["inspect", "--type", kind, target] if kind in ("container", "image") else [kind, "inspect", target]
        _, out, _ = await docker(args)
    except DockerError as exc:
        return _fail(exc)
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        return _err("Réponse illisible de docker inspect", "PARSE_ERROR")
    return {"data": data[0] if isinstance(data, list) and len(data) == 1 else data}


async def _pump(stream_id: str, reader, kind: str, buf: list):
    while True:
        line = await reader.readline()
        if not line:
            return
        text = line.decode("utf-8", "replace").rstrip("\r\n")
        buf.append({"s": kind, "t": text[:MAX_LOG_LINE]})


async def _run_stream(stream_id: str, proc, label: str):
    buf: list = []
    pumps = [asyncio.create_task(_pump(stream_id, proc.stdout, "out", buf)),
             asyncio.create_task(_pump(stream_id, proc.stderr, "err", buf))]
    try:
        while not all(p.done() for p in pumps):
            await asyncio.sleep(0.2)
            if buf:
                chunk, buf[:] = buf[:1000], buf[1000:]
                await module.events.emit("logs:lines", {"stream_id": stream_id, "lines": chunk})
        while buf:
            chunk, buf[:] = buf[:1000], buf[1000:]
            await module.events.emit("logs:lines", {"stream_id": stream_id, "lines": chunk})
        code = await proc.wait()
        await module.events.emit("logs:end", {"stream_id": stream_id, "code": code})
    except asyncio.CancelledError:
        for p in pumps:
            p.cancel()
        raise
    finally:
        _streams.pop(stream_id, None)
        if proc.returncode is None:
            try:
                proc.kill()
            except ProcessLookupError:
                pass


@module.on("logs.start")
async def logs_start(payload: dict):
    try:
        cid = _cid(payload)
        tail = int(payload.get("tail", 300))
    except DockerError as exc:
        return _fail(exc)
    except (TypeError, ValueError):
        return _err("Nombre de lignes invalide")
    tail = max(0, min(tail, 20000))
    for sid, st in list(_streams.items()):
        if st["container"] == cid:
            await _stop_stream(sid)
    while len(_streams) >= MAX_STREAMS:
        await _stop_stream(next(iter(_streams)))
    args = ["logs", "--tail", str(tail)]
    if payload.get("follow", True):
        args.append("--follow")
    if payload.get("timestamps"):
        args.append("--timestamps")
    args.append(cid)
    try:
        proc = await _spawn(args)
    except DockerError as exc:
        return _fail(exc)
    stream_id = uuid.uuid4().hex
    task = asyncio.create_task(_run_stream(stream_id, proc, cid))
    _streams[stream_id] = {"container": cid, "proc": proc, "task": task, "started": time.time()}
    return {"stream_id": stream_id}


async def _stop_stream(stream_id: str) -> bool:
    st = _streams.pop(stream_id, None)
    if not st:
        return False
    proc = st["proc"]
    if proc.returncode is None:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
    try:
        await asyncio.wait_for(asyncio.shield(st["task"]), 3)
    except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
        st["task"].cancel()
    return True


@module.on("logs.stop")
async def logs_stop(payload: dict):
    return {"ok": await _stop_stream(str(payload.get("stream_id") or ""))}


@module.on("images.list")
async def images_list(payload: dict):
    try:
        rows = await docker_json(["images", "--no-trunc"] + (["-a"] if payload.get("all") else []))
        used_rows = await docker_json(["ps", "-a", "--no-trunc"])
    except DockerError as exc:
        return _fail(exc)
    used = {str(r.get("Image") or "") for r in used_rows}
    images = []
    for r in rows:
        img = normalize_image(r)
        img["in_use"] = img["ref"] in used or (img["repository"] in used and img["tag"] == "latest")
        images.append(img)
    images.sort(key=lambda i: (i["dangling"], i["repository"], i["tag"]))
    return {"images": images}


@module.on("images.remove")
async def images_remove(payload: dict):
    try:
        ref = _image_ref(payload.get("id"))
        await docker(["rmi", *(["-f"] if payload.get("force") else []), ref], timeout=120)
    except DockerError as exc:
        return _fail(exc)
    return {"ok": True}


@module.job("images.pull")
async def images_pull(payload: dict, job):
    ref = _image_ref(payload.get("image"))
    if ":" not in ref.rsplit("/", 1)[-1] and "@" not in ref:
        ref += ":latest"
    await job.progress(0, f"Téléchargement de {ref}…")
    proc = await _spawn(["pull", ref], merge=True)
    layers: dict[str, str] = {}
    tail: list[str] = []
    try:
        while True:
            line = await proc.stdout.readline()
            if not line:
                break
            text = line.decode("utf-8", "replace").strip()
            if not text:
                continue
            tail = (tail + [text])[-30:]
            parse_pull_line(text, layers)
            await job.progress(pull_percent(layers), text[:200], layers=len(layers),
                               done=sum(1 for s in layers.values() if s == "done"))
        code = await proc.wait()
    except asyncio.CancelledError:
        if proc.returncode is None:
            proc.kill()
        raise
    if code != 0:
        raise DockerError("PULL_FAILED", daemon_error_message("\n".join(tail)))
    log.info("pulled %s", ref)
    return {"image": ref, "output": tail[-5:]}


@module.on("volumes.list")
async def volumes_list(payload: dict):
    try:
        rows = await docker_json(["volume", "ls"])
    except DockerError as exc:
        return _fail(exc)
    return {"volumes": sorted((normalize_volume(r) for r in rows), key=lambda v: v["name"])}


@module.on("volumes.remove")
async def volumes_remove(payload: dict):
    try:
        name = _cid(payload, "name")
        await docker(["volume", "rm", *(["-f"] if payload.get("force") else []), name])
    except DockerError as exc:
        return _fail(exc)
    return {"ok": True}


@module.on("networks.list")
async def networks_list(payload: dict):
    try:
        rows = await docker_json(["network", "ls", "--no-trunc"])
    except DockerError as exc:
        return _fail(exc)
    return {"networks": sorted((normalize_network(r) for r in rows), key=lambda n: (not n["builtin"], n["name"]))}


@module.on("networks.remove")
async def networks_remove(payload: dict):
    try:
        name = _cid(payload, "name")
        if name in BUILTIN_NETWORKS:
            raise DockerError("BUILTIN", "Les réseaux intégrés (bridge, host, none) ne peuvent pas être supprimés")
        await docker(["network", "rm", name])
    except DockerError as exc:
        return _fail(exc)
    return {"ok": True}


def _saved_files_path() -> Path:
    return module.data_path("compose_files.json")


def _load_saved() -> list[str]:
    try:
        data = json.loads(_saved_files_path().read_text(encoding="utf-8"))
        return [str(p) for p in data if isinstance(p, str)][:30]
    except (OSError, ValueError):
        return []


def _save_saved(files: list[str]) -> None:
    _saved_files_path().write_text(json.dumps(files[:30], ensure_ascii=False), encoding="utf-8")


@module.on("compose.list")
async def compose_list(payload: dict):
    saved = await asyncio.to_thread(_load_saved)
    try:
        rows = await docker_json(["ps", "-a", "--no-trunc"])
    except DockerError as exc:
        return {**_fail(exc), "files": saved}
    return {"projects": compose_projects([normalize_container(r) for r in rows]),
            "files": [{"path": p, "exists": os.path.isfile(p)} for p in saved]}


@module.on("compose.forget")
async def compose_forget(payload: dict):
    target = str(payload.get("path") or "")
    files = [p for p in await asyncio.to_thread(_load_saved) if p != target]
    await asyncio.to_thread(_save_saved, files)
    return {"ok": True}


@module.job("compose.run")
async def compose_run(payload: dict, job):
    action = payload.get("action")
    commands = {"up": ["up", "-d", "--remove-orphans"], "down": ["down"], "restart": ["restart"],
                "stop": ["stop"], "start": ["start"], "pull": ["pull"]}
    if action not in commands:
        raise DockerError("BAD_ACTION", "Action Compose inconnue")
    args = ["compose"]
    cwd = None
    if payload.get("file"):
        path = _compose_file(payload.get("file"))
        args += ["-f", str(path)]
        cwd = str(path.parent)
        files = [str(path)] + [p for p in await asyncio.to_thread(_load_saved) if p != str(path)]
        await asyncio.to_thread(_save_saved, files)
    project = str(payload.get("project") or "").strip()
    if project:
        if not PROJECT_RE.match(project):
            raise DockerError("BAD_PROJECT", "Nom de projet invalide")
        args += ["-p", project]
    if not payload.get("file") and not project:
        raise DockerError("BAD_REQUEST", "Indiquez un fichier Compose ou un projet")
    if action in ("up", "pull") and not payload.get("file"):
        raise DockerError("BAD_REQUEST", f"« {action} » nécessite le fichier docker-compose")
    args += commands[action]
    await job.progress(None, "docker " + " ".join(args[:1] + args[-len(commands[action]):]))
    proc = await _spawn(args, cwd=cwd, merge=True)
    lines: list[str] = []
    try:
        while True:
            line = await proc.stdout.readline()
            if not line:
                break
            text = line.decode("utf-8", "replace").rstrip()
            if text:
                lines.append(text)
                await job.progress(None, text[:300])
        code = await proc.wait()
    except asyncio.CancelledError:
        if proc.returncode is None:
            proc.kill()
        raise
    if code != 0:
        raise DockerError("COMPOSE_FAILED", "\n".join(lines[-8:]) or "Échec de docker compose")
    log.info("compose %s (%s)", action, project or payload.get("file"))
    return {"ok": True, "output": lines[-200:]}


@module.on("compose.validate")
async def compose_validate(payload: dict):
    try:
        path = _compose_file(payload.get("file"))
    except DockerError as exc:
        return _fail(exc)
    return {"ok": True, "path": str(path)}


@module.on("prune")
async def prune(payload: dict):
    target = payload.get("target")
    commands = {
        "containers": ["container", "prune", "-f"],
        "images": ["image", "prune", "-f"] + (["-a"] if payload.get("all") else []),
        "volumes": ["volume", "prune", "-f"] + (["-a"] if payload.get("all") else []),
        "networks": ["network", "prune", "-f"],
        "builder": ["builder", "prune", "-f"],
        "system": ["system", "prune", "-f"] + (["-a"] if payload.get("all") else []),
    }
    if target not in commands:
        return _err("Cible de nettoyage inconnue", "BAD_TARGET")
    if payload.get("confirm") is not True:
        return _err("Confirmation requise", "CONFIRM_REQUIRED")
    try:
        _, out, _ = await docker(commands[target], timeout=600)
    except DockerError as exc:
        if exc.code == "DOCKER_ERROR" and target == "volumes" and payload.get("all"):
            try:
                _, out, _ = await docker(commands[target][:-1], timeout=600)
            except DockerError as exc2:
                return _fail(exc2)
        else:
            return _fail(exc)
    m = re.search(r"Total reclaimed space:\s*(.+)", out)
    log.info("prune %s", target)
    return {"ok": True, "reclaimed": m.group(1).strip() if m else "0B", "output": out.strip()[-4000:]}


@module.on("disk_usage")
async def disk_usage(payload: dict):
    try:
        rows = await docker_json(["system", "df"])
    except DockerError as exc:
        return _fail(exc)
    return {"usage": [{"type": r.get("Type", ""), "total": r.get("TotalCount", ""), "active": r.get("Active", ""),
                       "size": r.get("Size", ""), "reclaimable": r.get("Reclaimable", "")} for r in rows]}


@module.on_shutdown
async def cleanup():
    for sid in list(_streams):
        await _stop_stream(sid)
