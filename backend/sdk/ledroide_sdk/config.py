from typing import Any, Callable


class _Config:
    """Read/write module config persisted by the registry in backend/configs.json."""

    _values: dict = {}
    _save_fn: Callable = None

    def _inject(self, values: dict, save_fn: Callable):
        self._values = values
        self._save_fn = save_fn

    def get(self, key: str, default: Any = None) -> Any:
        return self._values.get(key, default)

    def set(self, key: str, value: Any):
        self._values[key] = value
        if self._save_fn:
            self._save_fn(self._values)

    def all(self) -> dict:
        return dict(self._values)


config = _Config()
