"""
Маршрути Flask API для System Monitor.
Реалізує ендпоінти:
- GET /api/status
- GET /api/metrics/current
- GET /api/metrics/history
- GET /api/processes
"""

import json
import time
from typing import Optional

import psutil
from flask import Blueprint, current_app, jsonify, request

from app.db.connection import db_session
from app.server.helpers import format_local_time, format_local_time_short

api_bp = Blueprint("api", __name__, url_prefix="/api")

@api_bp.route("/status", methods=["GET"])
def get_status():
    """Перевірка стану системи та сервера."""
    start_time = getattr(current_app, "start_time", time.time())
    app_uptime = int(time.time() - start_time)

    # Системний аптайм через psutil
    system_boot = psutil.boot_time()
    system_uptime = int(time.time() - system_boot)

    collector = getattr(current_app, "collector", None)
    collector_alive = bool(collector and collector._thread and collector._thread.is_alive())

    return jsonify({
        "status": "ok",
        "version": "1.0.0",
        "app_uptime_sec": app_uptime,
        "system_uptime_sec": system_uptime,
        "collector_active": collector_alive,
        "logical_cores": psutil.cpu_count(logical=True) or 1,
    })

@api_bp.route("/metrics/current", methods=["GET"])
def get_current_metrics():
    """Повертає найостанніший знімок телеметрії."""
    collector = getattr(current_app, "collector", None)
    metrics = None
    if collector:
        metrics = collector.get_current_metrics()

    # Якщо колектор ще не наповнив кеш, читаємо останній запис із БД
    if not metrics:
        db_path = getattr(current_app, "db_path", None)
        with db_session(db_path) as conn:
            cur = conn.cursor()
            cur.execute("""
                SELECT * FROM metrics ORDER BY timestamp DESC LIMIT 1
            """)
            row = cur.fetchone()
            if row:
                metrics = dict(row)
                metrics["cpu_cores"] = json.loads(row["cpu_cores"])
                metrics["disks_info"] = json.loads(row["disks_info"])

    if not metrics:
        return jsonify({"error": "Метрики ще не зібрані"}), 503

    metrics["local_time"] = format_local_time(metrics["timestamp"])
    return jsonify(metrics)

@api_bp.route("/metrics/history", methods=["GET"])
def get_metrics_history():
    """
    Повертає часовий ряд метрик:
    - range=live: останні 60 с (посекундні сирі дані)
    - range=hour: останні 60 хв (3600 с)
    - range=day: останні 24 год (похвилинні агрегати)
    """
    range_param = request.args.get("range", "live").lower()
    now = int(time.time())
    db_path = getattr(current_app, "db_path", None)

    if range_param == "day":
        cutoff = now - 86400
        # Для добового графіку беремо похвилинні агрегати (is_aggregated = 1),
        # або сирі дані, якщо агрегатів за цей час ще немає
        query = """
        SELECT timestamp, cpu_total, cpu_max, ram_percent, ram_max,
               disk_read_bytes_sec, disk_write_bytes_sec, net_sent_bytes_sec, net_recv_bytes_sec,
               is_aggregated
        FROM metrics
        WHERE timestamp >= ?
        ORDER BY timestamp ASC
        """
        params = (cutoff,)
    elif range_param == "hour":
        cutoff = now - 3600
        query = """
        SELECT timestamp, cpu_total, cpu_max, ram_percent, ram_max,
               disk_read_bytes_sec, disk_write_bytes_sec, net_sent_bytes_sec, net_recv_bytes_sec,
               is_aggregated
        FROM metrics
        WHERE timestamp >= ?
        ORDER BY timestamp ASC
        """
        params = (cutoff,)
    else:  # live (за замовчуванням 60 секунд)
        cutoff = now - 60
        query = """
        SELECT timestamp, cpu_total, cpu_max, ram_percent, ram_max,
               disk_read_bytes_sec, disk_write_bytes_sec, net_sent_bytes_sec, net_recv_bytes_sec,
               is_aggregated
        FROM metrics
        WHERE is_aggregated = 0 AND timestamp >= ?
        ORDER BY timestamp ASC
        """
        params = (cutoff,)

    with db_session(db_path) as conn:
        cur = conn.cursor()
        cur.execute(query, params)
        rows = cur.fetchall()

    history = []
    for r in rows:
        history.append({
            "timestamp": r["timestamp"],
            "local_time": format_local_time_short(r["timestamp"]),
            "cpu_total": r["cpu_total"],
            "cpu_max": r["cpu_max"],
            "ram_percent": r["ram_percent"],
            "ram_max": r["ram_max"],
            "disk_read_bytes_sec": r["disk_read_bytes_sec"],
            "disk_write_bytes_sec": r["disk_write_bytes_sec"],
            "net_sent_bytes_sec": r["net_sent_bytes_sec"],
            "net_recv_bytes_sec": r["net_recv_bytes_sec"],
            "is_aggregated": r["is_aggregated"],
        })

    return jsonify({
        "range": range_param,
        "count": len(history),
        "data": history,
    })

@api_bp.route("/processes", methods=["GET"])
def get_processes():
    """
    Повертає актуальний список топ-процесів.
    Query:
    - sort=cpu (за замовчуванням) або ram
    - limit=15 (за замовчуванням 15, максимум 50)
    """
    sort_by = request.args.get("sort", "cpu").lower()
    if sort_by not in ("cpu", "ram"):
        sort_by = "cpu"

    try:
        limit = min(max(int(request.args.get("limit", 15)), 1), 50)
    except ValueError:
        limit = 15

    collector = getattr(current_app, "collector", None)
    procs = []
    if collector:
        procs = collector.get_top_processes(sort_by=sort_by, limit=limit)

    # Якщо колектор ще не заповнив пам'ять, беремо останній збережений знімок із бази
    if not procs:
        db_path = getattr(current_app, "db_path", None)
        with db_session(db_path) as conn:
            cur = conn.cursor()
            # Знаходимо останній timestamp у таблиці
            cur.execute("SELECT MAX(timestamp) AS last_ts FROM process_snapshots WHERE alert_id IS NULL")
            row = cur.fetchone()
            if row and row["last_ts"]:
                order_col = "memory_percent" if sort_by == "ram" else "cpu_percent"
                cur.execute(f"""
                    SELECT pid, name, cpu_percent, memory_percent, memory_rss_bytes, status
                    FROM process_snapshots
                    WHERE alert_id IS NULL AND timestamp = ?
                    ORDER BY {order_col} DESC
                    LIMIT ?
                """, (row["last_ts"], limit))
                procs = [dict(r) for r in cur.fetchall()]

    return jsonify({
        "sort_by": sort_by,
        "count": len(procs),
        "processes": procs,
    })
