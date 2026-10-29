"""
Тести базових маршрутів Flask API та віддачі дашборду.
"""

import tempfile
import unittest
from pathlib import Path

from app.core.collector import MetricCollector
from app.db.connection import init_db
from app.server import create_app

class TestServerAPI(unittest.TestCase):
    """Перевірка відповідей Flask API та статичних файлів дашборду."""

    def setUp(self):
        self.temp_file = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.db_path = Path(self.temp_file.name)
        self.temp_file.close()
        init_db(self.db_path)

        self.collector = MetricCollector(db_path=self.db_path)
        # Зберемо один знімок для перевірки
        metrics = self.collector._collect_system_metrics()
        self.collector._save_system_metrics(metrics)
        procs = self.collector._refresh_processes()
        self.collector._save_process_snapshots(procs)

        self.app = create_app(collector=self.collector, db_path=self.db_path)
        self.client = self.app.test_client()

    def tearDown(self):
        self.collector.stop()
        for p in [self.db_path, Path(str(self.db_path) + "-wal"), Path(str(self.db_path) + "-shm")]:
            if p.exists():
                try:
                    p.unlink()
                except Exception:
                    pass

    def test_index_serves_html(self):
        """GET / повертає 200 та HTML-документ дашборду."""
        with self.client.get("/") as res:
            self.assertEqual(res.status_code, 200)
            self.assertIn("text/html", res.content_type)
            html = res.get_data(as_text=True)
            self.assertIn("System Monitor", html)
            self.assertIn("mainTelemetryChart", html)
            self.assertIn("tabLive", html)

    def test_static_files_served(self):
        """Перевірка віддачі файлів CSS та JS зі статичного каталогу."""
        # CSS
        with self.client.get("/static/css/style.css") as res_css:
            self.assertEqual(res_css.status_code, 200)
            self.assertIn("text/css", res_css.content_type)
            self.assertIn("--bg-primary", res_css.get_data(as_text=True))

        # JS chart
        with self.client.get("/static/js/chart.js") as res_chart:
            self.assertEqual(res_chart.status_code, 200)
            self.assertIn("TelemetryChart", res_chart.get_data(as_text=True))

        # JS app
        with self.client.get("/static/js/app.js") as res_app:
            self.assertEqual(res_app.status_code, 200)
            self.assertIn("fetchCurrentMetrics", res_app.get_data(as_text=True))

        # JS tabs
        with self.client.get("/static/js/tabs.js") as res_tabs:
            self.assertEqual(res_tabs.status_code, 200)
            self.assertIn("TabsController", res_tabs.get_data(as_text=True))

    def test_status_endpoint(self):
        """GET /api/status повертає 200, статус ok та логічні ядра."""
        with self.client.get("/api/status") as res:
            self.assertEqual(res.status_code, 200)
            data = res.get_json()
            self.assertEqual(data["status"], "ok")
            self.assertIn("logical_cores", data)
            self.assertGreater(data["logical_cores"], 0)

    def test_current_metrics_endpoint(self):
        """GET /api/metrics/current містить локальний час та показники."""
        with self.client.get("/api/metrics/current") as res:
            self.assertEqual(res.status_code, 200)
            data = res.get_json()
            self.assertIn("cpu_total", data)
            self.assertIn("local_time", data)
            self.assertIn("ram_percent", data)

    def test_history_metrics_endpoint(self):
        """GET /api/metrics/history повертає часовий ряд."""
        with self.client.get("/api/metrics/history?range=live") as res:
            self.assertEqual(res.status_code, 200)
            data = res.get_json()
            self.assertEqual(data["range"], "live")
            self.assertGreaterEqual(data["count"], 1)

    def test_processes_endpoint(self):
        """GET /api/processes повертає список процесів без PID 0."""
        with self.client.get("/api/processes?sort=cpu&limit=10") as res:
            self.assertEqual(res.status_code, 200)
            data = res.get_json()
            self.assertEqual(data["sort_by"], "cpu")
            procs = data["processes"]
            self.assertIsInstance(procs, list)
            for p in procs:
                self.assertNotEqual(p["pid"], 0)

if __name__ == "__main__":
    unittest.main()
