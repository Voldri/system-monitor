"""
Модуль безперервного збору метрик системи, процесів та відеокарти NVIDIA.
- Використовує окремі незалежні потоки для системних метрик (1 с), процесів (5 с) та GPU (2 с).
- Оптимізоване сканування процесів через oneshot(), кешування імен та швидкий розрахунок RAM.
- Повна ізоляція: 1-секундний збір ніколи не блокується опитуванням процесів.
- Відкидання першого нульового виміру CPU після старту psutil.
- Підтримка pynvml (nvidia-ml-py) із безпечним фолбеком при відсутності GPU чи бібліотеки.
- In-memory буфер для історії 'live' для усунення зайвих навантажень на SQLite.
"""

import collections
import json
import logging
import threading
import time
from typing import Any, Dict, List, Optional

import psutil

from app.config import (
    DB_PATH,
    GPU_INTERVAL_SEC,
    GPU_MONITORING,
    METRIC_INTERVAL_SEC,
    PROCESS_INTERVAL_SEC,
)
from app.db.connection import db_session

logger = logging.getLogger(__name__)

class MetricCollector:
    """
    Фоновий збирач метрик системи, процесів та GPU:
    - _system_thread: щосекунди збирає системні метрики та зберігає в SQLite.
    - _process_thread: кожні 5 с оновлює кеш процесів та зберігає знімки.
    - _gpu_thread: кожні 2 с опитує стан NVIDIA GPU через pynvml.
    """

    def __init__(self, db_path=None):
        self.db_path = db_path or DB_PATH
        self._stop_event = threading.Event()
        self._lock = threading.Lock()

        # Потоки воркерів
        self._system_thread: Optional[threading.Thread] = None
        self._process_thread: Optional[threading.Thread] = None
        self._gpu_thread: Optional[threading.Thread] = None

        # Системні ресурси
        self.logical_cores = psutil.cpu_count(logical=True) or 1
        self._total_ram = psutil.virtual_memory().total or 1

        # Оптимізований кеш процесів
        self._process_cache: Dict[int, psutil.Process] = {}
        self._proc_names: Dict[int, str] = {}
        self._ignored_pids: set[int] = set()

        # In-memory стан для швидкого відгуку API без частих запитів до SQLite
        self._latest_metrics: Dict[str, Any] = {}
        self._latest_processes: List[Dict[str, Any]] = []
        self._history_live = collections.deque(maxlen=120)

        # Стан GPU
        self._gpu_available = False
        self._gpu_handle = None
        self._gpu_name: Optional[str] = None
        self._latest_gpu_data: Dict[str, Any] = {
            "available": False,
            "gpu_name": None,
            "gpu_percent": None,
            "gpu_mem_used_bytes": None,
            "gpu_mem_total_bytes": None,
            "gpu_temp_c": None,
        }

        # Лічильники I/O
        self._last_time = time.time()
        self._last_disk_io = psutil.disk_io_counters()
        self._last_net_io = psutil.net_io_counters()

        # Прапорець первинного прогріву CPU (щоб відкинути початковий 0%)
        self._cpu_warmed_up = False

        # Початкова ініціалізація psutil лічильників
        try:
            psutil.cpu_percent(interval=None)
            psutil.cpu_percent(interval=None, percpu=True)
        except Exception:
            pass

        # Ініціалізація NVML
        self._init_nvml()

    def _init_nvml(self) -> None:
        """Безпечна ініціалізація NVIDIA NVML через pynvml."""
        if not GPU_MONITORING:
            logger.info("Моніторинг GPU вимкнено конфігурацією GPU_MONITORING=false.")
            return

        try:
            import pynvml
            pynvml.nvmlInit()
            count = pynvml.nvmlDeviceGetCount()
            if count > 0:
                handle = pynvml.nvmlDeviceGetHandleByIndex(0)
                name = pynvml.nvmlDeviceGetName(handle)
                if isinstance(name, bytes):
                    name = name.decode("utf-8", errors="ignore")
                self._gpu_handle = handle
                self._gpu_name = name
                self._gpu_available = True
                self._latest_gpu_data["available"] = True
                self._latest_gpu_data["gpu_name"] = name
                logger.info(f"NVIDIA NVML успішно ініціалізовано: {name}")
            else:
                logger.info("Пристроїв NVIDIA GPU не знайдено.")
        except Exception as e:
            logger.warning(f"NVIDIA NVML недоступний ({e}). Режим роботи без GPU.")
            self._gpu_available = False

    def start(self) -> None:
        """Запуск незалежних фонових потоків збору."""
        self._stop_event.clear()

        # 1. Потік системних метрик (1 с)
        if self._system_thread is None or not self._system_thread.is_alive():
            self._system_thread = threading.Thread(
                target=self._run_system_loop,
                name="SystemMetricsThread",
                daemon=True,
            )
            self._system_thread.start()

        # 2. Потік процесів (5 с)
        if self._process_thread is None or not self._process_thread.is_alive():
            self._process_thread = threading.Thread(
                target=self._run_process_loop,
                name="ProcessCollectorThread",
                daemon=True,
            )
            self._process_thread.start()

        # 3. Потік відеокарти (2 с)
        if self._gpu_available and (self._gpu_thread is None or not self._gpu_thread.is_alive()):
            self._gpu_thread = threading.Thread(
                target=self._run_gpu_loop,
                name="GpuCollectorThread",
                daemon=True,
            )
            self._gpu_thread.start()

        logger.info("MetricCollector: Усі потоки моніторингу успішно запущені.")

    def stop(self, timeout: float = 3.0) -> None:
        """Зупинка всіх фонових потоків."""
        self._stop_event.set()

        for th in [self._system_thread, self._process_thread, self._gpu_thread]:
            if th and th.is_alive():
                th.join(timeout=timeout)

        # Зупинка NVML
        if self._gpu_available:
            try:
                import pynvml
                pynvml.nvmlShutdown()
            except Exception:
                pass

        logger.info("MetricCollector успішно зупинено.")

    def is_running(self) -> bool:
        """
        Перевіряє, чи активні фонові потоки збирача.
        Повертає True, якщо збирач запущено і працює хоча б один робочий потік
        (потік системних метрик або процесів).
        """
        if self._stop_event.is_set():
            return False
        return bool(
            (self._system_thread and self._system_thread.is_alive())
            or (self._process_thread and self._process_thread.is_alive())
        )

    def get_threads_status(self) -> Dict[str, bool]:
        """
        Повертає стан активності кожного фонового потоку збирача:
        - metrics: потік щосекундного збору системних метрик (1 с)
        - processes: потік опитування процесів (5 с)
        - gpu: потік моніторингу NVIDIA GPU (2 с)
        """
        return {
            "metrics": bool(self._system_thread and self._system_thread.is_alive()),
            "processes": bool(self._process_thread and self._process_thread.is_alive()),
            "gpu": bool(self._gpu_thread and self._gpu_thread.is_alive()),
        }

    @property
    def threads_status(self) -> Dict[str, bool]:
        """Аліас властивості для get_threads_status()."""
        return self.get_threads_status()

    @property
    def _thread(self) -> Optional[threading.Thread]:
        """
        Сумісність зі старим кодом: повертає основний системний потік.
        """
        return self._system_thread

    # =========================================================================
    # 1. СИСТЕМНІ МЕТРИКИ (1 с)
    # =========================================================================
    def _run_system_loop(self) -> None:
        """Цикл швидкого збору системних метрик строго кожну 1 секунду."""
        while not self._stop_event.is_set():
            loop_start = time.time()

            try:
                metrics_data = self._collect_system_metrics()
                # Якщо перший вимір був нульовим прогревом, пропускаємо запис
                if metrics_data is not None:
                    self._save_system_metrics(metrics_data)
                    with self._lock:
                        self._history_live.append(metrics_data)
            except Exception as e:
                logger.error(f"Помилка в _run_system_loop: {e}", exc_info=True)

            elapsed = time.time() - loop_start
            sleep_time = max(0.05, METRIC_INTERVAL_SEC - elapsed)
            if self._stop_event.wait(sleep_time):
                break

    def _collect_system_metrics(self) -> Optional[Dict[str, Any]]:
        """
        Зчитує системні показники через psutil та останні дані GPU.
        Перший нульовий вимір після старту відкидається.
        """
        now = time.time()
        dt = max(now - self._last_time, 0.1)

        # CPU
        cpu_total = float(psutil.cpu_percent(interval=None))
        cpu_cores = [float(c) for c in psutil.cpu_percent(interval=None, percpu=True)]

        # Відкидання першого нульового виміру CPU після запуску psutil
        if not self._cpu_warmed_up:
            self._cpu_warmed_up = True
            if cpu_total == 0.0 and all(c == 0.0 for c in cpu_cores):
                logger.debug("Перший нульовий вимір CPU відкинуто (прогрів psutil).")
                self._last_time = now
                return None

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

        # Дані відеокарти з останнього знімка GPU-воркера
        with self._lock:
            gpu_snap = dict(self._latest_gpu_data)

        gpu_pct = gpu_snap.get("gpu_percent")
        gpu_mem_used = gpu_snap.get("gpu_mem_used_bytes")
        gpu_mem_total = gpu_snap.get("gpu_mem_total_bytes")
        gpu_temp = gpu_snap.get("gpu_temp_c")
        gpu_name = gpu_snap.get("gpu_name")

        metrics = {
            "timestamp": int(now),
            "cpu_total": cpu_total,
            "cpu_max": cpu_total,
            "cpu_cores": cpu_cores,
            "ram_used_bytes": vmem.used,
            "ram_total_bytes": vmem.total,
            "ram_percent": float(vmem.percent),
            "ram_max": float(vmem.percent),
            "swap_used_bytes": swap.used,
            "swap_total_bytes": swap.total,
            "disk_read_bytes_sec": disk_read_bytes_sec,
            "disk_write_bytes_sec": disk_write_bytes_sec,
            "net_sent_bytes_sec": net_sent_bytes_sec,
            "net_recv_bytes_sec": net_recv_bytes_sec,
            "disks_info": disks_info,
            "is_aggregated": 0,
            "gpu_percent": gpu_pct,
            "gpu_max": gpu_pct,
            "gpu_mem_used_bytes": gpu_mem_used,
            "gpu_mem_total_bytes": gpu_mem_total,
            "gpu_temp_c": gpu_temp,
            "gpu_name": gpu_name,
        }

        with self._lock:
            self._latest_metrics = metrics

        return metrics

    def _save_system_metrics(self, data: Dict[str, Any]) -> None:
        """Збереження 1-секундного знімка в базу даних SQLite."""
        query = """
        INSERT INTO metrics (
            timestamp, cpu_total, cpu_max, cpu_cores, ram_used_bytes, ram_total_bytes,
            ram_percent, ram_max, swap_used_bytes, swap_total_bytes, disk_read_bytes_sec,
            disk_write_bytes_sec, net_sent_bytes_sec, net_recv_bytes_sec, disks_info, is_aggregated,
            gpu_percent, gpu_max, gpu_mem_used_bytes, gpu_mem_total_bytes, gpu_temp_c
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                data.get("gpu_percent"),
                data.get("gpu_max"),
                data.get("gpu_mem_used_bytes"),
                data.get("gpu_mem_total_bytes"),
                data.get("gpu_temp_c"),
            ))

    # =========================================================================
    # 2. ПРОЦЕСИ (Окремий потік, 5 с)
    # =========================================================================
    def _run_process_loop(self) -> None:
        """Окремий фоновий цикл збору топ-процесів кожні 5 с."""
        # Первинне заповнення кешу
        try:
            self._refresh_processes()
        except Exception:
            pass

        while not self._stop_event.is_set():
            if self._stop_event.wait(PROCESS_INTERVAL_SEC):
                break

            try:
                procs = self._refresh_processes()
                self._save_process_snapshots(procs)
            except Exception as e:
                logger.error(f"Помилка в _run_process_loop: {e}", exc_info=True)

    def _refresh_processes(self) -> List[Dict[str, Any]]:
        """
        Високопродуктивне опитування процесів:
        - Process.oneshot() мінімізує дескрипторні виклики Windows API.
        - Кешування назв процесів у _proc_names (назва PID не змінюється).
        - Виключення PID 0 та системних службових процесів.
        - Розрахунок memory_percent через rss / total_ram без виклику proc.memory_percent().
        """
        current_pids = set(psutil.pids())
        total_ram = self._total_ram

        # Очищення мертвих процесів із кешів
        dead_pids = [pid for pid in self._process_cache if pid not in current_pids]
        for pid in dead_pids:
            self._process_cache.pop(pid, None)
            self._proc_names.pop(pid, None)
            self._ignored_pids.discard(pid)

        procs_data: List[Dict[str, Any]] = []

        for pid in current_pids:
            if pid == 0 or pid in self._ignored_pids:
                continue

            proc = self._process_cache.get(pid)
            if proc is None:
                try:
                    proc = psutil.Process(pid)
                    proc.cpu_percent(interval=None)
                    self._process_cache[pid] = proc
                except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                    continue

            try:
                with proc.oneshot():
                    name = self._proc_names.get(pid)
                    if name is None:
                        name = proc.name()
                        if name.lower() in ("system idle process", "system"):
                            self._ignored_pids.add(pid)
                            continue
                        self._proc_names[pid] = name

                    raw_cpu = proc.cpu_percent(interval=None)
                    normalized_cpu = round(raw_cpu / self.logical_cores, 2)

                    mem_info = proc.memory_info()
                    rss = mem_info.rss
                    mem_pct = round((rss / total_ram) * 100.0, 2)
                    status = proc.status()

                    procs_data.append({
                        "pid": pid,
                        "name": name,
                        "cpu_percent": normalized_cpu,
                        "memory_percent": mem_pct,
                        "memory_rss_bytes": rss,
                        "status": status,
                    })
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                self._process_cache.pop(pid, None)
                self._proc_names.pop(pid, None)
                continue

        procs_data.sort(key=lambda x: x["cpu_percent"], reverse=True)

        with self._lock:
            self._latest_processes = procs_data

        return procs_data

    def _save_process_snapshots(self, procs: List[Dict[str, Any]], limit: int = 15) -> None:
        """Збереження топ-N процесів за CPU та пам'яттю кожні 5 с."""
        now = int(time.time())
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

    # =========================================================================
    # 3. ВІДЕОКАРТА NVIDIA (Окремий потік, 2 с)
    # =========================================================================
    def _run_gpu_loop(self) -> None:
        """Цикл збору телеметрії GPU кожні 2 с."""
        while not self._stop_event.is_set():
            if self._gpu_available and self._gpu_handle:
                try:
                    import pynvml
                    util = pynvml.nvmlDeviceGetUtilizationRates(self._gpu_handle)
                    gpu_pct = float(util.gpu)

                    mem = pynvml.nvmlDeviceGetMemoryInfo(self._gpu_handle)
                    used_bytes = int(mem.used)
                    total_bytes = int(mem.total)

                    temp = float(pynvml.nvmlDeviceGetTemperature(
                        self._gpu_handle, pynvml.NVML_TEMPERATURE_GPU
                    ))

                    with self._lock:
                        self._latest_gpu_data = {
                            "available": True,
                            "gpu_name": self._gpu_name,
                            "gpu_percent": gpu_pct,
                            "gpu_mem_used_bytes": used_bytes,
                            "gpu_mem_total_bytes": total_bytes,
                            "gpu_temp_c": temp,
                        }
                except Exception as e:
                    logger.debug(f"Помилка опитування GPU: {e}")

            if self._stop_event.wait(GPU_INTERVAL_SEC):
                break

    # =========================================================================
    # 4. In-Memory API ГЕТЕРИ (Миттєвий доступ)
    # =========================================================================
    def get_current_metrics(self) -> Dict[str, Any]:
        """Повертає найостанніший стан метрик із пам'яті (O(1))."""
        with self._lock:
            res = dict(self._latest_metrics)
            # Якщо є свіжі дані GPU, доповнюємо
            if self._latest_gpu_data.get("available"):
                res["gpu_name"] = self._latest_gpu_data.get("gpu_name")
                res["gpu_percent"] = self._latest_gpu_data.get("gpu_percent")
                res["gpu_mem_used_bytes"] = self._latest_gpu_data.get("gpu_mem_used_bytes")
                res["gpu_mem_total_bytes"] = self._latest_gpu_data.get("gpu_mem_total_bytes")
                res["gpu_temp_c"] = self._latest_gpu_data.get("gpu_temp_c")
            return res

    def get_live_history(self, limit: int = 60) -> List[Dict[str, Any]]:
        """Повертає історію 'live' із пам'яті без запитів до SQLite."""
        with self._lock:
            items = list(self._history_live)
        return items[-limit:] if len(items) > limit else items

    def get_top_processes(self, sort_by: str = "cpu", limit: int = 15) -> List[Dict[str, Any]]:
        """Повертає відсортований топ процесів із пам'яті (O(1))."""
        with self._lock:
            procs = list(self._latest_processes)

        if sort_by == "ram":
            procs.sort(key=lambda x: x["memory_percent"], reverse=True)
        else:
            procs.sort(key=lambda x: x["cpu_percent"], reverse=True)

        return procs[:limit]
