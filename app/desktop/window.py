"""
Модуль створення та керування нативним десктопним вікном через pywebview.
"""

import base64
import logging
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

class DesktopApi:
    """JS API для виклику нативних функцій Windows із веб-інтерфейсу."""

    def __init__(self):
        self._window = None

    def set_window(self, window) -> None:
        self._window = window

    def save_excel(self, base64_data: str, filename: str) -> Dict[str, Any]:
        """
        Відкриває нативний діалог Windows («Зберегти як») та записує Excel файл.
        """
        try:
            import webview
            file_types = ('Excel (*.xlsx)', 'All files (*.*)')
            target_path = self._window.create_file_dialog(
                webview.SAVE_DIALOG,
                save_filename=filename,
                file_types=file_types
            )
            if not target_path:
                return {"success": False, "cancelled": True}

            if isinstance(target_path, (list, tuple)):
                if len(target_path) == 0 or not target_path[0]:
                    return {"success": False, "cancelled": True}
                target_path = target_path[0]

            file_bytes = base64.b64decode(base64_data)
            with open(target_path, "wb") as f:
                f.write(file_bytes)

            logger.info(f"Файл звіту Excel успішно збережено: {target_path}")
            return {"success": True, "path": str(target_path)}
        except Exception as e:
            logger.error(f"Помилка збереження Excel через pywebview: {e}", exc_info=True)
            return {"success": False, "error": str(e)}

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

    api = DesktopApi()
    window = webview.create_window(
        title=title,
        url=url,
        width=1280,
        height=800,
        min_size=(1100, 700),
        background_color="#0f1115",
        text_select=False,
        js_api=api,
    )
    api.set_window(window)
    return window

def start_gui() -> None:
    """
    Запускає головний цикл подій pywebview.
    Блокує потік до закриття вікна користувачем.
    """
    import webview
    # Запуск із нативним рушієм Windows (Edge Chromium / WebView2)
    webview.start()
