"""
Модульні тести десктопної інтеграції, системного трею та середовища PyInstaller:
1. Визначення шляхів у звичайному та «frozen» режимі (з моками sys.frozen, sys.executable, sys._MEIPASS).
2. Меню системного трею (pystray): пункти меню, подвійний клік (default=True), динамічна зміна «Призупинити/Відновити збір», експорт у Документи\\SystemMonitor\\.
3. Обробка закриття вікна хрестиком: ховає вікно і НЕ зупиняє збирач при наявному треї; дозволяє закриття без трею.
4. Наявність та валідність іконок icon.png (256x256 RGBA) та icon.ico.
5. Захист від повторного запуску (single-instance probe).
"""

import io
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from PIL import Image

from app.config import (
    BASE_DIR,
    DB_PATH,
    ICON_ICO_PATH,
    ICON_PNG_PATH,
    STATIC_DIR,
    get_base_dir,
    get_bundle_dir,
    is_frozen,
)
from app.core.collector import MetricCollector
from app.db.connection import init_db
from app.desktop.tray import SystemTrayManager
from app.desktop.window import (
    create_window,
    destroy_window,
    hide_window,
    set_exiting,
    show_window,
)
from run import is_our_server_running
from tools.make_icon import generate_icons


def _get_menu_item_text(item) -> str:
    """
    Повертає текстове значення пункту меню pystray:
    - Якщо item.text є callable — викликає item.text(item) або item.text()
    - Якщо item.text є рядком або property — повертає його значення
    - Якщо сам item є callable — викликає item()
    """
    if item is None:
        return ""
    text_attr = getattr(item, "text", None)
    if callable(text_attr):
        try:
            return str(text_attr(item))
        except TypeError:
            return str(text_attr())
    if text_attr is not None:
        return str(text_attr)
    if callable(item):
        try:
            return str(item(item))
        except TypeError:
            return str(item())
    return str(item)


class MockEvent:
    """Мок об'єкта події pywebview з підтримкою оператора += (__iadd__)."""

    def __init__(self):
        self.handlers = []

    def __iadd__(self, handler):
        self.handlers.append(handler)
        return self

    def __isub__(self, handler):
        if handler in self.handlers:
            self.handlers.remove(handler)
        return self

    def __call__(self, *args, **kwargs):
        for handler in self.handlers:
            res = handler(*args, **kwargs)
            if res is False:
                return False
        return True


class TestDesktopEnvironment(unittest.TestCase):
    """Тести визначення шляхів для режиму звичайної розробки та скомпільованого .exe."""

    def test_paths_in_normal_mode(self):
        """1.1 Перевірка шляхів у звичайному режимі (sys.frozen = False)."""
        with patch.object(sys, "frozen", False, create=True):
            if hasattr(sys, "_MEIPASS"):
                delattr(sys, "_MEIPASS")

            self.assertFalse(is_frozen())
            base_dir = get_base_dir()
            bundle_dir = get_bundle_dir()

            # У режимі розробки базовий каталог і каталог ресурсів збігаються з коренем проєкту
            self.assertEqual(base_dir, Path(__file__).resolve().parent.parent)
            self.assertEqual(bundle_dir, Path(__file__).resolve().parent.parent)

    def test_paths_in_frozen_mode(self):
        """1.2 Перевірка шляхів у скомпільованому PyInstaller режимі."""
        mock_exe_dir = Path("C:/Program Files/SystemMonitor").resolve()
        mock_exe_path = str(mock_exe_dir / "SystemMonitor.exe")
        mock_meipass_dir = Path("C:/Users/User/AppData/Local/Temp/_MEI123456").resolve()

        with patch.object(sys, "frozen", True, create=True), \
             patch.object(sys, "executable", mock_exe_path), \
             patch.object(sys, "_MEIPASS", str(mock_meipass_dir), create=True):

            self.assertTrue(is_frozen())
            # Базовий каталог для monitor.db та .env — каталог біля .exe
            self.assertEqual(get_base_dir(), mock_exe_dir)
            # Каталог статичних файлів — тимчасова папка розпакування sys._MEIPASS
            self.assertEqual(get_bundle_dir(), mock_meipass_dir)


class TestIconGenerationAndFiles(unittest.TestCase):
    """Тести генерації та перевірки наявності icon.png та icon.ico."""

    def test_icon_files_exist_and_valid(self):
        """2.1 Перевірка генерації та розмірів іконок через tools/make_icon.py."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            test_png = tmp_path / "icon.png"
            test_ico = tmp_path / "icon.ico"
            test_root_ico = tmp_path / "root_icon.ico"

            generate_icons(test_png, test_ico, test_root_ico)

            # Перевірка наявності файлів
            self.assertTrue(test_png.is_file(), "icon.png має бути створений")
            self.assertTrue(test_ico.is_file(), "icon.ico має бути створений")
            self.assertTrue(test_root_ico.is_file(), "root icon.ico має бути створений")

            # Перевірка характеристик PNG
            with Image.open(test_png) as img:
                self.assertEqual(img.size, (256, 256), "Розмір PNG має бути рівно 256x256")
                self.assertEqual(img.format, "PNG")
                self.assertEqual(img.mode, "RGBA")

            # Перевірка файлу ICO
            self.assertGreater(test_ico.stat().st_size, 1000, "Файл ICO має бути непорожнім")

    def test_project_assets_icons_present(self):
        """2.2 Перевірка наявності іконок у дереві проєкту."""
        generate_icons()
        self.assertTrue(ICON_PNG_PATH.is_file(), f"Іконка {ICON_PNG_PATH} має бути наявна")
        self.assertTrue(ICON_ICO_PATH.is_file(), f"Іконка {ICON_ICO_PATH} має бути наявна")


class TestSystemTrayManager(unittest.TestCase):
    """Тести модуля системного трею Windows (pystray)."""

    def setUp(self):
        self.temp_file = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.db_path = Path(self.temp_file.name)
        self.temp_file.close()
        init_db(self.db_path)

        self.collector = MetricCollector(db_path=self.db_path)
        self.collector.start()

        self.opened = False
        self.exited = False

        def on_open():
            self.opened = True

        def on_exit():
            self.exited = True

        self.tray = SystemTrayManager(
            on_open_window=on_open,
            on_exit_app=on_exit,
            collector=self.collector,
            db_path=self.db_path,
        )

    def tearDown(self):
        self.collector.stop()
        self.tray.stop()
        for p in [self.db_path, Path(str(self.db_path) + "-wal"), Path(str(self.db_path) + "-shm")]:
            if p.exists():
                try:
                    p.unlink()
                except Exception:
                    pass

    def test_tray_menu_structure_and_default_action(self):
        """3.1 Перевірка пунктів меню трею та властивості default=True для подвійного кліку."""
        menu = self.tray._build_menu()
        items = list(menu.items)

        # 1. Пункт «Відкрити монітор» має бути default=True (активація подвійним кліком)
        item_open = items[0]
        self.assertEqual(item_open.text, "Відкрити монітор")
        self.assertTrue(item_open.default, "Пункт 'Відкрити монітор' повинен мати default=True для подвійного кліку")

        # 2. Пункт «Експорт звіту (24 год)»
        item_export = items[1]
        self.assertEqual(item_export.text, "Експорт звіту (24 год)")

        # 3. Пункт паузи
        item_pause = items[2]
        self.assertIn("збір", _get_menu_item_text(item_pause))

        # 4. Пункт «Вихід»
        item_exit = items[-1]
        self.assertEqual(item_exit.text, "Вихід")

    def test_tray_open_and_exit_callbacks(self):
        """3.2 Перевірка виклику callback дій відкриття вікна та виходу."""
        self.tray.open_window()
        self.assertTrue(self.opened, "on_open_window має викликатися при відкритті")

        self.tray.exit_app()
        self.assertTrue(self.exited, "on_exit_app має викликатися при виході з трею")

    def test_tray_dynamic_pause_resume_toggle(self):
        """3.3 Перевірка динамічного тексту та перемикання паузи збирача."""
        menu = self.tray._build_menu()
        pause_item = menu.items[2]

        # Початковий стан: збирач працює -> напис «Призупинити збір»
        self.assertFalse(self.collector.is_paused)
        self.assertEqual(_get_menu_item_text(pause_item), "Призупинити збір")

        # Перемикаємо на паузу
        new_state = self.tray.toggle_pause()
        self.assertTrue(new_state)
        self.assertTrue(self.collector.is_paused)
        self.assertEqual(_get_menu_item_text(pause_item), "Відновити збір")

        # Відновлюємо збір
        new_state_2 = self.tray.toggle_pause()
        self.assertFalse(new_state_2)
        self.assertFalse(self.collector.is_paused)
        self.assertEqual(_get_menu_item_text(pause_item), "Призупинити збір")

    def test_tray_export_24h_to_documents_folder(self):
        """3.4 Перевірка експорту 24h звіту у Документи\\SystemMonitor\\."""
        with patch("app.services.exporter.generate_excel_report") as mock_gen:
            mock_gen.return_value = (io.BytesIO(b"dummy excel data"), "report_24h_test.xlsx")

            target_file = self.tray.export_report_24h()
            self.assertIsNotNone(target_file)
            self.assertTrue(str(target_file).endswith("report_24h_test.xlsx"))
            self.assertIn("SystemMonitor", str(target_file))
            self.assertTrue(target_file.is_file())

            # Очищення створеного тестового файлу
            try:
                target_file.unlink()
            except Exception:
                pass


class TestWindowClosingBehavior(unittest.TestCase):
    """Тести перехоплення закриття вікна хрестиком при наявному треї."""

    def test_closing_with_active_tray_hides_window_and_keeps_collector_running(self):
        """
        4.1 При активному треї закриття вікна хрестиком:
        - викликає window.hide()
        - повертає False (забороняє знищення вікна)
        - збирач MetricCollector продовжує працювати
        """
        mock_window = MagicMock()
        mock_window.events.closing = MockEvent()

        mock_tray = MagicMock()
        mock_tray.is_running.return_value = True

        collector = MetricCollector()
        collector.start()

        try:
            # Створюємо вікно з активним треєм через мок pywebview
            with patch("webview.create_window", return_value=mock_window):
                win = create_window("http://127.0.0.1:5050", title="System Monitor", tray_manager=mock_tray)

            self.assertEqual(len(mock_window.events.closing.handlers), 1)
            on_closing_handler = mock_window.events.closing.handlers[0]

            # Симулюємо натискання користувачем на хрестик вікна
            res = on_closing_handler()

            # Результат має бути False (вікно НЕ знищується)
            self.assertFalse(res, "on_closing має повертати False при активному треї")
            mock_window.hide.assert_called_once()

            # Збирач залишається активним
            self.assertTrue(collector.is_running(), "Збирач повинен продовжувати працювати після згортання")
        finally:
            collector.stop()
            set_exiting(False)

    def test_closing_without_tray_allows_closing(self):
        """4.2 Якщо системний трей недоступний — закриття хрестиком дозволяється (повертає True)."""
        mock_window = MagicMock()
        mock_window.events.closing = MockEvent()

        # Трей недоступний
        mock_tray = MagicMock()
        mock_tray.is_running.return_value = False

        with patch("webview.create_window", return_value=mock_window):
            win = create_window("http://127.0.0.1:5050", title="System Monitor", tray_manager=mock_tray)

        self.assertEqual(len(mock_window.events.closing.handlers), 1)
        on_closing_handler = mock_window.events.closing.handlers[0]
        res = on_closing_handler()

        self.assertTrue(res, "on_closing має повертати True при відсутності трею")
        mock_window.hide.assert_not_called()


class TestSingleInstanceProtection(unittest.TestCase):
    """Тести перевірки повторного запуску (Single Instance)."""

    @patch("urllib.request.urlopen")
    def test_is_our_server_running_detected(self, mock_urlopen):
        """5.1 Розпізнавання запущеного екземпляра System Monitor."""
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.read.return_value = json.dumps({"status": "ok", "uptime": 120, "collector": True}).encode("utf-8")
        mock_resp.__enter__.return_value = mock_resp
        mock_urlopen.return_value = mock_resp

        self.assertTrue(is_our_server_running("127.0.0.1", 5050))

    @patch("urllib.request.urlopen")
    def test_is_our_server_running_foreign_or_offline(self, mock_urlopen):
        """5.2 Порт зайнятий сторонньою програмою або вимкнений."""
        mock_urlopen.side_effect = OSError("Connection refused")
        self.assertFalse(is_our_server_running("127.0.0.1", 5050))


if __name__ == "__main__":
    unittest.main()
