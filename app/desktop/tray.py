"""
Модуль системного трею Windows (pystray) для System Monitor.
- Іконка біля годинника з фірмовим логотипом
- Меню:
  * «Відкрити монітор» (default=True, відкриття/відновлення вікна по подвійному кліку)
  * «Експорт звіту (24 год)» (збереження у Документи\\SystemMonitor\\)
  * «Призупинити/Відновити збір» (динамічний текст перемикання паузи збирача)
  * «Вихід» (коректна зупинка всіх служб та повний вихід)
- Безпечний фолбек при відсутності pystray.
"""

import logging
import threading
from pathlib import Path
from typing import Callable, Optional

from app.config import DB_PATH, ICON_ICO_PATH, ICON_PNG_PATH

logger = logging.getLogger(__name__)

try:
    import pystray
    from PIL import Image
    PYSTRAY_AVAILABLE = True
except ImportError:
    pystray = None
    Image = None
    PYSTRAY_AVAILABLE = False


class SystemTrayManager:
    """
    Керує життєвим циклом та подіями іконки системного трею Windows через pystray.
    """

    def __init__(
        self,
        on_open_window: Optional[Callable[[], None]] = None,
        on_exit_app: Optional[Callable[[], None]] = None,
        collector=None,
        db_path: Optional[Path] = None,
    ):
        self.on_open_window = on_open_window
        self.on_exit_app = on_exit_app
        self.collector = collector
        self.db_path = db_path or DB_PATH
        self.icon: Optional["pystray.Icon"] = None
        self._thread: Optional[threading.Thread] = None
        self._is_running = False

    @staticmethod
    def is_available() -> bool:
        """Перевіряє, чи доступні бібліотеки pystray та Pillow у системі."""
        return PYSTRAY_AVAILABLE

    def is_running(self) -> bool:
        """Повертає True, якщо трей активний."""
        return self._is_running and self.icon is not None

    def _get_icon_image(self) -> Optional["Image.Image"]:
        """
        Завантажує зображення іконки для трею з assets або генерує на льоту.
        """
        if not PYSTRAY_AVAILABLE or Image is None:
            return None

        # 1. Перевіряємо наявність згенерованого PNG
        if ICON_PNG_PATH.is_file():
            try:
                return Image.open(str(ICON_PNG_PATH))
            except Exception as e:
                logger.warning(f"Не вдалося відкрити {ICON_PNG_PATH}: {e}")

        # 2. Перевіряємо наявність ICO
        if ICON_ICO_PATH.is_file():
            try:
                return Image.open(str(ICON_ICO_PATH))
            except Exception as e:
                logger.warning(f"Не вдалося відкрити {ICON_ICO_PATH}: {e}")

        # 3. Спроба згенерувати через tools.make_icon
        try:
            from tools.make_icon import create_icon_image
            return create_icon_image(64)
        except Exception as e:
            logger.warning(f"Не вдалося створити іконку на льоту: {e}")

        # 4. Мінімальний аварійний квадрат синього кольору
        return Image.new("RGBA", (64, 64), (59, 130, 246, 255))

    def _build_menu(self) -> "pystray.Menu":
        """
        Формує контекстне меню іконки системного трею.
        """
        def get_pause_label(*args, **kwargs) -> str:
            if self.collector and getattr(self.collector, "is_paused", False):
                return "Відновити збір"
            return "Призупинити збір"

        menu_items = [
            pystray.MenuItem(
                "Відкрити монітор",
                lambda icon, item: self.open_window(),
                default=True,
            ),
            pystray.MenuItem(
                "Експорт звіту (24 год)",
                lambda icon, item: self.export_report_24h(),
            ),
            pystray.MenuItem(
                get_pause_label,
                lambda icon, item: self.toggle_pause(),
            ),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(
                "Вихід",
                lambda icon, item: self.exit_app(),
            ),
        ]
        return pystray.Menu(*menu_items)

    def open_window(self) -> None:
        """Показує або розгортає десктопне вікно монітора."""
        if self.on_open_window:
            try:
                self.on_open_window()
            except Exception as e:
                logger.error(f"Помилка при відкритті вікна з трею: {e}", exc_info=True)

    def export_report_24h(self) -> Optional[Path]:
        """
        Генерує Excel-звіт за останні 24 години та зберігає його у:
        %USERPROFILE%\\Documents\\SystemMonitor\\
        """
        try:
            from app.services.exporter import generate_excel_report

            # Каталог Документи\\SystemMonitor
            docs_dir = Path.home() / "Documents" / "SystemMonitor"
            docs_dir.mkdir(parents=True, exist_ok=True)

            bio, filename = generate_excel_report(period="24h", db_path=self.db_path)
            target_path = docs_dir / filename

            with open(target_path, "wb") as f:
                f.write(bio.getvalue())

            logger.info(f"Звіт Excel успішно експортовано у треї: {target_path}")

            # Сповіщення Windows через трей
            if self.icon and hasattr(self.icon, "notify"):
                try:
                    self.icon.notify(
                        f"Звіт збережено у Документи\\SystemMonitor\\:\n{filename}",
                        "System Monitor - Експорт завершено"
                    )
                except Exception:
                    pass

            return target_path
        except Exception as e:
            logger.error(f"Помилка експорту звіту з трею: {e}", exc_info=True)
            if self.icon and hasattr(self.icon, "notify"):
                try:
                    self.icon.notify(f"Помилка експорту: {e}", "System Monitor")
                except Exception:
                    pass
            return None

    def toggle_pause(self) -> bool:
        """
        Перемикає стан призупинення/відновлення фонового збору метрик.
        """
        if not self.collector:
            return False

        new_paused = self.collector.toggle_pause()
        if self.icon and hasattr(self.icon, "update_menu"):
            try:
                self.icon.update_menu()
            except Exception:
                pass

        # Сповіщення в треї
        if self.icon and hasattr(self.icon, "notify"):
            try:
                status_text = "Збір телеметрії призупинено" if new_paused else "Збір телеметрії відновлено"
                self.icon.notify(status_text, "System Monitor")
            except Exception:
                pass

        return new_paused

    def exit_app(self) -> None:
        """Ініціює повне коректне завершення програми."""
        logger.info("Ініційовано повний вихід із System Monitor через меню трею.")
        self.stop()
        if self.on_exit_app:
            try:
                self.on_exit_app()
            except Exception as e:
                logger.error(f"Помилка під час виклику on_exit_app: {e}", exc_info=True)

    def start(self) -> None:
        """Запускає іконку системного трею у фоновому потоці."""
        if not PYSTRAY_AVAILABLE or pystray is None:
            logger.warning("pystray недоступний. Системний трей не буде запущено.")
            return

        if self._is_running:
            return

        img = self._get_icon_image()
        if img is None:
            logger.warning("Не вдалося завантажити зображення іконки для трею.")
            return

        menu = self._build_menu()
        self.icon = pystray.Icon(
            name="SystemMonitor",
            icon=img,
            title="System Monitor",
            menu=menu,
        )

        def _run():
            self._is_running = True
            logger.info("Системний трей System Monitor успішно запущено.")
            try:
                self.icon.run()
            except Exception as e:
                logger.debug(f"Цикл трею завершено: {e}")
            finally:
                self._is_running = False
                logger.info("Системний трей зупинено.")

        self._thread = threading.Thread(target=_run, name="PystrayTrayThread", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Зупиняє іконку системного трею."""
        if self.icon:
            try:
                self.icon.stop()
            except Exception:
                pass
            self.icon = None
        self._is_running = False
