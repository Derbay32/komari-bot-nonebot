"""总结结果图片渲染。"""

from __future__ import annotations

import base64
import io
from pathlib import Path
from typing import Any

from nonebot import logger
from PIL import Image, ImageDraw, ImageFont

FontLike = ImageFont.FreeTypeFont | ImageFont.ImageFont

CHARACTER_IMAGE_PATH = Path("data") / "image-summary.png"
FONT_DIR = Path("data") / "fonts"
FONT_EXTENSIONS = (".ttf", ".otf", ".ttc")
DEFAULT_LAYOUT_PARAMS: dict[str, Any] = {
    "canvas_width": 1365,
    "canvas_height": 645,
    "bg_color": "#444444",
    "title_x": 110,
    "title_y": 80,
    "title_size": 64,
    "title_color": "#FFFFFF",
    "body_x": 112,
    "body_y": 185,
    "body_size": 30,
    "body_color": "#F3F3F3",
    "body_line_gap": 10,
    "body_max_width": 750,
    "char_enabled": True,
    "char_scale": 0.3,
    "char_max_height_ratio": 0.82,
    "char_x_offset": -10,
    "char_y_offset": 0,
}


def _load_font(size: int) -> FontLike:
    """统一从 data/fonts 加载自定义字体。"""
    if FONT_DIR.exists() and FONT_DIR.is_dir():
        for font_path in sorted(FONT_DIR.iterdir()):
            if font_path.suffix.lower() not in FONT_EXTENSIONS:
                continue
            try:
                return ImageFont.truetype(str(font_path), size=size)
            except OSError:
                continue

    logger.warning(
        "[GroupHistorySummary] 未在 {} 找到可用自定义字体，降级为默认字体",
        FONT_DIR,
    )
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
    scale_ratio: float,
    max_height_ratio: float,
) -> Image.Image:
    max_width = int(canvas_width * scale_ratio)
    max_height = int(canvas_height * max_height_ratio)

    src_w, src_h = image.size
    if src_w <= 0 or src_h <= 0:
        return image

    scale = min(max_width / src_w, max_height / src_h)
    dst_w = max(1, int(src_w * scale))
    dst_h = max(1, int(src_h * scale))
    return image.resize((dst_w, dst_h), Image.Resampling.LANCZOS)


def _merge_layout_params(layout_params: dict[str, Any] | None) -> dict[str, Any]:
    merged = dict(DEFAULT_LAYOUT_PARAMS)
    if not layout_params:
        return merged
    for key in DEFAULT_LAYOUT_PARAMS:
        if key in layout_params:
            merged[key] = layout_params[key]
    return merged


def render_summary_image_base64(
    title: str,
    subtitle: str,
    body_lines: list[str],
    layout_params: dict[str, Any] | None = None,
) -> str:
    """渲染总结图片，返回 base64 字符串。"""
    # 先保留参数，后续如果需要可用于在卡片上显示时间范围等辅助信息。
    _ = subtitle
    params = _merge_layout_params(layout_params)
    canvas_width = int(params["canvas_width"])
    canvas_height = int(params["canvas_height"])
    image = Image.new("RGB", (canvas_width, canvas_height), color=str(params["bg_color"]))
    draw = ImageDraw.Draw(image)

    # 字体
    title_font = _load_font(int(params["title_size"]))
    body_font = _load_font(int(params["body_size"]))

    title_x = int(params["title_x"])
    title_y = int(params["title_y"])
    body_x = int(params["body_x"])
    body_y = int(params["body_y"])
    text_max_width = int(params["body_max_width"])
    line_gap = int(params["body_line_gap"])

    body_line_height = _line_height(draw, body_font)

    wrapped_body: list[str] = []
    for line in body_lines:
        wrapped_body.extend(_wrap_text(draw, line, body_font, text_max_width))
    if not wrapped_body:
        wrapped_body = ["本次没有可总结的文本内容。"]

    max_body_lines = max(
        1,
        (canvas_height - body_y - int(canvas_height * 0.06))
        // (body_line_height + line_gap),
    )
    if len(wrapped_body) > max_body_lines:
        wrapped_body = wrapped_body[:max_body_lines]
        last = wrapped_body[-1]
        wrapped_body[-1] = f"{last[:-1]}…" if len(last) > 1 else "…"

    draw.text((title_x, title_y), title, fill=str(params["title_color"]), font=title_font)

    y = body_y
    for line in wrapped_body:
        draw.text((body_x, y), line, fill=str(params["body_color"]), font=body_font)
        y += body_line_height + line_gap

    # 右侧角色图（可选）
    if bool(params["char_enabled"]):
        character_image = _load_character_image()
        if character_image is not None:
            character_image = _resize_character_image(
                character_image,
                canvas_width,
                canvas_height,
                scale_ratio=float(params["char_scale"]),
                max_height_ratio=float(params["char_max_height_ratio"]),
            )
            dst_x = canvas_width - character_image.width + int(params["char_x_offset"])
            dst_y = canvas_height - character_image.height + int(params["char_y_offset"])
            image.paste(character_image, (dst_x, dst_y), character_image)

    output = io.BytesIO()
    image.save(output, format="PNG")
    return base64.b64encode(output.getvalue()).decode("ascii")
