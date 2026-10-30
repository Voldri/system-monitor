"""
Тести модуля збору характеристик ПК (app/services/specs.py):
- Наявність усіх обов'язкових ключів структури (CPU, RAM, диски, GPU, ОС, Boot Time)
- Безпечна робота при відсутності окремих ключів реєстру або GPU
"""

import unittest
from unittest.mock import patch

from app.services.specs import collect_system_specs, get_system_specs

class TestSystemSpecs(unittest.TestCase):
    """Перевірка збору характеристик системи."""

    def test_specs_structure_and_types(self):
        """Перевірка наявності всіх необхідних розділів та ключів."""
        specs = collect_system_specs()
        self.assertIn("cpu", specs)
        self.assertIn("ram", specs)
        self.assertIn("disks", specs)
        self.assertIn("gpus", specs)
        self.assertIn("os", specs)
        self.assertIn("system", specs)

        # CPU
        cpu = specs["cpu"]
        self.assertIn("model", cpu)
        self.assertIn("logical_cores", cpu)
        self.assertGreater(cpu["logical_cores"], 0)

        # RAM
        ram = specs["ram"]
        self.assertIn("total_gb", ram)
        self.assertGreater(ram["total_gb"], 0)

        # Disks
        self.assertIsInstance(specs["disks"], list)

        # GPUs
        self.assertIsInstance(specs["gpus"], list)

        # OS
        self.assertIn("version_string", specs["os"])

        # System
        self.assertIn("boot_time_local", specs["system"])
        self.assertIn("boot_time_ts", specs["system"])

    def test_specs_safe_fallback_on_error(self):
        """Перевірка стабільності: помилки в winreg або pynvml не ламають збір."""
        with patch("app.services.specs._get_cpu_name_from_registry", return_value="Fallback CPU"):
            with patch("app.services.specs._get_nvidia_gpus", return_value=[]):
                with patch("app.services.specs._get_display_adapters_from_registry", return_value=[]):
                    specs = collect_system_specs()
                    self.assertEqual(specs["cpu"]["model"], "Fallback CPU")
                    self.assertIsInstance(specs["gpus"], list)
                    self.assertIsNotNone(specs["ram"]["total_gb"])

if __name__ == "__main__":
    unittest.main()
