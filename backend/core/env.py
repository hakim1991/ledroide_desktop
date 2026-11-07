import json
import logging
import os
import re

from pydantic import BaseModel, Field, field_validator

from core import settings
from core.errors import LedroideError

log = logging.getLogger("ledroide.env")

ENV_FILE = settings.DATA_DIR / "env.json"
KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")
# Overriding these would break the backend, pip or the Electron/Python handshake.
PROTECTED = {"PATH", "PYTHONPATH", "PYTHONHOME", "SYSTEMROOT", "COMSPEC", "PATHEXT", "TEMP", "TMP"}


class EnvVar(BaseModel):
    key: str
    value: str = ""
    secret: bool = False
    description: str = ""

    @field_validator("key")
    @classmethod
    def _check_key(cls, key: str) -> str:
        if not KEY_RE.match(key):
            raise ValueError(f"Nom de variable invalide : {key!r}")
        if key.upper() in PROTECTED or key.upper().startswith("LEDROIDE_"):
            raise ValueError(f"Variable réservée : {key}")
        return key


class EnvPayload(BaseModel):
    vars: list[EnvVar] = Field(default_factory=list)


class EnvStore:
    """User-defined variables, persisted in the data dir and applied to os.environ
    so module code and the subprocesses it spawns see them."""

    def __init__(self):
        self._vars: list[EnvVar] = []
        self._originals: dict[str, str | None] = {}
        self._load()
        self._apply()

    def _load(self):
        try:
            raw = json.loads(ENV_FILE.read_text(encoding="utf-8"))
            self._vars = EnvPayload.model_validate(raw).vars
        except FileNotFoundError:
            self._vars = []
        except Exception:
            log.exception("unreadable %s, ignoring", ENV_FILE)
            self._vars = []

    def _save(self):
        tmp = ENV_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(EnvPayload(vars=self._vars).model_dump(), indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, ENV_FILE)

    def _apply(self):
        wanted = {v.key: v.value for v in self._vars}
        for key in list(self._originals):
            if key not in wanted:
                original = self._originals.pop(key)
                if original is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = original
        for key, value in wanted.items():
            if key not in self._originals:
                self._originals[key] = os.environ.get(key)
            os.environ[key] = value

    def all(self) -> list[dict]:
        return [v.model_dump() for v in self._vars]

    def keys(self) -> set[str]:
        return {v.key for v in self._vars}

    def replace(self, payload: EnvPayload) -> tuple[list[dict], list[str]]:
        seen: set[str] = set()
        for v in payload.vars:
            if v.key in seen:
                raise LedroideError("DUPLICATE_ENV_KEY", f"Variable en double : {v.key}", 422)
            seen.add(v.key)
        before = {v.key: v.value for v in self._vars}
        self._vars = list(payload.vars)
        self._save()
        self._apply()
        after = {v.key: v.value for v in self._vars}
        changed = sorted(k for k in before.keys() | after.keys() if before.get(k) != after.get(k))
        log.info("env updated: %d variable(s), %d changed", len(self._vars), len(changed))
        return self.all(), changed
