"""
Тести агрегації погодинного використання програм (process_usage_hourly) та ротації.
"""

import tempfile
import time
import unittest
from pathlib import Path

from app.core.aggregator import DataAggregator
from app.db.connection import db_session, init_db

class TestProcessUsageHourly(unittest.TestCase):
    """Перевірка заповнення та ротації таблиці process_usage_hourly."""

    def setUp(self):
        self.temp_file = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.db_path = Path(self.temp_file.name)
        self.temp_file.close()
        init_db(self.db_path)
        self.aggregator = DataAggregator(db_path=self.db_path)

    def tearDown(self):
        for p in [self.db_path, Path(str(self.db_path) + "-wal"), Path(str(self.db_path) + "-shm")]:
            if p.exists():
                try:
                    p.unlink()
                except Exception:
                    pass

    def test_hourly_aggregation_from_snapshots(self):
        """Перевірка обчислення кількості появ, середнього та максимального CPU/RAM."""
        base_hour = 1700000000  # Фіксована точка часу

        # Вставляємо тестові знімки процесів
        with db_session(self.db_path) as conn:
            cur = conn.cursor()
            # 3 знімки для 'app_a.exe': CPU 10, 20, 30 -> avg 20.0, max 30.0
            cur.execute("INSERT INTO process_snapshots (timestamp, pid, name, cpu_percent, memory_percent, memory_rss_bytes) VALUES (?, 100, 'app_a.exe', 10.0, 5.0, 1000)", (base_hour + 10,))
            cur.execute("INSERT INTO process_snapshots (timestamp, pid, name, cpu_percent, memory_percent, memory_rss_bytes) VALUES (?, 100, 'app_a.exe', 20.0, 6.0, 1000)", (base_hour + 20,))
            cur.execute("INSERT INTO process_snapshots (timestamp, pid, name, cpu_percent, memory_percent, memory_rss_bytes) VALUES (?, 100, 'app_a.exe', 30.0, 7.0, 1000)", (base_hour + 30,))

            # Знімки PID 0 та System (мають бути виключені)
            cur.execute("INSERT INTO process_snapshots (timestamp, pid, name, cpu_percent, memory_percent, memory_rss_bytes) VALUES (?, 0, 'System Idle Process', 99.0, 0.1, 0)", (base_hour + 10,))
            cur.execute("INSERT INTO process_snapshots (timestamp, pid, name, cpu_percent, memory_percent, memory_rss_bytes) VALUES (?, 4, 'System', 50.0, 1.0, 1000)", (base_hour + 10,))

        # Виконуємо агрегацію
        count = self.aggregator.aggregate_process_usage_hourly()
        self.assertGreaterEqual(count, 1)

        # Перевіряємо таблицю process_usage_hourly
        with db_session(self.db_path) as conn:
            cur = conn.cursor()
            cur.execute("SELECT * FROM process_usage_hourly WHERE process_name = 'app_a.exe'")
            row = cur.fetchone()
            self.assertIsNotNone(row)
            self.assertEqual(row["appearances_count"], 3)
            self.assertEqual(row["avg_cpu_percent"], 20.0)
            self.assertEqual(row["max_cpu_percent"], 30.0)
            self.assertEqual(row["max_memory_percent"], 7.0)
            self.assertEqual(row["first_seen_ts"], base_hour + 10)
            self.assertEqual(row["last_seen_ts"], base_hour + 30)

            # Переконуємось, що PID 0 та System не потрапили у підсумок
            cur.execute("SELECT COUNT(*) AS c FROM process_usage_hourly WHERE process_name IN ('System Idle Process', 'System')")
            self.assertEqual(cur.fetchone()["c"], 0)

    def test_hourly_rotation_30_days(self):
        """Перевірка видалення записів process_usage_hourly, старших за 30 днів."""
        now = int(time.time())
        old_hour = now - (32 * 86400)      # 32 дні тому -> має видалитись
        recent_hour = now - (10 * 86400)   # 10 днів тому -> має залишитись

        with db_session(self.db_path) as conn:
            cur = conn.cursor()
            cur.execute("""
                INSERT INTO process_usage_hourly (hour, process_name, appearances_count, avg_cpu_percent, max_cpu_percent, max_memory_percent, first_seen_ts, last_seen_ts)
                VALUES (?, 'old_app.exe', 1, 10.0, 10.0, 5.0, ?, ?)
            """, (old_hour, old_hour, old_hour))
            cur.execute("""
                INSERT INTO process_usage_hourly (hour, process_name, appearances_count, avg_cpu_percent, max_cpu_percent, max_memory_percent, first_seen_ts, last_seen_ts)
                VALUES (?, 'recent_app.exe', 1, 10.0, 10.0, 5.0, ?, ?)
            """, (recent_hour, recent_hour, recent_hour))

        cleaned = self.aggregator.cleanup_old_data()
        self.assertIn("proc_usage_hourly", cleaned)
        self.assertGreaterEqual(cleaned["proc_usage_hourly"], 1)

        with db_session(self.db_path) as conn:
            cur = conn.cursor()
            cur.execute("SELECT process_name FROM process_usage_hourly")
            remaining = [r["process_name"] for r in cur.fetchall()]
            self.assertNotIn("old_app.exe", remaining)
            self.assertIn("recent_app.exe", remaining)

if __name__ == "__main__":
    unittest.main()
