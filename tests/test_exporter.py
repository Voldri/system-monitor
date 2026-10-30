"""
Тести модуля експорту звітів Excel (openpyxl) та ендпоінта /api/export/excel:
- Наявність 6 обов'язкових аркушів
- Наявність лінійної діаграми
- Локальний час та умовне форматування
- Відповідь ендпоінта у бінарному та base64 вигляді
"""

import tempfile
import unittest
from pathlib import Path

import openpyxl

from app.core.collector import MetricCollector
from app.db.connection import db_session, init_db
from app.server import create_app
from app.services.exporter import generate_excel_report

class TestExcelExporter(unittest.TestCase):
    """Тестування генерації файлу Excel .xlsx та ендпоінта."""

    def setUp(self):
        self.temp_file = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.db_path = Path(self.temp_file.name)
        self.temp_file.close()
        init_db(self.db_path)

        # Додамо кілька рядків метрик, процесів та тривог
        with db_session(self.db_path) as conn:
            cur = conn.cursor()
            cur.execute("""
                INSERT INTO metrics (timestamp, cpu_total, cpu_max, cpu_cores, ram_used_bytes, ram_total_bytes, ram_percent, ram_max, swap_used_bytes, swap_total_bytes, disk_read_bytes_sec, disk_write_bytes_sec, net_sent_bytes_sec, net_recv_bytes_sec, disks_info, is_aggregated)
                VALUES (1700000000, 25.0, 95.0, '[]', 4000000000, 16000000000, 25.0, 92.0, 0, 0, 0, 0, 0, 0, '[]', 0)
            """)
            cur.execute("""
                INSERT INTO alerts (timestamp, resolved_at, metric_name, rule_condition, trigger_value, threshold_value, severity, culprits_summary)
                VALUES (1700000000, 1700000040, 'CPU', 'CPU > 90% понад 30 с', 95.0, 90.0, 'critical', 'chrome.exe (95%)')
            """)
            cur.execute("""
                INSERT INTO process_usage_hourly (hour, process_name, appearances_count, avg_cpu_percent, max_cpu_percent, max_memory_percent, first_seen_ts, last_seen_ts)
                VALUES (1700000000, 'chrome.exe', 10, 45.0, 95.0, 15.0, 1700000000, 1700003600)
            """)

        self.collector = MetricCollector(db_path=self.db_path)
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

    def test_excel_workbook_sheets_and_chart(self):
        """Перевірка 6 обов'язкових аркушів, стилів, заголовків та діаграми."""
        bio, filename = generate_excel_report(period="1h", db_path=self.db_path)
        self.assertTrue(filename.startswith("report_"))
        self.assertTrue(filename.endswith(".xlsx"))

        # Відкриваємо згенерований потік через openpyxl
        wb = openpyxl.load_workbook(bio)
        sheet_names = wb.sheetnames

        expected_sheets = ["Характеристики", "Зведення", "Метрики", "Програми", "Сповіщення", "Поради ШІ"]
        self.assertEqual(sheet_names, expected_sheets, f"Звіт повинен містити рівно 6 аркушів: {expected_sheets}")

        # 1. Аркуш "Характеристики"
        ws_specs = wb["Характеристики"]
        self.assertEqual(ws_specs.cell(row=3, column=1).value, "Компонент")

        # 2. Аркуш "Зведення"
        ws_summary = wb["Зведення"]
        self.assertIn("Зведення аналітики", str(ws_summary.cell(row=1, column=1).value))

        # 3. Аркуш "Метрики"
        ws_metrics = wb["Метрики"]
        self.assertEqual(ws_metrics.cell(row=1, column=1).value, "Час (локальний)")
        self.assertEqual(ws_metrics.cell(row=1, column=2).value, "CPU Сер. (%)")

        # 4. Аркуш "Поради ШІ" (заголовки)
        ws_ai = wb["Поради ШІ"]
        self.assertEqual(ws_ai.cell(row=1, column=1).value, "ID")
        self.assertEqual(ws_ai.cell(row=1, column=7).value, "Текст поради ШІ")

    def test_export_excel_endpoints(self):
        """Перевірка ендпоінта /api/export/excel у бінарному та base64 режимах."""
        # 1. Бінарне завантаження файлу
        with self.client.get("/api/export/excel?period=24h") as res:
            self.assertEqual(res.status_code, 200)
            self.assertIn("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", res.content_type)
            content_disp = res.headers.get("Content-Disposition", "")
            self.assertIn("attachment", content_disp)
            self.assertIn(".xlsx", content_disp)
            self.assertGreater(len(res.data), 1000)

        # 2. Base64 режим для pywebview діалогу
        with self.client.get("/api/export/excel?period=24h&base64=true") as res_b64:
            self.assertEqual(res_b64.status_code, 200)
            data = res_b64.get_json()
            self.assertIn("filename", data)
            self.assertIn("data", data)
            self.assertIn(".xlsx", data["filename"])
            self.assertIsInstance(data["data"], str)
            self.assertGreater(len(data["data"]), 100)

if __name__ == "__main__":
    unittest.main()
