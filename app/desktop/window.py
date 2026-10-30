"""
Модуль створення та керування нативним десктопним вікном через pywebview.
Підтримує інтеграцію з системним треєм: згортання у трей при закритті хрестиком
та відновлення по подвійному кліку або пункту меню.
"""

import base64
import inspect
import logging
from typing import Any, Dict, Optional

from app.config import ICON_ICO_PATH, ICON_PNG_PATH

logger = logging.getLogger(__name__)

# Глобальне посилання на поточне десктопне вікно
_CURRENT_WINDOW = None
_IS_EXITING = False


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


def get_current_window():
    """Повертає поточне вікно pywebview."""
    return _CURRENT_WINDOW


def show_window() -> None:
    """Показує та розгортає вікно із системного трею."""
    global _CURRENT_WINDOW
    if _CURRENT_WINDOW:
        try:
            _CURRENT_WINDOW.show()
            if hasattr(_CURRENT_WINDOW, "restore"):
                _CURRENT_WINDOW.restore()
        except Exception as e:
            logger.debug(f"Помилка при show_window: {e}")


def hide_window() -> None:
    """Ховає вікно у системний трей."""
    global _CURRENT_WINDOW
    if _CURRENT_WINDOW:
        try:
            _CURRENT_WINDOW.hide()
        except Exception as e:
            logger.debug(f"Помилка при hide_window: {e}")


def set_exiting(val: bool = True) -> None:
    """Встановлює прапорець повного виходу, дозволяючи закриття вікна."""
    global _IS_EXITING
    _IS_EXITING = val


def destroy_window() -> None:
    """Знищує вікно для повного виходу з програми."""
    global _CURRENT_WINDOW
    set_exiting(True)
    if _CURRENT_WINDOW:
        try:
            _CURRENT_WINDOW.destroy()
        except Exception as e:
            logger.debug(f"Помилка при destroy_window: {e}")
        _CURRENT_WINDOW = None


def create_window(url: str, title: str = "System Monitor", tray_manager=None):
    """
    Створює десктопне вікно pywebview (WebView2 у Windows 11).
    Параметри: 1280x800, мінімальний розмір: 1100x700.
    Якщо tray_manager активний — закриття хрестиком ховає вікно замість завершення процесу.
    """
    global _CURRENT_WINDOW, _IS_EXITING
    _IS_EXITING = False

    try:
        import webview
    except ImportError as e:
        logger.error("Бібліотека pywebview не встановлена. Запустіть із прапорцем --browser.")
        raise e

    api = DesktopApi()
    window_kwargs = {
        "title": title,
        "url": url,
        "width": 1280,
        "height": 800,
        "min_size": (1100, 700),
        "background_color": "#0f1115",
        "text_select": False,
        "js_api": api,
    }

    # Якщо версія pywebview підтримує встановлення іконки вікна
    try:
        sig = inspect.signature(webview.create_window)
        if "icon" in sig.parameters:
            if ICON_ICO_PATH.is_file():
                window_kwargs["icon"] = str(ICON_ICO_PATH)
            elif ICON_PNG_PATH.is_file():
                window_kwargs["icon"] = str(ICON_PNG_PATH)
    except Exception:
        pass

    window = webview.create_window(**window_kwargs)
    api.set_window(window)
    _CURRENT_WINDOW = window

    # Обробка закриття вікна (хрестик)
    def on_closing(*args, **kwargs) -> bool:
        """
        Перехоплювач закриття вікна:
        - Якщо активний трей і не ініційовано повний вихід: ховаємо вікно і повертаємо False (не знищувати).
        - Якщо трею немає або натиснуто 'Вихід': повертаємо True (дозволити закриття).
        """
        global _IS_EXITING
        if _IS_EXITING:
            return True

        if tray_manager and getattr(tray_manager, "is_running", lambda: False)():
            try:
                window.hide()
                logger.info("Вікно System Monitor сховано у системний трей. Збір телеметрії триває.")
                return False
            except Exception as e:
                logger.warning(f"Не вдалося сховати вікно: {e}")
                return True

        # Якщо трей недоступний — дозволяємо закриття вікна
        return True

    try:
        window.events.closing += on_closing
    except TypeError:
        if hasattr(window.events.closing, "append"):
            window.events.closing.append(on_closing)
        else:
            raise
    return window


def start_gui() -> None:
    """
    Запускає головний цикл подій pywebview.
    Блокує потік до закриття вікна користувачем.
    """
    import webview
    # Запуск із нативним рушієм Windows (Edge Chromium / WebView2)
    webview.start()
