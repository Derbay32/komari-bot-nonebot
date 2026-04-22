"""Komari Help 命令展示逻辑测试。"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import TYPE_CHECKING

from komari_bot.plugins.komari_help import rendering as rendering_module
from komari_bot.plugins.komari_help.models import HelpEntry, HelpSearchResult

if TYPE_CHECKING:
    import pytest


def _build_config(**overrides: object) -> SimpleNamespace:
    values: dict[str, object] = {
        "show_category_emoji": True,
        "default_result_limit": 5,
        "max_reply_result_count": 2,
        "max_content_preview_length": 100,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _build_result(
    title: str, content: str, plugin_name: str = "sr"
) -> HelpSearchResult:
    return HelpSearchResult(
        id=1,
        category="command",
        plugin_name=plugin_name,
        title=title,
        content=content,
        similarity=0.95,
        source="keyword",
    )


def _build_entry(title: str, plugin_name: str = "sr") -> HelpEntry:
    timestamp = datetime(2026, 4, 22, 22, 30, tzinfo=UTC)
    return HelpEntry(
        id=1,
        category="command",
        plugin_name=plugin_name,
        keywords=["帮助"],
        title=title,
        content="示例内容",
        notes=None,
        is_auto_generated=False,
        created_at=timestamp,
        updated_at=timestamp,
    )


def test_format_results_preserves_multiline_usage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        rendering_module,
        "get_config",
        lambda: _build_config(max_reply_result_count=3),
    )

    rendered = rendering_module.format_results(
        [
            _build_result(
                "sr",
                "核心指令：\n.sr\n.sr 随机从神人榜内抽取一个\n.sr add 向神人榜内添加神人",
            )
        ]
    )

    assert "⌨️ sr" in rendered
    assert "⌨️ sr (sr)" not in rendered
    assert "  核心指令：" in rendered
    assert "  .sr" in rendered
    assert "  .sr 随机从神人榜内抽取一个" in rendered
    assert "  .sr add 向神人榜内添加神人" in rendered
    assert "核心指令： .sr" not in rendered


def test_format_results_limits_reply_count(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rendering_module, "get_config", lambda: _build_config())

    rendered = rendering_module.format_results(
        [
            _build_result("指令 1", "内容 1", "plugin_1"),
            _build_result("指令 2", "内容 2", "plugin_2"),
            _build_result("指令 3", "内容 3", "plugin_3"),
        ]
    )

    assert "指令 1" in rendered
    assert "指令 2" in rendered
    assert "(plugin_1)" not in rendered
    assert "(plugin_2)" not in rendered
    assert "指令 3" not in rendered
    assert "……其余 1 条结果已省略" in rendered


def test_get_search_result_limit_uses_reply_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        rendering_module,
        "get_config",
        lambda: _build_config(default_result_limit=5, max_reply_result_count=2),
    )

    assert rendering_module.get_search_result_limit() == 2


def test_format_list_page_shows_page_navigation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(rendering_module, "get_config", lambda: _build_config())

    rendered = rendering_module.format_list_page(
        [_build_entry("指令 1", "plugin_1")],
        21,
        2,
    )

    assert "📚 当前帮助条目共 21 条（第 2/3 页）" in rendered
    assert "⌨️ 指令 1" in rendered
    assert "(plugin_1)" not in rendered
    assert "查看下一页请使用 .docs list 3" in rendered
