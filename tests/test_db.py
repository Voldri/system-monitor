"""
Тести схеми бази даних SQLite, агрегатора, ротації та міграції GPU колонок.
"""

import json
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path

from app.core.aggregator import DataAggregator
from app.db.connection import db_session, get_db_connection, init_db, migrate_db

class TestDatabaseAndAggregator(unittest.TestCase):
    """Тестування операцій з базою даних, міграції, розрахунку avg/max та ротації."""

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
        """Перевірка наявності cpu_max, ram_max, колонок GPU та відсутності username."""
        conn = get_db_connection(self.db_path)
        cur = conn.cursor()

        # Перевірка таблиці metrics
        cur.execute("PRAGMA table_info(metrics)")
        metrics_cols = {row["name"] for row in cur.fetchall()}
        self.assertIn("cpu_total", metrics_cols)
        self.assertIn("cpu_max", metrics_cols)
        self.assertIn("ram_percent", metrics_cols)
        self.assertIn("ram_max", metrics_cols)
        self.assertIn("gpu_percent", metrics_cols)
        self.assertIn("gpu_max", metrics_cols)
        self.assertIn("gpu_mem_used_bytes", metrics_cols)
        self.assertIn("gpu_mem_total_bytes", metrics_cols)
        self.assertIn("gpu_temp_c", metrics_cols)

        # Перевірка таблиці process_snapshots
        cur.execute("PRAGMA table_info(process_snapshots)")
        proc_cols = {row["name"] for row in cur.fetchall()}
        self.assertIn("cpu_percent", proc_cols)
        self.assertIn("alert_id", proc_cols)
        self.assertNotIn("username", proc_cols, "Поле username повинно бути відсутнє в схемі")

        conn.close()

    def test_process_usage_hourly_table_exists(self):
        """Перевірка створення таблиці process_usage_hourly при ініціалізації та міграції."""
        conn = get_db_connection(self.db_path)
        cur = conn.cursor()
        cur.execute("PRAGMA table_info(process_usage_hourly)")
        cols = {row["name"] for row in cur.fetchall()}
        self.assertIn("hour", cols)
        self.assertIn("process_name", cols)
        self.assertIn("appearances_count", cols)
        self.assertIn("avg_cpu_percent", cols)
        self.assertIn("max_cpu_percent", cols)
        self.assertIn("max_memory_percent", cols)
        self.assertIn("first_seen_ts", cols)
        self.assertIn("last_seen_ts", cols)
        conn.close()

    def test_schema_migration_preserves_old_data(self):
        """
        Перевірка міграції схеми:
        - стара база без GPU колонок із даними
        - після migrate_db старі дані зберігаються
        - нові колонки додаються з DEFAULT NULL
        """
        temp_old = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        old_db_path = Path(temp_old.name)
        temp_old.close()

        try:
            conn = sqlite3.connect(str(old_db_path))
            conn.row_factory = sqlite3.Row
            # Створюємо таблицю за старою схемою Етапу 1 (без GPU колонок)
            conn.execute("""
                CREATE TABLE metrics (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp INTEGER NOT NULL,
                    cpu_total REAL NOT NULL,
                    cpu_max REAL NOT NULL,
                    cpu_cores TEXT NOT NULL,
                    ram_used_bytes INTEGER NOT NULL,
                    ram_total_bytes INTEGER NOT NULL,
                    ram_percent REAL NOT NULL,
                    ram_max REAL NOT NULL,
                    swap_used_bytes INTEGER NOT NULL,
                    swap_total_bytes INTEGER NOT NULL,
                    disk_read_bytes_sec REAL NOT NULL,
                    disk_write_bytes_sec REAL NOT NULL,
                    net_sent_bytes_sec REAL NOT NULL,
                    net_recv_bytes_sec REAL NOT NULL,
                    disks_info TEXT NOT NULL,
                    is_aggregated INTEGER DEFAULT 0
                )
            """)
            conn.execute("""
                INSERT INTO metrics (
                    timestamp, cpu_total, cpu_max, cpu_cores, ram_used_bytes, ram_total_bytes,
                    ram_percent, ram_max, swap_used_bytes, swap_total_bytes, disk_read_bytes_sec,
                    disk_write_bytes_sec, net_sent_bytes_sec, net_recv_bytes_sec, disks_info, is_aggregated
                ) VALUES (1000, 45.0, 45.0, '[]', 100, 200, 60.0, 60.0, 10, 20, 0, 0, 0, 0, '[]', 0)
            """)
            conn.commit()

            # Виконуємо міграцію
            migrate_db(conn)

            # Перевіряємо, що стара колонка та дані збережені
            cur = conn.cursor()
            cur.execute("SELECT * FROM metrics WHERE timestamp = 1000")
            row = cur.fetchone()
            self.assertIsNotNone(row)
            self.assertEqual(row["cpu_total"], 45.0)
            # Перевіряємо, що нові колонки GPU існують і мають значення None
            self.assertIn("gpu_percent", row.keys())
            self.assertIsNone(row["gpu_percent"])
            self.assertIsNone(row["gpu_max"])
            conn.close()
        finally:
            if old_db_path.exists():
                try:
                    old_db_path.unlink()
                except Exception:
                    pass

    def test_minute_aggregation_with_max(self):
        """Перевірка коректності обчислення середнього та максимуму (cpu_max, ram_max, gpu_max)."""
        aggregator = DataAggregator(db_path=self.db_path)

        now = int(time.time())
        target_minute = ((now - 600) // 60) * 60

        # Вставляємо 3 сирі посекундні записи в межах цієї хвилини з показниками GPU
        sample_rows = [
            (target_minute + 5, 20.0, 20.0, 50.0, 50.0, 15.0, 15.0, 2000, 8000, 45.0),
            (target_minute + 25, 95.0, 95.0, 88.0, 88.0, 85.0, 85.0, 6000, 8000, 65.0),  # Пік
            (target_minute + 45, 35.0, 35.0, 60.0, 60.0, 25.0, 25.0, 3000, 8000, 50.0),
        ]

        query = """
        INSERT INTO metrics (
            timestamp, cpu_total, cpu_max, cpu_cores, ram_used_bytes, ram_total_bytes,
            ram_percent, ram_max, swap_used_bytes, swap_total_bytes, disk_read_bytes_sec,
            disk_write_bytes_sec, net_sent_bytes_sec, net_recv_bytes_sec, disks_info, is_aggregated,
            gpu_percent, gpu_max, gpu_mem_used_bytes, gpu_mem_total_bytes, gpu_temp_c
        ) VALUES (?, ?, ?, ?, 1000, 2000, ?, ?, 100, 200, 10.0, 20.0, 5.0, 15.0, '[]', 0, ?, ?, ?, ?, ?)
        """

        with db_session(self.db_path) as conn:
            for ts, cpu, cpu_m, ram, ram_m, gpu, gpu_m, gpu_u, gpu_t, temp in sample_rows:
                conn.execute(query, (
                    ts, cpu, cpu_m, json.dumps([cpu]), ram, ram_m,
                    gpu, gpu_m, gpu_u, gpu_t, temp
                ))

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
            self.assertEqual(agg_row["cpu_max"], 95.0)

            # Середнє RAM: (50 + 88 + 60) / 3 = 66.0
            self.assertAlmostEqual(agg_row["ram_percent"], 66.0, places=1)
            self.assertEqual(agg_row["ram_max"], 88.0)

            # GPU середнє: (15 + 85 + 25) / 3 = 41.67
            self.assertAlmostEqual(agg_row["gpu_percent"], 41.67, places=1)
            # GPU максимум: 85.0
            self.assertEqual(agg_row["gpu_max"], 85.0)
            self.assertEqual(agg_row["gpu_mem_used_bytes"], (2000 + 6000 + 3000) // 3)
            self.assertAlmostEqual(agg_row["gpu_temp_c"], (45.0 + 65.0 + 50.0) / 3, places=1)

    def test_minute_aggregation_gpu_none_safe(self):
        """Перевірка агрегації, коли дані GPU відсутні (NULL) — не повинно бути помилок."""
        aggregator = DataAggregator(db_path=self.db_path)

        now = int(time.time())
        target_minute = ((now - 600) // 60) * 60

        query = """
        INSERT INTO metrics (
            timestamp, cpu_total, cpu_max, cpu_cores, ram_used_bytes, ram_total_bytes,
            ram_percent, ram_max, swap_used_bytes, swap_total_bytes, disk_read_bytes_sec,
            disk_write_bytes_sec, net_sent_bytes_sec, net_recv_bytes_sec, disks_info, is_aggregated,
            gpu_percent, gpu_max, gpu_mem_used_bytes, gpu_mem_total_bytes, gpu_temp_c
        ) VALUES (?, 10.0, 10.0, '[]', 100, 200, 30.0, 30.0, 10, 20, 0, 0, 0, 0, '[]', 0, NULL, NULL, NULL, NULL, NULL)
        """
        with db_session(self.db_path) as conn:
            conn.execute(query, (target_minute + 10,))

        aggregated_count = aggregator.aggregate_completed_minutes()
        self.assertEqual(aggregated_count, 1)

        with db_session(self.db_path) as conn:
            cur = conn.cursor()
            cur.execute("SELECT * FROM metrics WHERE is_aggregated = 1 AND timestamp = ?", (target_minute,))
            agg_row = cur.fetchone()
            self.assertIsNotNone(agg_row)
            self.assertIsNone(agg_row["gpu_percent"])
            self.assertIsNone(agg_row["gpu_max"])

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
