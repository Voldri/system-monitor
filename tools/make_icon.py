"""
Скрипт генерації фірмової іконки для System Monitor:
- icon.png (256x256 RGBA)
- icon.ico (мультиформатний файл: 16x16, 24x24, 32x32, 48x48, 64x64, 128x128, 256x256)

Дизайн: стилізований пульс/графік моніторингу телеметрії на заокругленому синьому
квадраті (темна палітра дашборду #0f172a - #1e3a8a - #3b82f6).
"""

import sys
from pathlib import Path
from typing import Optional

try:
    from PIL import Image, ImageDraw
except ImportError:
    Image = None
    ImageDraw = None

# Базові шляхи
PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PNG_PATH = PROJECT_ROOT / "app" / "static" / "assets" / "icon.png"
DEFAULT_ICO_PATH = PROJECT_ROOT / "app" / "static" / "assets" / "icon.ico"
ROOT_ICO_PATH = PROJECT_ROOT / "icon.ico"

ICO_SIZES = [(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]


def create_icon_image(size: int = 256) -> "Image.Image":
    """
    Створює зображення іконки за допомогою Pillow у векторно-подібному стилі:
    - Заокруглений прямокутник з темно-синім градієнтним фоном
    - Неонова лінія пульсу телеметрії кольору Cyan (#38bdf8) / Royal Blue (#4C8DFF)
    - Напівпрозора заливка під графіком та сяюча точка активності
    """
    if Image is None or ImageDraw is None:
        raise RuntimeError("Бібліотека Pillow не встановлена. Встановіть її через: pip install Pillow")

    scale = 4  # Малювання з 4-кратним суперсемплінгом для ідеального згладжування (Anti-Aliasing)
    canvas_size = size * scale
    img = Image.new("RGBA", (canvas_size, canvas_size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    margin = 12 * scale
    radius = 56 * scale

    # 1. Тінь під іконкою
    shadow_offset = 6 * scale
    draw.rounded_rectangle(
        [margin, margin + shadow_offset, canvas_size - margin, canvas_size - margin + shadow_offset],
        radius=radius,
        fill=(5, 10, 20, 140)
    )

    # 2. Основна плашка: темно-синій фон дашборду
    # Імітація вертикального градієнта через шари
    draw.rounded_rectangle(
        [margin, margin, canvas_size - margin, canvas_size - margin],
        radius=radius,
        fill=(15, 23, 42, 255)  # Slate-900
    )

    # Внутрішня градієнтна підсвітка (темно-синій перелив до #1e3a8a)
    inner_margin = margin + 4 * scale
    draw.rounded_rectangle(
        [inner_margin, inner_margin, canvas_size - inner_margin, canvas_size - inner_margin],
        radius=radius - 4 * scale,
        fill=(22, 36, 71, 255)
    )

    # Зовнішня тонка рамка акцентного синього кольору
    border_width = int(3.5 * scale)
    draw.rounded_rectangle(
        [margin, margin, canvas_size - margin, canvas_size - margin],
        radius=radius,
        outline=(59, 130, 246, 220),  # Blue-500
        width=border_width
    )

    # 3. Сітка монітора (делікатні тонкі горизонтальні лінії)
    grid_color = (37, 56, 95, 120)
    grid_width = int(1.5 * scale)
    for y_pos in [canvas_size * 0.35, canvas_size * 0.50, canvas_size * 0.65]:
        draw.line(
            [(margin + 16 * scale, y_pos), (canvas_size - margin - 16 * scale, y_pos)],
            fill=grid_color,
            width=grid_width
        )

    # 4. Точки хвилі пульсу (ECG / Telemetry Wave)
    # Координати приведені до масштабу 256x256 і домножені на scale
    base_points = [
        (28, 142),
        (64, 142),
        (82, 124),
        (96, 166),
        (116, 52),   # Головний пік навантаження
        (136, 206),  # Спад
        (154, 114),
        (170, 150),
        (186, 142),
        (228, 142),
    ]
    scaled_points = [(p[0] * scale, p[1] * scale) for p in base_points]

    # 4.1 Напівпрозора заливка площі під графіком (Area Chart Glow)
    fill_polygon = list(scaled_points)
    fill_polygon.append((scaled_points[-1][0], canvas_size - margin - 8 * scale))
    fill_polygon.append((scaled_points[0][0], canvas_size - margin - 8 * scale))
    draw.polygon(fill_polygon, fill=(56, 189, 248, 45))  # Cyan напівпрозорий

    # 4.2 М'яке світіння навколо лінії пульсу (Glow)
    glow_width = int(14 * scale)
    draw.line(scaled_points, fill=(56, 189, 248, 60), width=glow_width, joint="round")
    draw.line(scaled_points, fill=(76, 141, 255, 120), width=int(10 * scale), joint="round")

    # 4.3 Основна неонова лінія пульсу
    line_width = int(6.5 * scale)
    draw.line(scaled_points, fill=(56, 189, 248, 255), width=line_width, joint="round")

    # Внутрішня біла серцевина лінії для яскравості
    draw.line(scaled_points, fill=(240, 249, 255, 230), width=int(2.5 * scale), joint="round")

    # 4.4 Сяюча активна точка на піку (116, 52)
    peak_x, peak_y = 116 * scale, 52 * scale
    # Зовнішній ореол
    halo_r = 14 * scale
    draw.ellipse(
        [peak_x - halo_r, peak_y - halo_r, peak_x + halo_r, peak_y + halo_r],
        fill=(56, 189, 248, 90)
    )
    # Середнє коло
    mid_r = 7 * scale
    draw.ellipse(
        [peak_x - mid_r, peak_y - mid_r, peak_x + mid_r, peak_y + mid_r],
        fill=(56, 189, 248, 255)
    )
    # Білий центр
    core_r = 3.5 * scale
    draw.ellipse(
        [peak_x - core_r, peak_y - core_r, peak_x + core_r, peak_y + core_r],
        fill=(255, 255, 255, 255)
    )

    # 5. Зменшення розміру з високоякісною фільтрацією Lanczos (Super-sampling Anti-Aliasing)
    resample_filter = getattr(Image, "Resampling", Image).LANCZOS
    final_img = img.resize((size, size), resample=resample_filter)
    return final_img


def generate_icons(
    target_png: Optional[Path] = None,
    target_ico: Optional[Path] = None,
    target_root_ico: Optional[Path] = None
) -> None:
    """
    Генерує файли іконки icon.png та icon.ico.
    Створює необхідні підкаталоги автоматично.
    """
    png_path = Path(target_png or DEFAULT_PNG_PATH)
    ico_path = Path(target_ico or DEFAULT_ICO_PATH)
    root_ico = Path(target_root_ico or ROOT_ICO_PATH)

    # Створюємо директорії
    png_path.parent.mkdir(parents=True, exist_ok=True)
    ico_path.parent.mkdir(parents=True, exist_ok=True)
    root_ico.parent.mkdir(parents=True, exist_ok=True)

    img_256 = create_icon_image(256)

    # Зберігаємо PNG (256x256)
    img_256.save(png_path, format="PNG", optimize=True)

    # Зберігаємо ICO у двох локаціях (у static/assets та у корені проєкту)
    img_256.save(ico_path, format="ICO", sizes=ICO_SIZES)
    img_256.save(root_ico, format="ICO", sizes=ICO_SIZES)


if __name__ == "__main__":
    generate_icons()
    print(f"Іконки успішно згенеровано:")
    print(f" - PNG: {DEFAULT_PNG_PATH}")
    print(f" - ICO: {DEFAULT_ICO_PATH}")
    print(f" - Root ICO: {ROOT_ICO_PATH}")
