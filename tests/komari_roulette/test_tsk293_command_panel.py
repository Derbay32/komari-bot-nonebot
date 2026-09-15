"""Validate the static menu artifact against supported command entry points.

These checks do not emulate the QQ client or prove its slash/@ insertion.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from komari_bot.plugins.komari_roulette.qq.parser import parse_command

_MANIFEST = Path(__file__).resolve().parents[2] / "resources/qq/command-panel.json"
_ROULETTE_COMMANDS = [
    ("轮盘 开局", "create"),
    ("轮盘 加入", "join"),
    ("轮盘 退出", "leave"),
    ("轮盘 取消", "cancel"),
    ("轮盘 开始", "start"),
    ("轮盘 开枪", "shoot"),
    ("轮盘 弃权", "forfeit"),
    ("轮盘 结束", "end_turn"),
    ("轮盘 装填", "reload"),
    ("轮盘 道具", "open_item_panel"),
    ("轮盘 排行榜", "leaderboard"),
]


def _items() -> list[dict[str, object]]:
    return json.loads(_MANIFEST.read_text(encoding="utf-8"))["items"]


def test_catalog_uses_canonical_names_without_dynamic_arguments() -> None:
    names = [item["name"] for item in _items()]
    expected = {"bind", "bind rename", "bind unbind"} | {
        name for name, _intent in _ROULETTE_COMMANDS
    }
    assert len(names) == len(expected)
    assert set(names) == expected


def test_catalog_is_public_fill_only_and_has_no_scope_or_credentials() -> None:
    manifest = json.loads(_MANIFEST.read_text(encoding="utf-8"))
    assert set(manifest) == {"items", "remark"}
    assert 0 < len(manifest["items"]) <= 20
    for item in manifest["items"]:
        assert item["type"] == "command"
        assert item["only_admin"] is False
        assert "link" not in item
        assert isinstance(item["name"], str)
        assert 0 < len(item["name"]) <= 14
        assert isinstance(item["desc"], str)
        assert 0 < len(item["desc"]) <= 30


@pytest.mark.parametrize(("name", "intent"), _ROULETTE_COMMANDS)
def test_menu_text_maps_to_the_real_roulette_parser(name: str, intent: str) -> None:
    assert name in [item["name"] for item in _items()]
    command = parse_command(f"/{name}")
    assert command is not None
    assert command.intent == intent
