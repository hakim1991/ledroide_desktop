import asyncio
import logging
import os
import re
import shutil
import subprocess
import sys
import time

from ledroide_sdk import Module

log = logging.getLogger("ledroide.git-viewer")

module = Module(__file__)
router = module.router

# Read-only subcommands only; `fetch` is the single exception and is only run on explicit request.
ALLOWED = {"version", "rev-parse", "status", "log", "show", "for-each-ref", "blame", "ls-files", "ls-tree",
           "stash", "rev-list", "fetch", "remote"}
# Neutralise repo-level settings that could run external programs from read-only commands.
SAFE_CONFIG = ["-c", "core.quotepath=false", "-c", "color.ui=never", "-c", "core.pager=cat",
               "-c", "core.fsmonitor=false", "-c", "i18n.logOutputEncoding=UTF-8", "-c", "log.showSignature=false",
               "-c", "diff.external=", "-c", "core.untrackedCache=false", "-c", "gc.auto=0"]
REF_RE = re.compile(r"^[A-Za-z0-9._/@{}~^+-]{1,250}$")
SHA_RE = re.compile(r"^[0-9a-fA-F]{4,64}$")
STASH_RE = re.compile(r"^stash@\{\d{1,5}\}$")
SEP, REC = "\x1f", "\x1e"


class GitError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _git_exe() -> str:
    exe = shutil.which("git")
    if not exe:
        raise GitError("GIT_MISSING", "La commande git est introuvable. Installez Git (https://git-scm.com) "
                                      "puis relancez l'application.")
    return exe


def _run(repo: str | None, args: list[str], timeout: float = 30, allow_fail: bool = False) -> str:
    if not args or args[0] not in ALLOWED:
        raise GitError("FORBIDDEN", "Commande git non autorisée")
    if args[0] == "stash" and args[1:2] != ["list"]:
        raise GitError("FORBIDDEN", "Seul « git stash list » est autorisé")
    if args[0] == "remote" and len(args) > 1 and args[1] not in ("-v",):
        raise GitError("FORBIDDEN", "Seul « git remote -v » est autorisé")
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_OPTIONAL_LOCKS": "0", "GCM_INTERACTIVE": "never",
           "GIT_PAGER": "cat", "LC_ALL": "C", "LANG": "C", "GIT_ASKPASS": "", "SSH_ASKPASS": ""}
    env.pop("GIT_DIR", None)
    env.pop("GIT_WORK_TREE", None)
    flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    try:
        proc = subprocess.run([_git_exe(), *SAFE_CONFIG, *args], cwd=repo, capture_output=True, timeout=timeout,
                              env=env, creationflags=flags, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        raise GitError("TIMEOUT", f"git {args[0]} : délai dépassé ({timeout:g} s)")
    except OSError as exc:
        raise GitError("GIT_FAILED", f"Impossible de lancer git : {exc}")
    out = proc.stdout.decode("utf-8", "replace")
    if proc.returncode != 0 and not allow_fail:
        err = proc.stderr.decode("utf-8", "replace").strip()
        if "dubious ownership" in err:
            raise GitError("UNSAFE_REPO", "Git refuse ce dépôt (propriétaire différent). Ajoutez-le avec "
                                          "« git config --global --add safe.directory <chemin> » si vous lui faites confiance.")
        if "not a git repository" in err.lower():
            raise GitError("NOT_A_REPO", "Ce dossier n'est pas un dépôt Git")
        raise GitError("GIT_ERROR", err.splitlines()[-1] if err else f"git {args[0]} a échoué ({proc.returncode})")
    return out


def _repo(path: str) -> str:
    path = (path or "").strip().strip('"')
    if not path:
        raise GitError("PATH_REQUIRED", "Indiquez le dossier du dépôt")
    if not os.path.isabs(path):
        raise GitError("BAD_PATH", "Le chemin doit être absolu")
    if not os.path.isdir(path):
        raise GitError("NOT_FOUND", f"Dossier introuvable : {path}")
    root = _run(path, ["rev-parse", "--show-toplevel"]).strip()
    if not root:
        raise GitError("NOT_A_REPO", "Dépôt sans arbre de travail (dépôt nu) : non pris en charge")
    return os.path.normpath(root)


def _ref(ref: str | None) -> str | None:
    ref = (ref or "").strip()
    if not ref or ref == "--all":
        return None
    if ref.startswith("-") or not REF_RE.match(ref):
        raise GitError("BAD_REF", f"Référence invalide : {ref}")
    return ref


def _sha(value: str) -> str:
    value = (value or "").strip()
    if not (SHA_RE.match(value) or STASH_RE.match(value)):
        raise GitError("BAD_SHA", "Identifiant de commit invalide")
    return value


def _rel_file(root: str, path: str, must_exist: bool) -> str:
    p = (path or "").strip().replace("\\", "/").lstrip("/")
    if not p or p.startswith("-") or any(part == ".." for part in p.split("/")) or re.match(r"^[A-Za-z]:", p):
        raise GitError("BAD_FILE", "Chemin de fichier invalide (relatif au dépôt attendu)")
    full = os.path.realpath(os.path.join(root, p))
    if not full.startswith(os.path.realpath(root).rstrip(os.sep) + os.sep):
        raise GitError("BAD_FILE", "Le fichier doit être dans le dépôt")
    if must_exist and not os.path.isfile(full):
        raise GitError("FILE_NOT_FOUND", f"Fichier introuvable : {p}")
    return p


# ── Parsers ──────────────────────────────────────────────────────────────────

def _parse_status(out: str) -> dict:
    branch = {"head": None, "oid": None, "upstream": None, "ahead": 0, "behind": 0}
    staged, unstaged, untracked, conflicts = [], [], [], []
    parts = out.split("\0")
    i = 0
    labels = {"M": "modifié", "A": "ajouté", "D": "supprimé", "R": "renommé", "C": "copié", "T": "type modifié", "U": "conflit"}
    while i < len(parts):
        line = parts[i]
        i += 1
        if not line:
            continue
        if line.startswith("# "):
            k, _, v = line[2:].partition(" ")
            if k == "branch.head":
                branch["head"] = None if v == "(detached)" else v
            elif k == "branch.oid":
                branch["oid"] = None if v == "(initial)" else v
            elif k == "branch.upstream":
                branch["upstream"] = v
            elif k == "branch.ab":
                m = re.match(r"\+(\d+) -(\d+)", v)
                if m:
                    branch["ahead"], branch["behind"] = int(m[1]), int(m[2])
        elif line[0] in "12":
            fields = line.split(" ", 8 if line[0] == "1" else 9)
            xy, path = fields[1], fields[-1]
            orig = None
            if line[0] == "2":
                orig = parts[i]
                i += 1
            x, y = xy[0], xy[1]
            if x != ".":
                staged.append({"path": path, "orig": orig, "code": x, "label": labels.get(x, x)})
            if y != ".":
                unstaged.append({"path": path, "orig": orig, "code": y, "label": labels.get(y, y)})
        elif line[0] == "u":
            conflicts.append({"path": line.split(" ", 10)[-1], "code": line.split(" ")[1]})
        elif line[0] == "?":
            untracked.append({"path": line[2:]})
    return {"branch": branch, "staged": staged, "unstaged": unstaged, "untracked": untracked, "conflicts": conflicts,
            "clean": not (staged or unstaged or untracked or conflicts)}


def _parse_log(out: str, limit: int) -> tuple[list[dict], bool]:
    rows, commits = [], 0
    more = False
    for line in out.split("\n"):
        if REC in line:
            graph, _, rest = line.partition(REC)
            f = rest.split(SEP)
            if len(f) < 7:
                continue
            commits += 1
            if commits > limit:
                more = True
                break
            rows.append({"graph": graph, "sha": f[0], "parents": f[1].split() if f[1] else [], "author": f[2],
                         "email": f[3], "time": int(f[4] or 0), "refs": [r.strip() for r in f[5].split(",") if r.strip()],
                         "subject": SEP.join(f[6:])})
        else:
            rows.append({"graph": line})
    if more:
        while rows and "sha" not in rows[-1]:
            rows.pop()
    return rows, more


def _unquote(p: str) -> str:
    if len(p) >= 2 and p[0] == '"' and p[-1] == '"':
        try:
            return p[1:-1].encode("latin-1", "backslashreplace").decode("unicode_escape").encode("latin-1").decode("utf-8", "replace")
        except (UnicodeDecodeError, UnicodeEncodeError):
            return p[1:-1]
    return p


def _parse_patch(out: str, max_lines_per_file: int = 4000) -> list[dict]:
    files: list[dict] = []
    cur = None
    for line in out.split("\n"):
        if line.startswith("diff --git ") or line.startswith("diff --cc ") or line.startswith("diff --combined "):
            m = re.match(r'^diff --git (?:"?a/)(.+?)"? (?:"?b/)(.+?)"?$', line)
            old = new = None
            if m:
                old, new = m[1], m[2]
            else:
                new = old = line.split(" ", 2)[-1]
            cur = {"old": old, "new": new, "status": "M", "binary": False, "add": 0, "del": 0, "lines": [],
                   "truncated": False, "header": []}
            files.append(cur)
            continue
        if cur is None:
            continue
        if not cur["lines"] and not line.startswith("@@"):
            if line.startswith("new file mode"):
                cur["status"] = "A"
            elif line.startswith("deleted file mode"):
                cur["status"] = "D"
            elif line.startswith("rename from "):
                cur["status"] = "R"
                cur["old"] = _unquote(line[12:])
            elif line.startswith("rename to "):
                cur["new"] = _unquote(line[10:])
            elif line.startswith("copy from "):
                cur["status"] = "C"
                cur["old"] = _unquote(line[10:])
            elif line.startswith("copy to "):
                cur["new"] = _unquote(line[8:])
            elif line.startswith("Binary files") or line.startswith("GIT binary patch"):
                cur["binary"] = True
            elif line.startswith("--- "):
                p = _unquote(line[4:].rstrip("\t"))
                if p != "/dev/null":
                    cur["old"] = p[2:] if p.startswith("a/") else p
            elif line.startswith("+++ "):
                p = _unquote(line[4:].rstrip("\t"))
                if p != "/dev/null":
                    cur["new"] = p[2:] if p.startswith("b/") else p
            elif line.startswith(("old mode", "new mode", "similarity index", "index ")):
                cur["header"].append(line)
            continue
        if line.startswith("+") and not line.startswith("+++ "):
            cur["add"] += 1
        elif line.startswith("-") and not line.startswith("--- "):
            cur["del"] += 1
        if len(cur["lines"]) < max_lines_per_file:
            cur["lines"].append(line)
        else:
            cur["truncated"] = True
    for f in files:
        while f["lines"] and f["lines"][-1] == "":
            f["lines"].pop()
        f["path"] = f["new"] if f["status"] != "D" else f["old"]
    return files


def _parse_blame(out: str) -> list[dict]:
    commits: dict[str, dict] = {}
    lines = []
    cur = None
    it = iter(out.split("\n"))
    for line in it:
        if not line:
            continue
        if line.startswith("\t"):
            if cur:
                lines.append({"sha": cur, "text": line[1:].rstrip("\r")})
            continue
        head = line.split(" ")
        if len(head) >= 3 and re.match(r"^[0-9a-f]{40,64}$", head[0]):
            cur = head[0]
            commits.setdefault(cur, {})
            continue
        k, _, v = line.partition(" ")
        if cur and k in ("author", "author-mail", "author-time", "summary"):
            commits[cur][k] = v
    for ln in lines:
        c = commits.get(ln["sha"], {})
        ln.update(author=c.get("author", ""), time=int(c.get("author-time", 0) or 0), summary=c.get("summary", ""))
    return lines


# ── Actions (blocking, run in a thread) ──────────────────────────────────────

def _status(repo: str) -> dict:
    root = _repo(repo)
    st = _parse_status(_run(root, ["status", "--porcelain=v2", "--branch", "-z", "--untracked-files=all"]))
    remotes = _run(root, ["remote", "-v"], allow_fail=True)
    seen = {}
    for line in remotes.split("\n"):
        parts = line.split()
        if len(parts) >= 2:
            seen.setdefault(parts[0], re.sub(r"//[^/@]+@", "//", parts[1]))
    count = _run(root, ["rev-list", "--count", "HEAD"], allow_fail=True).strip()
    return {"root": root, "name": os.path.basename(root), **st, "remotes": seen,
            "commits": int(count) if count.isdigit() else 0}


def _log(repo: str, p: dict, page_size: int) -> dict:
    root = _repo(repo)
    skip = max(0, int(p.get("skip") or 0))
    limit = max(1, min(1000, int(p.get("limit") or page_size)))
    fmt = f"--format={REC}%H{SEP}%P{SEP}%an{SEP}%ae{SEP}%at{SEP}%D{SEP}%s"
    args = ["log", fmt, f"--skip={skip}", f"-n{limit + 1}", "--date-order", "--no-textconv"]
    q = p.get("search") or {}
    text = str(q.get("text") or "").strip()
    field = q.get("field") or "message"
    path = None
    if text:
        if field == "message":
            args += [f"--grep={text}", "-i", "--fixed-strings"]
        elif field == "author":
            args += [f"--author={text}", "-i", "--fixed-strings"]
        elif field == "code":
            args += [f"-S{text}"]
        elif field == "file":
            path = _rel_file(root, text, False)
        else:
            raise GitError("BAD_SEARCH", "Type de recherche inconnu")
    if p.get("graph", True) and not text:
        args.append("--graph")
    ref = _ref(p.get("ref"))
    args += [ref] if ref else ["--exclude=refs/stash", "--all"]
    args.append("--")
    if path:
        args.append(path)
    out = _run(root, args, timeout=60, allow_fail=True)
    if not out.strip():
        head = _run(root, ["rev-parse", "--verify", "-q", "HEAD"], allow_fail=True).strip()
        if not head:
            return {"rows": [], "more": False, "skip": skip, "empty_repo": True}
        if ref:
            if not _run(root, ["rev-parse", "--verify", "-q", f"{ref}^{{commit}}"], allow_fail=True).strip():
                raise GitError("BAD_REF", f"Référence inconnue : {ref}")
    rows, more = _parse_log(out, limit)
    return {"rows": rows, "more": more, "skip": skip, "limit": limit}


def _commit(repo: str, sha: str, max_bytes: int) -> dict:
    root = _repo(repo)
    sha = _sha(sha)
    meta = _run(root, ["show", "-s", "--no-show-signature",
                       "--format=%H%x00%P%x00%an%x00%ae%x00%at%x00%cn%x00%ce%x00%ct%x00%D%x00%B", sha, "--"])
    f = meta.split("\0")
    if len(f) < 10:
        raise GitError("BAD_SHA", "Commit introuvable")
    patch = _run(root, ["show", "--format=", "--patch", "--find-renames", "--no-color", "--no-ext-diff",
                        "--no-textconv", "--diff-merges=first-parent", "--submodule=short", f[0], "--"], timeout=60)
    truncated = len(patch.encode("utf-8", "replace")) > max_bytes
    if truncated:
        patch = patch.encode("utf-8", "replace")[:max_bytes].decode("utf-8", "ignore")
    files = _parse_patch(patch)
    return {"sha": f[0], "parents": f[1].split(), "author": f[2], "email": f[3], "time": int(f[4] or 0),
            "committer": f[5], "committer_email": f[6], "commit_time": int(f[7] or 0),
            "refs": [r.strip() for r in f[8].split(",") if r.strip()], "message": "\0".join(f[9:]).rstrip(),
            "files": files, "truncated": truncated,
            "stats": {"files": len(files), "add": sum(x["add"] for x in files), "del": sum(x["del"] for x in files)}}


def _refs(repo: str) -> dict:
    root = _repo(repo)
    fmt = ("%(refname)%00%(refname:short)%00%(objectname:short)%00%(*objectname:short)%00%(upstream:short)%00"
           "%(upstream:track)%00%(creatordate:unix)%00%(subject)%00%(HEAD)%00%(objecttype)")
    out = _run(root, ["for-each-ref", f"--format={fmt}", "--sort=-creatordate", "refs/heads", "refs/remotes", "refs/tags"])
    branches, remotes, tags = [], [], []
    for line in out.split("\n"):
        f = line.split("\0")
        if len(f) < 10:
            continue
        full, short = f[0], f[1]
        item = {"ref": full, "name": short, "sha": f[3] or f[2], "upstream": f[4], "track": f[5].strip("[]"),
                "time": int(f[6] or 0), "subject": f[7], "head": f[8] == "*", "annotated": f[9] == "tag"}
        if full.startswith("refs/heads/"):
            branches.append(item)
        elif full.startswith("refs/remotes/"):
            if not full.endswith("/HEAD"):
                remotes.append(item)
        else:
            tags.append(item)
    stash = []
    sout = _run(root, ["stash", "list", "--format=%gd%x00%H%x00%at%x00%gs"], allow_fail=True)
    for line in sout.split("\n"):
        f = line.split("\0")
        if len(f) >= 4:
            stash.append({"ref": f[0], "sha": f[1], "time": int(f[2] or 0), "subject": f[3]})
    return {"branches": branches, "remotes": remotes, "tags": tags, "stash": stash}


def _files(repo: str, rev: str | None) -> dict:
    root = _repo(repo)
    ref = _ref(rev)
    if ref:
        out = _run(root, ["ls-tree", "-r", "--name-only", "-z", ref])
    else:
        out = _run(root, ["ls-files", "-z"])
    files = [x for x in out.split("\0") if x]
    return {"files": files[:50000], "total": len(files)}


def _blame(repo: str, path: str, rev: str | None) -> dict:
    root = _repo(repo)
    ref = _ref(rev)
    rel = _rel_file(root, path, must_exist=ref is None)
    args = ["blame", "--porcelain", "--no-textconv"]
    if ref:
        args.append(ref)
    args += ["--", rel]
    out = _run(root, args, timeout=90)
    lines = _parse_blame(out)
    if len(lines) > 30000:
        lines = lines[:30000]
    return {"path": rel, "rev": ref, "lines": lines}


def _fetch(repo: str) -> dict:
    root = _repo(repo)
    start = time.time()
    _run(root, ["fetch", "--all", "--no-recurse-submodules", "--quiet"], timeout=180)
    return {"ok": True, "duration_s": round(time.time() - start, 1)}


# ── Handlers ─────────────────────────────────────────────────────────────────

async def _guard(fn, *args):
    try:
        return await asyncio.to_thread(fn, *args)
    except GitError as exc:
        return {"error": exc.message, "code": exc.code}
    except (TypeError, ValueError) as exc:
        return {"error": f"Paramètre invalide : {exc}", "code": "BAD_REQUEST"}


async def _remember(root: str):
    try:
        found = [d for d in await module.db.query("recents") if os.path.normcase(d["path"]) == os.path.normcase(root)]
        doc = {"path": root, "name": os.path.basename(root), "at": time.time()}
        if found:
            doc["_id"] = found[0]["_id"]
        await module.db.save("recents", doc)
        for old in (await module.db.query("recents"))[12:]:
            await module.db.delete("recents", old["_id"])
    except Exception:
        log.exception("recents update failed")


@module.on("check")
async def check(payload: dict):
    def run():
        return {"version": _run(None, ["version"]).strip(), "path": _git_exe()}
    return await _guard(run)


@module.on("open")
async def open_repo(payload: dict):
    res = await _guard(_status, str(payload.get("path") or ""))
    if "error" not in res:
        await _remember(res["root"])
    return res


@module.on("status")
async def status(payload: dict):
    return await _guard(_status, str(payload.get("path") or ""))


@module.on("log")
async def log_(payload: dict):
    return await _guard(_log, str(payload.get("path") or ""), payload, int(module.config.get("page_size", 100)))


@module.on("commit")
async def commit(payload: dict):
    return await _guard(_commit, str(payload.get("path") or ""), str(payload.get("sha") or ""),
                        int(module.config.get("max_diff_kb", 2048)) * 1024)


@module.on("refs")
async def refs(payload: dict):
    return await _guard(_refs, str(payload.get("path") or ""))


@module.on("files")
async def files(payload: dict):
    return await _guard(_files, str(payload.get("path") or ""), payload.get("rev"))


@module.on("blame")
async def blame(payload: dict):
    return await _guard(_blame, str(payload.get("path") or ""), str(payload.get("file") or ""), payload.get("rev"))


@module.on("fetch")
async def fetch(payload: dict):
    res = await _guard(_fetch, str(payload.get("path") or ""))
    log.info("git fetch: %s", "ok" if "error" not in res else res["code"])
    return res


@module.on("recents")
async def recents(payload: dict):
    items = await module.db.query("recents")
    return {"items": [{"_id": d["_id"], "path": d["path"], "name": d.get("name") or os.path.basename(d["path"]),
                       "exists": os.path.isdir(d["path"])} for d in items]}


@module.on("recent_delete")
async def recent_delete(payload: dict):
    return await module.db.delete("recents", str(payload.get("id", "")))
