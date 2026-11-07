from .module import Job, Module, data_path
from .db import db, SQLiteAdapter
from .events import events
from .config import config


def env(key: str, default: str | None = None) -> str | None:
    """Variable defined in the app's « Variables » menu (applied to os.environ)."""
    import os
    return os.environ.get(key, default)


# `events`, `config`, `db` are placeholders rebound per module by the registry;
# prefer `module.events`, `module.config`, `module.db`, `module.router`.
__all__ = ["Module", "Job", "db", "events", "config", "SQLiteAdapter", "data_path", "env"]
