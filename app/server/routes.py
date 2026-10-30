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
from flask import Blueprint, current_app, jsonify, request, send_file

from app.core.alerts import AlertManager
from app.db.connection import db_session
from app.utils import format_local_time, format_local_time_short
from app.services.analytics import get_system_analytics
from app.services.exporter import generate_excel_report
from app.services.specs import get_system_specs

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
    collector_alive = False
    threads_status = {
        "metrics": False,
        "processes": False,
        "gpu": False,
    }

    if collector:
        if hasattr(collector, "is_running") and callable(collector.is_running):
            collector_alive = bool(collector.is_running())
        elif hasattr(collector, "is_running"):
            collector_alive = bool(collector.is_running)
        elif hasattr(collector, "_system_thread") and collector._system_thread:
            collector_alive = bool(collector._system_thread.is_alive())

        if hasattr(collector, "get_threads_status") and callable(collector.get_threads_status):
            threads_status = collector.get_threads_status()
        elif hasattr(collector, "threads_status"):
            threads_status = collector.threads_status

    return jsonify({
        "status": "ok",
        "version": "1.0.0",
        "app_uptime_sec": app_uptime,
        "system_uptime_sec": system_uptime,
        "collector_active": collector_alive,
        "threads": threads_status,
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
    - range=live: останні 60 с (посекундні сирі дані, читаються з пам'яті для 0% навантаження на SQLite)
    - range=hour: останні 60 хв (3600 с)
    - range=day: останні 24 год (похвилинні агрегати)
    """
    range_param = request.args.get("range", "live").lower()
    now = int(time.time())
    db_path = getattr(current_app, "db_path", None)
    collector = getattr(current_app, "collector", None)

    # 1. Швидкий шлях для live-режиму з пам'яті без звернення до диску
    if range_param == "live" and collector:
        live_data = collector.get_live_history(limit=60)
        if live_data:
            history = []
            for r in live_data:
                history.append({
                    "timestamp": r["timestamp"],
                    "local_time": format_local_time_short(r["timestamp"]),
                    "cpu_total": r["cpu_total"],
                    "cpu_max": r["cpu_max"],
                    "ram_percent": r["ram_percent"],
                    "ram_max": r["ram_max"],
                    "gpu_percent": r.get("gpu_percent"),
                    "gpu_max": r.get("gpu_max"),
                    "disk_read_bytes_sec": r["disk_read_bytes_sec"],
                    "disk_write_bytes_sec": r["disk_write_bytes_sec"],
                    "net_sent_bytes_sec": r["net_sent_bytes_sec"],
                    "net_recv_bytes_sec": r["net_recv_bytes_sec"],
                    "is_aggregated": r.get("is_aggregated", 0),
                })
            return jsonify({
                "range": "live",
                "count": len(history),
                "data": history,
            })

    # 2. Читання з SQLite для тривалих періодів або фолбеку
    if range_param == "day":
        cutoff = now - 86400
        query = """
        SELECT timestamp, cpu_total, cpu_max, ram_percent, ram_max,
               gpu_percent, gpu_max,
               disk_read_bytes_sec, disk_write_bytes_sec, net_sent_bytes_sec, net_recv_bytes_sec,
               is_aggregated
        FROM metrics
        WHERE timestamp >= ?
        ORDER BY timestamp ASC
        """
        params = (cutoff,)
    elif range_param == "hour":
        cutoff = now - 3600
        # Усереднення по 10 с з лінією піків cpu_max / ram_max
        query = """
        SELECT (timestamp / 10) * 10 AS timestamp,
               ROUND(AVG(cpu_total), 1) AS cpu_total,
               ROUND(MAX(cpu_max), 1) AS cpu_max,
               ROUND(AVG(ram_percent), 1) AS ram_percent,
               ROUND(MAX(ram_max), 1) AS ram_max,
               ROUND(AVG(gpu_percent), 1) AS gpu_percent,
               ROUND(MAX(gpu_max), 1) AS gpu_max,
               ROUND(AVG(disk_read_bytes_sec), 1) AS disk_read_bytes_sec,
               ROUND(AVG(disk_write_bytes_sec), 1) AS disk_write_bytes_sec,
               ROUND(AVG(net_sent_bytes_sec), 1) AS net_sent_bytes_sec,
               ROUND(AVG(net_recv_bytes_sec), 1) AS net_recv_bytes_sec,
               MAX(is_aggregated) AS is_aggregated
        FROM metrics
        WHERE timestamp >= ?
        GROUP BY timestamp / 10
        ORDER BY timestamp ASC
        """
        params = (cutoff,)
    else:  # live фолбек з бази
        cutoff = now - 60
        query = """
        SELECT timestamp, cpu_total, cpu_max, ram_percent, ram_max,
               gpu_percent, gpu_max,
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
            "gpu_percent": r["gpu_percent"],
            "gpu_max": r["gpu_max"],
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

@api_bp.route("/system/specs", methods=["GET"])
def get_specs_endpoint():
    """Повертає характеристики комп'ютера (CPU, RAM, диски, GPU, ОС, Boot Time)."""
    force = request.args.get("refresh", "false").lower() in ("true", "1")
    specs = get_system_specs(force_refresh=force)
    return jsonify(specs)

@api_bp.route("/alerts", methods=["GET"])
def get_alerts_endpoint():
    """
    Повертає журнал тривог із процесами-винуватцями:
    - active=true: лише активні
    - limit=50: кількість
    """
    active_only = request.args.get("active", "false").lower() in ("true", "1")
    limit = min(max(int(request.args.get("limit", 50)), 1), 200)

    collector = getattr(current_app, "collector", None)
    if collector and hasattr(collector, "alert_manager"):
        if active_only:
            alerts = collector.alert_manager.get_active_alerts()
        else:
            alerts = collector.alert_manager.get_alerts(limit=limit)
    else:
        db_path = getattr(current_app, "db_path", None)
        mgr = AlertManager(db_path=db_path)
        if active_only:
            alerts = mgr.get_active_alerts()
        else:
            alerts = mgr.get_alerts(limit=limit)

    return jsonify({
        "count": len(alerts),
        "active_count": sum(1 for a in alerts if a.get("is_active")),
        "alerts": alerts,
    })

@api_bp.route("/analytics", methods=["GET"])
def get_analytics_endpoint():
    """
    Повертає системну аналітику:
    - period=1h|24h|7d (за замовчуванням 24h)
    """
    period = request.args.get("period", "24h").lower()
    if period not in ("1h", "24h", "7d"):
        period = "24h"

    db_path = getattr(current_app, "db_path", None)
    data = get_system_analytics(period=period, db_path=db_path)
    return jsonify(data)

@api_bp.route("/export/excel", methods=["GET"])
def export_excel_endpoint():
    """
    Генерує файл report_YYYY-MM-DD_HH-MM.xlsx із 6 аркушами:
    - period=1h|24h|7d|all
    - base64=true: повертає JSON із base64 для нативного діалогу pywebview
    """
    period = request.args.get("period", "24h").lower()
    if period not in ("1h", "24h", "7d", "all"):
        period = "24h"

    db_path = getattr(current_app, "db_path", None)
    bio, filename = generate_excel_report(period=period, db_path=db_path)

    if request.args.get("base64", "false").lower() in ("true", "1"):
        import base64
        b64_data = base64.b64encode(bio.getvalue()).decode("utf-8")
        return jsonify({
            "filename": filename,
            "data": b64_data,
            "mime": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        })

    return send_file(
        bio,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        as_attachment=True,
        download_name=filename,
    )

@api_bp.route("/ai/advise", methods=["POST"])
def post_ai_advise():
    """
    Отримання автопоради ШІ:
    - period: 1h | 24h | 7d (за замовчуванням 24h)
    - force: boolean (скидання 5-хвилинного кешу)
    """
    from app.services.ai_advisor import get_auto_advice
    req_json = request.get_json(silent=True) or {}
    period = req_json.get("period") or request.args.get("period", "24h")
    if period not in ("1h", "24h", "7d"):
        period = "24h"
    force = bool(req_json.get("force", False) or request.args.get("force", "false").lower() in ("true", "1"))

    db_path = getattr(current_app, "db_path", None)
    result = get_auto_advice(period=period, db_path=db_path, force=force)
    return jsonify(result)

@api_bp.route("/ai/chat", methods=["POST"])
def post_ai_chat():
    """
    Діалог із ШІ-порадником:
    - message: рядок <= 500 символів (інакше 400 Bad Request)
    - history: список реплік (до 6 останніх)
    - period: 1h | 24h | 7d (за замовчуванням 24h)
    """
    from app.services.ai_advisor import chat_with_advisor
    req_json = request.get_json(silent=True) or {}
    message = (req_json.get("message") or "").strip()
    if not message:
        return jsonify({"error": "Повідомлення не може бути порожнім"}), 400
    if len(message) > 500:
        return jsonify({"error": "Повідомлення не може перевищувати 500 символів"}), 400

    history = req_json.get("history", [])
    period = req_json.get("period", "24h")
    if period not in ("1h", "24h", "7d"):
        period = "24h"

    db_path = getattr(current_app, "db_path", None)
    try:
        result = chat_with_advisor(message=message, history=history, period=period, db_path=db_path)
        return jsonify(result)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        return jsonify({"error": "Не вдалося отримати відповідь ШІ"}), 500

@api_bp.route("/ai/history", methods=["GET"])
def get_ai_history_endpoint():
    """Повертає історію порад та відповідей ШІ."""
    from app.services.ai_advisor import get_ai_history
    limit = min(max(int(request.args.get("limit", 50)), 1), 200)
    db_path = getattr(current_app, "db_path", None)
    history = get_ai_history(limit=limit, db_path=db_path)
    return jsonify({
        "count": len(history),
        "history": history,
    })

@api_bp.route("/ai/status", methods=["GET"])
def get_ai_status():
    """
    Повертає статус інтеграції ШІ:
    - has_key: чи налаштовано GEMINI_API_KEY (без показу ключа)
    - model: назва моделі
    - send_process_names: стан надсилання назв процесів
    """
    from app import config
    has_key = bool(config.GEMINI_API_KEY and config.GEMINI_API_KEY.strip())
    return jsonify({
        "has_key": has_key,
        "model": config.GEMINI_MODEL,
        "send_process_names": config.AI_SEND_PROCESS_NAMES,
    })

