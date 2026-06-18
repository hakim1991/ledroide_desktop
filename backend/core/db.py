"""
Database abstraction layer.
Routes to CouchDB or PostgreSQL based on the module manifest's database.type field.
Phase 3: implement concrete adapters.
"""


class DatabaseProxy:
    def __init__(self, db_type: str, module_id: str):
        self.db_type = db_type
        self.module_id = module_id

    async def save(self, collection: str, document: dict) -> dict:
        raise NotImplementedError(f"DB adapter '{self.db_type}' not yet implemented (Phase 3)")

    async def query(self, collection: str, filters: dict = None) -> list:
        raise NotImplementedError(f"DB adapter '{self.db_type}' not yet implemented (Phase 3)")

    async def delete(self, collection: str, doc_id: str) -> dict:
        raise NotImplementedError(f"DB adapter '{self.db_type}' not yet implemented (Phase 3)")


def get_db(module_id: str, db_type: str) -> DatabaseProxy:
    return DatabaseProxy(db_type=db_type, module_id=module_id)
