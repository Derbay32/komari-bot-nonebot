# ruff: noqa: RUF001
"""TSK-278 RED baseline: help is provided by komari_help metadata.usage.

TSK-266 10.1: roulette help lives in the existing ``komari_help`` scanner via
``PluginMetadata.usage``; there is no ``/轮盘 帮助`` command.  Two seams are
RED (missing ``__plugin_meta__`` and ``parse_command``); the scanner
mechanism itself already works and is asserted green here.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast

from komari_bot.plugins.komari_help.scanner import scan_and_sync

if TYPE_CHECKING:
    import pytest

    from komari_bot.plugins.komari_help.engine import HelpEngine

# 已确认的四份帮助文案的关键片段（TSK-266 10.1 / 6a9bf92b）。
ROULETTE_USAGE_MARKERS = (
    "俄罗斯轮盘",
    "2～6 人参与",
    "@Bot /轮盘 开局",
    "俄罗斯轮盘 · 等候帮助",
    "俄罗斯轮盘 · 行动帮助",
    "俄罗斯轮盘 · 奖励选择帮助",
    "道具字母固定为",
    "A＝放大镜",
    "奖励 替换",
    "D<玩家编号>",
    "转让 <玩家编号>",
)


def test_roulette_plugin_declares_help_in_metadata_usage() -> None:
    # RED: 生产插件必须暴露 __plugin_meta__（usage 收录四份已确认文案）。
    from komari_bot.plugins.komari_roulette import __plugin_meta__

    assert __plugin_meta__ is not None
    usage = str(getattr(__plugin_meta__, "usage", "") or "")
    assert usage, "komari_roulette 必须通过 PluginMetadata.usage 提供帮助"
    for marker in ROULETTE_USAGE_MARKERS:
        assert marker in usage, f"usage 缺少已确认文案片段: {marker!r}"


def test_parser_rejects_qq_help_command() -> None:
    # RED: /轮盘 帮助 已删除；按未知命令解析，绝不成为轮盘命令入口。
    from komari_bot.plugins.komari_roulette.qq.parser import parse_command

    command = parse_command("/轮盘 帮助")
    assert command is not None
    assert command.intent == "syntax_failure"
    assert command.syntax_code == "unknown_command"


class _FakeScanLeaseEngine:
    index_rebuild_count: int
    scan_lease_owner: str | None = None

    async def acquire_scan_lease(
        self,
        owner_token: str,
        *,
        lease_seconds: int,
    ) -> bool:
        assert owner_token
        assert lease_seconds > 0
        self.scan_lease_owner = owner_token
        return True

    async def renew_scan_lease(
        self,
        owner_token: str,
        *,
        lease_seconds: int,
    ) -> bool:
        assert owner_token == self.scan_lease_owner
        assert lease_seconds > 0
        return True

    async def release_scan_lease(self, owner_token: str) -> None:
        assert owner_token == self.scan_lease_owner
        self.scan_lease_owner = None

    async def rebuild_keyword_index(self) -> None:
        self.index_rebuild_count += 1


def test_existing_scanner_collects_metadata_usage_as_one_entry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """现有 komari_help 扫描器可从 metadata.usage 收录轮盘为单篇帮助。"""
    usage = "\n\n".join(ROULETTE_USAGE_MARKERS)
    plugins = [
        SimpleNamespace(
            name="komari_roulette",
            metadata=SimpleNamespace(
                name="俄罗斯轮盘",
                description="QQ 群俄罗斯轮盘小游戏",
                usage=usage,
            ),
        )
    ]

    class _FakeEngine(_FakeScanLeaseEngine):
        def __init__(self) -> None:
            self.sync_calls: list[dict[str, Any]] = []
            self.index_rebuild_count = 0
            self.delete_calls: list[tuple[set[str], bool]] = []

        async def delete_auto_generated_help_by_plugins(
            self,
            plugin_names: set[str],
            *,
            rebuild_index: bool = True,
        ) -> int:
            self.delete_calls.append((plugin_names, rebuild_index))
            return 0

        async def sync_auto_generated_help(self, **kwargs: Any) -> bool:
            self.sync_calls.append(kwargs)
            return True

        async def _build_keyword_index(self) -> None:
            self.index_rebuild_count += 1

    monkeypatch.setattr(
        "komari_bot.plugins.komari_help.scanner.get_loaded_plugins",
        lambda: plugins,
    )
    engine = _FakeEngine()

    updated = asyncio.run(scan_and_sync(cast("HelpEngine", engine)))

    assert updated == 1
    assert len(engine.sync_calls) == 1
    call = engine.sync_calls[0]
    assert call["plugin_name"] == "komari_roulette"
    assert call["category"] == "command"
    content = str(call["content"])
    assert "俄罗斯轮盘 · 等候帮助" in content
    assert "奖励 替换" in content
    assert "D<玩家编号>" in content
    assert "俄罗斯轮盘" in call["keywords"]
