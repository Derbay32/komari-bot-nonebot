"""总结结果图片渲染。"""

from __future__ import annotations

import base64
import io
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

FontLike = ImageFont.FreeTypeFont | ImageFont.ImageFont


def _load_font(size: int) -> FontLike:
    font_candidates = [
        "/System/Library/Fonts/PingFang.ttc",
        "/System/Library/Fonts/Hiragino Sans GB.ttc",
        "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ]
    for font_path in font_candidates:
        if Path(font_path).exists():
            try:
                return ImageFont.truetype(font_path, size=size)
            except OSError:
                continue
    return ImageFont.load_default()


def _text_width(draw: ImageDraw.ImageDraw, text: str, font: FontLike) -> int:
    left, _top, right, _bottom = draw.textbbox((0, 0), text, font=font)
    return int(right - left)


def _line_height(draw: ImageDraw.ImageDraw, font: FontLike) -> int:
    _left, top, _right, bottom = draw.textbbox((0, 0), "测Ag", font=font)
    return int(bottom - top)


def _wrap_text(
    draw: ImageDraw.ImageDraw,
    text: str,
    font: FontLike,
    max_width: int,
) -> list[str]:
    if not text:
        return [""]

    wrapped: list[str] = []
    paragraphs = text.splitlines() or [text]

    for paragraph in paragraphs:
        if not paragraph:
            wrapped.append("")
            continue

        current = ""
        for char in paragraph:
            candidate = f"{current}{char}"
            if _text_width(draw, candidate, font) <= max_width:
                current = candidate
            else:
                if current:
                    wrapped.append(current)
                current = char

        if current:
            wrapped.append(current)

    return wrapped


def render_summary_image_base64(
    title: str,
    subtitle: str,
    body_lines: list[str],
    width: int,
    font_size: int,
) -> str:
    """渲染总结图片，返回 base64 字符串。"""
    title_font = _load_font(font_size + 12)
    subtitle_font = _load_font(max(16, font_size - 8))
    body_font = _load_font(font_size)

    margin = 64
    max_text_width = width - margin * 2

    measurement_image = Image.new("RGB", (width, 100), color="#FFFFFF")
    draw = ImageDraw.Draw(measurement_image)

    wrapped_body: list[str] = []
    for line in body_lines:
        wrapped_body.extend(_wrap_text(draw, line, body_font, max_text_width))

    title_height = _line_height(draw, title_font)
    subtitle_height = _line_height(draw, subtitle_font)
    body_height = _line_height(draw, body_font)

    section_spacing = 18
    content_height = max(1, len(wrapped_body)) * (body_height + 10)
    image_height = (
        margin * 2 + title_height + subtitle_height + content_height + section_spacing * 3
    )

    image = Image.new("RGB", (width, image_height), color="#F7F8FA")
    draw = ImageDraw.Draw(image)

    y = margin
    draw.text((margin, y), title, fill="#151515", font=title_font)
    y += title_height + section_spacing

    draw.text((margin, y), subtitle, fill="#444444", font=subtitle_font)
    y += subtitle_height + section_spacing * 2

    for line in wrapped_body:
        draw.text((margin, y), line, fill="#1F1F1F", font=body_font)
        y += body_height + 10

    output = io.BytesIO()
    image.save(output, format="PNG")
    return base64.b64encode(output.getvalue()).decode("ascii")
