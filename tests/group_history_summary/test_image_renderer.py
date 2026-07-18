"""群总结图片资源路径测试。"""

from __future__ import annotations

import base64
from typing import TYPE_CHECKING

from komari_bot.plugins.group_history_summary.image_renderer import (
    CHARACTER_IMAGE_PATH,
    FONT_DIR,
    MAX_IMAGE_PAGES,
    render_summary_image_base64,
    render_summary_image_pages_base64,
)

if TYPE_CHECKING:
    from pathlib import Path

    import pytest


def test_summary_assets_are_independent_of_working_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)

    assert CHARACTER_IMAGE_PATH.is_absolute()
    assert CHARACTER_IMAGE_PATH.is_file()
    assert FONT_DIR.is_absolute()
    assert any(FONT_DIR.iterdir())

    image_bytes = base64.b64decode(
        render_summary_image_base64("群聊总结", "", ["测试内容"])
    )
    assert image_bytes.startswith(b"\x89PNG\r\n\x1a\n")


def test_long_summary_is_paginated_without_silent_crop() -> None:
    result = render_summary_image_pages_base64(
        "群聊总结",
        "最近消息 20 条",
        [f"第 {index} 行内容" for index in range(30)],
        {
            "canvas_width": 500,
            "canvas_height": 180,
            "title_x": 10,
            "title_y": 5,
            "title_size": 18,
            "body_x": 10,
            "body_y": 65,
            "body_size": 18,
            "body_line_gap": 0,
            "body_max_width": 450,
            "char_enabled": False,
        },
    )

    assert len(result.images_base64) > 1
    assert result.truncated is False
    assert result.rendered_line_count == result.total_line_count
    assert all(
        base64.b64decode(page).startswith(b"\x89PNG\r\n\x1a\n")
        for page in result.images_base64
    )


def test_extreme_summary_reports_hard_page_limit_truncation() -> None:
    result = render_summary_image_pages_base64(
        "群聊总结",
        "极长内容",
        [f"第 {index} 行" for index in range(1_000)],
        {
            "canvas_width": 500,
            "canvas_height": 180,
            "title_x": 10,
            "title_y": 5,
            "title_size": 18,
            "body_x": 10,
            "body_y": 65,
            "body_size": 18,
            "body_line_gap": 0,
            "body_max_width": 450,
            "char_enabled": False,
        },
    )

    assert len(result.images_base64) == MAX_IMAGE_PAGES
    assert result.truncated is True
    assert result.rendered_line_count < result.total_line_count
