"""
Ініціалізація Flask додатку для System Monitor.
"""

import time
from pathlib import Path
from typing import Optional

from flask import Flask, jsonify, send_from_directory

from app.config import BASE_DIR, DB_PATH
from app.server.routes import api_bp

STATIC_DIR = BASE_DIR / "app" / "static"

def create_app(collector=None, db_path: Optional[Path] = None) -> Flask:
    """
    Фабрика Flask застосунку.
    Прив'язує колектор метрик та шлях до БД SQLite до контексту додатку.
    """
    app = Flask(__name__, static_folder=str(STATIC_DIR))
    app.config["JSON_AS_ASCII"] = False

    app.collector = collector
    app.db_path = db_path or DB_PATH
    app.start_time = time.time()

    # Реєстрація API Blueprint
    app.register_blueprint(api_bp)

    @app.route("/")
    def index():
        index_file = STATIC_DIR / "index.html"
        if index_file.is_file():
            return send_from_directory(str(STATIC_DIR), "index.html")
        return jsonify({
            "message": "System Monitor Flask Backend активний",
            "dashboard": "Дашборд буде підключено на Етапі 2",
            "api_docs": {
                "status": "/api/status",
                "current_metrics": "/api/metrics/current",
                "history": "/api/metrics/history?range=live|hour|day",
                "processes": "/api/processes?sort=cpu|ram&limit=15"
            }
        })

    return app
