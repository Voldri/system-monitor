"""
Модуль безперервного збору метрик системи та процесів.
Використовує psutil з кешуванням екземплярів Process, нормуванням CPU
та виключенням службових процесів PID 0 та System.
"""

import json
import logging
import threading
import time
from typing import Any, Dict, List, Optional

import psutil

from app.config import (
    DB_PATH,
    METRIC_INTERVAL_SEC,
    PROCESS_INTERVAL_SEC,
)
from app.db.connection import db_session

logger = logging.getLogger(__name__)

class MetricCollector:
    """
    Фоновий збирач метрик системи та процесів.
    - Метрики системи збираються кожну 1 секунду.
    - Знімки процесів оновлюються кожні 5 секунд із кешуванням дескрипторів.
    """

    def __init__(self, db_path=None):
        self.db_path = db_path or DB_PATH
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()

        # Кількість логічних ядер для нормалізації CPU
        self.logical_cores = psutil.cpu_count(logical=True) or 1

        # Кеш екземплярів psutil.Process: {pid: Process}
        self._process_cache: Dict[int, psutil.Process] = {}

        # Останні зібрані дані для швидкого відклику API
        self._latest_metrics: Dict[str, Any] = {}
        self._latest_processes: List[Dict[str, Any]] = []

        # Попередні лічильники для розрахунку delta швидкості (байт/с)
        self._last_time = time.time()
        self._last_disk_io = psutil.disk_io_counters()
        self._last_net_io = psutil.net_io_counters()

        # Первинний виклик cpu_percent для ініціалізації внутрішнього лічильника psutil
        try:
            psutil.cpu_percent(interval=None)
            psutil.cpu_percent(interval=None, percpu=True)
        except Exception:
            pass

    def start(self) -> None:
        """Запуск фонового потоку збору метрик."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run_loop, name="MetricCollectorThread", daemon=True)
        self._thread.start()
        logger.info("MetricCollector успішно запущено.")

    def stop(self, timeout: float = 3.0) -> None:
        """Зупинка фонового потоку."""
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=timeout)
        logger.info("MetricCollector зупинено.")

    def _run_loop(self) -> None:
        """Головний цикл збору метрик."""
        last_process_sample = 0.0

        # Попередній прогрів кешу процесів (перший виклик cpu_percent повертає 0.0)
        self._refresh_processes()

        while not self._stop_event.is_set():
            loop_start = time.time()

            try:
                # 1. Збір посекундних метрик
                metrics_data = self._collect_system_metrics()
                self._save_system_metrics(metrics_data)

                # 2. Збір процесів кожні 5 секунд
                if loop_start - last_process_sample >= PROCESS_INTERVAL_SEC:
                    procs = self._refresh_processes()
                    self._save_process_snapshots(procs)
                    last_process_sample = loop_start

            except Exception as e:
                logger.error(f"Помилка в циклі MetricCollector: {e}", exc_info=True)

            # Точне витримування 1-секундного інтервалу
            elapsed = time.time() - loop_start
            sleep_time = max(0.05, METRIC_INTERVAL_SEC - elapsed)
            if self._stop_event.wait(sleep_time):
                break

    def _collect_system_metrics(self) -> Dict[str, Any]:
        """Зчитує системні показники через psutil."""
        now = time.time()
        dt = max(now - self._last_time, 0.1)

        # CPU
        cpu_total = float(psutil.cpu_percent(interval=None))
        cpu_cores = [float(c) for c in psutil.cpu_percent(interval=None, percpu=True)]

        # Пам'ять
        vmem = psutil.virtual_memory()
        swap = psutil.swap_memory()

        # Дисковий I/O
        current_disk_io = psutil.disk_io_counters()
        disk_read_bytes_sec = 0.0
        disk_write_bytes_sec = 0.0
        if current_disk_io and self._last_disk_io:
            read_diff = current_disk_io.read_bytes - self._last_disk_io.read_bytes
            write_diff = current_disk_io.write_bytes - self._last_disk_io.write_bytes
            disk_read_bytes_sec = max(0.0, read_diff / dt)
            disk_write_bytes_sec = max(0.0, write_diff / dt)
        self._last_disk_io = current_disk_io

        # Мережевий I/O
        current_net_io = psutil.net_io_counters()
        net_sent_bytes_sec = 0.0
        net_recv_bytes_sec = 0.0
        if current_net_io and self._last_net_io:
            sent_diff = current_net_io.bytes_sent - self._last_net_io.bytes_sent
            recv_diff = current_net_io.bytes_recv - self._last_net_io.bytes_recv
            net_sent_bytes_sec = max(0.0, sent_diff / dt)
            net_recv_bytes_sec = max(0.0, recv_diff / dt)
        self._last_net_io = current_net_io

        self._last_time = now

        # Інформація про дискові розділи
        disks_info = []
        try:
            for part in psutil.disk_partitions(all=False):
                try:
                    usage = psutil.disk_usage(part.mountpoint)
                    disks_info.append({
                        "mount": part.mountpoint,
                        "device": part.device,
                        "total_bytes": usage.total,
                        "used_bytes": usage.used,
                        "free_bytes": usage.free,
                        "percent": float(usage.percent),
                    })
                except (PermissionError, OSError):
                    continue
        except Exception:
            pass

        metrics = {
            "timestamp": int(now),
            "cpu_total": cpu_total,
            "cpu_max": cpu_total,  # Для 1s сирого значення дорівнює поточному
            "cpu_cores": cpu_cores,
            "ram_used_bytes": vmem.used,
            "ram_total_bytes": vmem.total,
            "ram_percent": float(vmem.percent),
            "ram_max": float(vmem.percent),  # Для 1s сирого значення дорівнює поточному
            "swap_used_bytes": swap.used,
            "swap_total_bytes": swap.total,
            "disk_read_bytes_sec": disk_read_bytes_sec,
            "disk_write_bytes_sec": disk_write_bytes_sec,
            "net_sent_bytes_sec": net_sent_bytes_sec,
            "net_recv_bytes_sec": net_recv_bytes_sec,
            "disks_info": disks_info,
            "is_aggregated": 0,
        }

        with self._lock:
            self._latest_metrics = metrics

        return metrics

    def _refresh_processes(self) -> List[Dict[str, Any]]:
        """
        Опитування процесів через кеш psutil.Process:
        - Нормує CPU на кількість логічних ядер
        - Виключає PID 0 (System Idle Process) та 'System'
        - Не використовує поле username
        """
        current_pids = set(psutil.pids())

        # Видалення мертвих процесів із кешу
        dead_pids = [pid for pid in self._process_cache if pid not in current_pids]
        for pid in dead_pids:
            self._process_cache.pop(pid, None)

        procs_data: List[Dict[str, Any]] = []

        for pid in current_pids:
            if pid == 0:
                continue  # Виключаємо System Idle Process (PID 0)

            # Отримуємо або створюємо об'єкт Process
            proc = self._process_cache.get(pid)
            if proc is None:
                try:
                    proc = psutil.Process(pid)
                    # Первинний виклик cpu_percent для початку відліку
                    proc.cpu_percent(interval=None)
                    self._process_cache[pid] = proc
                except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                    continue

            try:
                name = proc.name()
                # Виключаємо System Idle Process та System з топу
                if name.lower() in ("system idle process", "system"):
                    continue

                # Отримуємо відсоток CPU та нормуємо на кількість логічних ядер
                raw_cpu = proc.cpu_percent(interval=None)
                normalized_cpu = round(raw_cpu / self.logical_cores, 2)

                mem_pct = round(proc.memory_percent(), 2)
                mem_info = proc.memory_info()
                status = proc.status()

                procs_data.append({
                    "pid": pid,
                    "name": name,
                    "cpu_percent": normalized_cpu,
                    "memory_percent": mem_pct,
                    "memory_rss_bytes": mem_info.rss,
                    "status": status,
                })
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                self._process_cache.pop(pid, None)
                continue

        # Сортуємо за споживанням CPU
        procs_data.sort(key=lambda x: x["cpu_percent"], reverse=True)

        with self._lock:
            self._latest_processes = procs_data

        return procs_data

    def _save_system_metrics(self, data: Dict[str, Any]) -> None:
        """Збереження 1-секундного знімка в базу даних."""
        query = """
        INSERT INTO metrics (
            timestamp, cpu_total, cpu_max, cpu_cores, ram_used_bytes, ram_total_bytes,
            ram_percent, ram_max, swap_used_bytes, swap_total_bytes, disk_read_bytes_sec,
            disk_write_bytes_sec, net_sent_bytes_sec, net_recv_bytes_sec, disks_info, is_aggregated
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """
        with db_session(self.db_path) as conn:
            conn.execute(query, (
                data["timestamp"],
                data["cpu_total"],
                data["cpu_max"],
                json.dumps(data["cpu_cores"]),
                data["ram_used_bytes"],
                data["ram_total_bytes"],
                data["ram_percent"],
                data["ram_max"],
                data["swap_used_bytes"],
                data["swap_total_bytes"],
                data["disk_read_bytes_sec"],
                data["disk_write_bytes_sec"],
                data["net_sent_bytes_sec"],
                data["net_recv_bytes_sec"],
                json.dumps(data["disks_info"]),
                0,
            ))

    def _save_process_snapshots(self, procs: List[Dict[str, Any]], limit: int = 15) -> None:
        """Збереження топ-N процесів за CPU та пам'яттю кожні 5 с."""
        now = int(time.time())
        # Беремо топ за CPU та топ за RAM (без дублікатів PID)
        top_cpu = sorted(procs, key=lambda x: x["cpu_percent"], reverse=True)[:limit]
        top_ram = sorted(procs, key=lambda x: x["memory_percent"], reverse=True)[:limit]

        unique_procs = {p["pid"]: p for p in top_cpu + top_ram}.values()

        query = """
        INSERT INTO process_snapshots (
            timestamp, alert_id, pid, name, cpu_percent, memory_percent,
            memory_rss_bytes, status
        ) VALUES (?, NULL, ?, ?, ?, ?, ?, ?)
        """
        rows = [
            (
                now,
                p["pid"],
                p["name"],
                p["cpu_percent"],
                p["memory_percent"],
                p["memory_rss_bytes"],
                p["status"],
            )
            for p in unique_procs
        ]

        if rows:
            with db_session(self.db_path) as conn:
                conn.executemany(query, rows)

    def get_current_metrics(self) -> Dict[str, Any]:
        """Повертає найостанніший стан метрик із пам'яті (O(1))."""
        with self._lock:
            return dict(self._latest_metrics)

    def get_top_processes(self, sort_by: str = "cpu", limit: int = 15) -> List[Dict[str, Any]]:
        """Повертає відсортований топ процесів із пам'яті."""
        with self._lock:
            procs = list(self._latest_processes)

        if sort_by == "ram":
            procs.sort(key=lambda x: x["memory_percent"], reverse=True)
        else:
            procs.sort(key=lambda x: x["cpu_percent"], reverse=True)

        return procs[:limit]
