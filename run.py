"""
Головна точка входу для запуску System Monitor (Етап 2).
- Перевіряє порт 5050 з дружнім повідомленням
- Ініціалізує базу даних SQLite (WAL)
- Запускає фоновий збирач метрик та процесів (MetricCollector)
- Запускає фоновий агрегатор даних (DataAggregator)
- Запускає Flask сервер
- Відкриває нативне десктопне вікно pywebview (1280x800)
  або працює в режимі сервера, якщо передано прапорець --browser
"""

import logging
import signal
import socket
import sys
import threading
import time

from werkzeug.serving import make_server

from app.config import DB_PATH, HOST, PORT
from app.core.aggregator import DataAggregator
from app.core.collector import MetricCollector
from app.db.connection import init_db
from app.desktop.window import create_window, start_gui
from app.server import create_app

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
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
        self.server.shutdown()

def is_port_available(host: str, port: int) -> bool:
    """Перевіряє, чи вільний зазначений порт для сокета."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind((host, port))
            return True
        except OSError:
            return False

def main():
    use_browser_mode = "--browser" in sys.argv

    print("=" * 60)
    print("    System Monitor - Ресурси ПК з аналітикою та ШІ")
    print("=" * 60)

    # 1. Перевірка доступності порту
    if not is_port_available(HOST, PORT):
        print("\n" + "!" * 60)
        print(f"[ПОМИЛКА] Порт {PORT} на {HOST} вже зайнятий іншою програмою!")
        print("Будь ласка, змініть значення PORT у файлі .env або завершіть")
        print("процес, що займає цей порт, і спробуйте знову.")
        print("!" * 60 + "\n")
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

    is_shutting_down = False

    def shutdown(sig=None, frame=None):
        nonlocal is_shutting_down
        if is_shutting_down:
            return
        is_shutting_down = True
        print("\nЗавершення роботи System Monitor...")
        collector.stop()
        aggregator.stop()
        server_thread.shutdown()
        print("Всі служби успішно зупинено.")
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
        # Режим десктопного вікна pywebview
        try:
            print("[РЕЖИМ ВІКНА] Запуск десктопного вікна pywebview...")
            create_window(url, title="System Monitor")
            # Блокуючий виклик головного циклу вікна
            start_gui()
            # Вікно було закрито користувачем
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
