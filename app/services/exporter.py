"""
Модуль генерації звітів у форматі Excel (.xlsx) через openpyxl.
Містить 6 аркушів:
1. «Характеристики» (CPU, RAM, диски, GPU, ОС, Boot Time)
2. «Зведення» (Аналітика, індекс стану, піки, червона зона, прогноз дисків)
3. «Метрики» (10 с усереднення для 1h або 1m агрегати; лінійна діаграма CPU/RAM/GPU, умовне форматування > 90%)
4. «Програми» (Дані з process_usage_hourly за період)
5. «Сповіщення» (Журнал тривог із винуватцями)
6. «Поради ШІ» (Заголовки колонок для майбутніх порад)
"""

import io
import json
import logging
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import openpyxl
from openpyxl.chart import LineChart, Reference
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

from app.config import DB_PATH
from app.db.connection import db_session
from app.utils import format_local_time
from app.services.analytics import get_system_analytics
from app.services.specs import get_system_specs

logger = logging.getLogger(__name__)

# Стилі
FONT_TITLE = Font(name="Segoe UI", size=14, bold=True, color="1F4E79")
FONT_HEADER = Font(name="Segoe UI", size=11, bold=True, color="FFFFFF")
FONT_BOLD = Font(name="Segoe UI", size=10, bold=True)
FONT_REGULAR = Font(name="Segoe UI", size=10)
FONT_ALERT_RED = Font(name="Segoe UI", size=10, bold=True, color="9C0006")

FILL_HEADER = PatternFill(start_color="2B5797", end_color="2B5797", fill_type="solid")
FILL_SUBHEADER = PatternFill(start_color="D9E1F2", end_color="D9E1F2", fill_type="solid")
FILL_ALERT_RED = PatternFill(start_color="FFC7CE", end_color="FFC7CE", fill_type="solid")
FILL_ZEBRA = PatternFill(start_color="F2F2F2", end_color="F2F2F2", fill_type="solid")

BORDER_THIN = Border(
    left=Side(style="thin", color="D9D9D9"),
    right=Side(style="thin", color="D9D9D9"),
    top=Side(style="thin", color="D9D9D9"),
    bottom=Side(style="thin", color="D9D9D9"),
)

def _style_header_row(ws, row_idx: int, col_count: int) -> None:
    """Застосовує фірмовий синій стиль до рядка заголовка."""
    for col in range(1, col_count + 1):
        cell = ws.cell(row=row_idx, column=col)
        cell.font = FONT_HEADER
        cell.fill = FILL_HEADER
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        cell.border = BORDER_THIN
    ws.row_dimensions[row_idx].height = 24

def _auto_fit_columns(ws, max_cols: int = 15, max_width: int = 50) -> None:
    """Автоматично встановлює ширину колонок за довжиною тексту."""
    for col in range(1, max_cols + 1):
        col_letter = get_column_letter(col)
        max_len = 0
        for row in range(1, min(ws.max_row + 1, 300)):
            val = ws.cell(row=row, column=col).value
            if val is not None:
                lines = str(val).split("\n")
                max_len = max(max_len, max(len(l) for l in lines))
        ws.column_dimensions[col_letter].width = min(max(max_len + 4, 12), max_width)

def generate_excel_report(
    period: str = "24h",
    db_path: Optional[Path] = None
) -> Tuple[io.BytesIO, str]:
    """
    Генерує звіт у форматі .xlsx із 6 обов'язковими аркушами.
    Повертає бінарний потік io.BytesIO та рекомендоване ім'я файлу.
    """
    wb = openpyxl.Workbook()
    now_dt = datetime.now()
    filename = now_dt.strftime("report_%Y-%m-%d_%H-%M.xlsx")
    target_db = db_path or DB_PATH

    # Отримуємо характеристики та аналітику
    specs = get_system_specs()
    analytics = get_system_analytics(period=period, db_path=target_db)

    # 1. Аркуш: «Характеристики»
    ws_specs = wb.active
    ws_specs.title = "Характеристики"
    _fill_specs_sheet(ws_specs, specs)

    # 2. Аркуш: «Зведення»
    ws_summary = wb.create_sheet(title="Зведення")
    _fill_summary_sheet(ws_summary, analytics, period)

    # 3. Аркуш: «Метрики»
    ws_metrics = wb.create_sheet(title="Метрики")
    _fill_metrics_sheet(ws_metrics, period, target_db)

    # 4. Аркуш: «Програми»
    ws_procs = wb.create_sheet(title="Програми")
    _fill_procs_sheet(ws_procs, analytics.get("top_apps", []))

    # 5. Аркуш: «Сповіщення»
    ws_alerts = wb.create_sheet(title="Сповіщення")
    _fill_alerts_sheet(ws_alerts, period, target_db)

    # 6. Аркуш: «Поради ШІ»
    ws_ai = wb.create_sheet(title="Поради ШІ")
    _fill_ai_sheet(ws_ai)

    output = io.BytesIO()
    wb.save(output)
    output.seek(0)
    return output, filename

def _fill_specs_sheet(ws, specs: Dict[str, Any]) -> None:
    """Заповнення аркуша 'Характеристики'."""
    ws.views.sheetView[0].showGridLines = True
    ws.cell(row=1, column=1, value="Характеристики комп'ютера (System Specifications)").font = FONT_TITLE
    ws.row_dimensions[1].height = 28

    headers = ["Компонент", "Властивість", "Значення"]
    for col_idx, h in enumerate(headers, 1):
        ws.cell(row=3, column=col_idx, value=h)
    _style_header_row(ws, 3, len(headers))

    cpu = specs.get("cpu", {})
    ram = specs.get("ram", {})
    os_info = specs.get("os", {})
    sys_info = specs.get("system", {})

    rows_data = [
        ("Процесор (CPU)", "Модель", cpu.get("model", "Невідомо")),
        ("Процесор (CPU)", "Фізичні ядра", cpu.get("physical_cores", 1)),
        ("Процесор (CPU)", "Логічні ядра", cpu.get("logical_cores", 1)),
        ("Процесор (CPU)", "Базова / поточна частота", f"{cpu.get('base_freq_mhz', '--')} МГц / {cpu.get('current_freq_mhz', '--')} МГц"),
        ("Пам'ять (RAM)", "Загальний обсяг", f"{ram.get('total_gb', '--')} ГБ ({ram.get('total_bytes', 0):,} байт)"),
        ("Операційна система", "Версія Windows", os_info.get("version_string", "Windows")),
        ("Операційна система", "Архітектура", os_info.get("architecture", "AMD64")),
        ("Система", "Останній запуск (Boot Time)", sys_info.get("boot_time_local", "--")),
    ]

    # Додаємо відеокарти
    gpus = specs.get("gpus", [])
    if gpus:
        for idx, g in enumerate(gpus, 1):
            mem_str = f"{g.get('memory_total_gb')} ГБ" if g.get("memory_total_gb") else "Спільна системна"
            drv_str = f", Драйвер: {g.get('driver_version')}" if g.get("driver_version") else ""
            rows_data.append((f"Відеокарта #{idx}", g.get("name", "GPU"), f"{mem_str} ({g.get('source', '')}{drv_str})"))
    else:
        rows_data.append(("Відеокарта", "Модель", "Дискретна відеокарта не виявлена"))

    # Додаємо диски
    disks = specs.get("disks", [])
    for d in disks:
        rows_data.append((
            f"Диск {d.get('mountpoint', '')}",
            f"ФС: {d.get('fstype', '')}",
            f"Загалом: {d.get('total_gb')} ГБ, Вільно: {d.get('free_gb')} ГБ (Використано {d.get('used_percent')}%)"
        ))

    curr_row = 4
    for comp, prop, val in rows_data:
        c1 = ws.cell(row=curr_row, column=1, value=comp)
        c2 = ws.cell(row=curr_row, column=2, value=prop)
        c3 = ws.cell(row=curr_row, column=3, value=val)
        for c in (c1, c2, c3):
            c.font = FONT_REGULAR
            c.border = BORDER_THIN
        c1.font = FONT_BOLD
        curr_row += 1

    _auto_fit_columns(ws, 3, 60)

def _fill_summary_sheet(ws, analytics: Dict[str, Any], period: str) -> None:
    """Заповнення аркуша 'Зведення'."""
    ws.views.sheetView[0].showGridLines = True
    ws.cell(row=1, column=1, value=f"Зведення аналітики та індекс стану (Період: {period})").font = FONT_TITLE
    ws.row_dimensions[1].height = 28

    health_idx = analytics.get("health_index", 100)
    health_status = analytics.get("health_status", "Відмінний")
    summary = analytics.get("summary", {})

    # Блок індексу стану
    ws.cell(row=3, column=1, value="Індекс здоров'я системи (0–100)").font = FONT_BOLD
    ws.cell(row=3, column=2, value=health_idx).font = FONT_TITLE
    ws.cell(row=3, column=3, value=f"Стан: {health_status}").font = FONT_BOLD

    # Основні показники телеметрії
    ws.cell(row=5, column=1, value="Показник телеметрії")
    ws.cell(row=5, column=2, value="Середнє значення")
    ws.cell(row=5, column=3, value="Пікове значення (MAX)")
    ws.cell(row=5, column=4, value="Час у червоній зоні")
    _style_header_row(ws, 5, 4)

    metrics_rows = [
        ("Процесор (CPU)", f"{summary.get('cpu_avg', 0)} %", f"{summary.get('cpu_peak', 0)} %", f"{summary.get('cpu_redzone_sec', 0)} с ({summary.get('cpu_redzone_percent', 0)} %)"),
        ("Пам'ять (RAM)", f"{summary.get('ram_avg', 0)} %", f"{summary.get('ram_peak', 0)} %", f"{summary.get('ram_redzone_sec', 0)} с ({summary.get('ram_redzone_percent', 0)} %)"),
    ]
    if summary.get("gpu_avg") is not None:
        metrics_rows.append((
            "Відеокарта (GPU)",
            f"{summary.get('gpu_avg')} %",
            f"{summary.get('gpu_peak')} %",
            "--"
        ))
    if summary.get("gpu_temp_avg") is not None:
        metrics_rows.append((
            "Температура GPU",
            f"{summary.get('gpu_temp_avg')} °C",
            f"{summary.get('gpu_temp_peak')} °C",
            f"{summary.get('gpu_temp_redzone_sec', 0)} с"
        ))

    curr_row = 6
    for name, avg_v, peak_v, rz_v in metrics_rows:
        ws.cell(row=curr_row, column=1, value=name).font = FONT_BOLD
        ws.cell(row=curr_row, column=2, value=avg_v).font = FONT_REGULAR
        ws.cell(row=curr_row, column=3, value=peak_v).font = FONT_REGULAR
        ws.cell(row=curr_row, column=4, value=rz_v).font = FONT_REGULAR
        for c in range(1, 5):
            ws.cell(row=curr_row, column=c).border = BORDER_THIN
        curr_row += 1

    # Прогноз заповнення дисків
    curr_row += 2
    ws.cell(row=curr_row, column=1, value="Розділ диска")
    ws.cell(row=curr_row, column=2, value="Загальний обсяг")
    ws.cell(row=curr_row, column=3, value="Вільний обсяг")
    ws.cell(row=curr_row, column=4, value="Швидкість витрати / Прогноз")
    _style_header_row(ws, curr_row, 4)
    curr_row += 1

    projections = analytics.get("disk_projections", [])
    if projections:
        for p in projections:
            ws.cell(row=curr_row, column=1, value=p.get("mountpoint", "")).font = FONT_BOLD
            ws.cell(row=curr_row, column=2, value=f"{p.get('total_gb')} ГБ").font = FONT_REGULAR
            ws.cell(row=curr_row, column=3, value=f"{p.get('free_gb')} ГБ ({p.get('free_percent')}%)").font = FONT_REGULAR
            ws.cell(row=curr_row, column=4, value=p.get("projection_text", "--")).font = FONT_REGULAR
            for c in range(1, 5):
                ws.cell(row=curr_row, column=c).border = BORDER_THIN
            curr_row += 1
    else:
        ws.cell(row=curr_row, column=1, value="Дані дисків відсутні").font = FONT_REGULAR
        curr_row += 1

    _auto_fit_columns(ws, 4, 60)

def _fill_metrics_sheet(ws, period: str, db_path: Path) -> None:
    """
    Заповнення аркуша 'Метрики':
    - Для 1h: усереднення сирих точок у 10-секундні кошики
    - Для інших періодів: похвилинні агрегати
    - Додавання вбудованої лінійної діаграми Excel
    - Умовне форматування клітинок > 90% червоним
    """
    ws.views.sheetView[0].showGridLines = True
    now = int(time.time())
    duration_sec = 3600 if period == "1h" else (86400 if period == "24h" else 7 * 86400)
    if period == "all":
        cutoff = 0
    else:
        cutoff = now - duration_sec

    headers = [
        "Час (локальний)", "CPU Сер. (%)", "CPU Пік (%)",
        "RAM Сер. (%)", "RAM Пік (%)", "GPU Сер. (%)", "GPU Пік (%)", "GPU Темп. (°C)"
    ]
    for col_idx, h in enumerate(headers, 1):
        ws.cell(row=1, column=col_idx, value=h)
    _style_header_row(ws, 1, len(headers))

    with db_session(db_path) as conn:
        cur = conn.cursor()
        if period == "1h":
            # Усереднення по 10 с із сирих записів
            cur.execute("""
                SELECT
                    (timestamp / 10) * 10 AS bucket_ts,
                    ROUND(AVG(cpu_total), 1) AS cpu_avg,
                    ROUND(MAX(cpu_max), 1) AS cpu_max_val,
                    ROUND(AVG(ram_percent), 1) AS ram_avg,
                    ROUND(MAX(ram_max), 1) AS ram_max_val,
                    ROUND(AVG(gpu_percent), 1) AS gpu_avg,
                    ROUND(MAX(gpu_max), 1) AS gpu_max_val,
                    ROUND(AVG(gpu_temp_c), 1) AS gpu_temp_avg
                FROM metrics
                WHERE timestamp >= ?
                GROUP BY bucket_ts
                ORDER BY bucket_ts ASC
            """, (cutoff,))
            rows = cur.fetchall()
        else:
            # Похвилинні агрегати (або сирі, якщо агрегатів замало)
            cur.execute("""
                SELECT
                    timestamp AS bucket_ts,
                    ROUND(cpu_total, 1) AS cpu_avg,
                    ROUND(cpu_max, 1) AS cpu_max_val,
                    ROUND(ram_percent, 1) AS ram_avg,
                    ROUND(ram_max, 1) AS ram_max_val,
                    ROUND(gpu_percent, 1) AS gpu_avg,
                    ROUND(gpu_max, 1) AS gpu_max_val,
                    ROUND(gpu_temp_c, 1) AS gpu_temp_avg
                FROM metrics
                WHERE timestamp >= ?
                ORDER BY timestamp ASC
            """, (cutoff,))
            rows = cur.fetchall()

    curr_row = 2
    for r in rows:
        ts_str = format_local_time(r["bucket_ts"])
        cpu_avg = r["cpu_avg"] or 0.0
        cpu_peak = r["cpu_max_val"] or cpu_avg
        ram_avg = r["ram_avg"] or 0.0
        ram_peak = r["ram_max_val"] or ram_avg
        gpu_avg = r["gpu_avg"]
        gpu_peak = r["gpu_max_val"]
        gpu_temp = r["gpu_temp_avg"]

        row_vals = [ts_str, cpu_avg, cpu_peak, ram_avg, ram_peak, gpu_avg, gpu_peak, gpu_temp]

        for col_idx, val in enumerate(row_vals, 1):
            cell = ws.cell(row=curr_row, column=col_idx, value=val)
            cell.font = FONT_REGULAR
            cell.border = BORDER_THIN
            cell.alignment = Alignment(horizontal="center" if col_idx == 1 else "right")

            # Умовне форматування: значення > 90% підсвічуються червоним
            if isinstance(val, (int, float)) and col_idx in (2, 3, 4, 5, 6, 7):
                if val >= 90.0:
                    cell.fill = FILL_ALERT_RED
                    cell.font = FONT_ALERT_RED

        curr_row += 1

    last_data_row = curr_row - 1

    # Додавання лінійної діаграми Excel
    if last_data_row >= 3:
        chart = LineChart()
        chart.title = "Телеметрія системи: CPU, RAM та GPU"
        chart.style = 13
        chart.y_axis.title = "Відсоток (%)"
        chart.x_axis.number_format = "yyyy-mm-dd hh:mm:ss"
        chart.width = 20
        chart.height = 12

        # Стовпці 2 (CPU), 4 (RAM), 6 (GPU)
        data = Reference(ws, min_col=2, min_row=1, max_col=6, max_row=min(last_data_row, 300))
        cats = Reference(ws, min_col=1, min_row=2, max_row=min(last_data_row, 300))
        chart.add_data(data, titles_from_data=True)
        chart.set_categories(cats)
        ws.add_chart(chart, "J2")

    _auto_fit_columns(ws, len(headers), 25)

def _fill_procs_sheet(ws, top_apps: List[Dict[str, Any]]) -> None:
    """Заповнення аркуша 'Програми'."""
    ws.views.sheetView[0].showGridLines = True
    headers = [
        "Назва програми", "Кількість появ у топі", "Середній CPU (%)",
        "Максимальний CPU (%)", "Максимальна RAM (%)", "Перша поява", "Остання поява"
    ]
    for col_idx, h in enumerate(headers, 1):
        ws.cell(row=1, column=col_idx, value=h)
    _style_header_row(ws, 1, len(headers))

    curr_row = 2
    for p in top_apps:
        first_s = format_local_time(p.get("first_seen")) if p.get("first_seen") else "--"
        last_s = format_local_time(p.get("last_seen")) if p.get("last_seen") else "--"

        vals = [
            p.get("process_name", ""),
            p.get("total_appearances", 0),
            p.get("avg_cpu", 0.0),
            p.get("max_cpu", 0.0),
            p.get("max_memory", 0.0),
            first_s,
            last_s,
        ]
        for col_idx, val in enumerate(vals, 1):
            cell = ws.cell(row=curr_row, column=col_idx, value=val)
            cell.font = FONT_REGULAR
            cell.border = BORDER_THIN
            if col_idx == 1:
                cell.font = FONT_BOLD
                cell.alignment = Alignment(horizontal="left")
            else:
                cell.alignment = Alignment(horizontal="center" if col_idx in (2, 6, 7) else "right")
        curr_row += 1

    _auto_fit_columns(ws, len(headers), 35)

def _fill_alerts_sheet(ws, period: str, db_path: Path) -> None:
    """Заповнення аркуша 'Сповіщення' з фіксацією винуватців."""
    ws.views.sheetView[0].showGridLines = True
    headers = [
        "ID", "Час виникнення", "Час вирішення", "Статус",
        "Метрика", "Умова правила", "Значення", "Поріг", "Процеси-винуватці"
    ]
    for col_idx, h in enumerate(headers, 1):
        ws.cell(row=1, column=col_idx, value=h)
    _style_header_row(ws, 1, len(headers))

    now = int(time.time())
    duration_sec = 3600 if period == "1h" else (86400 if period == "24h" else 7 * 86400)
    cutoff = 0 if period == "all" else (now - duration_sec)

    with db_session(db_path) as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT id, timestamp, resolved_at, metric_name, rule_condition,
                   trigger_value, threshold_value, severity, culprits_summary
            FROM alerts
            WHERE timestamp >= ? OR resolved_at IS NULL
            ORDER BY timestamp DESC
        """, (cutoff,))
        alerts = cur.fetchall()

    curr_row = 2
    for a in alerts:
        start_time_str = format_local_time(a["timestamp"])
        resolved_time_str = format_local_time(a["resolved_at"]) if a["resolved_at"] else "АКТИВНА"
        status_str = "Вирішено" if a["resolved_at"] else "АКТИВНА"

        vals = [
            a["id"],
            start_time_str,
            resolved_time_str,
            status_str,
            a["metric_name"],
            a["rule_condition"],
            a["trigger_value"],
            a["threshold_value"],
            a["culprits_summary"],
        ]

        for col_idx, val in enumerate(vals, 1):
            cell = ws.cell(row=curr_row, column=col_idx, value=val)
            cell.font = FONT_REGULAR
            cell.border = BORDER_THIN
            if col_idx == 4 and status_str == "АКТИВНА":
                cell.fill = FILL_ALERT_RED
                cell.font = FONT_ALERT_RED
        curr_row += 1

    _auto_fit_columns(ws, len(headers), 50)

def _fill_ai_sheet(ws) -> None:
    """Заповнення аркуша 'Поради ШІ' (поки лише заголовки колонок)."""
    ws.views.sheetView[0].showGridLines = True
    headers = [
        "ID", "Час запиту", "Режим", "Джерело",
        "Запит користувача", "Зріз метрик", "Текст поради ШІ", "Статус"
    ]
    for col_idx, h in enumerate(headers, 1):
        ws.cell(row=1, column=col_idx, value=h)
    _style_header_row(ws, 1, len(headers))
    _auto_fit_columns(ws, len(headers), 35)
