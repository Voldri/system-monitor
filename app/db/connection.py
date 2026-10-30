"""
Модуль роботи зі з'єднаннями SQLite.
Забезпечує WAL-режим, thread-safety, таймаути очікування та ініціалізацію схеми.
"""

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Generator, Optional

from app.config import BUNDLE_DIR, DB_PATH

SCHEMA_PATH = Path(__file__).resolve().parent / "schema.sql"
if not SCHEMA_PATH.is_file():
    SCHEMA_PATH = BUNDLE_DIR / "app" / "db" / "schema.sql"

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

def migrate_db(conn: sqlite3.Connection) -> None:
    """
    Виконує автоматичну міграцію схеми: додає нові колонки до існуючих таблиць,
    якщо вони відсутні, зберігаючи всі накопичені раніше дані.
    """
    cursor = conn.cursor()
    cursor.execute("PRAGMA table_info(metrics)")
    existing_cols = {row["name"] for row in cursor.fetchall()}

    new_metrics_columns = [
        ("gpu_percent", "REAL"),
        ("gpu_max", "REAL"),
        ("gpu_mem_used_bytes", "INTEGER"),
        ("gpu_mem_total_bytes", "INTEGER"),
        ("gpu_temp_c", "REAL"),
    ]

    for col_name, col_type in new_metrics_columns:
        if col_name not in existing_cols:
            cursor.execute(f"ALTER TABLE metrics ADD COLUMN {col_name} {col_type} DEFAULT NULL")

    # Створення таблиці process_usage_hourly, якщо її ще немає
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS process_usage_hourly (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            hour INTEGER NOT NULL,
            process_name TEXT NOT NULL,
            appearances_count INTEGER NOT NULL,
            avg_cpu_percent REAL NOT NULL,
            max_cpu_percent REAL NOT NULL,
            max_memory_percent REAL NOT NULL,
            first_seen_ts INTEGER NOT NULL,
            last_seen_ts INTEGER NOT NULL,
            UNIQUE(hour, process_name) ON CONFLICT REPLACE
        )
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_proc_usage_hourly_hour ON process_usage_hourly(hour)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_proc_usage_hourly_name ON process_usage_hourly(process_name)")

    # Створення таблиці ai_advice, якщо її ще немає
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS ai_advice (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp INTEGER NOT NULL,
            mode TEXT NOT NULL,
            source TEXT NOT NULL,
            user_query TEXT DEFAULT NULL,
            metrics_summary TEXT NOT NULL,
            advice_text TEXT NOT NULL,
            status TEXT NOT NULL
        )
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_ai_advice_timestamp ON ai_advice(timestamp)")

    cursor.close()

def init_db(db_path: Optional[Path] = None) -> None:
    """
    Ініціалізує таблиці та індекси з файлу schema.sql та виконує міграцію схеми.
    """
    path = db_path or DB_PATH
    with open(SCHEMA_PATH, "r", encoding="utf-8") as f:
        schema_sql = f.read()

    with db_session(path) as conn:
        conn.executescript(schema_sql)
        migrate_db(conn)
