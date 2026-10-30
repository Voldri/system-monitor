# -*- mode: python ; coding: utf-8 -*-

import os
from pathlib import Path

block_cipher = None

project_dir = Path.cwd()

# Додаємо лише статичні ресурси дашборду та схему БД SQLite
# БАЗА monitor.db ТА ФАЙЛ .env КАТЕГОРИЧНО НЕ ВШИВАЮТЬСЯ В .EXE
datas = [
    ('app/static', 'app/static'),
    ('app/db/schema.sql', 'app/db'),
]

# Приховані імпорти динамічних та нативних бібліотек Windows
hiddenimports = [
    'pystray',
    'pystray._win32',
    'PIL',
    'PIL.Image',
    'PIL.ImageDraw',
    'webview',
    'webview.platforms.winforms',
    'webview.platforms.edgechromium',
    'pynvml',
    'openpyxl',
    'openpyxl.chart',
    'openpyxl.chart.line_chart',
    'psutil',
    'werkzeug',
    'werkzeug.serving',
    'flask',
    'sqlite3',
    'logging.handlers',
]

# Шлях до іконки
icon_file = 'app/static/assets/icon.ico'
if not Path(icon_file).is_file():
    icon_file = 'icon.ico'

a = Analysis(
    ['run.py'],
    pathex=[str(project_dir)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['monitor.db', '.env', '.env.example'],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name='SystemMonitor',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,  # Windowed режим без виводу чорного вікна консолі
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=icon_file if Path(icon_file).is_file() else None,
)
