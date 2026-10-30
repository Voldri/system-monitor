"""
Ініціалізація Flask додатку для System Monitor.
"""

import logging
import time
from pathlib import Path
from typing import Optional

from flask import Flask, jsonify, send_from_directory

from app.config import DB_PATH, STATIC_DIR
from app.server.routes import api_bp

# Приглушення службових логів Werkzeug (рівень WARNING)
logging.getLogger("werkzeug").setLevel(logging.WARNING)

def create_app(collector=None, db_path: Optional[Path] = None) -> Flask:
    """
    Фабрика Flask застосунку.
    Прив'язує статичні файли дашборду (/static) та віддає index.html на корінь (/).
    """
    app = Flask(
        __name__,
        static_folder=str(STATIC_DIR),
        static_url_path="/static"
    )
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
            "status": "Очікується розмітка index.html"
        })

    return app
