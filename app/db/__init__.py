"""
Database package for System Monitor.
"""

from app.db.connection import get_db_connection, init_db

__all__ = ["get_db_connection", "init_db"]
