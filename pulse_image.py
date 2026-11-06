"""Картинка «Пульс дня» на белом фоне (PNG)."""

import io
import os

from PIL import Image, ImageDraw, ImageFont

FONT_DIRS = [
    "/usr/share/fonts/truetype/dejavu",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "fonts"),
]

GREEN, RED, GREY, DARK, GOLD, STRIPE = "#12a150", "#d93a3a", "#8a8f98", "#1b1f27", "#e8a23a", "#f5f6f8"


def _font(bold, size):
    name = "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"
    for d in FONT_DIRS:
        path = os.path.join(d, name)
        if os.path.exists(path):
            return ImageFont.truetype(path, size)
    return ImageFont.load_default(size=size)


def _plain_title(title):
    """«🇺🇸 США» → «США»: эмодзи шрифт не рисует."""
    parts = title.split(" ", 1)
    if len(parts) == 2 and not parts[0].isalpha():
        return parts[1]
    return title


def render(groups, date_str, footer="Bull Run Yerevan · @bullrunyerevan"):
    """groups: [(название группы, [(название, цена-строкой, изменение в %), ...]), ...]"""
    W, pad, row_h, head_h, group_h = 1080, 60, 58, 170, 70
    H = head_h + sum(group_h + row_h * len(rows) for _, rows in groups) + 120

    img = Image.new("RGB", (W, H), "#ffffff")
    d = ImageDraw.Draw(img)
    f_title, f_sub = _font(True, 52), _font(False, 28)
    f_group, f_row, f_rowb = _font(True, 30), _font(False, 34), _font(True, 34)

    d.rectangle([0, 0, W, 12], fill=GOLD)
    d.text((pad, 50), "Пульс дня", font=f_title, fill=DARK)
    d.text((pad, 115), f"{date_str} · изменение к закрытию прошлого дня", font=f_sub, fill=GREY)

    y = head_h
    for title, rows in groups:
        d.text((pad, y + 22), _plain_title(title).upper(), font=f_group, fill=GOLD)
        y += group_h
        for i, (name, price, ch) in enumerate(rows):
            if i % 2 == 0:
                d.rounded_rectangle([pad - 20, y - 6, W - pad + 20, y + row_h - 10], 12, fill=STRIPE)
            d.text((pad, y + 4), name, font=f_row, fill=DARK)
            pw = d.textlength(price, font=f_rowb)
            d.text((W - pad - 260 - pw, y + 4), price, font=f_rowb, fill=DARK)
            if ch > 0.005:
                color, arrow = GREEN, "▲"
            elif ch < -0.005:
                color, arrow = RED, "▼"
            else:
                color, arrow = GREY, "•"
            pct = f"{arrow} {abs(ch):.2f}%".replace(".", ",")
            cw = d.textlength(pct, font=f_rowb)
            d.text((W - pad - cw, y + 4), pct, font=f_rowb, fill=color)
            y += row_h

    d.line([pad, H - 90, W - pad, H - 90], fill="#e6e8ec", width=2)
    d.text((pad, H - 70), footer, font=f_sub, fill=GREY)

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()
