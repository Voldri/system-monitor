"""
Модуль одноразового збору характеристик комп'ютера (Windows 11).
- Лише читання, не вимагає прав адміністратора.
- Зчитує:
  * Процесор (назва з реєстру Windows ProcessorNameString, фізичні/логічні ядра, частота)
  * Оперативна пам'ять (загальний обсяг)
  * Розділи дисків (точка монтування, обсяг, вільне місце, файлова система)
  * Відеокарти (NVIDIA через NVML + відеоадаптери з реєстру Windows)
  * Версія Windows (реліз, білд, розрядність)
  * Час останнього запуску системи (Boot time)
- Кешує результат у пам'яті.
"""

import logging
import platform
import sys
import time
from typing import Any, Dict, List, Optional

import psutil

from app.utils import format_local_time

logger = logging.getLogger(__name__)

# Кеш для одноразового збору
_cached_specs: Optional[Dict[str, Any]] = None

def _get_cpu_name_from_registry() -> str:
    """Зчитує назву моделі CPU з реєстру Windows без прав адміністратора."""
    if sys.platform != "win32":
        return platform.processor() or "Generic CPU"

    try:
        import winreg
        key_path = r"HARDWARE\DESCRIPTION\System\CentralProcessor\0"
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key_path, 0, winreg.KEY_READ) as key:
            val, _ = winreg.QueryValueEx(key, "ProcessorNameString")
            return str(val).strip()
    except Exception as e:
        logger.debug(f"Не вдалося зчитати назву CPU з реєстру: {e}")
        return platform.processor() or "Unknown CPU"

def _get_display_adapters_from_registry() -> List[Dict[str, Any]]:
    r"""
    Зчитує перелік відеокарт із реєстру Windows:
    HKEY_LOCAL_MACHINE\SYSTEM\CurrentControlSet\Control\Class\{4d36e968-e325-11ce-bfc1-08002be10318}
    Працює без прав адміністратора (KEY_READ).
    """
    adapters = []
    if sys.platform != "win32":
        return adapters

    try:
        import winreg
        class_key_path = r"SYSTEM\CurrentControlSet\Control\Class\{4d36e968-e325-11ce-bfc1-08002be10318}"
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, class_key_path, 0, winreg.KEY_READ) as class_key:
            subkeys_count, _, _ = winreg.QueryInfoKey(class_key)
            for i in range(subkeys_count):
                try:
                    subkey_name = winreg.EnumKey(class_key, i)
                    if not subkey_name.isdigit():
                        continue
                    with winreg.OpenKey(class_key, subkey_name, 0, winreg.KEY_READ) as dev_key:
                        try:
                            desc, _ = winreg.QueryValueEx(dev_key, "DriverDesc")
                            desc = str(desc).strip()
                            if desc and desc.lower() not in ("remote display",):
                                driver_version = None
                                try:
                                    dv, _ = winreg.QueryValueEx(dev_key, "DriverVersion")
                                    driver_version = str(dv).strip()
                                except Exception:
                                    pass
                                adapters.append({
                                    "name": desc,
                                    "driver_version": driver_version,
                                    "source": "Windows Registry",
                                })
                        except FileNotFoundError:
                            pass
                except Exception:
                    continue
    except Exception as e:
        logger.debug(f"Не вдалося опитати відеоадаптери з реєстру: {e}")

    return adapters

def _get_nvidia_gpus() -> List[Dict[str, Any]]:
    """Зчитує детальні характеристики відеокарт NVIDIA через NVML."""
    gpus = []
    try:
        import pynvml
        pynvml.nvmlInit()
        count = pynvml.nvmlDeviceGetCount()
        for i in range(count):
            try:
                handle = pynvml.nvmlDeviceGetHandleByIndex(i)
                name = pynvml.nvmlDeviceGetName(handle)
                if isinstance(name, bytes):
                    name = name.decode("utf-8", errors="ignore")
                mem = pynvml.nvmlDeviceGetMemoryInfo(handle)
                driver_ver = pynvml.nvmlSystemGetDriverVersion()
                if isinstance(driver_ver, bytes):
                    driver_ver = driver_ver.decode("utf-8", errors="ignore")

                gpus.append({
                    "name": name,
                    "memory_total_bytes": int(mem.total),
                    "memory_total_gb": round(mem.total / (1024**3), 2),
                    "driver_version": driver_ver,
                    "source": "NVIDIA NVML",
                })
            except Exception as e:
                logger.debug(f"Помилка отримання даних GPU #{i} через NVML: {e}")
    except Exception as e:
        logger.debug(f"NVML недоступний для характеристики GPU: {e}")

    return gpus

def _get_windows_version_string() -> str:
    """Формує людинозрозумілий рядок версії Windows з номером релізу та білдом."""
    os_name = platform.system()
    os_release = platform.release()
    os_ver = platform.version()

    display_version = None
    if sys.platform == "win32":
        try:
            import winreg
            cv_path = r"SOFTWARE\Microsoft\Windows NT\CurrentVersion"
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, cv_path, 0, winreg.KEY_READ) as cv_key:
                try:
                    dv, _ = winreg.QueryValueEx(cv_key, "DisplayVersion")
                    display_version = str(dv).strip()
                except FileNotFoundError:
                    pass
        except Exception:
            pass

    details = f"{os_name} {os_release}"
    if display_version:
        details += f" ({display_version})"
    details += f" [Build {os_ver}] {platform.machine()}"
    return details

def collect_system_specs() -> Dict[str, Any]:
    """
    Збирає повний набір характеристик ПК без прав адміністратора.
    """
    # 1. Процесор
    cpu_name = _get_cpu_name_from_registry()
    logical_cores = psutil.cpu_count(logical=True) or 1
    physical_cores = psutil.cpu_count(logical=False) or logical_cores

    cpu_freq = psutil.cpu_freq()
    base_freq_mhz = round(cpu_freq.max, 0) if (cpu_freq and cpu_freq.max) else None
    current_freq_mhz = round(cpu_freq.current, 0) if (cpu_freq and cpu_freq.current) else None

    # 2. Оперативна пам'ять
    vmem = psutil.virtual_memory()
    total_ram_bytes = vmem.total
    total_ram_gb = round(total_ram_bytes / (1024**3), 2)

    # 3. Розділи дисків
    disks = []
    try:
        partitions = psutil.disk_partitions(all=False)
        for p in partitions:
            try:
                usage = psutil.disk_usage(p.mountpoint)
                disks.append({
                    "device": p.device,
                    "mountpoint": p.mountpoint,
                    "fstype": p.fstype,
                    "total_bytes": usage.total,
                    "total_gb": round(usage.total / (1024**3), 2),
                    "free_bytes": usage.free,
                    "free_gb": round(usage.free / (1024**3), 2),
                    "used_percent": round(usage.percent, 1),
                })
            except (PermissionError, OSError):
                continue
    except Exception as e:
        logger.debug(f"Помилка опитування розділів: {e}")

    # 4. Відеокарти: спершу NVIDIA (з VRAM), потім решта з реєстру
    nvidia_gpus = _get_nvidia_gpus()
    registry_gpus = _get_display_adapters_from_registry()

    all_gpus = list(nvidia_gpus)
    nvidia_names = {g["name"].lower() for g in nvidia_gpus}
    for rg in registry_gpus:
        # Не дублюємо відеокарту, якщо її вже знайдено через NVML
        rg_name = rg["name"].lower()
        if not any(rg_name in n or n in rg_name for n in nvidia_names):
            all_gpus.append({
                "name": rg["name"],
                "memory_total_bytes": None,
                "memory_total_gb": None,
                "driver_version": rg.get("driver_version"),
                "source": rg.get("source", "Windows Registry"),
            })

    # 5. ОС та аптайм
    os_info_str = _get_windows_version_string()
    boot_time_ts = int(psutil.boot_time())
    boot_time_local = format_local_time(boot_time_ts)

    specs = {
        "cpu": {
            "model": cpu_name,
            "physical_cores": physical_cores,
            "logical_cores": logical_cores,
            "base_freq_mhz": base_freq_mhz,
            "current_freq_mhz": current_freq_mhz,
        },
        "ram": {
            "total_bytes": total_ram_bytes,
            "total_gb": total_ram_gb,
        },
        "disks": disks,
        "gpus": all_gpus,
        "os": {
            "version_string": os_info_str,
            "platform": platform.platform(),
            "architecture": platform.machine(),
        },
        "system": {
            "boot_time_ts": boot_time_ts,
            "boot_time_local": boot_time_local,
        }
    }
    return specs

def get_system_specs(force_refresh: bool = False) -> Dict[str, Any]:
    """
    Повертає кешовані характеристики ПК.
    При першому виклику або force_refresh=True виконує збір.
    """
    global _cached_specs
    if _cached_specs is None or force_refresh:
        _cached_specs = collect_system_specs()
    return _cached_specs
