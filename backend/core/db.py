from ledroide_sdk.db import SQLiteAdapter

SUPPORTED_DB_TYPES = {"none", "sqlite"}
RESERVED_DB_TYPES = {"couchdb", "postgresql"}

__all__ = ["SQLiteAdapter", "SUPPORTED_DB_TYPES", "RESERVED_DB_TYPES"]
