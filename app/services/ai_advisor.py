"""
Модуль інтеграції з Gemini API для аналізу продуктивності ПК.
- REST клієнт на основі стандартної бібліотеки urllib.request
- Передача ключа виключно в заголовку x-goog-api-key (без ключа в URL)
- Без параметрів temperature / top_p / top_k
- Контекст: характеристики, аналітика, тривоги, топ-5 програм
- Підтримка анонімізації назв процесів (AI_SEND_PROCESS_NAMES)
- Кеш автопоради на 5 хвилин
- Автоматичний офлайн-фолбек при 429, 401, 403, таймауті чи відсутності ключа
- Безпека: ключ ніколи не потрапляє у відповіді API, логи чи консоль
"""

import json
import logging
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

from app import config
from app.db.connection import db_session
from app.services.analytics import get_system_analytics
from app.services.fallback_advisor import (
    generate_fallback_advice,
    generate_fallback_chat_reply,
)
from app.services.specs import get_system_specs
from app.utils import format_local_time

logger = logging.getLogger(__name__)

# In-memory кеш для автопорад (TTL: 5 хвилин)
# Формат: {period: {"timestamp": float, "data": dict}}
_AUTO_ADVICE_CACHE: Dict[str, Dict[str, Any]] = {}
CACHE_TTL_SEC = 300  # 5 хвилин


def _anonymize_context(context: Dict[str, Any]) -> Dict[str, Any]:
    """
    Замінює реальні назви процесів на «процес 1, 2…», якщо AI_SEND_PROCESS_NAMES == False.
    """
    ctx = json.loads(json.dumps(context))  # Глибока копія
    proc_map: Dict[str, str] = {}
    proc_counter = 1

    def get_anon(name: str) -> str:
        nonlocal proc_counter
        if not name or name in ("--", "невідомо"):
            return name
        if name not in proc_map:
            proc_map[name] = f"процес {proc_counter}"
            proc_counter += 1
        return proc_map[name]

    # Реєструємо процеси з топ-програм
    for app in ctx.get("top_apps", []):
        pname = app.get("process_name")
        if pname:
            app["process_name"] = get_anon(pname)

    # Реєструємо процеси з тривог
    for a in ctx.get("alerts", []):
        culprits = a.get("culprits_summary")
        if culprits:
            # Спочатку замінюємо вже відомі назви
            for real_name, anon in list(proc_map.items()):
                culprits = culprits.replace(real_name, anon)
            # Якщо у рядку залишились незамінені назви до коми або дужки
            # наприклад "app.exe (95% CPU)"
            for part in culprits.split(","):
                part_clean = part.strip().split(" ")[0].strip()
                if part_clean and "." in part_clean and part_clean not in proc_map:
                    anon = get_anon(part_clean)
                    culprits = culprits.replace(part_clean, anon)
            a["culprits_summary"] = culprits

        # Якщо є список структурованих винуватців
        if "culprits" in a and isinstance(a["culprits"], list):
            for c in a["culprits"]:
                if isinstance(c, dict) and c.get("name"):
                    c["name"] = get_anon(c["name"])

    return ctx


def build_ai_context(period: str = "24h", db_path=None) -> Dict[str, Any]:
    """
    Формує компактний JSON-зріз системи для передачі в модель:
    - Характеристики (CPU, ядра, RAM, GPU, диски, ОС)
    - Аналітика (середні/піки, червона зона, індекс здоров'я, прогноз дисків)
    - Тривоги (активні та останні)
    - Топ-5 програм за навантаженням
    - Анонімізація назв процесів при AI_SEND_PROCESS_NAMES=False
    """
    specs = get_system_specs()
    analytics = get_system_analytics(period=period, db_path=db_path)

    # Компактні характеристики
    cpu_info = specs.get("cpu", {})
    ram_info = specs.get("ram", {})
    os_info = specs.get("os", {})
    sys_info = specs.get("system", {})
    gpus_info = specs.get("gpus", [])
    disks_info = specs.get("disks", [])

    compact_specs = {
        "cpu": {
            "model": cpu_info.get("model", "Невідомо"),
            "physical_cores": cpu_info.get("physical_cores", 1),
            "logical_cores": cpu_info.get("logical_cores", 1),
            "base_freq_mhz": cpu_info.get("base_freq_mhz"),
        },
        "ram_total_gb": ram_info.get("total_gb", 0),
        "os": f"{os_info.get('version_string', 'Windows')} ({os_info.get('architecture', 'x64')})",
        "boot_time": sys_info.get("boot_time_local", "--"),
        "gpus": [
            {
                "name": g.get("name"),
                "vram_gb": g.get("memory_total_gb"),
            }
            for g in gpus_info
        ],
        "disks": [
            {
                "mount": d.get("mountpoint") or d.get("mount"),
                "total_gb": d.get("total_gb"),
                "free_gb": d.get("free_gb"),
                "used_percent": d.get("used_percent"),
            }
            for d in disks_info
        ],
    }

    # Компактна аналітика
    compact_analytics = {
        "period": period,
        "health_index": analytics.get("health_index", 100),
        "health_status": analytics.get("health_status", "Відмінний"),
        "summary": analytics.get("summary", {}),
        "disk_projections": [
            {
                "mount": p.get("mountpoint"),
                "free_gb": p.get("free_gb"),
                "free_percent": p.get("free_percent"),
                "days_to_full": p.get("days_to_full"),
                "status": p.get("status"),
                "projection_text": p.get("projection_text"),
            }
            for p in analytics.get("disk_projections", [])
        ],
    }

    # Топ-5 програм
    top_5_apps = analytics.get("top_apps", [])[:5]

    # Останні тривоги
    recent_alerts = []
    for a in analytics.get("culprits_ranking", [])[:5]:
        recent_alerts.append({
            "name": a.get("name"),
            "triggers_count": a.get("alert_triggers_count"),
            "avg_cpu": a.get("avg_cpu"),
            "max_cpu": a.get("max_cpu"),
        })

    # Читаємо активні тривоги з бази даних
    active_alerts = []
    try:
        with db_session(db_path) as conn:
            cur = conn.cursor()
            cur.execute("""
                SELECT id, metric_name, rule_condition, trigger_value, threshold_value, culprits_summary
                FROM alerts
                WHERE resolved_at IS NULL
                ORDER BY timestamp DESC
                LIMIT 5
            """)
            for row in cur.fetchall():
                active_alerts.append({
                    "is_active": True,
                    "metric": row["metric_name"],
                    "rule_condition": row["rule_condition"],
                    "trigger_value": row["trigger_value"],
                    "threshold_value": row["threshold_value"],
                    "culprits_summary": row["culprits_summary"],
                })
    except Exception as e:
        logger.warning(f"Не вдалося зчитати активні тривоги для ШІ: {e}")

    context: Dict[str, Any] = {
        "specs": compact_specs,
        "analytics": compact_analytics,
        "disks": compact_specs["disks"],
        "top_apps": top_5_apps,
        "alerts": active_alerts or recent_alerts,
    }

    # Застосовуємо анонімізацію назв процесів за потреби
    if not config.AI_SEND_PROCESS_NAMES:
        context = _anonymize_context(context)

    return context


def _call_gemini_rest(contents: List[Dict[str, Any]]) -> str:
    """
    Виконує прямий виклик Google Gemini generateContent REST API через urllib.request:
    - Модель: config.GEMINI_MODEL (gemini-3.5-flash-lite)
    - Ключ у заголовку x-goog-api-key (без ключа в URL)
    - Таймаут: config.GEMINI_TIMEOUT_SEC (15 с)
    - Без параметрів temperature / top_p / top_k
    """
    key = config.GEMINI_API_KEY
    if not key or not key.strip():
        raise ValueError("GEMINI_API_KEY не налаштовано")

    model = config.GEMINI_MODEL
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

    # Формування payload strictly без temperature/top_p/top_k
    payload = {
        "system_instruction": {
            "parts": [{"text": config.AI_SYSTEM_INSTRUCTION}]
        },
        "contents": contents,
    }

    req_data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=req_data,
        headers={
            "Content-Type": "application/json",
            "x-goog-api-key": key,
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=config.GEMINI_TIMEOUT_SEC) as response:
            raw_res = response.read().decode("utf-8")
    except Exception as e:
        if hasattr(e, "close"):
            try:
                e.close()
            except Exception:
                pass
        raise

    res_json = json.loads(raw_res)
    candidates = res_json.get("candidates", [])
    if not candidates:
        raise ValueError("Gemini API не повернув варіантів відповіді (candidates)")

    parts = candidates[0].get("content", {}).get("parts", [])
    if not parts or not parts[0].get("text"):
        raise ValueError("Порожня відповідь від Gemini API")

    return parts[0]["text"].strip()


def save_ai_advice_record(
    mode: str,
    source: str,
    user_query: Optional[str],
    metrics_summary: str,
    advice_text: str,
    status: str,
    db_path=None,
) -> int:
    """
    Зберігає запис поради або чат-репліки в таблицю ai_advice.
    """
    now = int(time.time())
    with db_session(db_path) as conn:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO ai_advice (timestamp, mode, source, user_query, metrics_summary, advice_text, status)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (now, mode, source, user_query, metrics_summary, advice_text, status),
        )
        return cur.lastrowid


def get_auto_advice(period: str = "24h", db_path=None, force: bool = False) -> Dict[str, Any]:
    """
    Повертає автопораду з оптимізації комп'ютера за вказаний період:
    - Перевіряє 5-хвилинний in-memory кеш
    - Звертається до Gemini API
    - При помилці/таймауті/відсутності ключа виконує офлайн-фолбек
    - Зберігає результат в ai_advice
    """
    now = time.time()

    # 1. Перевірка 5-хвилинного кешу
    if not force and period in _AUTO_ADVICE_CACHE:
        entry = _AUTO_ADVICE_CACHE[period]
        if (now - entry["timestamp"]) < CACHE_TTL_SEC:
            logger.info(f"Повернення автопоради з кешу для period={period}")
            return entry["data"]

    context = build_ai_context(period=period, db_path=db_path)
    context_json = json.dumps(context, ensure_ascii=False)

    source = "Gemini"
    status = "ok"
    advice_text = ""

    # 2. Якщо ключ не налаштовано — відразу офлайн-фолбек
    if not config.GEMINI_API_KEY:
        source = "Автономний режим"
        status = "fallback"
        advice_text = generate_fallback_advice(context)
    else:
        # 3. Запит до Gemini generateContent
        user_prompt = (
            f"Ось поточні телеметричні дані цього комп'ютера за період {period}:\n"
            f"```json\n{context_json}\n```\n"
            f"Проаналізуй навантаження ресурсів, характеристики та можливі вузькі місця. "
            f"Сформулюй короткий висновок та від 2 до 4 конкретних порад з оптимізації."
        )

        contents = [
            {
                "role": "user",
                "parts": [{"text": user_prompt}],
            }
        ]

        try:
            advice_text = _call_gemini_rest(contents)
        except Exception as e:
            # Логуємо помилку БЕЗ ключа
            err_type = type(e).__name__
            logger.warning(
                f"Помилка запиту до Gemini API ({err_type}). Виконується автономний фолбек."
            )
            source = "Автономний режим"
            status = "fallback"
            advice_text = generate_fallback_advice(context)

    # 4. Збереження в базу даних
    try:
        save_ai_advice_record(
            mode="auto",
            source=source,
            user_query=None,
            metrics_summary=context_json,
            advice_text=advice_text,
            status=status,
            db_path=db_path,
        )
    except Exception as e:
        logger.error(f"Не вдалося зберегти пораду ШІ в БД: {e}")

    result = {
        "advice": advice_text,
        "source": source,
        "status": status,
        "period": period,
        "timestamp": int(now),
    }

    # Оновлення кешу
    _AUTO_ADVICE_CACHE[period] = {
        "timestamp": now,
        "data": result,
    }

    return result


def chat_with_advisor(
    message: str,
    history: Optional[List[Dict[str, str]]] = None,
    period: str = "24h",
    db_path=None,
) -> Dict[str, Any]:
    """
    Обробляє запит у режимі діалогу (чат):
    - Ліміт повідомлення: <= 500 символів (інакше ValueError)
    - Контекст історії: не більше 6 останніх реплік
    - Додає компактний зріз телеметрії системи
    - Офлайн-фолбек при недоступності Gemini
    - Зберігає результат в ai_advice
    """
    if not message or not message.strip():
        raise ValueError("Повідомлення не може бути порожнім")

    if len(message) > 500:
        raise ValueError("Повідомлення не може перевищувати 500 символів")

    context = build_ai_context(period=period, db_path=db_path)
    context_json = json.dumps(context, ensure_ascii=False)

    # Обмежуємо історію до 6 останніх реплік
    safe_history = history[-6:] if history else []

    source = "Gemini"
    status = "ok"
    reply_text = ""

    if not config.GEMINI_API_KEY:
        source = "Автономний режим"
        status = "fallback"
        reply_text = generate_fallback_chat_reply(message, context)
    else:
        contents: List[Dict[str, Any]] = []

        # Додаємо попередні репліки з історії
        for item in safe_history:
            role = "user" if item.get("role") == "user" else "model"
            text_content = item.get("text") or item.get("content") or ""
            if text_content:
                contents.append({
                    "role": role,
                    "parts": [{"text": text_content}],
                })

        # Поточне запитання разом зі свіжим контекстом системи
        current_prompt = (
            f"Телеметричний стан системи:\n```json\n{context_json}\n```\n\n"
            f"Запитання користувача: {message}"
        )
        contents.append({
            "role": "user",
            "parts": [{"text": current_prompt}],
        })

        try:
            reply_text = _call_gemini_rest(contents)
        except Exception as e:
            err_type = type(e).__name__
            logger.warning(
                f"Помилка чату з Gemini API ({err_type}). Виконується автономний фолбек."
            )
            source = "Автономний режим"
            status = "fallback"
            reply_text = generate_fallback_chat_reply(message, context)

    # Збереження в базу даних
    now = int(time.time())
    try:
        save_ai_advice_record(
            mode="chat",
            source=source,
            user_query=message,
            metrics_summary=context_json,
            advice_text=reply_text,
            status=status,
            db_path=db_path,
        )
    except Exception as e:
        logger.error(f"Не вдалося зберегти репліку чату ШІ в БД: {e}")

    return {
        "reply": reply_text,
        "source": source,
        "status": status,
        "timestamp": now,
    }


def get_ai_history(limit: int = 50, db_path=None) -> List[Dict[str, Any]]:
    """
    Повертає історію порад та відповідей із таблиці ai_advice.
    """
    records = []
    with db_session(db_path) as conn:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT id, timestamp, mode, source, user_query, advice_text, status
            FROM ai_advice
            ORDER BY timestamp DESC
            LIMIT ?
            """,
            (limit,),
        )
        for r in cur.fetchall():
            records.append({
                "id": r["id"],
                "timestamp": r["timestamp"],
                "local_time": format_local_time(r["timestamp"]),
                "mode": r["mode"],
                "source": r["source"],
                "user_query": r["user_query"],
                "advice_text": r["advice_text"],
                "status": r["status"],
            })
    return records
