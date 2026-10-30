"""
Модуль автономного ШІ-порадника (Fallback Advisor).
Формує експертні рекомендації на основі евристичних правил телеметрії,
коли Gemini API недоступний (429, таймаут, відсутній ключ або офлайн).
Завжди позначає свої відповіді міткою «[Автономний режим]».
"""

import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


def _clean_mount(raw: Any) -> str:
    if not raw:
        return "Диск"
    s = str(raw).strip()
    if len(s) >= 2 and s[1] == ":":
        return s[:2].upper()
    return s.rstrip("\\/ ")


def generate_fallback_advice(context: Dict[str, Any]) -> str:
    """
    Генерує структуровану автопораду на основі поточного зрізу метрик та правил:
    - Вільне місце на дисках < 10%
    - RAM > 85% або час у червоній зоні
    - Піки CPU > 90%
    - Температура GPU > 80 °C
    - Зафіксовані тривоги та їх винуватці
    """
    analytics = context.get("analytics", {})
    summary = analytics.get("summary", {})
    health = analytics.get("health_index", 100)
    health_status = analytics.get("health_status", "Відмінний")
    disks = context.get("disks", [])
    alerts = context.get("alerts", [])
    top_apps = context.get("top_apps", [])

    tips: List[str] = []
    issues: List[str] = []

    # 1. Перевірка дискового простору (< 10% вільного)
    for d in disks:
        used_pct = d.get("used_percent")
        free_pct = d.get("free_percent")
        if free_pct is None and used_pct is not None:
            free_pct = max(0.0, 100.0 - float(used_pct))
        elif free_pct is not None:
            free_pct = float(free_pct)

        mount = _clean_mount(d.get("mountpoint") or d.get("mount") or d.get("device"))
        if free_pct is not None and free_pct < 10.0:
            free_gb = d.get("free_gb", "--")
            issues.append(f"на розділі {mount} залишилося лише {free_pct:.1f}% вільного місця ({free_gb} ГБ)")
            tips.append(
                f"Критично мало місця на розділі {mount} ({free_pct:.1f}% вільно). "
                f"Очистіть Кошик, видаліть тимчасові файли або перемістіть великі медіафайли на інший диск."
            )

    # 2. Перевірка оперативної пам'яті (RAM)
    ram_peak = summary.get("ram_peak", 0.0) or 0.0
    ram_avg = summary.get("ram_avg", 0.0) or 0.0
    ram_rz = summary.get("ram_redzone_sec", 0) or 0

    if ram_peak >= 85.0 or ram_rz > 0:
        issues.append(f"високе навантаження RAM (пік {ram_peak}%, червона зона {ram_rz} с)")
        tips.append(
            f"Зафіксовано високе використання оперативної пам'яті (пік {ram_peak}%). "
            f"Закрийте невикористовувані вкладки веб-браузера або завершіть ресурсоємні фонові процеси."
        )

    # 3. Перевірка процесора (CPU)
    cpu_peak = summary.get("cpu_peak", 0.0) or 0.0
    cpu_avg = summary.get("cpu_avg", 0.0) or 0.0
    cpu_rz = summary.get("cpu_redzone_sec", 0) or 0

    if cpu_peak >= 90.0 or cpu_rz > 30:
        issues.append(f"пікові навантаження процесора (пік {cpu_peak}%, червона зона {cpu_rz} с)")
        # Знаходимо найактивніші процеси
        top_cpu_names = [a.get("process_name") for a in top_apps[:3] if a.get("process_name")]
        procs_str = f" ({', '.join(top_cpu_names)})" if top_cpu_names else ""
        tips.append(
            f"Процесор тривалий час працював під високим навантаженням{procs_str}. "
            f"Перевірте список автозавантаження та вимкніть програми, що запускаються без потреби."
        )

    # 4. Перевірка відеокарти (GPU)
    gpu_temp_peak = summary.get("gpu_temp_peak")
    gpu_peak = summary.get("gpu_peak")
    if gpu_temp_peak and gpu_temp_peak >= 80.0:
        issues.append(f"висока температура GPU ({gpu_temp_peak}°C)")
        tips.append(
            f"Температура відеокарти сягала {gpu_temp_peak}°C. "
            f"Перевірте вентиляційні отвори пристрою, очистіть пил та переконайтеся у вільному потоці повітря."
        )
    elif gpu_peak and gpu_peak >= 90.0:
        tips.append(
            f"Виявлено значне навантаження GPU (пік {gpu_peak}%). "
            f"Знизьте графічні параметри або обмежте кадрову частоту (FPS) у важких програмах."
        )

    # 5. Перевірка тривог (Alerts)
    if alerts:
        active_alerts = [a for a in alerts if a.get("is_active")]
        if active_alerts:
            first_a = active_alerts[0]
            culprits = first_a.get("culprits_summary") or "--"
            tips.append(
                f"Є активні тривоги: «{first_a.get('rule_condition')}» (винуватці: {culprits}). "
                f"Зверніть увагу на ці процеси."
            )

    # Якщо критичних проблем не знайдено — пропонуємо корисні профілактичні поради
    if not issues:
        conclusion = (
            f"Система працює стабільно, всі показники перебувають в оптимальних межах. "
            f"Індекс здоров'я оцінюється у {health}/100 ({health_status})."
        )
        tips.append("Підтримуйте актуальність оновлень Windows та драйверів пристроїв.")
        tips.append("Періодично перевіряйте стан автозавантаження, щоб запобігти накопиченню фонового навантаження.")
        tips.append("Залишайте щонайменше 15–20% вільного простору на системному SSD для стабільної роботи.")
    else:
        conclusion = f"Виявлено фактори, що знижують продуктивність: {'; '.join(issues)}."

    # Обмежуємо кількість порад від 2 до 4
    if len(tips) < 2:
        tips.append("Регулярно очищайте кеш браузера та тимчасові файли ОС.")
    final_tips = tips[:4]

    tips_formatted = "\n".join(f"{idx}. {tip}" for idx, tip in enumerate(final_tips, 1))
    return (
        f"[Автономний режим]\n\n"
        f"**Висновок:** {conclusion}\n\n"
        f"**Поради:**\n"
        f"{tips_formatted}"
    )

def generate_fallback_chat_reply(message: str, context: Dict[str, Any]) -> str:
    """
    Генерує відповідь на запитання користувача в автономному режимі,
    орієнтуючись на ключові слова запиту та наявний контекст.
    """
    msg_lower = (message or "").lower()
    analytics = context.get("analytics", {})
    summary = analytics.get("summary", {})
    specs = context.get("specs", {})
    top_apps = context.get("top_apps", [])
    disks = context.get("disks", [])

    # 1. Запитання про оперативну пам'ять (RAM)
    if any(k in msg_lower for k in ("ram", "пам'ят", "памят", "озу", "memory")):
        ram_specs = specs.get("ram", {})
        total_gb = ram_specs.get("total_gb", "--")
        ram_avg = summary.get("ram_avg", "--")
        ram_peak = summary.get("ram_peak", "--")
        return (
            f"[Автономний режим]\n\n"
            f"**Аналіз оперативної пам'яті:**\n"
            f"Загальний обсяг RAM: **{total_gb} ГБ**. "
            f"Середнє завантаження за період: **{ram_avg}%**, пікове: **{ram_peak}%**.\n\n"
            f"**Поради:**\n"
            f"1. Закривайте невикористовувані вкладки браузера або застосовуйте розширення для призупинення вкладок.\n"
            f"2. Перевірте фонові служби та вимкніть автозапуск важких застосунків."
        )

    # 2. Запитання про процесор (CPU)
    if any(k in msg_lower for k in ("cpu", "процесор", "ядер", "ядра", "навантаженн")):
        cpu_specs = specs.get("cpu", {})
        model = cpu_specs.get("model", "Процесор")
        cpu_avg = summary.get("cpu_avg", "--")
        cpu_peak = summary.get("cpu_peak", "--")
        top_procs = ", ".join(a.get("process_name", "") for a in top_apps[:3] if a.get("process_name")) or "не зафіксовано"
        return (
            f"[Автономний режим]\n\n"
            f"**Аналіз процесора ({model}):**\n"
            f"Середнє навантаження: **{cpu_avg}%**, максимальне пікове: **{cpu_peak}%**.\n"
            f"Головні процеси за навантаженням: **{top_procs}**.\n\n"
            f"**Поради:**\n"
            f"1. Вимкніть програми у автозавантаженні (Диспетчер завдань → Автозавантаження).\n"
            f"2. Перевірте, чи не виконується у фоні індексація або фонове оновлення програм."
        )

    # 3. Запитання про диски (Disk / SSD / HDD)
    if any(k in msg_lower for k in ("диск", "disk", "ssd", "hdd", "місц", "папк", "вільно")):
        disks_info = []
        for d in disks:
            m = _clean_mount(d.get("mountpoint") or d.get("mount") or d.get("device"))
            f_gb = d.get("free_gb", "--")
            t_gb = d.get("total_gb", "--")
            disks_info.append(f"{m} ({f_gb} ГБ вільно з {t_gb} ГБ)")
        disks_str = "; ".join(disks_info) if disks_info else "дані відсутні"
        return (
            f"[Автономний режим]\n\n"
            f"**Стан накопичувачів:**\n"
            f"Розділи: {disks_str}.\n\n"
            f"**Поради:**\n"
            f"1. Запустіть вбудовану утиліту Windows «Очищення диска» (cleanmgr) для видалення старих системних кешів.\n"
            f"2. Використовуйте Storage Sense (Параметри → Система → Сховище) для автоматичного видалення тимчасових файлів."
        )

    # 4. Запитання про відеокарту (GPU / Температура)
    if any(k in msg_lower for k in ("gpu", "відеокарт", "відео", "температур", "градус")):
        gpus = specs.get("gpus", [])
        gpu_name = gpus[0].get("name") if gpus else "Відеокарта"
        gpu_avg = summary.get("gpu_avg")
        gpu_peak = summary.get("gpu_peak")
        gpu_temp = summary.get("gpu_temp_peak")
        temp_str = f"{gpu_temp}°C" if gpu_temp is not None else "--"
        load_str = f"{gpu_avg}% (пік {gpu_peak}%)" if gpu_avg is not None else "--"
        return (
            f"[Автономний режим]\n\n"
            f"**Аналіз GPU ({gpu_name}):**\n"
            f"Навантаження: **{load_str}**, пікова температура: **{temp_str}**.\n\n"
            f"**Поради:**\n"
            f"1. Слідкуйте, щоб пікова температура GPU не перевищувала 80–85°C.\n"
            f"2. Оновіть графічний драйвер NVIDIA до останньої версії з офіційного сайту."
        )

    # 5. Загальна оптимізація / очищення
    if any(k in msg_lower for k in ("оптиміз", "прискор", "очисти", "покращ", "гальму", "зависа")):
        return generate_fallback_advice(context)

    # 6. Стороння тема (політика, погода, вірші тощо) — ввічлива відмова з прикладом допомоги
    return (
        "[Автономний режим]\n\n"
        "Я можу допомогти лише з аналізом ресурсів, процесів, характеристик та оптимізацією продуктивності цього комп'ютера. "
        "Наприклад, запитайте мене про навантаження процесора (CPU), використання оперативної пам'яті (RAM) або стан вільного місця на дисках."
    )
