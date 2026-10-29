"""
Тести модуля десктопного вікна app/desktop/window.py.
"""

import unittest
from unittest.mock import MagicMock, patch

from app.desktop.window import create_window

class TestDesktopWindow(unittest.TestCase):
    """Перевірка конфігурації та параметрів виклику pywebview вікна."""

    @patch("webview.create_window")
    def test_create_window_parameters(self, mock_create):
        """Перевірка параметрів створення вікна (розмір 1280x800, min 1100x700)."""
        mock_create.return_value = MagicMock()
        url = "http://127.0.0.1:5050"

        win = create_window(url, title="System Monitor")
        self.assertIsNotNone(win)
        mock_create.assert_called_once_with(
            title="System Monitor",
            url=url,
            width=1280,
            height=800,
            min_size=(1100, 700),
            background_color="#0f1115",
            text_select=False,
        )

if __name__ == "__main__":
    unittest.main()
