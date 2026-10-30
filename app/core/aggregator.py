"""
Модуль агрегації та ротації даних у SQLite.
- Згортає 1-секундні записи у похвилинні рядки з обчисленням середніх та MAX (cpu_max, ram_max)
- Реалізує диференційовану ротацію:
  * 1s сирі метрики: 2 години
  * 1m агреговані метрики: 30 днів
  * звичайні знімки процесів: 24 години
  * тривоги та їх знімки процесів: 30 днів
"""

import json
import logging
import threading
import time
from typing import Optional

from app.config import (
    AGGREGATOR_INTERVAL_SEC,
    DB_PATH,
    RETENTION_AGG_METRICS_DAYS,
    RETENTION_ALERTS_DAYS,
    RETENTION_PROCESS_USAGE_HOURLY_DAYS,
    RETENTION_PROC_SNAPSHOTS_HOURS,
    RETENTION_RAW_METRICS_HOURS,
)
from app.db.connection import db_session

logger = logging.getLogger(__name__)

class DataAggregator:
    """
    Фоновий сервіс похвилинної агрегації метрик та періодичної ротації бази даних.
    """

    def __init__(self, db_path=None):
        self.db_path = db_path or DB_PATH
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        """Запуск фонового потоку агрегації."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run_loop, name="DataAggregatorThread", daemon=True)
        self._thread.start()
        logger.info("DataAggregator успішно запущено.")

    def stop(self, timeout: float = 3.0) -> None:
        """Зупинка фонового потоку."""
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=timeout)
        logger.info("DataAggregator зупинено.")

    def is_running(self) -> bool:
        """Перевіряє, чи активний фоновий потік агрегатора."""
        return bool(self._thread and self._thread.is_alive())

    def _run_loop(self) -> None:
        """Головний цикл агрегації та ротації (кожні 60 с)."""
        while not self._stop_event.is_set():
            try:
                self.aggregate_completed_minutes()
                self.aggregate_process_usage_hourly()
                self.cleanup_old_data()
            except Exception as e:
                logger.error(f"Помилка в циклі DataAggregator: {e}", exc_info=True)

            if self._stop_event.wait(AGGREGATOR_INTERVAL_SEC):
                break

    def aggregate_completed_minutes(self) -> int:
        """
        Знаходить завершені хвилини з неагрегованими сирими даними (is_aggregated = 0),
        обчислює середні та максимуми (cpu_max, ram_max) та записує 1 рядок (is_aggregated = 1).
        Повертає кількість агрегованих хвилин.
        """
        now = int(time.time())
        current_minute_start = (now // 60) * 60

        # Знаходимо всі завершені хвилини, які мають сирі дані та ще не мають агрегованого запису
        find_query = """
        SELECT DISTINCT (timestamp / 60) * 60 AS minute_bucket
        FROM metrics
        WHERE is_aggregated = 0 AND timestamp < ?
        AND (timestamp / 60) * 60 NOT IN (
            SELECT timestamp FROM metrics WHERE is_aggregated = 1
        )
        ORDER BY minute_bucket ASC
        """

        aggregated_count = 0
        with db_session(self.db_path) as conn:
            cur = conn.cursor()
            cur.execute(find_query, (current_minute_start,))
            buckets = [row["minute_bucket"] for row in cur.fetchall()]

            for minute_bucket in buckets:
                minute_end = minute_bucket + 60

                # Зчитуємо сирі дані за цю хвилину
                cur.execute("""
                    SELECT cpu_total, cpu_max, cpu_cores, ram_used_bytes, ram_total_bytes,
                           ram_percent, ram_max, swap_used_bytes, swap_total_bytes,
                           disk_read_bytes_sec, disk_write_bytes_sec,
                           net_sent_bytes_sec, net_recv_bytes_sec, disks_info,
                           gpu_percent, gpu_max, gpu_mem_used_bytes, gpu_mem_total_bytes, gpu_temp_c
                    FROM metrics
                    WHERE is_aggregated = 0 AND timestamp >= ? AND timestamp < ?
                """, (minute_bucket, minute_end))

                rows = cur.fetchall()
                if not rows:
                    continue

                count = len(rows)
                cpu_avg = sum(r["cpu_total"] for r in rows) / count
                cpu_max_val = max(r["cpu_max"] for r in rows)

                ram_used_avg = int(sum(r["ram_used_bytes"] for r in rows) / count)
                ram_total_avg = int(sum(r["ram_total_bytes"] for r in rows) / count)
                ram_pct_avg = sum(r["ram_percent"] for r in rows) / count
                ram_max_val = max(r["ram_max"] for r in rows)

                swap_used_avg = int(sum(r["swap_used_bytes"] for r in rows) / count)
                swap_total_avg = int(sum(r["swap_total_bytes"] for r in rows) / count)

                disk_read_avg = sum(r["disk_read_bytes_sec"] for r in rows) / count
                disk_write_avg = sum(r["disk_write_bytes_sec"] for r in rows) / count
                net_sent_avg = sum(r["net_sent_bytes_sec"] for r in rows) / count
                net_recv_avg = sum(r["net_recv_bytes_sec"] for r in rows) / count

                # Розрахунок показників GPU (якщо є)
                gpu_vals = [r["gpu_percent"] for r in rows if r["gpu_percent"] is not None]
                gpu_max_vals = [r["gpu_max"] for r in rows if r["gpu_max"] is not None]
                gpu_avg = round(sum(gpu_vals) / len(gpu_vals), 2) if gpu_vals else None
                gpu_max_val = round(max(gpu_max_vals), 2) if gpu_max_vals else None

                gpu_mem_used_vals = [r["gpu_mem_used_bytes"] for r in rows if r["gpu_mem_used_bytes"] is not None]
                gpu_mem_used_avg = int(sum(gpu_mem_used_vals) / len(gpu_mem_used_vals)) if gpu_mem_used_vals else None

                gpu_mem_totals = [r["gpu_mem_total_bytes"] for r in rows if r["gpu_mem_total_bytes"] is not None]
                gpu_mem_total_val = gpu_mem_totals[-1] if gpu_mem_totals else None

                gpu_temps = [r["gpu_temp_c"] for r in rows if r["gpu_temp_c"] is not None]
                gpu_temp_avg = round(sum(gpu_temps) / len(gpu_temps), 1) if gpu_temps else None

                # Розрахунок середнього завантаження ядер
                all_cores_lists = [json.loads(r["cpu_cores"]) for r in rows if r["cpu_cores"]]
                if all_cores_lists:
                    num_cores = len(all_cores_lists[0])
                    cores_avg = []
                    for c_idx in range(num_cores):
                        c_mean = sum(clist[c_idx] for clist in all_cores_lists if len(clist) > c_idx) / count
                        cores_avg.append(round(c_mean, 1))
                else:
                    cores_avg = []

                # Беремо останній доступний disks_info за хвилину
                last_disks_info = rows[-1]["disks_info"]

                # Записуємо 1 агрегований рядок
                cur.execute("""
                    INSERT INTO metrics (
                        timestamp, cpu_total, cpu_max, cpu_cores, ram_used_bytes, ram_total_bytes,
                        ram_percent, ram_max, swap_used_bytes, swap_total_bytes,
                        disk_read_bytes_sec, disk_write_bytes_sec, net_sent_bytes_sec,
                        net_recv_bytes_sec, disks_info, is_aggregated,
                        gpu_percent, gpu_max, gpu_mem_used_bytes, gpu_mem_total_bytes, gpu_temp_c
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?)
                """, (
                    minute_bucket,
                    round(cpu_avg, 2),
                    round(cpu_max_val, 2),
                    json.dumps(cores_avg),
                    ram_used_avg,
                    ram_total_avg,
                    round(ram_pct_avg, 2),
                    round(ram_max_val, 2),
                    swap_used_avg,
                    swap_total_avg,
                    round(disk_read_avg, 2),
                    round(disk_write_avg, 2),
                    round(net_sent_avg, 2),
                    round(net_recv_avg, 2),
                    last_disks_info,
                    gpu_avg,
                    gpu_max_val,
                    gpu_mem_used_avg,
                    gpu_mem_total_val,
                    gpu_temp_avg,
                ))

                aggregated_count += 1

        return aggregated_count

    def aggregate_process_usage_hourly(self) -> int:
        """
        Агрегує знімки процесів із таблиці process_snapshots у process_usage_hourly:
        - Групує за годиною ((timestamp / 3600) * 3600) та назвою процесу
        - Виключає системні службові процеси та PID 0
        - Рахує кількість появ, середній та максимальний CPU %, максимальний RAM %, першу та останню появу
        - Зберігає агрегати з унікальним ключем (hour, process_name)
        """
        query = """
        INSERT INTO process_usage_hourly (
            hour, process_name, appearances_count,
            avg_cpu_percent, max_cpu_percent, max_memory_percent,
            first_seen_ts, last_seen_ts
        )
        SELECT
            (timestamp / 3600) * 3600 AS hour_bucket,
            name,
            COUNT(*) AS appearances_count,
            ROUND(AVG(cpu_percent), 2) AS avg_cpu_percent,
            ROUND(MAX(cpu_percent), 2) AS max_cpu_percent,
            ROUND(MAX(memory_percent), 2) AS max_memory_percent,
            MIN(timestamp) AS first_seen_ts,
            MAX(timestamp) AS last_seen_ts
        FROM process_snapshots
        WHERE name != '' AND pid != 0 AND LOWER(name) NOT IN ('system idle process', 'system')
        GROUP BY hour_bucket, name
        ON CONFLICT(hour, process_name) DO UPDATE SET
            appearances_count = excluded.appearances_count,
            avg_cpu_percent = excluded.avg_cpu_percent,
            max_cpu_percent = excluded.max_cpu_percent,
            max_memory_percent = excluded.max_memory_percent,
            first_seen_ts = MIN(process_usage_hourly.first_seen_ts, excluded.first_seen_ts),
            last_seen_ts = MAX(process_usage_hourly.last_seen_ts, excluded.last_seen_ts);
        """
        with db_session(self.db_path) as conn:
            cur = conn.cursor()
            cur.execute(query)
            affected = cur.rowcount
        return max(0, affected)

    def cleanup_old_data(self) -> dict:
        """
        Видалення застарілих даних за правилами:
        - 1s сирі метрики: старше RETENTION_RAW_METRICS_HOURS (2 год)
        - 1m агреговані метрики: старше RETENTION_AGG_METRICS_DAYS (30 днів)
        - Звичайні знімки процесів (alert_id IS NULL): старше RETENTION_PROC_SNAPSHOTS_HOURS (24 год)
        - Тривоги та їх знімки процесів: старше RETENTION_ALERTS_DAYS (30 днів)
        """
        now = int(time.time())
        cutoff_raw_metrics = now - (RETENTION_RAW_METRICS_HOURS * 3600)
        cutoff_agg_metrics = now - (RETENTION_AGG_METRICS_DAYS * 86400)
        cutoff_proc_snapshots = now - (RETENTION_PROC_SNAPSHOTS_HOURS * 3600)
        cutoff_alerts = now - (RETENTION_ALERTS_DAYS * 86400)

        cleaned = {}
        with db_session(self.db_path) as conn:
            cur = conn.cursor()

            # 1. Видалення сирих метрик старше 2 годин
            cur.execute("DELETE FROM metrics WHERE is_aggregated = 0 AND timestamp < ?", (cutoff_raw_metrics,))
            cleaned["raw_metrics"] = cur.rowcount

            # 2. Видалення старих агрегатів старше 30 днів
            cur.execute("DELETE FROM metrics WHERE is_aggregated = 1 AND timestamp < ?", (cutoff_agg_metrics,))
            cleaned["agg_metrics"] = cur.rowcount

            # 3. Видалення звичайних процесів старше 24 годин
            cur.execute("DELETE FROM process_snapshots WHERE alert_id IS NULL AND timestamp < ?", (cutoff_proc_snapshots,))
            cleaned["proc_snapshots"] = cur.rowcount

            # 4. Видалення старих тривог старше 30 днів (знімки видаляться каскадно або окремим запитом)
            cur.execute("DELETE FROM alerts WHERE timestamp < ?", (cutoff_alerts,))
            cleaned["alerts"] = cur.rowcount
            cur.execute("DELETE FROM process_snapshots WHERE alert_id IS NOT NULL AND timestamp < ?", (cutoff_alerts,))

            # 5. Видалення старих погодинних підсумків програм старше 30 днів
            cutoff_proc_usage = now - (RETENTION_PROCESS_USAGE_HOURLY_DAYS * 86400)
            cur.execute("DELETE FROM process_usage_hourly WHERE hour < ?", (cutoff_proc_usage,))
            cleaned["proc_usage_hourly"] = cur.rowcount

        return cleaned
