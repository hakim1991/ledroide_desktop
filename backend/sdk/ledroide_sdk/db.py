from typing import Callable


class _DB:
    """
    Injected by the registry at module load time.
    Routes calls to the appropriate adapter (CouchDB / PostgreSQL) — Phase 3.
    """

    def __init__(self):
        self._proxy = None

    def _inject(self, proxy):
        self._proxy = proxy

    async def save(self, collection: str, document: dict) -> dict:
        self._assert_ready()
        return await self._proxy.save(collection, document)

    async def query(self, collection: str, filters: dict = None) -> list:
        self._assert_ready()
        return await self._proxy.query(collection, filters or {})

    async def delete(self, collection: str, doc_id: str) -> dict:
        self._assert_ready()
        return await self._proxy.delete(collection, doc_id)

    def _assert_ready(self):
        if not self._proxy:
            raise RuntimeError("DB not available — Phase 3 not yet implemented")


db = _DB()
