"""
Тести механізму системних тривог (Alerts Engine):
- Спрацювання правил після заданої тривалості (CPU, RAM, GPU, диски)
- Закриття тривог при нормалізації показників (resolved_at)
- Відсутність спаму (не створюється дублікат, поки тривога активна)
- Топ-3 винуватці у process_snapshots без PID 0 та System
"""

import tempfile
import time
import unittest
from pathlib import Path

from app.core.alerts import AlertManager
from app.db.connection import db_session, init_db

class TestAlertsEngine(unittest.TestCase):
    """Тестування життєвого циклу тривог та фіксації винуватців."""

    def setUp(self):
        self.temp_file = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.db_path = Path(self.temp_file.name)
        self.temp_file.close()
        init_db(self.db_path)
        self.alert_manager = AlertManager(db_path=self.db_path)

    def tearDown(self):
        for p in [self.db_path, Path(str(self.db_path) + "-wal"), Path(str(self.db_path) + "-shm")]:
            if p.exists():
                try:
                    p.unlink()
                except Exception:
                    pass

    def test_cpu_alert_duration_and_resolution(self):
        """CPU > 90% не спрацьовує одразу, спрацьовує через 30 с і закривається при падінні."""
        t0 = 100000.0
        sample_procs = [
            {"pid": 0, "name": "System Idle Process", "cpu_percent": 99.0, "memory_percent": 0.1, "memory_rss_bytes": 0},
            {"pid": 4, "name": "System", "cpu_percent": 50.0, "memory_percent": 1.0, "memory_rss_bytes": 1000},
            {"pid": 101, "name": "heavy_worker.exe", "cpu_percent": 65.0, "memory_percent": 12.0, "memory_rss_bytes": 500000},
            {"pid": 102, "name": "compiler.exe", "cpu_percent": 25.0, "memory_percent": 8.0, "memory_rss_bytes": 300000},
            {"pid": 103, "name": "browser.exe", "cpu_percent": 5.0, "memory_percent": 20.0, "memory_rss_bytes": 900000},
        ]

        metrics_high = {"timestamp": int(t0), "cpu_total": 95.0, "ram_percent": 50.0, "disks_info": []}

        # 1. Початок перевищення (0 с) -> ще не повинно спрацювати
        res1 = self.alert_manager.check_metrics(metrics_high, sample_procs, now=t0)
        self.assertEqual(len(res1), 0)
        self.assertEqual(len(self.alert_manager.get_active_alerts()), 0)

        # 2. Минуло 20 с -> поріг 30 с ще не досягнуто
        res2 = self.alert_manager.check_metrics(metrics_high, sample_procs, now=t0 + 20)
        self.assertEqual(len(res2), 0)
        self.assertEqual(len(self.alert_manager.get_active_alerts()), 0)

        # 3. Минуло 31 с -> ТРИВОГА МАЄ СПРАЦЮВАТИ
        res3 = self.alert_manager.check_metrics(metrics_high, sample_procs, now=t0 + 31)
        self.assertEqual(len(res3), 1)
        self.assertEqual(res3[0]["type"], "triggered")

        active_alerts = self.alert_manager.get_active_alerts()
        self.assertEqual(len(active_alerts), 1)
        alert = active_alerts[0]
        self.assertIn("CPU > 90", alert["rule_condition"])
        self.assertIsNone(alert["resolved_at"])

        # Перевірка винуватців: PID 0 та System мають бути відфільтровані
        culprits = alert["culprits"]
        self.assertEqual(len(culprits), 3)
        culprit_names = [c["name"] for c in culprits]
        self.assertNotIn("System Idle Process", culprit_names)
        self.assertNotIn("System", culprit_names)
        self.assertEqual(culprits[0]["name"], "heavy_worker.exe")
        self.assertEqual(culprits[1]["name"], "compiler.exe")
        self.assertEqual(culprits[2]["name"], "browser.exe")

        # 4. Захист від спаму: наступне опитування при високому CPU НЕ створює нову тривогу
        res4 = self.alert_manager.check_metrics(metrics_high, sample_procs, now=t0 + 35)
        self.assertEqual(len(res4), 0)
        self.assertEqual(len(self.alert_manager.get_active_alerts()), 1)

        # 5. Показник повернувся в норму (CPU 40%) -> тривога закривається (resolved_at)
        metrics_normal = {"timestamp": int(t0 + 40), "cpu_total": 40.0, "ram_percent": 50.0, "disks_info": []}
        res5 = self.alert_manager.check_metrics(metrics_normal, sample_procs, now=t0 + 40)
        self.assertEqual(len(res5), 1)
        self.assertEqual(res5[0]["type"], "resolved")

        # Активних тривог більше немає
        self.assertEqual(len(self.alert_manager.get_active_alerts()), 0)

        # В історії тривога збережена з resolved_at
        all_alerts = self.alert_manager.get_alerts()
        self.assertEqual(len(all_alerts), 1)
        self.assertIsNotNone(all_alerts[0]["resolved_at"])
        self.assertEqual(all_alerts[0]["resolved_at"], int(t0 + 40))

    def test_ram_and_disk_alerts(self):
        """Перевірка спрацювання тривог по RAM (понад 10 с) та критичному вільному місцю на диску (< 5%)."""
        t0 = 200000.0
        sample_procs = [
            {"pid": 201, "name": "memory_leak.exe", "cpu_percent": 5.0, "memory_percent": 88.0, "memory_rss_bytes": 14000000000},
            {"pid": 202, "name": "database.exe", "cpu_percent": 10.0, "memory_percent": 5.0, "memory_rss_bytes": 1000000000},
        ]

        metrics_ram_high = {
            "timestamp": int(t0),
            "cpu_total": 10.0,
            "ram_percent": 89.0,
            "disks_info": [
                {"mountpoint": "C:", "device": "C:", "total_bytes": 100000, "free_bytes": 3000, "free_percent": 3.0}
            ],
            "gpu_temp_c": 50.0,
        }

        # Дисковий алерт спрацьовує миттєво (< 5%)
        # RAM ще не досяг 10 секунд
        res1 = self.alert_manager.check_metrics(metrics_ram_high, sample_procs, now=t0)
        active = self.alert_manager.get_active_alerts()
        self.assertEqual(len(active), 1)
        self.assertIn("DISK: C:", active[0]["metric_name"])

        # Через 11 секунд спрацьовує і RAM алерт
        res2 = self.alert_manager.check_metrics(metrics_ram_high, sample_procs, now=t0 + 11)
        active = self.alert_manager.get_active_alerts()
        self.assertEqual(len(active), 2)

        # Винуватець для RAM сортується за пам'яттю
        ram_alert = next(a for a in active if "RAM" in a["metric_name"])
        self.assertEqual(ram_alert["culprits"][0]["name"], "memory_leak.exe")

        # Для диска винуватці не визначаються ("не застосовується")
        disk_alert = next(a for a in active if "DISK" in a["metric_name"])
        self.assertEqual(disk_alert["culprits"], [])
        self.assertEqual(disk_alert["culprits_summary"], "не застосовується")

    def test_gpu_temp_alert(self):
        """Перевірка тривоги по температурі GPU > 85 °C понад 30 с."""
        t0 = 300000.0
        sample_procs = [
            {"pid": 301, "name": "3d_game.exe", "cpu_percent": 85.0, "memory_percent": 15.0, "memory_rss_bytes": 1000000},
            {"pid": 302, "name": "miner.exe", "cpu_percent": 12.0, "memory_percent": 50.0, "memory_rss_bytes": 500000},
        ]
        metrics_gpu_hot = {
            "timestamp": int(t0),
            "cpu_total": 20.0,
            "ram_percent": 30.0,
            "disks_info": [],
            "gpu_temp_c": 88.0,
        }

        # 0 с
        self.alert_manager.check_metrics(metrics_gpu_hot, sample_procs, now=t0)
        self.assertEqual(len(self.alert_manager.get_active_alerts()), 0)

        # 31 с -> спрацьовує
        self.alert_manager.check_metrics(metrics_gpu_hot, sample_procs, now=t0 + 31)
        active = self.alert_manager.get_active_alerts()
        self.assertEqual(len(active), 1)
        gpu_alert = active[0]
        self.assertIn("GPU_TEMP", gpu_alert["metric_name"])
        self.assertIn("окремої GPU-статистики процесів немає", gpu_alert["rule_condition"])
        self.assertEqual(gpu_alert["culprits"][0]["name"], "3d_game.exe")

        # Температура впала до 70 °C -> закривається
        metrics_cool = dict(metrics_gpu_hot)
        metrics_cool["gpu_temp_c"] = 70.0
        self.alert_manager.check_metrics(metrics_cool, [], now=t0 + 35)
        self.assertEqual(len(self.alert_manager.get_active_alerts()), 0)

if __name__ == "__main__":
    unittest.main()
