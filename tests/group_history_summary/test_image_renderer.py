"""群总结图片资源与固定字体测试。"""

from __future__ import annotations

import base64
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


def test_renderer_uses_only_the_fixed_font_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixed_path = tmp_path / "fixed" / "LXGWWenKaiGBScreen.ttf"
    calls: list[tuple[str, int]] = []
    default_font = ImageFont.load_default(size=23)

    def _truetype(font: str, size: int, **_kwargs: Any) -> Any:
        calls.append((font, size))
        return default_font

    monkeypatch.setattr(image_renderer, "FONT_PATH", fixed_path)
    monkeypatch.setattr(image_renderer.ImageFont, "truetype", _truetype)

    loaded = image_renderer._load_font(23)

    assert isinstance(loaded, ImageFont.FreeTypeFont)
    assert calls == [(str(fixed_path), 23)]
    assert not hasattr(image_renderer, "FONT_DIR")
    assert not hasattr(image_renderer, "FONT_EXTENSIONS")


def test_renderer_propagates_missing_fixed_font_without_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Use a basename absent from host font directories so Pillow cannot mask
    # the missing fixed path through its own platform lookup.
    missing_path = tmp_path / "fonts" / "tsk209-missing-fixture-font.ttf"
    monkeypatch.setattr(image_renderer, "FONT_PATH", missing_path)

    def _unexpected_default(*_args: object, **_kwargs: object) -> Any:
        raise AssertionError

    monkeypatch.setattr(image_renderer.ImageFont, "load_default", _unexpected_default)

    with pytest.raises(OSError):
        render_summary_image_base64(
            "群聊总结",
            "",
            ["测试内容"],
            {"char_enabled": False},
        )

    assert not missing_path.exists()


def test_renderer_propagates_corrupt_fixed_font_without_scanning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    font_dir = tmp_path / "fonts"
    font_dir.mkdir()
    # Same uniqueness constraint as the missing-path case above: a corrupt
    # target must not be replaced by a same-named host font.
    corrupt_path = font_dir / "tsk209-corrupt-fixture-font.ttf"
    corrupt_path.write_bytes(b"not a font")
    other_path = font_dir / "tsk209-other-fixture-font.ttf"
    other_path.write_bytes(b"also not a font")
    monkeypatch.setattr(image_renderer, "FONT_PATH", corrupt_path)

    def _unexpected_default(*_args: object, **_kwargs: object) -> Any:
        raise AssertionError

    monkeypatch.setattr(image_renderer.ImageFont, "load_default", _unexpected_default)

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
