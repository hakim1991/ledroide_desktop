"""Modules catalog hosted in a GitHub repository (one folder per module at the root).

The repository is downloaded as a zip archive and extracted into
`<data>/remote-catalog/`; the registry lists the modules found there that are
not present locally and installs them by copying their folder.
"""
import asyncio
import json
import logging
import os
import shutil
import stat
import time
import urllib.error
import urllib.request
import zipfile
from io import BytesIO
from pathlib import Path

from core import settings
from core.errors import LedroideError

log = logging.getLogger("ledroide.catalog")

MAX_ARCHIVE_BYTES = 200 * 1024 * 1024
CACHE_TTL = 15 * 60
TOKEN_VARS = ("LEDROIDE_GITHUB_TOKEN", "GITHUB_TOKEN", "GH_TOKEN")


def _rmtree(path: Path):
    def onerror(func, p, _exc):
        os.chmod(p, stat.S_IWRITE)
        func(p)
    if path.exists():
        shutil.rmtree(path, onerror=onerror)


def _token() -> str | None:
    for name in TOKEN_VARS:
        value = (os.environ.get(name) or "").strip()
        if value:
            return value
    return None


class _KeepAuthRedirect(urllib.request.HTTPRedirectHandler):
    """api.github.com answers a zipball request with a redirect to codeload.github.com
    carrying its own short-lived token: the Authorization header must not follow it
    to another host."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is not None and urllib.request.urlparse(newurl).hostname != urllib.request.urlparse(req.full_url).hostname:
            new.headers.pop("Authorization", None)
            new.unredirected_hdrs.pop("Authorization", None)
        return new


class RemoteCatalog:
    def __init__(self):
        self.root = settings.REMOTE_CATALOG_DIR
        self.state_file = settings.DATA_DIR / "remote-catalog.json"
        self._lock = asyncio.Lock()
        self.status: dict = self._read_status()

    @property
    def repo(self) -> str:
        return settings.CATALOG_REPO

    @property
    def branch(self) -> str:
        return settings.CATALOG_BRANCH

    def _read_status(self) -> dict:
        try:
            return json.loads(self.state_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}

    def _write_status(self):
        tmp = self.state_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.status, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, self.state_file)

    def info(self) -> dict:
        return {
            "repo": self.repo,
            "branch": self.branch,
            "enabled": bool(self.repo),
            "has_token": _token() is not None,
            "updated_at": self.status.get("updated_at"),
            "sha": self.status.get("sha"),
            "count": self.status.get("count", 0),
            "error": self.status.get("error"),
            "code": self.status.get("code"),
        }

    def module_dirs(self) -> dict[str, Path]:
        """Module folders of the last successful download."""
        content = self.root / "content"
        out: dict[str, Path] = {}
        if not content.is_dir():
            return out
        for d in sorted(content.iterdir()):
            if d.is_dir() and (d / "manifest.json").is_file():
                out[d.name] = d
        return out

    def is_stale(self) -> bool:
        return not self.status.get("updated_at") or time.time() - self.status.get("checked_at", 0) > CACHE_TTL

    async def refresh(self, force: bool = False) -> dict:
        if not self.repo:
            return self.info()
        async with self._lock:
            if not force and not self.is_stale() and not self.status.get("error"):
                return self.info()
            try:
                sha = await asyncio.to_thread(self._download)
                self.status.update(sha=sha, updated_at=time.time(), error=None, code=None,
                                   count=len(self.module_dirs()))
                log.info("remote catalog %s@%s: %d module(s)", self.repo, self.branch, self.status["count"])
            except LedroideError as exc:
                self.status.update(error=exc.message, code=exc.code)
                log.warning("remote catalog refresh failed: %s", exc.message)
            self.status["checked_at"] = time.time()
            self._write_status()
            return self.info()

    def _request(self, url: str):
        headers = {"Accept": "application/vnd.github+json", "User-Agent": "LedroideDesktop",
                   "X-GitHub-Api-Version": "2022-11-28"}
        token = _token()
        if token:
            headers["Authorization"] = f"Bearer {token}"
        opener = urllib.request.build_opener(_KeepAuthRedirect)
        try:
            return opener.open(urllib.request.Request(url, headers=headers), timeout=30)
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403):
                raise LedroideError("CATALOG_AUTH", "Accès refusé au dépôt de modules : jeton GitHub invalide ou "
                                    "sans droit de lecture (variable GITHUB_TOKEN)", 502)
            if exc.code == 404:
                hint = "" if token else " — dépôt privé : définissez GITHUB_TOKEN dans « Variables »"
                raise LedroideError("CATALOG_NOT_FOUND", f"Dépôt {self.repo} (branche {self.branch}) introuvable{hint}", 502)
            raise LedroideError("CATALOG_HTTP", f"GitHub a répondu {exc.code}", 502)
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            reason = getattr(exc, "reason", exc)
            raise LedroideError("CATALOG_OFFLINE", f"Dépôt de modules injoignable : {reason}", 502)

    def _download(self) -> str:
        api = f"https://api.github.com/repos/{self.repo}"
        with self._request(f"{api}/commits/{self.branch}") as r:
            sha = json.loads(r.read().decode("utf-8")).get("sha", "")
        if sha and sha == self.status.get("sha") and self.module_dirs():
            return sha
        with self._request(f"{api}/zipball/{sha or self.branch}") as r:
            data = r.read(MAX_ARCHIVE_BYTES + 1)
        if len(data) > MAX_ARCHIVE_BYTES:
            raise LedroideError("CATALOG_TOO_LARGE", "Archive du dépôt trop volumineuse", 502)
        self._extract(data)
        return sha

    def _extract(self, data: bytes):
        self.root.mkdir(parents=True, exist_ok=True)
        tmp = self.root / "content.tmp"
        _rmtree(tmp)
        tmp.mkdir()
        try:
            zf = zipfile.ZipFile(BytesIO(data))
        except zipfile.BadZipFile:
            raise LedroideError("CATALOG_BAD_ARCHIVE", "Archive du dépôt illisible", 502)
        tmp_resolved = tmp.resolve()
        with zf:
            for info in zf.infolist():
                # GitHub zipballs wrap everything in a single "<owner>-<repo>-<sha>/" folder.
                parts = info.filename.split("/", 1)
                if len(parts) < 2 or not parts[1]:
                    continue
                dest = (tmp / parts[1]).resolve()
                if tmp_resolved not in dest.parents:
                    raise LedroideError("CATALOG_BAD_ARCHIVE", f"Chemin interdit dans l'archive : {info.filename}", 502)
                if info.is_dir():
                    dest.mkdir(parents=True, exist_ok=True)
                    continue
                dest.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(info) as src, open(dest, "wb") as out:
                    shutil.copyfileobj(src, out)
        content = self.root / "content"
        old = self.root / "content.old"
        _rmtree(old)
        if content.exists():
            os.replace(content, old)
        os.replace(tmp, content)
        _rmtree(old)
