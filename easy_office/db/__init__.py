"""SQLite database bootstrap and shared connection."""

from easy_office.db.database import Database, VectorDimensionMismatch

__all__ = ["Database", "VectorDimensionMismatch"]
