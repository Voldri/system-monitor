"""
Модуль створення та керування нативним десктопним вікном через pywebview.
"""

import logging
from typing import Optional

logger = logging.getLogger(__name__)

def create_window(url: str, title: str = "System Monitor"):
    """
    Створює десктопне вікно pywebview (WebView2 у Windows 11).
    Параметри: 1280x800, мінімальний розмір: 1100x700.
    """
    try:
        import webview
    except ImportError as e:
        logger.error("Бібліотека pywebview не встановлена. Запустіть із прапорцем --browser.")
        raise e

    window = webview.create_window(
        title=title,
        url=url,
        width=1280,
        height=800,
        min_size=(1100, 700),
        background_color="#0f1115",
        text_select=False,
    )
    return window

def start_gui() -> None:
    """
    Запускає головний цикл подій pywebview.
    Блокує потік до закриття вікна користувачем.
    """
    import webview
    # Запуск із нативним рушієм Windows (Edge Chromium / WebView2)
    webview.start()
