"""
Тести збирача метрик psutil, кешу процесів та нормування на ядра.
"""

import tempfile
import time
import unittest
from pathlib import Path

from app.core.collector import MetricCollector
from app.db.connection import init_db

class TestMetricCollector(unittest.TestCase):
    """Перевірка логіки збору метрик системи та процесів."""

    def setUp(self):
        self.temp_file = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.db_path = Path(self.temp_file.name)
        self.temp_file.close()
        init_db(self.db_path)
        self.collector = MetricCollector(db_path=self.db_path)

    def tearDown(self):
        self.collector.stop()
        for p in [self.db_path, Path(str(self.db_path) + "-wal"), Path(str(self.db_path) + "-shm")]:
            if p.exists():
                try:
                    p.unlink()
                except Exception:
                    pass

    def test_collect_system_metrics(self):
        """Перевірка структури зібраних системних метрик."""
        metrics = self.collector._collect_system_metrics()
        self.assertIn("timestamp", metrics)
        self.assertIn("cpu_total", metrics)
        self.assertIn("cpu_max", metrics)
        self.assertEqual(metrics["cpu_total"], metrics["cpu_max"], "Для 1s ряду cpu_max має дорівнювати cpu_total")
        self.assertIn("ram_percent", metrics)
        self.assertIn("ram_max", metrics)
        self.assertEqual(metrics["ram_percent"], metrics["ram_max"], "Для 1s ряду ram_max має дорівнювати ram_percent")
        self.assertIn("cpu_cores", metrics)
        self.assertIsInstance(metrics["cpu_cores"], list)
        self.assertIn("disks_info", metrics)
        self.assertIsInstance(metrics["disks_info"], list)

    def test_process_cache_and_filtering(self):
        """
        Перевірка фільтрації процесів:
        - PID 0 та System виключені;
        - CPU кожного процесу <= 100% (нормовано на ядра);
        - Кеш процесів зберігається між викликами.
        """
        # Перший виклик - наповнення кешу
        procs_1 = self.collector._refresh_processes()
        initial_cache_size = len(self.collector._process_cache)
        self.assertGreater(initial_cache_size, 0, "Кеш процесів має містити активні процеси")

        # Невелика пауза і другий виклик для заміру CPU
        time.sleep(0.1)
        procs_2 = self.collector._refresh_processes()

        for p in procs_2:
            self.assertNotEqual(p["pid"], 0, "PID 0 (System Idle Process) не повинен бути у списку")
            self.assertNotIn(p["name"].lower(), ("system idle process", "system"), "Службові процеси мають бути виключені")
            self.assertLessEqual(p["cpu_percent"], 100.0, "Нормований CPU не може перевищувати 100%")
            self.assertGreaterEqual(p["cpu_percent"], 0.0)

        # Перевірка сортування
        top_cpu = self.collector.get_top_processes(sort_by="cpu", limit=5)
        self.assertLessEqual(len(top_cpu), 5)
        for i in range(len(top_cpu) - 1):
            self.assertGreaterEqual(top_cpu[i]["cpu_percent"], top_cpu[i + 1]["cpu_percent"])

if __name__ == "__main__":
    unittest.main()
