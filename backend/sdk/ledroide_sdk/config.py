from typing import Any, Callable


class _Config:
    """Module config, persisted by the registry (configs.json in the data dir)."""

    def __init__(self, defaults: dict | None = None):
        self._defaults: dict = dict(defaults or {})
        self._values: dict = {}
        self._save_fn: Callable | None = None

    def _inject(self, values: dict, save_fn: Callable):
        self._values = values
        self._save_fn = save_fn

    def get(self, key: str, default: Any = None) -> Any:
        if key in self._values:
            return self._values[key]
        return self._defaults.get(key, default)

    def set(self, key: str, value: Any):
        self._values[key] = value
        if self._save_fn:
            self._save_fn(self._values)

    def all(self) -> dict:
        return {**self._defaults, **self._values}


config = _Config()
