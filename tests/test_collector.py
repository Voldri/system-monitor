"""
Тести збирача метрик psutil, кешу процесів, відкидання 0% CPU та моніторингу GPU.
"""

import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import psutil

from app.core.collector import MetricCollector
from app.db.connection import init_db

class TestMetricCollector(unittest.TestCase):
    """Перевірка логіки збору метрик системи, процесів та GPU."""

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

    def test_collect_system_metrics_structure(self):
        """Перевірка структури зібраних системних метрик."""
        # Робимо виклик для прогріву, якщо потрібно
        time.sleep(0.1)
        metrics = self.collector._collect_system_metrics()
        if metrics is None:
            time.sleep(0.1)
            metrics = self.collector._collect_system_metrics()

        self.assertIsNotNone(metrics)
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

        # Перевірка наявності полів GPU (навіть якщо None)
        self.assertIn("gpu_percent", metrics)
        self.assertIn("gpu_max", metrics)
        self.assertIn("gpu_temp_c", metrics)

    def test_first_zero_cpu_measurement_discarded(self):
        """Перевірка відкидання першого нульового виміру CPU після старту psutil."""
        self.collector._cpu_warmed_up = False

        with patch("psutil.cpu_percent") as mock_cpu:
            # Симулюємо 0.0 при першому опитуванні
            mock_cpu.side_effect = [0.0, [0.0] * self.collector.logical_cores]
            result = self.collector._collect_system_metrics()
            self.assertIsNone(result, "Перший вимір 0.0 CPU повинен бути відкинутий (повертає None)")
            self.assertTrue(self.collector._cpu_warmed_up)

        # Наступний вимір уже не відкидається
        with patch("psutil.cpu_percent") as mock_cpu:
            mock_cpu.side_effect = [25.0, [25.0] * self.collector.logical_cores]
            result2 = self.collector._collect_system_metrics()
            self.assertIsNotNone(result2)
            self.assertEqual(result2["cpu_total"], 25.0)

    def test_gpu_none_does_not_break_saving(self):
        """Перевірка безпечного збереження метрик у SQLite, коли дані GPU = None."""
        metrics_data = {
            "timestamp": int(time.time()),
            "cpu_total": 12.0,
            "cpu_max": 12.0,
            "cpu_cores": [12.0],
            "ram_used_bytes": 1024,
            "ram_total_bytes": 4096,
            "ram_percent": 25.0,
            "ram_max": 25.0,
            "swap_used_bytes": 0,
            "swap_total_bytes": 0,
            "disk_read_bytes_sec": 0.0,
            "disk_write_bytes_sec": 0.0,
            "net_sent_bytes_sec": 0.0,
            "net_recv_bytes_sec": 0.0,
            "disks_info": [],
            "gpu_percent": None,
            "gpu_max": None,
            "gpu_mem_used_bytes": None,
            "gpu_mem_total_bytes": None,
            "gpu_temp_c": None,
        }
        # Не повинно викидати помилок
        self.collector._save_system_metrics(metrics_data)

    def test_gpu_mock_telemetry_flow(self):
        """Перевірка передачі зібраних даних GPU у зріз метрик."""
        with self.collector._lock:
            self.collector._latest_gpu_data = {
                "available": True,
                "gpu_name": "NVIDIA GeForce RTX Mock",
                "gpu_percent": 55.5,
                "gpu_mem_used_bytes": 4000000000,
                "gpu_mem_total_bytes": 8000000000,
                "gpu_temp_c": 58.0,
            }

        self.collector._cpu_warmed_up = True
        metrics = self.collector._collect_system_metrics()
        self.assertIsNotNone(metrics)
        self.assertEqual(metrics["gpu_percent"], 55.5)
        self.assertEqual(metrics["gpu_max"], 55.5)
        self.assertEqual(metrics["gpu_name"], "NVIDIA GeForce RTX Mock")
        self.assertEqual(metrics["gpu_temp_c"], 58.0)

    def test_process_cache_and_filtering(self):
        """
        Перевірка фільтрації процесів:
        - PID 0 та System виключені;
        - CPU кожного процесу <= 100% (нормовано на ядра);
        - Кеш процесів та імен працює.
        """
        procs_1 = self.collector._refresh_processes()
        initial_cache_size = len(self.collector._process_cache)
        self.assertGreater(initial_cache_size, 0, "Кеш процесів має містити активні процеси")

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

    def test_threads_status_and_is_running(self):
        """Перевірка методів is_running(), get_threads_status() та властивості _thread."""
        # До запуску
        self.assertFalse(self.collector.is_running())
        status = self.collector.get_threads_status()
        self.assertFalse(status["metrics"])
        self.assertFalse(status["processes"])
        self.assertFalse(status["gpu"])
        self.assertIsNone(self.collector._thread)

        # Симулюємо активний системний потік
        mock_th = MagicMock(is_alive=lambda: True)
        self.collector._system_thread = mock_th
        self.assertTrue(self.collector.is_running())
        self.assertEqual(self.collector._thread, mock_th)

        status = self.collector.get_threads_status()
        self.assertTrue(status["metrics"])
        self.assertFalse(status["processes"])

        # Очищуємо
        self.collector._system_thread = None

if __name__ == "__main__":
    unittest.main()
