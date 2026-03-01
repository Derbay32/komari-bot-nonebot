"""总结结果图片渲染。"""

from __future__ import annotations

import base64
import io
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

FontLike = ImageFont.FreeTypeFont | ImageFont.ImageFont

CARD_BACKGROUND = "#1A1C21"
TEXT_COLOR = "#F3F3F3"
TITLE_COLOR = "#FFFFFF"
CHARACTER_IMAGE_PATH = Path("data") / "image-summary.png"


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
    for paragraph in text.splitlines() or [text]:
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


def _load_character_image() -> Image.Image | None:
    if not CHARACTER_IMAGE_PATH.exists():
        return None
    try:
        return Image.open(CHARACTER_IMAGE_PATH).convert("RGBA")
    except OSError:
        return None


def _resize_character_image(
    image: Image.Image,
    canvas_width: int,
    canvas_height: int,
) -> Image.Image:
    # 右侧预留区域，宽度约占 30%
    max_width = int(canvas_width * 0.30)
    max_height = int(canvas_height * 0.82)

    src_w, src_h = image.size
    if src_w <= 0 or src_h <= 0:
        return image

    scale = min(max_width / src_w, max_height / src_h)
    dst_w = max(1, int(src_w * scale))
    dst_h = max(1, int(src_h * scale))
    return image.resize((dst_w, dst_h), Image.Resampling.LANCZOS)


def render_summary_image_base64(
    title: str,
    subtitle: str,
    body_lines: list[str],
    width: int,
    font_size: int,
) -> str:
    """渲染总结图片，返回 base64 字符串。"""
    # 先保留参数，后续如果需要可用于在卡片上显示时间范围等辅助信息。
    _ = subtitle

    image_height = max(540, int(width * 0.47))
    image = Image.new("RGB", (width, image_height), color=CARD_BACKGROUND)
    draw = ImageDraw.Draw(image)

    # 字体
    title_font = _load_font(font_size + 14)
    body_font = _load_font(font_size + 2)

    # 布局参数
    left_margin = int(width * 0.08)
    top_margin = int(image_height * 0.12)
    right_reserved = int(width * 0.34)
    text_max_width = width - left_margin - right_reserved
    line_gap = 8

    # 固定样式：标题 + 正文
    title_height = _line_height(draw, title_font)
    body_line_height = _line_height(draw, body_font)
    body_start_y = top_margin + title_height + 48

    wrapped_body: list[str] = []
    for line in body_lines:
        wrapped_body.extend(_wrap_text(draw, line, body_font, text_max_width))
    if not wrapped_body:
        wrapped_body = ["本次没有可总结的文本内容。"]

    max_body_lines = max(
        1,
        (image_height - body_start_y - int(image_height * 0.10))
        // (body_line_height + line_gap),
    )
    if len(wrapped_body) > max_body_lines:
        wrapped_body = wrapped_body[:max_body_lines]
        last = wrapped_body[-1]
        wrapped_body[-1] = f"{last[:-1]}…" if len(last) > 1 else "…"

    draw.text((left_margin, top_margin), title, fill=TITLE_COLOR, font=title_font)

    y = body_start_y
    for line in wrapped_body:
        draw.text((left_margin, y), line, fill=TEXT_COLOR, font=body_font)
        y += body_line_height + line_gap

    # 右侧角色图（可选）
    character_image = _load_character_image()
    if character_image is not None:
        character_image = _resize_character_image(character_image, width, image_height)
        dst_x = width - character_image.width - int(width * 0.02)
        dst_y = image_height - character_image.height
        image.paste(character_image, (dst_x, dst_y), character_image)

    output = io.BytesIO()
    image.save(output, format="PNG")
    return base64.b64encode(output.getvalue()).decode("ascii")
