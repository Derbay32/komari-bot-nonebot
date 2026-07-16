"""群总结图片资源路径测试。"""

from __future__ import annotations

import base64
from typing import TYPE_CHECKING

from komari_bot.plugins.group_history_summary.image_renderer import (
    CHARACTER_IMAGE_PATH,
    FONT_DIR,
    render_summary_image_base64,
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
