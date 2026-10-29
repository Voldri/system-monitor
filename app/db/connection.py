"""
Модуль роботи зі з'єднаннями SQLite.
Забезпечує WAL-режим, thread-safety, таймаути очікування та ініціалізацію схеми.
"""

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Generator, Optional

from app.config import DB_PATH

SCHEMA_PATH = Path(__file__).resolve().parent / "schema.sql"

def get_db_connection(db_path: Optional[Path] = None) -> sqlite3.Connection:
    """
    Створює та налаштовує з'єднання з базою SQLite:
    - row_factory = sqlite3.Row для доступу за назвами колонок
    - WAL-режим для безпечного паралельного читання і запису
    - timeout 5000 мс для уникнення помилок 'database is locked'
    """
    path = str(db_path or DB_PATH)
    conn = sqlite3.connect(path, timeout=5.0)
    conn.row_factory = sqlite3.Row

    # Налаштування PRAGMA для максимальної надійності та швидкодії
    cursor = conn.cursor()
    cursor.execute("PRAGMA journal_mode = WAL;")
    cursor.execute("PRAGMA synchronous = NORMAL;")
    cursor.execute("PRAGMA foreign_keys = ON;")
    cursor.execute("PRAGMA busy_timeout = 5000;")
    cursor.close()

    return conn

@contextmanager
def db_session(db_path: Optional[Path] = None) -> Generator[sqlite3.Connection, None, None]:
    """
    Контекстний менеджер для транзакцій бази даних.
    Автоматично робить commit при успіху або rollback при винятку.
    """
    conn = get_db_connection(db_path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

def init_db(db_path: Optional[Path] = None) -> None:
    """
    Ініціалізує таблиці та індекси з файлу schema.sql.
    """
    path = db_path or DB_PATH
    with open(SCHEMA_PATH, "r", encoding="utf-8") as f:
        schema_sql = f.read()

    with db_session(path) as conn:
        conn.executescript(schema_sql)
