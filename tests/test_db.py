"""
Тести схеми бази даних SQLite, агрегатора та ротації.
"""

import json
import tempfile
import time
import unittest
from pathlib import Path

from app.core.aggregator import DataAggregator
from app.db.connection import db_session, get_db_connection, init_db

class TestDatabaseAndAggregator(unittest.TestCase):
    """Тестування операцій з базою даних, розрахунку avg/max та ротації."""

    def setUp(self):
        # Створюємо тимчасовий файл БД для кожного тесту
        self.temp_file = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.db_path = Path(self.temp_file.name)
        self.temp_file.close()
        init_db(self.db_path)

    def tearDown(self):
        # Видаляємо тестову базу
        for p in [self.db_path, Path(str(self.db_path) + "-wal"), Path(str(self.db_path) + "-shm")]:
            if p.exists():
                try:
                    p.unlink()
                except Exception:
                    pass

    def test_schema_columns(self):
        """Перевірка наявності cpu_max, ram_max та відсутності username."""
        conn = get_db_connection(self.db_path)
        cur = conn.cursor()

        # Перевірка таблиці metrics
        cur.execute("PRAGMA table_info(metrics)")
        metrics_cols = {row["name"] for row in cur.fetchall()}
        self.assertIn("cpu_total", metrics_cols)
        self.assertIn("cpu_max", metrics_cols)
        self.assertIn("ram_percent", metrics_cols)
        self.assertIn("ram_max", metrics_cols)

        # Перевірка таблиці process_snapshots
        cur.execute("PRAGMA table_info(process_snapshots)")
        proc_cols = {row["name"] for row in cur.fetchall()}
        self.assertIn("cpu_percent", proc_cols)
        self.assertIn("alert_id", proc_cols)
        self.assertNotIn("username", proc_cols, "Поле username повинно бути відсутнє в схемі")

        conn.close()

    def test_minute_aggregation_with_max(self):
        """Перевірка коректності обчислення середнього та максимуму (cpu_max, ram_max)."""
        aggregator = DataAggregator(db_path=self.db_path)

        # Фіксуємо хвилину в минулому (наприклад, 10 хвилин тому)
        now = int(time.time())
        target_minute = ((now - 600) // 60) * 60

        # Вставляємо 3 сирі посекундні записи в межах цієї хвилини
        sample_rows = [
            (target_minute + 5, 20.0, 20.0, 50.0, 50.0),
            (target_minute + 25, 95.0, 95.0, 88.0, 88.0),  # Пік
            (target_minute + 45, 35.0, 35.0, 60.0, 60.0),
        ]

        query = """
        INSERT INTO metrics (
            timestamp, cpu_total, cpu_max, cpu_cores, ram_used_bytes, ram_total_bytes,
            ram_percent, ram_max, swap_used_bytes, swap_total_bytes, disk_read_bytes_sec,
            disk_write_bytes_sec, net_sent_bytes_sec, net_recv_bytes_sec, disks_info, is_aggregated
        ) VALUES (?, ?, ?, ?, 1000, 2000, ?, ?, 100, 200, 10.0, 20.0, 5.0, 15.0, '[]', 0)
        """

        with db_session(self.db_path) as conn:
            for ts, cpu, cpu_m, ram, ram_m in sample_rows:
                conn.execute(query, (ts, cpu, cpu_m, json.dumps([cpu]), ram, ram_m))

        # Запускаємо цикл агрегації
        aggregated_count = aggregator.aggregate_completed_minutes()
        self.assertEqual(aggregated_count, 1, "Має бути агрегована рівно 1 завершена хвилина")

        # Перевіряємо агрегований запис
        with db_session(self.db_path) as conn:
            cur = conn.cursor()
            cur.execute("SELECT * FROM metrics WHERE is_aggregated = 1 AND timestamp = ?", (target_minute,))
            agg_row = cur.fetchone()
            self.assertIsNotNone(agg_row)

            # Середнє CPU: (20 + 95 + 35) / 3 = 50.0
            self.assertAlmostEqual(agg_row["cpu_total"], 50.0, places=1)
            # Максимум CPU: 95.0
            self.assertEqual(agg_row["cpu_max"], 95.0)

            # Середнє RAM: (50 + 88 + 60) / 3 = 66.0
            self.assertAlmostEqual(agg_row["ram_percent"], 66.0, places=1)
            # Максимум RAM: 88.0
            self.assertEqual(agg_row["ram_max"], 88.0)

    def test_cleanup_rotation_rules(self):
        """
        Перевірка правил ротації:
        - сирі 1s записи > 2 год видаляються;
        - звичайні знімки процесів > 24 год видаляються;
        - знімки з alert_id зберігаються до 30 днів.
        """
        aggregator = DataAggregator(db_path=self.db_path)
        now = int(time.time())

        old_raw_ts = now - (3 * 3600)         # 3 години тому (має видалитися)
        recent_raw_ts = now - (30 * 60)       # 30 хв тому (має залишитися)
        old_proc_ts = now - (25 * 3600)       # 25 годин тому (має видалитися для звичайних)

        with db_session(self.db_path) as conn:
            # 1. Створюємо запис тривоги 25 годин тому
            cur = conn.cursor()
            cur.execute("""
                INSERT INTO alerts (timestamp, metric_name, rule_condition, trigger_value, threshold_value, severity, culprits_summary)
                VALUES (?, 'CPU', '> 90%', 95.0, 90.0, 'critical', '[]')
            """, (old_proc_ts,))
            alert_id = cur.lastrowid

            # 2. Метрики (стара та свіжа)
            insert_metric = """
            INSERT INTO metrics (timestamp, cpu_total, cpu_max, cpu_cores, ram_used_bytes, ram_total_bytes,
                                ram_percent, ram_max, swap_used_bytes, swap_total_bytes, disk_read_bytes_sec,
                                disk_write_bytes_sec, net_sent_bytes_sec, net_recv_bytes_sec, disks_info, is_aggregated)
            VALUES (?, 10.0, 10.0, '[]', 100, 200, 50.0, 50.0, 10, 20, 0, 0, 0, 0, '[]', 0)
            """
            conn.execute(insert_metric, (old_raw_ts,))
            conn.execute(insert_metric, (recent_raw_ts,))

            # 3. Звичайний процес 25 годин тому (alert_id = NULL)
            conn.execute("""
                INSERT INTO process_snapshots (timestamp, alert_id, pid, name, cpu_percent, memory_percent, memory_rss_bytes)
                VALUES (?, NULL, 1111, 'old_normal.exe', 5.0, 2.0, 1024)
            """, (old_proc_ts,))

            # 4. Процес для алерту 25 годин тому (alert_id = alert_id)
            conn.execute("""
                INSERT INTO process_snapshots (timestamp, alert_id, pid, name, cpu_percent, memory_percent, memory_rss_bytes)
                VALUES (?, ?, 2222, 'culprit.exe', 85.0, 5.0, 4096)
            """, (old_proc_ts, alert_id))

        # Виконуємо очищення
        cleaned = aggregator.cleanup_old_data()

        with db_session(self.db_path) as conn:
            cur = conn.cursor()

            # Стара метрика видалена, нова залишилася
            cur.execute("SELECT COUNT(*) AS c FROM metrics WHERE timestamp = ?", (old_raw_ts,))
            self.assertEqual(cur.fetchone()["c"], 0)
            cur.execute("SELECT COUNT(*) AS c FROM metrics WHERE timestamp = ?", (recent_raw_ts,))
            self.assertEqual(cur.fetchone()["c"], 1)

            # Звичайний старий процес видалено
            cur.execute("SELECT COUNT(*) AS c FROM process_snapshots WHERE pid = 1111")
            self.assertEqual(cur.fetchone()["c"], 0)

            # Тривожний процес залишився!
            cur.execute("SELECT COUNT(*) AS c FROM process_snapshots WHERE pid = 2222")
            self.assertEqual(cur.fetchone()["c"], 1)

if __name__ == "__main__":
    unittest.main()
