"""
Головна точка входу для запуску System Monitor (Етап 4б).
- Перевіряє та захищає від повторного запуску (якщо наш сервер уже працює — відновлює вікно або інформує про трей)
- Ініціалізує систему логування з ротацією у logs/system-monitor.log поруч із .exe
- Ініціалізує базу даних SQLite (WAL)
- Запускає фоновий збирач метрик та процесів (MetricCollector) з можливістю паузи
- Запускає фоновий агрегатор даних (DataAggregator)
- Запускає Flask сервер
- Запускає іконку системного трею Windows (pystray) із контекстним меню та діями
- Відкриває нативне десктопне вікно pywebview (1280x800)
- Закриття вікна хрестиком згортає застосунок у трей, збір триває; повний вихід лише через «Вихід» у треї
"""

import json
import logging
import signal
import socket
import sys
import threading
import time
import urllib.request
from typing import Optional

from werkzeug.serving import make_server

from app.config import DB_PATH, HOST, PORT, setup_logging
from app.core.aggregator import DataAggregator
from app.core.collector import MetricCollector
from app.db.connection import init_db
from app.desktop.tray import SystemTrayManager
from app.desktop.window import create_window, destroy_window, set_exiting, show_window, start_gui
from app.server import create_app

logger = logging.getLogger("run")


class ServerThread(threading.Thread):
    """Фоновий потік для сервера Flask через Werkzeug make_server."""

    def __init__(self, app, host, port):
        super().__init__(name="FlaskServerThread", daemon=True)
        self.server = make_server(host, port, app)
        self.ctx = app.app_context()
        self.ctx.push()

    def run(self):
        logger.info(f"Flask сервер запущено на http://{self.server.host}:{self.server.port}")
        self.server.serve_forever()

    def shutdown(self):
        logger.info("Зупинка Flask сервера...")
        try:
            self.server.shutdown()
        except Exception as e:
            logger.debug(f"Помилка при зупинці сервера: {e}")


def is_port_available(host: str, port: int) -> bool:
    """Перевіряє, чи вільний зазначений порт для сокета."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind((host, port))
            return True
        except OSError:
            return False


def is_our_server_running(host: str, port: int) -> bool:
    """
    Перевіряє, чи порт зайнятий саме діючим екземпляром System Monitor.
    Опитує ендпоінт /api/status.
    """
    url = f"http://{host}:{port}/api/status"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "SystemMonitor-Probe"})
        with urllib.request.urlopen(req, timeout=1.5) as resp:
            if resp.status == 200:
                data = json.loads(resp.read().decode("utf-8"))
                return "collector" in data or "uptime" in data or "status" in data
    except Exception:
        pass
    return False


def restore_existing_instance() -> bool:
    """
    Спроба відновити і вивести на передній план наявне вікно System Monitor через Win32 API.
    Якщо вікно сховане або запущено без консолі — показує інформаційне повідомлення Windows.
    """
    try:
        import ctypes
        user32 = ctypes.windll.user32
        hwnd = user32.FindWindowW(None, "System Monitor")
        if hwnd:
            # 9 = SW_RESTORE
            user32.ShowWindow(hwnd, 9)
            user32.SetForegroundWindow(hwnd)
            return True
        else:
            # Якщо вікно сховане в треї
            user32.MessageBoxW(
                0,
                "System Monitor вже запущено і працює у системному треї (біля годинника).\n\n"
                "Клацніть на іконку в треї, щоб відкрити вікно, або перейдіть за адресою http://127.0.0.1:5050 у браузері.",
                "System Monitor",
                0x40  # MB_ICONINFORMATION
            )
            return True
    except Exception as e:
        logger.debug(f"Не вдалося активувати наявне вікно через Win32: {e}")
        return False


def main():
    # 0. Налаштування файлового логування з ротацією у logs/system-monitor.log
    setup_logging()

    use_browser_mode = "--browser" in sys.argv

    print("=" * 60)
    print("    System Monitor - Ресурси ПК з аналітикою та ШІ")
    print("=" * 60)

    # 1. Захист від повторного запуску та перевірка порту
    if not is_port_available(HOST, PORT):
        if is_our_server_running(HOST, PORT):
            logger.info(f"System Monitor уже працює на http://{HOST}:{PORT}. Відновлюємо наявний екземпляр.")
            print(f"\n[ІНФОРМАЦІЯ] System Monitor вже працює на http://{HOST}:{PORT}.")
            print("Активовано наявний екземпляр програми у вікні або системному треї.\n")
            restore_existing_instance()
            sys.exit(0)
        else:
            logger.error(f"Порт {PORT} на {HOST} зайнятий іншою сторонньою програмою.")
            print("\n" + "!" * 60)
            print(f"[ПОМИЛКА] Порт {PORT} на {HOST} вже зайнятий іншою програмою!")
            print("Будь ласка, змініть значення PORT у файлі .env або завершіть")
            print("процес, що займає цей порт, і спробуйте знову.")
            print("!" * 60 + "\n")
            try:
                import ctypes
                ctypes.windll.user32.MessageBoxW(
                    0,
                    f"Порт {PORT} вже зайнятий іншим процесом на цьому комп'ютері.\n"
                    "Змініть PORT у файлі .env або завершіть сторонню програму.",
                    "System Monitor - Конфлікт портів",
                    0x10  # MB_ICONERROR
                )
            except Exception:
                pass
            sys.exit(1)

    # 2. Ініціалізація бази даних
    logger.info(f"Ініціалізація бази даних: {DB_PATH}")
    init_db(DB_PATH)

    # 3. Запуск фонових воркерів
    collector = MetricCollector(db_path=DB_PATH)
    collector.start()

    aggregator = DataAggregator(db_path=DB_PATH)
    aggregator.start()

    # 4. Створення та запуск Flask сервера у фоновому потоці
    flask_app = create_app(collector=collector, db_path=DB_PATH)
    server_thread = ServerThread(flask_app, HOST, PORT)
    server_thread.start()

    tray_manager: Optional[SystemTrayManager] = None
    is_shutting_down = False

    def shutdown(sig=None, frame=None):
        nonlocal is_shutting_down
        if is_shutting_down:
            return
        is_shutting_down = True
        print("\nЗавершення роботи System Monitor...")
        logger.info("Завершення роботи System Monitor...")

        set_exiting(True)

        if tray_manager:
            try:
                tray_manager.stop()
            except Exception as e:
                logger.debug(f"Помилка при зупинці трею: {e}")

        try:
            destroy_window()
        except Exception as e:
            logger.debug(f"Помилка при закритті вікна: {e}")

        try:
            collector.stop()
        except Exception as e:
            logger.debug(f"Помилка при зупинці collector: {e}")

        try:
            aggregator.stop()
        except Exception as e:
            logger.debug(f"Помилка при зупинці aggregator: {e}")

        try:
            server_thread.shutdown()
        except Exception as e:
            logger.debug(f"Помилка при зупинці сервера: {e}")

        print("Всі служби успішно зупинено.")
        logger.info("Всі служби успішно зупинено.")
        sys.exit(0)

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    url = f"http://{HOST}:{PORT}"
    print(f"\n[ГОТОВО] Сервер активний за адресою: {url}")

    if use_browser_mode:
        print("[РЕЖИМ БРАУЗЕРА] Відкрийте посилання вище у вашому веб-браузері.")
        print("Для зупинки програми натисніть Ctrl+C.\n")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            shutdown()
    else:
        # 5. Запуск іконки системного трею (якщо доступний pystray)
        if SystemTrayManager.is_available():
            tray_manager = SystemTrayManager(
                on_open_window=show_window,
                on_exit_app=shutdown,
                collector=collector,
                db_path=DB_PATH,
            )
            tray_manager.start()
        else:
            logger.info("pystray не встановлено або недоступний. Робота без системного трею.")

        # 6. Режим десктопного вікна pywebview
        try:
            print("[РЕЖИМ ВІКНА] Запуск десктопного вікна pywebview...")
            create_window(url, title="System Monitor", tray_manager=tray_manager)
            # Блокуючий виклик головного циклу вікна
            start_gui()
            # Вікно було закрито (якщо трею не було або ініційовано вихід)
            shutdown()
        except Exception as e:
            logger.warning(f"Не вдалося відкрити десктопне вікно ({e}). Перехід у консольний режим.")
            print(f"\nВідкрийте дашборд у браузері: {url}")
            print("Натисніть Ctrl+C для виходу.\n")
            try:
                while True:
                    time.sleep(1)
            except KeyboardInterrupt:
                shutdown()


if __name__ == "__main__":
    main()
