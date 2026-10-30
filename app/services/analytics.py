"""
Модуль системної аналітики (Analytics Engine).
- Обчислює середні та пікові показники (CPU, RAM, GPU, GPU Temp)
- Розраховує час перебування в «червоній зоні» за порогами з config.py
- Рейтинг програм-винуватців тривог
- Топ програм за навантаженням із process_usage_hourly (з фолбеком на знімки)
- Прогноз заповнення дискових розділів
- Індекс стану системи (0–100) за прозорою багатофакторною формулою
"""

import json
import logging
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from app.config import (
    ALERT_DISK_FREE_PERCENT,
    DB_PATH,
    REDZONE_CPU_PERCENT,
    REDZONE_GPU_TEMP_C,
    REDZONE_RAM_PERCENT,
)
from app.db.connection import db_session

logger = logging.getLogger(__name__)

PERIOD_DURATIONS = {
    "1h": 3600,
    "24h": 86400,
    "7d": 7 * 86400,
}

def get_system_analytics(
    period: str = "24h",
    db_path: Optional[Path] = None,
    now: Optional[int] = None
) -> Dict[str, Any]:
    """
    Головна точка входу розрахунку системної аналітики за вказаний період (1h, 24h, 7d).
    """
    target_db = db_path or DB_PATH
    current_ts = now or int(time.time())
    duration_sec = PERIOD_DURATIONS.get(period.lower(), 86400)
    cutoff = current_ts - duration_sec

    with db_session(target_db) as conn:
        cur = conn.cursor()

        # 1. Запит метрик за вказаний період
        # Для 1h беремо всі записи (сирі + агрегати), для довших — переважно агреговані
        cur.execute("""
            SELECT timestamp, cpu_total, cpu_max, ram_percent, ram_max,
                   gpu_percent, gpu_max, gpu_temp_c, disks_info, is_aggregated
            FROM metrics
            WHERE timestamp >= ?
            ORDER BY timestamp ASC
        """, (cutoff,))
        metric_rows = cur.fetchall()

        # 2. Розрахунок середніх, піків та червоної зони
        summary = _calculate_metrics_summary(metric_rows, duration_sec)

        # 3. Активні та минулі тривоги
        cur.execute("""
            SELECT id, timestamp, resolved_at, metric_name, rule_condition, trigger_value, severity
            FROM alerts
            WHERE timestamp >= ? OR resolved_at IS NULL
        """, (cutoff,))
        alert_rows = cur.fetchall()
        active_alerts_count = sum(1 for a in alert_rows if a["resolved_at"] is None)
        period_alerts_count = len(alert_rows)

        # 4. Рейтинг винуватців тривог
        cur.execute("""
            SELECT p.name,
                   COUNT(DISTINCT a.id) AS alert_triggers_count,
                   ROUND(AVG(p.cpu_percent), 1) AS avg_cpu,
                   ROUND(MAX(p.cpu_percent), 1) AS max_cpu,
                   ROUND(AVG(p.memory_percent), 1) AS avg_ram,
                   ROUND(MAX(p.memory_percent), 1) AS max_ram
            FROM process_snapshots p
            JOIN alerts a ON p.alert_id = a.id
            WHERE a.timestamp >= ?
            GROUP BY p.name
            ORDER BY alert_triggers_count DESC, max_cpu DESC
            LIMIT 10
        """, (cutoff,))
        culprits_ranking = [dict(r) for r in cur.fetchall()]

        # 5. Топ програм за навантаженням із process_usage_hourly
        cur.execute("""
            SELECT process_name,
                   SUM(appearances_count) AS total_appearances,
                   ROUND(AVG(avg_cpu_percent), 1) AS avg_cpu,
                   ROUND(MAX(max_cpu_percent), 1) AS max_cpu,
                   ROUND(MAX(max_memory_percent), 1) AS max_memory,
                   MIN(first_seen_ts) AS first_seen,
                   MAX(last_seen_ts) AS last_seen
            FROM process_usage_hourly
            WHERE hour >= ?
            GROUP BY process_name
            ORDER BY avg_cpu DESC, total_appearances DESC
            LIMIT 15
        """, (cutoff,))
        top_apps = [dict(r) for r in cur.fetchall()]

        # Якщо таблиця погодинних підсумків ще порожня (перші хвилини запуску), фолбек на process_snapshots
        if not top_apps:
            cur.execute("""
                SELECT name AS process_name,
                       COUNT(*) AS total_appearances,
                       ROUND(AVG(cpu_percent), 1) AS avg_cpu,
                       ROUND(MAX(cpu_percent), 1) AS max_cpu,
                       ROUND(MAX(memory_percent), 1) AS max_memory,
                       MIN(timestamp) AS first_seen,
                       MAX(timestamp) AS last_seen
                FROM process_snapshots
                WHERE timestamp >= ? AND pid != 0 AND LOWER(name) NOT IN ('system idle process', 'system')
                GROUP BY name
                ORDER BY avg_cpu DESC, total_appearances DESC
                LIMIT 15
            """, (cutoff,))
            top_apps = [dict(r) for r in cur.fetchall()]

        # 6. Прогноз заповнення дисків
        disk_projections = _calculate_disk_projections(metric_rows)

        # Мінімальний відсоток вільного місця серед усіх розділів
        min_disk_free_pct = 100.0
        for dp in disk_projections:
            if dp["free_percent"] < min_disk_free_pct:
                min_disk_free_pct = dp["free_percent"]

        # 7. Розрахунок індексу стану системи (0–100)
        health_index, health_status, health_breakdown = _calculate_health_index(
            cpu_avg=summary["cpu_avg"],
            ram_avg=summary["ram_avg"],
            cpu_rz_sec=summary["cpu_redzone_sec"],
            ram_rz_sec=summary["ram_redzone_sec"],
            duration_sec=duration_sec,
            active_alerts_count=active_alerts_count,
            period_alerts_count=period_alerts_count,
            min_disk_free_pct=min_disk_free_pct,
            gpu_temp_avg=summary.get("gpu_temp_avg"),
            gpu_temp_peak=summary.get("gpu_temp_peak"),
        )

        return {
            "period": period,
            "duration_sec": duration_sec,
            "data_points_count": len(metric_rows),
            "summary": summary,
            "health_index": health_index,
            "health_status": health_status,
            "health_breakdown": health_breakdown,
            "active_alerts_count": active_alerts_count,
            "period_alerts_count": period_alerts_count,
            "culprits_ranking": culprits_ranking,
            "top_apps": top_apps,
            "disk_projections": disk_projections,
        }

def _calculate_metrics_summary(rows: list, duration_sec: int) -> Dict[str, Any]:
    """Обчислює середні, піки та час у червоній зоні."""
    if not rows:
        return {
            "cpu_avg": 0.0,
            "cpu_peak": 0.0,
            "ram_avg": 0.0,
            "ram_peak": 0.0,
            "gpu_avg": None,
            "gpu_peak": None,
            "gpu_temp_avg": None,
            "gpu_temp_peak": None,
            "cpu_redzone_sec": 0,
            "ram_redzone_sec": 0,
            "gpu_temp_redzone_sec": 0,
            "cpu_redzone_percent": 0.0,
            "ram_redzone_percent": 0.0,
        }

    cpu_totals = [r["cpu_total"] for r in rows]
    cpu_peaks = [r["cpu_max"] for r in rows]
    ram_percents = [r["ram_percent"] for r in rows]
    ram_peaks = [r["ram_max"] for r in rows]

    gpu_percents = [r["gpu_percent"] for r in rows if r["gpu_percent"] is not None]
    gpu_peaks = [r["gpu_max"] for r in rows if r["gpu_max"] is not None]
    gpu_temps = [r["gpu_temp_c"] for r in rows if r["gpu_temp_c"] is not None]

    # Розрахунок часу в червоній зоні (для сирих записів крок = 1с, для агрегатів = 60с)
    cpu_rz_sec = 0
    ram_rz_sec = 0
    gpu_rz_sec = 0

    for r in rows:
        step = 60 if r["is_aggregated"] == 1 else 1
        if r["cpu_max"] >= REDZONE_CPU_PERCENT:
            cpu_rz_sec += step
        if r["ram_max"] >= REDZONE_RAM_PERCENT:
            ram_rz_sec += step
        if r["gpu_temp_c"] is not None and r["gpu_temp_c"] >= REDZONE_GPU_TEMP_C:
            gpu_rz_sec += step

    cpu_rz_sec = min(cpu_rz_sec, duration_sec)
    ram_rz_sec = min(ram_rz_sec, duration_sec)
    gpu_rz_sec = min(gpu_rz_sec, duration_sec)

    return {
        "cpu_avg": round(sum(cpu_totals) / len(cpu_totals), 2),
        "cpu_peak": round(max(cpu_peaks), 2),
        "ram_avg": round(sum(ram_percents) / len(ram_percents), 2),
        "ram_peak": round(max(ram_peaks), 2),
        "gpu_avg": round(sum(gpu_percents) / len(gpu_percents), 2) if gpu_percents else None,
        "gpu_peak": round(max(gpu_peaks), 2) if gpu_peaks else None,
        "gpu_temp_avg": round(sum(gpu_temps) / len(gpu_temps), 2) if gpu_temps else None,
        "gpu_temp_peak": round(max(gpu_temps), 2) if gpu_temps else None,
        "cpu_redzone_sec": cpu_rz_sec,
        "ram_redzone_sec": ram_rz_sec,
        "gpu_temp_redzone_sec": gpu_rz_sec,
        "cpu_redzone_percent": round((cpu_rz_sec / duration_sec) * 100.0, 2),
        "ram_redzone_percent": round((ram_rz_sec / duration_sec) * 100.0, 2),
    }

def _calculate_disk_projections(rows: list) -> List[Dict[str, Any]]:
    """
    Розраховує прогноз заповнення для кожного знайденого дискового розділу.
    Якщо витрата простору відсутня або спостереження надто коротке — повертає відповідний статус.
    """
    if not rows:
        return []

    # Знаходимо першу і останню точку з валідними даними про диски
    earliest_disks = None
    earliest_ts = None
    latest_disks = None
    latest_ts = None

    for r in rows:
        raw_disks = r["disks_info"]
        if not raw_disks:
            continue
        try:
            parsed = json.loads(raw_disks) if isinstance(raw_disks, str) else raw_disks
            if parsed:
                if earliest_disks is None:
                    earliest_disks = {d.get("mountpoint") or d.get("mount") or d.get("device"): d for d in parsed}
                    earliest_ts = r["timestamp"]
                latest_disks = {d.get("mountpoint") or d.get("mount") or d.get("device"): d for d in parsed}
                latest_ts = r["timestamp"]
        except Exception:
            continue

    if not latest_disks:
        return []

    time_span = (latest_ts - earliest_ts) if (latest_ts and earliest_ts) else 0
    projections = []

    for mount, d_latest in latest_disks.items():
        total_b = d_latest.get("total_bytes", 1)
        free_b = d_latest.get("free_bytes", 0)
        used_b = d_latest.get("used_bytes", total_b - free_b)
        free_pct = round((free_b / total_b) * 100.0, 1) if total_b > 0 else 0.0

        d_earliest = earliest_disks.get(mount) if earliest_disks else None
        projection_text = "Недостатньо даних / заповнення не прогнозується"
        days_until_full = None
        rate_mb_per_day = 0.0
        status = "insufficient_data"

        if d_earliest and time_span >= 300:  # Мінімум 5 хвилин спостереження
            earliest_free_b = d_earliest.get("free_bytes", free_b)
            consumed_bytes = earliest_free_b - free_b

            if consumed_bytes > 0:
                rate_bytes_per_sec = consumed_bytes / time_span
                rate_mb_per_day = round((rate_bytes_per_sec * 86400) / (1024**2), 1)
                seconds_until_full = free_b / rate_bytes_per_sec
                days_until_full = round(seconds_until_full / 86400, 1)

                if days_until_full <= 1.0:
                    status = "critical"
                    projection_text = f"Критично: заповниться менш ніж за добу (витрата {rate_mb_per_day} МБ/день)"
                elif days_until_full <= 30.0:
                    status = "warning"
                    projection_text = f"Заповниться через ~{int(days_until_full)} дн. ({rate_mb_per_day} МБ/день)"
                else:
                    status = "ok"
                    projection_text = f"Заповниться через ~{int(days_until_full)} дн. ({rate_mb_per_day} МБ/день)"
            else:
                status = "ok"
                projection_text = "Заповнення не прогнозується (обсяг стабільний)"
        else:
            status = "insufficient_data"
            projection_text = "Недостатньо даних / заповнення не прогнозується"

        projections.append({
            "mountpoint": mount,
            "device": d_latest.get("device", mount),
            "fstype": d_latest.get("fstype", ""),
            "total_gb": round(total_b / (1024**3), 2),
            "free_gb": round(free_b / (1024**3), 2),
            "used_gb": round(used_b / (1024**3), 2),
            "free_percent": free_pct,
            "used_percent": round(100.0 - free_pct, 1),
            "rate_mb_per_day": rate_mb_per_day,
            "days_until_full": days_until_full,
            "days_to_full": days_until_full,
            "status": status,
            "projection_text": projection_text,
        })

    return projections

def _calculate_health_index(
    cpu_avg: float,
    ram_avg: float,
    cpu_rz_sec: int,
    ram_rz_sec: int,
    duration_sec: int,
    active_alerts_count: int,
    period_alerts_count: int,
    min_disk_free_pct: float,
    gpu_temp_avg: Optional[float] = None,
    gpu_temp_peak: Optional[float] = None,
) -> Tuple[int, str, Dict[str, float]]:
    """
    Прозора та зрозуміла формула індексу здоров'я системи (0–100):
    Базовий рівень: 100 балів.
    Віднімання штрафів:
    1. Навантаження CPU: до -20 балів (пропорційно середньому навантаженню)
    2. Навантаження RAM: до -20 балів (пропорційно середньому навантаженню)
    3. Червона зона:
       - CPU > 90%: до -15 балів (кожен 1% часу = -1.5 б)
       - RAM > 85%: до -15 балів (кожен 1% часу = -1.5 б)
    4. Тривоги:
       - Активні зараз: по -10 балів за кожну (макс -10)
       - Події за період: по -2 бали за кожну (макс -10)
    5. Вільне місце на дисках:
       - Якщо є розділ із вільним місцем < 5%: -10 балів; < 10%: -5 балів
    6. Температура GPU (якщо є):
       - Пікова > 85 °C: -5 балів
       - Середня > 80 °C: -5 балів
    """
    score = 100.0

    # 1. Штрафи за середнє навантаження
    penalty_cpu = round((min(cpu_avg, 100.0) / 100.0) * 20.0, 1)
    penalty_ram = round((min(ram_avg, 100.0) / 100.0) * 20.0, 1)
    score -= (penalty_cpu + penalty_ram)

    # 2. Штрафи за червону зону
    dur = max(duration_sec, 1)
    cpu_rz_pct = (cpu_rz_sec / dur) * 100.0
    ram_rz_pct = (ram_rz_sec / dur) * 100.0
    penalty_cpu_rz = round(min(15.0, cpu_rz_pct * 1.5), 1)
    penalty_ram_rz = round(min(15.0, ram_rz_pct * 1.5), 1)
    score -= (penalty_cpu_rz + penalty_ram_rz)

    # 3. Штрафи за тривоги
    penalty_active_alerts = round(min(10.0, active_alerts_count * 10.0), 1)
    penalty_period_alerts = round(min(10.0, period_alerts_count * 2.0), 1)
    score -= (penalty_active_alerts + penalty_period_alerts)

    # 4. Штрафи за диски
    penalty_disk = 0.0
    if min_disk_free_pct < 5.0:
        penalty_disk = 10.0
    elif min_disk_free_pct < 10.0:
        penalty_disk = 5.0
    score -= penalty_disk

    # 5. Штрафи за GPU
    penalty_gpu = 0.0
    if gpu_temp_peak is not None and gpu_temp_peak > 85.0:
        penalty_gpu += 5.0
    if gpu_temp_avg is not None and gpu_temp_avg > 80.0:
        penalty_gpu += 5.0
    score -= penalty_gpu

    final_index = int(round(max(0.0, min(100.0, score))))

    # Статус
    if final_index >= 85:
        status = "Відмінний"
    elif final_index >= 70:
        status = "Добрий"
    elif final_index >= 50:
        status = "Задовільний"
    else:
        status = "Критичний"

    breakdown = {
        "base_score": 100.0,
        "penalty_cpu_load": penalty_cpu,
        "penalty_ram_load": penalty_ram,
        "penalty_cpu_redzone": penalty_cpu_rz,
        "penalty_ram_redzone": penalty_ram_rz,
        "penalty_alerts": penalty_active_alerts + penalty_period_alerts,
        "penalty_disk_space": penalty_disk,
        "penalty_gpu_temp": penalty_gpu,
        "final_score": final_index,
    }

    return final_index, status, breakdown
