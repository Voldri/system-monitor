"""
Головна точка входу для запуску бекенду System Monitor (Етап 1).
- Перевіряє доступність порту 5050 з дружнім повідомленням
- Ініціалізує базу даних SQLite (WAL)
- Запускає фоновий збирач метрик та процесів (MetricCollector)
- Запускає фоновий агрегатор даних (DataAggregator)
- Запускає локальний веб-сервер Flask
"""

import logging
import signal
import socket
import sys
import time

from app.config import HOST, PORT, DB_PATH
from app.core.collector import MetricCollector
from app.core.aggregator import DataAggregator
from app.db.connection import init_db
from app.server import create_app

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("run")

def is_port_available(host: str, port: int) -> bool:
    """
    Перевіряє, чи вільний зазначений порт для прив'язки сокета.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind((host, port))
            return True
        except OSError:
            return False

def main():
    print("=" * 60)
    print("    System Monitor - Ресурси ПК з аналітикою та ШІ")
    print("=" * 60)

    # 1. Перевірка доступності порту
    if not is_port_available(HOST, PORT):
        print("\n" + "!" * 60)
        print(f"[ПОМИЛКА] Порт {PORT} на {HOST} вже зайнятий іншою програмою!")
        print(f"Будь ласка, змініть значення PORT у файлі .env або завершіть")
        print(f"процес, що займає цей порт, і спробуйте знову.")
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

    # 4. Створення Flask сервера
    app = create_app(collector=collector, db_path=DB_PATH)

    def shutdown(sig=None, frame=None):
        print("\nОтримано сигнал зупинки. Завершення фонових потоків...")
        collector.stop()
        aggregator.stop()
        print("System Monitor успішно зупинено.")
        sys.exit(0)

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    print(f"\n[ГОТОВО] Сервер запущено на http://{HOST}:{PORT}")
    print("Доступні API ендпоінти:")
    print(f"  - Стан системи:      http://{HOST}:{PORT}/api/status")
    print(f"  - Поточні метрики:   http://{HOST}:{PORT}/api/metrics/current")
    print(f"  - Історія:           http://{HOST}:{PORT}/api/metrics/history?range=live")
    print(f"  - Топ процесів:      http://{HOST}:{PORT}/api/processes?sort=cpu")
    print("\nНатисніть Ctrl+C для зупинки.\n")

    try:
        # Запуск Flask без reloader'а, щоб не дублювати фонові потоки
        app.run(host=HOST, port=PORT, debug=False, use_reloader=False)
    except KeyboardInterrupt:
        shutdown()

if __name__ == "__main__":
    main()
