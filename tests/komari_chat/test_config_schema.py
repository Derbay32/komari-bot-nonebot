"""KomariChat 强类型配置 Schema 测试（KOMARIBOT-7 验收基线）。

主动回复频控与 outbox 的 10 个活配置字段从 komari_memory_config 迁入
komari_chat 自有强类型配置表 komari_chat_config；死字段
proactive_score_threshold 随批删除，不迁移。
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from komari_bot.plugins.komari_chat.config_schema import KomariChatConfigSchema

KOMARI_CHAT_PLUGIN_DIR = (
    Path(__file__).resolve().parents[2] / "komari_bot" / "plugins" / "komari_chat"
)
KOMARI_MEMORY_CONFIG_INTERFACE = (
    "komari_bot.plugins.komari_memory.services.config_interface"
)

EXPECTED_FIELD_DEFAULTS: dict[str, object] = {
    "proactive_enabled": False,
    "proactive_cooldown": 300,
    "proactive_max_per_hour": 400,
    "proactive_reservation_ttl_seconds": 360,
    "reply_commit_worker_interval_seconds": 5,
    "reply_commit_batch_size": 20,
    "reply_commit_lease_seconds": 120,
    "reply_commit_max_attempts": 20,
    "reply_commit_retry_base_seconds": 5,
    "reply_commit_tombstone_retention_days": 30,
}

EXPECTED_FIELD_BOUNDS: dict[str, tuple[int, int]] = {
    "proactive_cooldown": (5, 3600),
    "proactive_max_per_hour": (1, 800),
    "proactive_reservation_ttl_seconds": (30, 900),
    "reply_commit_worker_interval_seconds": (1, 300),
    "reply_commit_batch_size": (1, 200),
    "reply_commit_lease_seconds": (30, 900),
    "reply_commit_max_attempts": (1, 100),
    "reply_commit_retry_base_seconds": (1, 300),
    "reply_commit_tombstone_retention_days": (1, 365),
}


def test_config_schema_declares_typed_table_metadata() -> None:
    """komari_chat 配置表挂在 typed config 注册表约定的命名下。"""
    assert KomariChatConfigSchema.plugin_name == "komari_chat"
    assert KomariChatConfigSchema.__tablename__ == "komari_chat_config"


def test_config_schema_declares_immediate_default_apply_mode() -> None:
    """频控/outbox 字段全部即时生效，模型级默认 apply_mode 为 immediate。"""
    assert KomariChatConfigSchema.model_config.get("json_schema_extra") == {
        "default_apply_mode": "immediate"
    }
    for field in KomariChatConfigSchema.model_fields.values():
        extra = field.json_schema_extra
        if isinstance(extra, dict) and "apply_mode" in extra:
            assert extra["apply_mode"] == "immediate"


def test_migrated_fields_expose_expected_defaults() -> None:
    """10 个迁入字段的默认值与原 komari_memory_config 完全一致。"""
    config = KomariChatConfigSchema()

    assert set(EXPECTED_FIELD_DEFAULTS) <= set(KomariChatConfigSchema.model_fields)
    for field_name, expected_default in EXPECTED_FIELD_DEFAULTS.items():
        assert getattr(config, field_name) == expected_default, field_name


def test_migrated_fields_enforce_bounds() -> None:
    """范围校验原样保留：越界值必须被拒绝。"""
    for field_name, (lower, upper) in EXPECTED_FIELD_BOUNDS.items():
        with pytest.raises(ValueError):
            KomariChatConfigSchema(**{field_name: lower - 1})
        with pytest.raises(ValueError):
            KomariChatConfigSchema(**{field_name: upper + 1})


def test_config_schema_drops_dead_proactive_score_threshold() -> None:
    """死字段 proactive_score_threshold 不迁移。"""
    assert "proactive_score_threshold" not in KomariChatConfigSchema.model_fields


def test_komari_chat_provides_own_config_interface() -> None:
    """komari_chat 拥有自己的 config_interface，挂接 komari_chat 配置资源。"""
    interface_path = KOMARI_CHAT_PLUGIN_DIR / "services" / "config_interface.py"

    assert interface_path.is_file()
    source = interface_path.read_text(encoding="utf-8")
    assert '"komari_chat"' in source
    assert "get_config_manager" in source


def test_komari_chat_does_not_import_komari_memory_config_interface() -> None:
    """ADR-0006：komari_chat 不得 import komari_memory 内部 config_interface。"""
    offenders: list[str] = []
    for path in sorted(KOMARI_CHAT_PLUGIN_DIR.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.ImportFrom)
                and node.module == KOMARI_MEMORY_CONFIG_INTERFACE
            ):
                offenders.append(f"{path.name}:{node.lineno}")
            elif isinstance(node, ast.Import):
                offenders.extend(
                    f"{path.name}:{node.lineno}"
                    for alias in node.names
                    if alias.name == KOMARI_MEMORY_CONFIG_INTERFACE
                )

    assert offenders == []
