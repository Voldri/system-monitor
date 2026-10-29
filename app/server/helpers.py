"""
Допоміжні утиліти для сервера: форматування локального часу та серіалізація.
"""

from datetime import datetime
from typing import Any, Dict

def format_local_time(timestamp: int) -> str:
    """
    Конвертує UNIX timestamp у читабельний рядок локального часу користувача.
    Наприклад: '2026-10-29 21:05:00'.
    """
    try:
        dt = datetime.fromtimestamp(timestamp).astimezone()
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return ""

def format_local_time_short(timestamp: int) -> str:
    """
    Конвертує UNIX timestamp у короткий рядок для осей графіка: 'HH:MM:SS'.
    """
    try:
        dt = datetime.fromtimestamp(timestamp).astimezone()
        return dt.strftime("%H:%M:%S")
    except Exception:
        return ""
