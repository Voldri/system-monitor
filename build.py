"""
Скрипт автоматизованої збірки System Monitor у портативний виконуваний файл .exe:
1. Перевіряє та за необхідності генерує icon.png / icon.ico через tools/make_icon.py
2. Запускає PyInstaller з конфігурацією monitor.spec (onefile, windowed)
3. Очищає тимчасовий каталог build/
4. Перевіряє наявність готового дистрибутиву dist/SystemMonitor.exe
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
SPEC_FILE = PROJECT_ROOT / "monitor.spec"
BUILD_DIR = PROJECT_ROOT / "build"
DIST_DIR = PROJECT_ROOT / "dist"
TARGET_EXE = DIST_DIR / "SystemMonitor.exe"


def main():
    print("=" * 65)
    print("      Збірка System Monitor у портативний .exe (PyInstaller)")
    print("=" * 65)

    # 1. Генерація іконок
    print("\n[1/4] Перевірка та генерація фірмових іконок...")
    try:
        from tools.make_icon import generate_icons
        generate_icons()
        print(" -> Іконки успішно згенеровані (PNG + ICO).")
    except Exception as e:
        print(f" -> [Попередження] Не вдалося згенерувати іконки: {e}")

    # 2. Перевірка наявності monitor.spec
    if not SPEC_FILE.is_file():
        print(f"\n[ПОМИЛКА] Файл специфікації {SPEC_FILE} не знайдено!")
        sys.exit(1)

    # 3. Запуск PyInstaller
    print(f"\n[2/4] Запуск PyInstaller із специфікацією {SPEC_FILE.name}...")
    try:
        import PyInstaller.__main__
        pyinstaller_args = [str(SPEC_FILE), "--noconfirm"]
        PyInstaller.__main__.run(pyinstaller_args)
    except Exception as e:
        print(f" -> Прямий виклик через PyInstaller.__main__ завершився помилкою: {e}")
        print(" -> Спроба запуску через підпроцес sys.executable...")
        cmd = [sys.executable, "-m", "PyInstaller", str(SPEC_FILE), "--noconfirm"]
        res = subprocess.run(cmd, cwd=str(PROJECT_ROOT))
        if res.returncode != 0:
            print(f"\n[ПОМИЛКА] Збірка завершилася з кодом {res.returncode}")
            sys.exit(res.returncode)

    # 4. Очищення тимчасового каталогу build/
    print("\n[3/4] Очищення тимчасового каталогу збірки build/...")
    if BUILD_DIR.is_dir():
        try:
            shutil.rmtree(BUILD_DIR)
            print(" -> Каталог build/ успішно видалено.")
        except Exception as e:
            print(f" -> [Попередження] Не вдалося повністю видалити build/: {e}")

    # 5. Перевірка результату
    print("\n[4/4] Перевірка вихідного файлу...")
    if TARGET_EXE.is_file():
        size_mb = TARGET_EXE.stat().st_size / (1024 * 1024)
        print("\n" + "=" * 65)
        print("  [УСПІХ] Портативний виконуваний файл успішно створено!")
        print(f"  Файл:   {TARGET_EXE}")
        print(f"  Розмір: {size_mb:.2f} МБ")
        print("=" * 65)
        print("\nДля запуску перемістіть SystemMonitor.exe у будь-яку зручну папку.")
        print("monitor.db, .env та logs/ будуть створюватися поруч із .exe.")
    else:
        print(f"\n[УВАГА] Виконуваний файл {TARGET_EXE} не знайдено у dist/.")
        sys.exit(1)


if __name__ == "__main__":
    main()
