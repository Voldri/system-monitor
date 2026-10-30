"""
Тести розрахунків системної аналітики (Analytics Engine):
- Середні та пікові значення CPU, RAM, GPU
- Час у червоній зоні (CPU > 90%, RAM > 85%)
- Прогноз заповнення дисків
- Розрахунок індексу здоров'я системи (0–100) та статусів
- Рейтинг винуватців тривог
"""

import json
import tempfile
import time
import unittest
from pathlib import Path

from app.db.connection import db_session, init_db
from app.services.analytics import _calculate_health_index, get_system_analytics

class TestAnalytics(unittest.TestCase):
    """Тестування модуля системної аналітики."""

    def setUp(self):
        self.temp_file = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.db_path = Path(self.temp_file.name)
        self.temp_file.close()
        init_db(self.db_path)

    def tearDown(self):
        for p in [self.db_path, Path(str(self.db_path) + "-wal"), Path(str(self.db_path) + "-shm")]:
            if p.exists():
                try:
                    p.unlink()
                except Exception:
                    pass

    def test_analytics_metrics_and_redzone(self):
        """Перевірка обчислення середніх, піків та часу в червоній зоні."""
        now = 1700000000

        with db_session(self.db_path) as conn:
            cur = conn.cursor()
            # 1. Звичайна точка
            cur.execute("""
                INSERT INTO metrics (timestamp, cpu_total, cpu_max, cpu_cores, ram_used_bytes, ram_total_bytes, ram_percent, ram_max, swap_used_bytes, swap_total_bytes, disk_read_bytes_sec, disk_write_bytes_sec, net_sent_bytes_sec, net_recv_bytes_sec, disks_info, is_aggregated, gpu_percent, gpu_max, gpu_temp_c)
                VALUES (?, 20.0, 20.0, '[]', 1000, 4000, 25.0, 25.0, 0, 0, 0, 0, 0, 0, '[]', 0, 10.0, 10.0, 50.0)
            """, (now - 100,))

            # 2. Точка в червоній зоні (CPU 95%, RAM 92%)
            cur.execute("""
                INSERT INTO metrics (timestamp, cpu_total, cpu_max, cpu_cores, ram_used_bytes, ram_total_bytes, ram_percent, ram_max, swap_used_bytes, swap_total_bytes, disk_read_bytes_sec, disk_write_bytes_sec, net_sent_bytes_sec, net_recv_bytes_sec, disks_info, is_aggregated, gpu_percent, gpu_max, gpu_temp_c)
                VALUES (?, 95.0, 95.0, '[]', 3800, 4000, 92.0, 92.0, 0, 0, 0, 0, 0, 0, '[]', 0, 80.0, 80.0, 86.0)
            """, (now - 50,))

        ana = get_system_analytics(period="1h", db_path=self.db_path, now=now)
        summary = ana["summary"]

        # Середні та піки
        self.assertEqual(summary["cpu_avg"], (20.0 + 95.0) / 2)
        self.assertEqual(summary["cpu_peak"], 95.0)
        self.assertEqual(summary["ram_avg"], (25.0 + 92.0) / 2)
        self.assertEqual(summary["ram_peak"], 92.0)
        self.assertEqual(summary["gpu_avg"], 45.0)
        self.assertEqual(summary["gpu_peak"], 80.0)
        self.assertEqual(summary["gpu_temp_peak"], 86.0)

        # Червона зона: 1 рядок по 1 с
        self.assertEqual(summary["cpu_redzone_sec"], 1)
        self.assertEqual(summary["ram_redzone_sec"], 1)
        self.assertEqual(summary["gpu_temp_redzone_sec"], 1)

    def test_disk_projection(self):
        """Перевірка розрахунку прогнозу заповнення дисків (число днів та статус)."""
        now = 1700000000
        # Диск витрачає 100 МБ за 600 секунд (10 хвилин) -> ~3.5 дні
        total_b = 100 * (1024**3)  # 100 GB
        free_t0 = 50 * (1024**3)   # 50 GB free
        consumed = 100 * (1024**2) # 100 MB consumed
        free_t1 = free_t0 - consumed

        disks_t0 = json.dumps([{"mountpoint": "C:", "device": "C:", "total_bytes": total_b, "free_bytes": free_t0}])
        disks_t1 = json.dumps([{"mountpoint": "C:", "device": "C:", "total_bytes": total_b, "free_bytes": free_t1}])

        with db_session(self.db_path) as conn:
            cur = conn.cursor()
            cur.execute("""
                INSERT INTO metrics (timestamp, cpu_total, cpu_max, cpu_cores, ram_used_bytes, ram_total_bytes, ram_percent, ram_max, swap_used_bytes, swap_total_bytes, disk_read_bytes_sec, disk_write_bytes_sec, net_sent_bytes_sec, net_recv_bytes_sec, disks_info, is_aggregated)
                VALUES (?, 10, 10, '[]', 0, 0, 10, 10, 0, 0, 0, 0, 0, 0, ?, 0)
            """, (now - 600, disks_t0))
            cur.execute("""
                INSERT INTO metrics (timestamp, cpu_total, cpu_max, cpu_cores, ram_used_bytes, ram_total_bytes, ram_percent, ram_max, swap_used_bytes, swap_total_bytes, disk_read_bytes_sec, disk_write_bytes_sec, net_sent_bytes_sec, net_recv_bytes_sec, disks_info, is_aggregated)
                VALUES (?, 10, 10, '[]', 0, 0, 10, 10, 0, 0, 0, 0, 0, 0, ?, 0)
            """, (now, disks_t1))

        ana = get_system_analytics(period="1h", db_path=self.db_path, now=now)
        projs = ana["disk_projections"]
        self.assertEqual(len(projs), 1)
        c_proj = projs[0]
        self.assertEqual(c_proj["mountpoint"], "C:")
        self.assertIsNotNone(c_proj["days_to_full"])
        self.assertGreater(c_proj["days_to_full"], 1.0)
        self.assertEqual(c_proj["status"], "warning")

    def test_disk_projection_critical_less_than_day(self):
        """Перевірка критичного прогнозу заповнення диска менш ніж за добу (days_to_full <= 1.0, status='critical')."""
        now = 1700000000
        # Диск витрачає 500 МБ за 600 секунд (10 хвилин) -> ~70 GB/добу при залишку ~49.5 GB -> 0.7 дні (< 1 доба)
        total_b = 100 * (1024**3)  # 100 GB
        free_t0 = 50 * (1024**3)   # 50 GB free
        consumed = 500 * (1024**2) # 500 MB consumed
        free_t1 = free_t0 - consumed

        disks_t0 = json.dumps([{"mountpoint": "D:", "device": "D:", "total_bytes": total_b, "free_bytes": free_t0}])
        disks_t1 = json.dumps([{"mountpoint": "D:", "device": "D:", "total_bytes": total_b, "free_bytes": free_t1}])

        with db_session(self.db_path) as conn:
            cur = conn.cursor()
            cur.execute("""
                INSERT INTO metrics (timestamp, cpu_total, cpu_max, cpu_cores, ram_used_bytes, ram_total_bytes, ram_percent, ram_max, swap_used_bytes, swap_total_bytes, disk_read_bytes_sec, disk_write_bytes_sec, net_sent_bytes_sec, net_recv_bytes_sec, disks_info, is_aggregated)
                VALUES (?, 10, 10, '[]', 0, 0, 10, 10, 0, 0, 0, 0, 0, 0, ?, 0)
            """, (now - 600, disks_t0))
            cur.execute("""
                INSERT INTO metrics (timestamp, cpu_total, cpu_max, cpu_cores, ram_used_bytes, ram_total_bytes, ram_percent, ram_max, swap_used_bytes, swap_total_bytes, disk_read_bytes_sec, disk_write_bytes_sec, net_sent_bytes_sec, net_recv_bytes_sec, disks_info, is_aggregated)
                VALUES (?, 10, 10, '[]', 0, 0, 10, 10, 0, 0, 0, 0, 0, 0, ?, 0)
            """, (now, disks_t1))

        ana = get_system_analytics(period="1h", db_path=self.db_path, now=now)
        projs = ana["disk_projections"]
        self.assertEqual(len(projs), 1)
        d_proj = projs[0]
        self.assertEqual(d_proj["mountpoint"], "D:")
        self.assertIsNotNone(d_proj["days_to_full"])
        self.assertLessEqual(d_proj["days_to_full"], 1.0)
        self.assertGreater(d_proj["days_to_full"], 0.0)
        self.assertEqual(d_proj["status"], "critical")

    def test_health_index_formula(self):
        """Перевірка меж індексу стану (0–100) та статусів."""
        # Ідеальний стан: 100
        score_perfect, status_perfect, _ = _calculate_health_index(
            cpu_avg=5.0, ram_avg=20.0, cpu_rz_sec=0, ram_rz_sec=0,
            duration_sec=3600, active_alerts_count=0, period_alerts_count=0,
            min_disk_free_pct=40.0
        )
        self.assertGreaterEqual(score_perfect, 90)
        self.assertEqual(status_perfect, "Відмінний")

        # Критичний стан: навантаження 100%, активні тривоги, диск переповнений
        score_bad, status_bad, bd = _calculate_health_index(
            cpu_avg=98.0, ram_avg=95.0, cpu_rz_sec=3600, ram_rz_sec=3600,
            duration_sec=3600, active_alerts_count=3, period_alerts_count=10,
            min_disk_free_pct=2.0, gpu_temp_avg=88.0, gpu_temp_peak=92.0
        )
        self.assertLessEqual(score_bad, 40)
        self.assertEqual(status_bad, "Критичний")
        self.assertGreaterEqual(score_bad, 0, "Індекс не може бути меншим за 0")

        # Перевірка нової формули штрафу за RAM: 55% -> 0, 80% -> 10, 100% -> 20
        _, _, bd_55 = _calculate_health_index(
            cpu_avg=0.0, ram_avg=55.0, cpu_rz_sec=0, ram_rz_sec=0,
            duration_sec=3600, active_alerts_count=0, period_alerts_count=0,
            min_disk_free_pct=40.0
        )
        self.assertEqual(bd_55["penalty_ram_load"], 0.0)

        _, _, bd_80 = _calculate_health_index(
            cpu_avg=0.0, ram_avg=80.0, cpu_rz_sec=0, ram_rz_sec=0,
            duration_sec=3600, active_alerts_count=0, period_alerts_count=0,
            min_disk_free_pct=40.0
        )
        self.assertEqual(bd_80["penalty_ram_load"], 10.0)

        _, _, bd_100 = _calculate_health_index(
            cpu_avg=0.0, ram_avg=100.0, cpu_rz_sec=0, ram_rz_sec=0,
            duration_sec=3600, active_alerts_count=0, period_alerts_count=0,
            min_disk_free_pct=40.0
        )
        self.assertEqual(bd_100["penalty_ram_load"], 20.0)

if __name__ == "__main__":
    unittest.main()
