"""
Тести конфігурації та вбудованого завантажувача .env.
"""

import os
import tempfile
import unittest
from pathlib import Path

from app.config import (
    ALERT_CPU_PERCENT,
    ALERT_DISK_FREE_PERCENT,
    ALERT_GPU_TEMP_C,
    ALERT_RAM_PERCENT,
    REDZONE_CPU_PERCENT,
    REDZONE_GPU_TEMP_C,
    REDZONE_RAM_PERCENT,
    load_dotenv,
)

class TestConfig(unittest.TestCase):
    """Перевірка коректності роботи завантажувача .env та конфігураційних констант."""

    def test_load_dotenv_custom_file(self):
        """Перевірка завантажувача: коментарі, лапки, пробіли, порожні рядки."""
        with tempfile.NamedTemporaryFile("w+", delete=False, encoding="utf-8") as f:
            f.write("# Це тестовий коментар\n")
            f.write("TEST_CUSTOM_KEY=test_value_123\n")
            f.write("\n")
            f.write('TEST_QUOTED_KEY="quoted_value"\n')
            f.write("TEST_SPACED_KEY = spaced_value \n")
            temp_path = Path(f.name)

        try:
            load_dotenv(temp_path)
            self.assertEqual(os.environ.get("TEST_CUSTOM_KEY"), "test_value_123")
            self.assertEqual(os.environ.get("TEST_QUOTED_KEY"), "quoted_value")
            self.assertEqual(os.environ.get("TEST_SPACED_KEY"), "spaced_value")
        finally:
            if temp_path.exists():
                temp_path.unlink()
            os.environ.pop("TEST_CUSTOM_KEY", None)
            os.environ.pop("TEST_QUOTED_KEY", None)
            os.environ.pop("TEST_SPACED_KEY", None)

    def test_thresholds_consistency(self):
        """Червона зона повинна використовувати ті самі пороги, що й тривоги."""
        self.assertEqual(ALERT_CPU_PERCENT, 90.0)
        self.assertEqual(ALERT_RAM_PERCENT, 85.0)
        self.assertEqual(ALERT_DISK_FREE_PERCENT, 5.0)
        self.assertEqual(ALERT_GPU_TEMP_C, 85.0)
        self.assertEqual(REDZONE_CPU_PERCENT, ALERT_CPU_PERCENT)
        self.assertEqual(REDZONE_RAM_PERCENT, ALERT_RAM_PERCENT)
        self.assertEqual(REDZONE_GPU_TEMP_C, ALERT_GPU_TEMP_C)

if __name__ == "__main__":
    unittest.main()
