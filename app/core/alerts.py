"""
Модуль моніторингу та обробки системних тривог (Alerts Engine).
- Правила:
  1. CPU > 90% понад 30 с
  2. RAM > 85% понад 10 с
  3. Вільне місце на будь-якому розділі < 5%
  4. Температура GPU > 85 °C понад 30 с
- При спрацюванні:
  - Запис у таблицю alerts
  - Знімок топ-3 процесів-винуватців у process_snapshots із прив'язкою alert_id (без PID 0 та System)
- При нормалізації показника: встановлення resolved_at.
- Захист від спаму: поки тривога активна, нова за тим самим правилом не створюється.
"""

import json
import logging
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from app.config import (
    ALERT_CPU_DURATION_SEC,
    ALERT_CPU_PERCENT,
    ALERT_DISK_FREE_PERCENT,
    ALERT_GPU_TEMP_C,
    ALERT_GPU_TEMP_DURATION_SEC,
    ALERT_RAM_DURATION_SEC,
    ALERT_RAM_PERCENT,
    DB_PATH,
)
from app.db.connection import db_session

logger = logging.getLogger(__name__)

class AlertManager:
    """Керування життєвим циклом тривог та фіксацією винуватців."""

    def __init__(self, db_path: Optional[Path] = None):
        self.db_path = db_path or DB_PATH
        # Час початку перевищення порогу для правил із таймером: rule_key -> float(ts)
        self._exceed_starts: Dict[str, float] = {}
        # Активні незакриті тривоги: rule_key -> alert_id
        self._active_alerts: Dict[str, int] = {}

        # Завантажуємо незакриті тривоги з БД для уникнення дублікатів після перезапуску
        self._load_active_alerts_from_db()

    def _load_active_alerts_from_db(self) -> None:
        """Зчитує активні (resolved_at IS NULL) тривоги з бази даних."""
        try:
            with db_session(self.db_path) as conn:
                cur = conn.cursor()
                cur.execute("SELECT id, metric_name, rule_condition FROM alerts WHERE resolved_at IS NULL")
                rows = cur.fetchall()
                for r in rows:
                    m_name = r["metric_name"] if "metric_name" in r.keys() else ""
                    cond = r["rule_condition"] if "rule_condition" in r.keys() else ""
                    if m_name == "CPU" or (not m_name and "CPU" in cond and "GPU" not in cond):
                        self._active_alerts["cpu_high"] = r["id"]
                    elif m_name == "RAM" or (not m_name and "RAM" in cond):
                        self._active_alerts["ram_high"] = r["id"]
                    elif m_name == "GPU_TEMP" or (not m_name and "GPU" in cond):
                        self._active_alerts["gpu_temp_high"] = r["id"]
                    elif m_name.startswith("DISK:") or "Вільне місце" in cond:
                        if m_name.startswith("DISK:"):
                            mount = m_name.split("DISK:", 1)[1].strip()
                        else:
                            parts = cond.split()
                            mount = parts[2] if len(parts) > 2 else "unknown"
                        self._active_alerts[f"disk_low_{mount}"] = r["id"]
        except Exception as e:
            logger.warning(f"Не вдалося завантажити активні алерти з БД: {e}")

    def check_metrics(
        self,
        metrics: Dict[str, Any],
        current_procs: Optional[List[Dict[str, Any]]] = None,
        now: Optional[float] = None
    ) -> List[Dict[str, Any]]:
        """
        Перевіряє поточні показники на відповідність правилам тривог.
        Повертає список новостворених або оновлених тривог за цей крок.
        """
        if not metrics:
            return []

        ts = now or metrics.get("timestamp") or time.time()
        triggered_events = []

        # 1. CPU > 90% понад 30 с
        cpu_val = float(metrics.get("cpu_total", 0.0))
        self._evaluate_rule_with_duration(
            rule_key="cpu_high",
            current_value=cpu_val,
            threshold=ALERT_CPU_PERCENT,
            duration_sec=ALERT_CPU_DURATION_SEC,
            operator="gt",
            metric_name="CPU",
            rule_condition=f"CPU > {ALERT_CPU_PERCENT}% понад {ALERT_CPU_DURATION_SEC} с",
            severity="critical",
            current_procs=current_procs,
            sort_by="cpu",
            ts=ts,
            triggered_events=triggered_events,
        )

        # 2. RAM > 85% понад 10 с
        ram_val = float(metrics.get("ram_percent", 0.0))
        self._evaluate_rule_with_duration(
            rule_key="ram_high",
            current_value=ram_val,
            threshold=ALERT_RAM_PERCENT,
            duration_sec=ALERT_RAM_DURATION_SEC,
            operator="gt",
            metric_name="RAM",
            rule_condition=f"RAM > {ALERT_RAM_PERCENT}% понад {ALERT_RAM_DURATION_SEC} с",
            severity="critical",
            current_procs=current_procs,
            sort_by="ram",
            ts=ts,
            triggered_events=triggered_events,
        )

        # 3. Вільне місце на будь-якому розділі < 5% (миттєве спрацювання)
        disks_info = metrics.get("disks_info", [])
        if isinstance(disks_info, str):
            try:
                disks_info = json.loads(disks_info)
            except Exception:
                disks_info = []

        for d in disks_info:
            mount = d.get("mountpoint") or d.get("device") or "Unknown"
            free_pct = d.get("free_percent")
            if free_pct is None:
                total_b = d.get("total_bytes", 0)
                free_b = d.get("free_bytes", 0)
                free_pct = (free_b / total_b * 100.0) if total_b > 0 else 100.0

            rule_key = f"disk_low_{mount}"
            if free_pct < ALERT_DISK_FREE_PERCENT:
                if rule_key not in self._active_alerts:
                    alert_id = self._create_alert(
                        metric_name=f"DISK: {mount}",
                        rule_condition=f"Вільне місце {mount} < {ALERT_DISK_FREE_PERCENT}%",
                        trigger_value=round(free_pct, 2),
                        threshold_value=ALERT_DISK_FREE_PERCENT,
                        severity="critical",
                        current_procs=None,
                        sort_by="disk",
                        ts=int(ts),
                    )
                    self._active_alerts[rule_key] = alert_id
                    triggered_events.append({"type": "triggered", "rule": rule_key, "alert_id": alert_id})
            else:
                if rule_key in self._active_alerts:
                    self._resolve_alert(rule_key, int(ts))
                    triggered_events.append({"type": "resolved", "rule": rule_key})

        # 4. Температура GPU > 85 °C понад 30 с
        gpu_temp = metrics.get("gpu_temp_c")
        if gpu_temp is not None:
            self._evaluate_rule_with_duration(
                rule_key="gpu_temp_high",
                current_value=float(gpu_temp),
                threshold=ALERT_GPU_TEMP_C,
                duration_sec=ALERT_GPU_TEMP_DURATION_SEC,
                operator="gt",
                metric_name="GPU_TEMP",
                rule_condition=f"Температура GPU > {ALERT_GPU_TEMP_C} °C понад {ALERT_GPU_TEMP_DURATION_SEC} с (окремої GPU-статистики процесів немає — винуватці за CPU)",
                severity="critical",
                current_procs=current_procs,
                sort_by="cpu",
                ts=ts,
                triggered_events=triggered_events,
            )

        return triggered_events

    def _evaluate_rule_with_duration(
        self,
        rule_key: str,
        current_value: float,
        threshold: float,
        duration_sec: int,
        operator: str,
        metric_name: str,
        rule_condition: str,
        severity: str,
        current_procs: Optional[List[Dict[str, Any]]],
        sort_by: str,
        ts: float,
        triggered_events: List[Dict[str, Any]],
    ) -> None:
        """Перевірка правила з фіксацією тривалості перевищення."""
        is_exceeded = (current_value > threshold) if operator == "gt" else (current_value < threshold)

        if is_exceeded:
            if rule_key not in self._exceed_starts:
                self._exceed_starts[rule_key] = ts

            exceeded_duration = ts - self._exceed_starts[rule_key]
            # Якщо поріг перевищено довше зазначеного часу і алерту ще немає
            if exceeded_duration >= duration_sec and rule_key not in self._active_alerts:
                alert_id = self._create_alert(
                    metric_name=metric_name,
                    rule_condition=rule_condition,
                    trigger_value=round(current_value, 2),
                    threshold_value=threshold,
                    severity=severity,
                    current_procs=current_procs,
                    sort_by=sort_by,
                    ts=int(ts),
                )
                self._active_alerts[rule_key] = alert_id
                triggered_events.append({"type": "triggered", "rule": rule_key, "alert_id": alert_id})
        else:
            # Скидаємо таймер
            self._exceed_starts.pop(rule_key, None)
            # Якщо був активний алерт за цим правилом — закриваємо
            if rule_key in self._active_alerts:
                self._resolve_alert(rule_key, int(ts))
                triggered_events.append({"type": "resolved", "rule": rule_key})

    def _create_alert(
        self,
        metric_name: str,
        rule_condition: str,
        trigger_value: float,
        threshold_value: float,
        severity: str,
        current_procs: Optional[List[Dict[str, Any]]],
        sort_by: str,
        ts: int,
    ) -> int:
        """Створює запис тривоги та зберігає знімки топ-3 винуватців."""
        if sort_by == "disk" or metric_name.startswith("DISK"):
            culprits = []
            culprits_summary = "не застосовується"
        else:
            culprits = self._extract_top_culprits(current_procs, sort_by=sort_by, limit=3)
            if culprits:
                if sort_by == "ram":
                    culprits_summary = ", ".join(
                        f"{p['name']} ({p.get('memory_percent', 0.0)}% RAM, {p.get('cpu_percent', 0.0)}% CPU)"
                        for p in culprits
                    )
                else:
                    culprits_summary = ", ".join(
                        f"{p['name']} ({p.get('cpu_percent', 0.0)}% CPU, {p.get('memory_percent', 0.0)}% RAM)"
                        for p in culprits
                    )
            else:
                culprits_summary = "Процеси не зафіксовано"

        with db_session(self.db_path) as conn:
            cur = conn.cursor()
            cur.execute("""
                INSERT INTO alerts (
                    timestamp, resolved_at, metric_name, rule_condition,
                    trigger_value, threshold_value, severity, culprits_summary
                ) VALUES (?, NULL, ?, ?, ?, ?, ?, ?)
            """, (
                ts, metric_name, rule_condition, trigger_value,
                threshold_value, severity, culprits_summary
            ))
            alert_id = cur.lastrowid

            # Зберігаємо знімки винуватців з прив'язкою alert_id
            for p in culprits:
                cur.execute("""
                    INSERT INTO process_snapshots (
                        timestamp, alert_id, pid, name, cpu_percent,
                        memory_percent, memory_rss_bytes, status
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    ts, alert_id, p["pid"], p["name"],
                    p["cpu_percent"], p["memory_percent"],
                    p["memory_rss_bytes"], p.get("status", "running")
                ))

        logger.warning(f"ТРИВОГА СПРАЦЮВАЛА [id={alert_id}]: {rule_condition} (значення: {trigger_value}). Винуватці: {culprits_summary}")
        return alert_id

    def _resolve_alert(self, rule_key: str, ts: int) -> None:
        """Закриває активну тривогу, проставляючи resolved_at."""
        alert_id = self._active_alerts.pop(rule_key, None)
        if alert_id is None:
            return

        with db_session(self.db_path) as conn:
            cur = conn.cursor()
            cur.execute("UPDATE alerts SET resolved_at = ? WHERE id = ?", (ts, alert_id))

        logger.info(f"ТРИВОГА ЗНЯТА [id={alert_id}]: правило '{rule_key}' нормалізувалось о {ts}.")

    def _extract_top_culprits(
        self,
        procs: Optional[List[Dict[str, Any]]],
        sort_by: str = "cpu",
        limit: int = 3
    ) -> List[Dict[str, Any]]:
        """Вибирає топ-3 процеси, відсіваючи службові та нульовий PID."""
        if not procs:
            return []

        cleaned = []
        for p in procs:
            pid = p.get("pid", 0)
            name = (p.get("name") or "").lower()
            if pid == 0 or name in ("system idle process", "system", ""):
                continue
            cleaned.append(p)

        if sort_by == "ram":
            cleaned.sort(key=lambda x: x.get("memory_percent", 0.0), reverse=True)
        else:
            cleaned.sort(key=lambda x: x.get("cpu_percent", 0.0), reverse=True)

        return cleaned[:limit]

    def get_alerts(self, limit: int = 50) -> List[Dict[str, Any]]:
        """Повертає історію тривог разом із їхніми винуватцями."""
        with db_session(self.db_path) as conn:
            cur = conn.cursor()
            cur.execute("""
                SELECT id, timestamp, resolved_at, metric_name, rule_condition,
                       trigger_value, threshold_value, severity, culprits_summary
                FROM alerts
                ORDER BY timestamp DESC
                LIMIT ?
            """, (limit,))
            alerts_rows = cur.fetchall()

            res = []
            for r in alerts_rows:
                a_dict = dict(r)
                a_dict["is_active"] = (r["resolved_at"] is None)

                # Додаємо список процесів-винуватців
                # ORDER BY id ASC зберігає порядок вставки винуватців (ранжування топ-3 для відповідної тривоги)
                cur.execute("""
                    SELECT pid, name, cpu_percent, memory_percent, memory_rss_bytes, status
                    FROM process_snapshots
                    WHERE alert_id = ?
                    ORDER BY id ASC
                """, (r["id"],))
                a_dict["culprits"] = [dict(cp) for cp in cur.fetchall()]
                res.append(a_dict)

            return res

    def get_active_alerts(self) -> List[Dict[str, Any]]:
        """Повертає лише активні (не закриті) тривоги."""
        all_alerts = self.get_alerts(limit=50)
        return [a for a in all_alerts if a.get("is_active")]
