"""群总结图片资源与固定字体测试。"""

from __future__ import annotations

import base64
import os
from typing import TYPE_CHECKING, Any

import pytest
from PIL import ImageFont

from komari_bot.plugins.group_history_summary import image_renderer
from komari_bot.plugins.group_history_summary.image_renderer import (
    CHARACTER_IMAGE_PATH,
    FONT_PATH,
    MAX_IMAGE_PAGES,
    render_summary_image_base64,
    render_summary_image_pages_base64,
)

if TYPE_CHECKING:
    from pathlib import Path

    from komari_bot.plugins.group_history_summary.image_renderer import FontLike


@pytest.fixture
def builtin_font_double(monkeypatch: pytest.MonkeyPatch) -> None:
    """分页/构图测试显式使用测试自带字体替身，不读取仓库字体。"""

    def _load_font(size: int) -> FontLike:
        return ImageFont.load_default(size=size)

    monkeypatch.setattr(image_renderer, "_load_font", _load_font)


@pytest.fixture
def builtin_font_bytes(monkeypatch: pytest.MonkeyPatch) -> bytes:
    """捕获 Pillow 测试内置字体字节，作为测试自有字体源。"""
    captured: list[bytes] = []
    original_truetype = ImageFont.truetype

    def _capture(font: Any, size: float = 10, **_kwargs: Any) -> Any:
        if hasattr(font, "read"):
            captured.append(font.read())
            font.seek(0)
        return original_truetype(font, size=size, **_kwargs)

    with monkeypatch.context() as context:
        context.setattr(ImageFont, "truetype", _capture)
        ImageFont.load_default(size=20)

    assert len(captured) == 1
    return captured[0]


@pytest.mark.usefixtures("builtin_font_double")
def test_summary_assets_are_independent_of_working_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)

    assert CHARACTER_IMAGE_PATH.is_absolute()
    assert CHARACTER_IMAGE_PATH.is_file()
    assert FONT_PATH.is_absolute()
    assert FONT_PATH.name == "LXGWWenKaiGBScreen.ttf"

    image_bytes = base64.b64decode(
        render_summary_image_base64("群聊总结", "", ["测试内容"])
    )
    assert image_bytes.startswith(b"\x89PNG\r\n\x1a\n")


def test_renderer_opens_exact_fixed_path_and_closes_handle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixed_path = tmp_path / "fixed" / "LXGWWenKaiGBScreen.ttf"
    fixed_path.parent.mkdir(parents=True)
    fixed_path.write_bytes(b"test-owned fixed font bytes")
    default_font = ImageFont.load_default(size=23)
    observed: dict[str, Any] = {}

    def _truetype(font: Any, size: int, **_kwargs: Any) -> Any:
        observed["name"] = font.name
        observed["bytes"] = font.read()
        observed["closed_during_call"] = font.closed
        observed["handle"] = font
        observed["size"] = size
        return default_font

    monkeypatch.setattr(image_renderer, "FONT_PATH", fixed_path)
    monkeypatch.setattr(image_renderer.ImageFont, "truetype", _truetype)

    loaded = image_renderer._load_font(23)

    assert loaded is default_font
    assert observed["name"] == str(fixed_path)
    assert observed["bytes"] == b"test-owned fixed font bytes"
    assert observed["closed_during_call"] is False
    assert observed["handle"].closed is True
    assert observed["size"] == 23
    assert not hasattr(image_renderer, "FONT_DIR")
    assert not hasattr(image_renderer, "FONT_EXTENSIONS")


def test_renderer_renders_from_fixed_file_with_test_owned_font(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    builtin_font_bytes: bytes,
) -> None:
    fixed_path = tmp_path / "fixed" / "LXGWWenKaiGBScreen.ttf"
    fixed_path.parent.mkdir(parents=True)
    fixed_path.write_bytes(builtin_font_bytes)
    monkeypatch.setattr(image_renderer, "FONT_PATH", fixed_path)

    image_bytes = base64.b64decode(
        render_summary_image_base64(
            "群聊总结",
            "",
            ["测试内容"],
            {"char_enabled": False},
        )
    )

    assert image_bytes.startswith(b"\x89PNG\r\n\x1a\n")


def test_renderer_propagates_missing_exact_fixed_name_without_system_scan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    missing_path = tmp_path / "fonts" / "LXGWWenKaiGBScreen.ttf"
    monkeypatch.setattr(image_renderer, "FONT_PATH", missing_path)

    def _unexpected_default(*_args: object, **_kwargs: object) -> Any:
        raise AssertionError

    def _unexpected_walk(*_args: object, **_kwargs: object) -> Any:
        raise AssertionError

    monkeypatch.setattr(image_renderer.ImageFont, "load_default", _unexpected_default)
    monkeypatch.setattr(os, "walk", _unexpected_walk)

    with pytest.raises(FileNotFoundError):
        render_summary_image_base64(
            "群聊总结",
            "",
            ["测试内容"],
            {"char_enabled": False},
        )

    assert not missing_path.exists()


def test_renderer_propagates_corrupt_exact_fixed_name_without_system_scan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    font_dir = tmp_path / "fonts"
    font_dir.mkdir()
    corrupt_path = font_dir / "LXGWWenKaiGBScreen.ttf"
    corrupt_path.write_bytes(b"not a font")
    other_path = font_dir / "AnotherFont.ttf"
    other_path.write_bytes(b"also not a font")
    monkeypatch.setattr(image_renderer, "FONT_PATH", corrupt_path)

    def _unexpected_default(*_args: object, **_kwargs: object) -> Any:
        raise AssertionError

    def _unexpected_walk(*_args: object, **_kwargs: object) -> Any:
        raise AssertionError

    monkeypatch.setattr(image_renderer.ImageFont, "load_default", _unexpected_default)
    monkeypatch.setattr(os, "walk", _unexpected_walk)

    with pytest.raises(OSError):
        image_renderer._load_font(23)


@pytest.mark.usefixtures("builtin_font_double")
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


@pytest.mark.usefixtures("builtin_font_double")
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
