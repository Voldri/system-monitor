"""
Тести модуля десктопного вікна app/desktop/window.py.
"""

import unittest
from unittest.mock import ANY, MagicMock, patch

from app.desktop.window import DesktopApi, create_window

class TestDesktopWindow(unittest.TestCase):
    """Перевірка конфігурації та параметрів виклику pywebview вікна."""

    @patch("webview.create_window")
    def test_create_window_parameters(self, mock_create):
        """Перевірка параметрів створення вікна (розмір 1280x800, min 1100x700, js_api)."""
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
            js_api=ANY,
        )

    def test_desktop_api_save_excel(self):
        """Перевірка методу збереження файлу через діалог pywebview."""
        api = DesktopApi()
        mock_window = MagicMock()
        api.set_window(mock_window)

        # Користувач скасував діалог
        mock_window.create_file_dialog.return_value = None
        res_cancel = api.save_excel("AAAA", "test.xlsx")
        self.assertTrue(res_cancel.get("cancelled"))

        # Користувач обрав шлях
        with patch("builtins.open", unittest.mock.mock_open()) as mock_file:
            mock_window.create_file_dialog.return_value = ("C:\\test.xlsx",)
            import base64
            test_b64 = base64.b64encode(b"hello excel").decode("utf-8")
            res_ok = api.save_excel(test_b64, "test.xlsx")
            self.assertTrue(res_ok.get("success"))
            mock_file.assert_called_once_with("C:\\test.xlsx", "wb")

if __name__ == "__main__":
    unittest.main()
