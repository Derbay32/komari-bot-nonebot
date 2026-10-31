"""KomariChat 强类型配置 Schema 测试（KOMARIBOT-7 验收基线）。

主动回复频控与 outbox 的 10 个活配置字段从 komari_memory_config 迁入
komari_chat 自有强类型配置表 komari_chat_config；回复履约另有独立的回复
时效字段。死字段 proactive_score_threshold 随批删除，不迁移。
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from pydantic import ValidationError

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
    "reply_fulfillment_worker_interval_seconds": 5,
    "reply_fulfillment_batch_size": 20,
    "reply_fulfillment_lease_seconds": 120,
    "reply_fulfillment_max_attempts": 20,
    "reply_fulfillment_retry_base_seconds": 5,
    "reply_fulfillment_retry_max_seconds": 3600,
    "reply_fulfillment_tombstone_retention_days": 30,
    "reply_fulfillment_freshness_seconds": 120,
}

EXPECTED_FIELD_BOUNDS: dict[str, tuple[int, int]] = {
    "proactive_cooldown": (5, 3600),
    "proactive_max_per_hour": (1, 800),
    "proactive_reservation_ttl_seconds": (30, 900),
    "reply_fulfillment_worker_interval_seconds": (1, 300),
    "reply_fulfillment_batch_size": (1, 200),
    "reply_fulfillment_lease_seconds": (30, 900),
    "reply_fulfillment_max_attempts": (1, 100),
    "reply_fulfillment_retry_base_seconds": (1, 300),
    "reply_fulfillment_retry_max_seconds": (1, 86_400),
    "reply_fulfillment_tombstone_retention_days": (1, 365),
    "reply_fulfillment_freshness_seconds": (30, 300),
}

# TSK-192：回复 Agent 执行预算字段（AC1/AC2）。
EXPECTED_AGENT_BUDGET_DEFAULTS: dict[str, object] = {
    "agent_max_rounds": 10,
    "agent_max_tool_calls_per_round": 4,
    "agent_max_total_tool_calls": 20,
}

EXPECTED_AGENT_BUDGET_BOUNDS: dict[str, tuple[int, int]] = {
    "agent_max_rounds": (2, 20),
    "agent_max_tool_calls_per_round": (1, 8),
    "agent_max_total_tool_calls": (2, 64),
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


def test_agent_budget_fields_expose_expected_defaults() -> None:
    """AC1：三项预算默认值为 10 / 4 / 20。"""
    config = KomariChatConfigSchema()

    assert set(EXPECTED_AGENT_BUDGET_DEFAULTS) <= set(
        KomariChatConfigSchema.model_fields
    )
    for field_name, expected_default in EXPECTED_AGENT_BUDGET_DEFAULTS.items():
        assert getattr(config, field_name) == expected_default, field_name


def test_agent_budget_fields_enforce_bounds() -> None:
    """AC1：轮次 2-20、单轮 1-8、总量 2-64，越界必须被拒绝。"""
    for field_name, (lower, upper) in EXPECTED_AGENT_BUDGET_BOUNDS.items():
        with pytest.raises(ValidationError):
            KomariChatConfigSchema(**{field_name: lower - 1})
        with pytest.raises(ValidationError):
            KomariChatConfigSchema(**{field_name: upper + 1})


@pytest.mark.parametrize(
    "overrides",
    [
        # total == per_round（下界相等边界合法）
        {"agent_max_tool_calls_per_round": 4, "agent_max_total_tool_calls": 4},
        # total == rounds * per_round（上界相等边界合法）
        {
            "agent_max_rounds": 2,
            "agent_max_tool_calls_per_round": 4,
            "agent_max_total_tool_calls": 8,
        },
        # 下界合法组合：total == per_round == 2，rounds=2 -> 2 <= 2 <= 4
        {
            "agent_max_rounds": 2,
            "agent_max_tool_calls_per_round": 2,
            "agent_max_total_tool_calls": 2,
        },
        # 上界合法组合：total == 20*8 == 64
        {
            "agent_max_rounds": 20,
            "agent_max_tool_calls_per_round": 8,
            "agent_max_total_tool_calls": 64,
        },
    ],
)
def test_agent_budget_cross_field_boundary_combinations_accepted(
    overrides: dict[str, int],
) -> None:
    """AC2：单轮预算 <= 总预算 <= 轮次 x 单轮预算的相等边界组合可接受。"""
    assert KomariChatConfigSchema(**overrides) is not None


@pytest.mark.parametrize(
    "overrides",
    [
        # 单轮预算 > 总预算
        {"agent_max_tool_calls_per_round": 5, "agent_max_total_tool_calls": 4},
        # 总预算 > 轮次 x 单轮预算
        {
            "agent_max_rounds": 2,
            "agent_max_tool_calls_per_round": 4,
            "agent_max_total_tool_calls": 9,
        },
        # 同时越界与跨字段非法：total 161 > 20*8
        {
            "agent_max_rounds": 20,
            "agent_max_tool_calls_per_round": 8,
            "agent_max_total_tool_calls": 161,
        },
        # 单字段下界 + 跨字段：total=1 低于 2
        {
            "agent_max_rounds": 2,
            "agent_max_tool_calls_per_round": 1,
            "agent_max_total_tool_calls": 1,
        },
    ],
)
def test_agent_budget_cross_field_invalid_combinations_rejected(
    overrides: dict[str, int],
) -> None:
    """AC2：Pydantic 拒绝跨字段非法组合。"""
    with pytest.raises(ValidationError):
        KomariChatConfigSchema(**overrides)


# ── TSK-193：工具调用约束模式字段（AC1） ────────────────────────────────

EXPECTED_AGENT_TOOL_CALL_MODE_DEFAULT = "required"
EXPECTED_AGENT_TOOL_CALL_MODE_CHOICES = ("required", "prompt_guided")










# ── TSK-194：图片理解模式与下载预算字段（AC1/AC2） ───────────────────

EXPECTED_IMAGE_UNDERSTANDING_MODE_DEFAULT = "delegated"
EXPECTED_IMAGE_UNDERSTANDING_MODE_CHOICES = ("native", "delegated")

EXPECTED_IMAGE_BUDGET_DEFAULTS: dict[str, object] = {
    "vision_image_download_max_count": 4,
    "vision_image_download_max_bytes": 8 * 1024 * 1024,
    "vision_image_download_total_max_bytes": 20 * 1024 * 1024,
    "vision_image_download_max_pixels": 40_000_000,
    "vision_image_download_concurrency": 2,
    "vision_image_download_connect_timeout_seconds": 5.0,
    "vision_image_download_read_timeout_seconds": 30.0,
    "vision_image_download_total_timeout_seconds": 45.0,
}

EXPECTED_IMAGE_BUDGET_BOUNDS: dict[str, tuple[int | float, int | float]] = {
    "vision_image_download_max_count": (1, 8),
    "vision_image_download_max_bytes": (64 * 1024, 16 * 1024 * 1024),
    "vision_image_download_total_max_bytes": (64 * 1024, 32 * 1024 * 1024),
    "vision_image_download_max_pixels": (1_000_000, 100_000_000),
    "vision_image_download_concurrency": (1, 4),
    "vision_image_download_connect_timeout_seconds": (0.5, 15.0),
    "vision_image_download_read_timeout_seconds": (1.0, 60.0),
    "vision_image_download_total_timeout_seconds": (5.0, 90.0),
}










def test_image_budget_fields_expose_expected_defaults() -> None:
    """AC2：8 项图片下载预算默认值与原 komari_memory_config 完全一致。"""
    config = KomariChatConfigSchema()

    assert set(EXPECTED_IMAGE_BUDGET_DEFAULTS) <= set(
        KomariChatConfigSchema.model_fields
    )
    for field_name, expected_default in EXPECTED_IMAGE_BUDGET_DEFAULTS.items():
        assert getattr(config, field_name) == expected_default, field_name


def test_image_budget_fields_enforce_bounds() -> None:
    """AC2：单字段范围校验原样保留，越界值必须被拒绝。"""
    for field_name, (lower, upper) in EXPECTED_IMAGE_BUDGET_BOUNDS.items():
        with pytest.raises(ValidationError):
            KomariChatConfigSchema(**{field_name: lower - 1})
        with pytest.raises(ValidationError):
            KomariChatConfigSchema(**{field_name: upper + 1})


def test_image_budget_apply_mode_is_immediate() -> None:
    """AC2：8 项预算字段全部即时生效。"""
    for field_name in EXPECTED_IMAGE_BUDGET_DEFAULTS:
        field = KomariChatConfigSchema.model_fields[field_name]
        extra = field.json_schema_extra
        assert isinstance(extra, dict), field_name
        assert extra["apply_mode"] == "immediate", field_name


@pytest.mark.parametrize(
    "overrides",
    [
        # 相等边界合法：total == max_bytes
        {
            "vision_image_download_max_bytes": 2 * 1024 * 1024,
            "vision_image_download_total_max_bytes": 2 * 1024 * 1024,
        },
        # 相等边界合法：total_timeout == connect_timeout
        {
            "vision_image_download_connect_timeout_seconds": 10.0,
            "vision_image_download_total_timeout_seconds": 10.0,
        },
    ],
)
def test_image_budget_cross_field_boundary_combinations_accepted(
    overrides: dict[str, object],
) -> None:
    """AC2：跨字段相等边界组合可接受。"""
    assert KomariChatConfigSchema(**overrides) is not None


@pytest.mark.parametrize(
    "overrides",
    [
        # 总字节上限 < 单图字节上限
        {
            "vision_image_download_max_bytes": 2 * 1024 * 1024,
            "vision_image_download_total_max_bytes": 1024 * 1024,
        },
        # 总时限 < 连接超时
        {
            "vision_image_download_connect_timeout_seconds": 10.0,
            "vision_image_download_total_timeout_seconds": 5.0,
        },
    ],
)
def test_image_budget_cross_field_invalid_combinations_rejected(
    overrides: dict[str, object],
) -> None:
    """AC2：Pydantic 拒绝跨字段非法组合。"""
    with pytest.raises(ValidationError):
        KomariChatConfigSchema(**overrides)


def test_config_schema_drops_dead_proactive_score_threshold() -> None:
    """死字段 proactive_score_threshold 不迁移。"""
    assert "proactive_score_threshold" not in KomariChatConfigSchema.model_fields


def test_config_schema_drops_legacy_reply_commit_language() -> None:
    """contract 后不保留旧字段别名或运行时兼容入口。"""
    assert not {
        "reply_commit_worker_interval_seconds",
        "reply_commit_batch_size",
        "reply_commit_lease_seconds",
        "reply_commit_max_attempts",
        "reply_commit_retry_base_seconds",
        "reply_commit_tombstone_retention_days",
    }.intersection(KomariChatConfigSchema.model_fields)


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


# ── TSK-192 phase B：chat/context configuration ownership ───────────────

TSK192_CHAT_OWNED_FIELDS = {
    "knowledge_enabled": True,
    "knowledge_limit": 3,
    "context_messages_limit": 10,
    "context_max_utf8_bytes": 24_000,
    "context_max_estimated_tokens": 6_000,
    "face_reaction_enabled": False,
    "face_reaction_id": "76",
}


def test_phase_b_fields_have_one_chat_owner_with_original_defaults() -> None:
    config = KomariChatConfigSchema()

    assert config.chat_enabled is False
    for field_name, expected in TSK192_CHAT_OWNED_FIELDS.items():
        assert field_name in KomariChatConfigSchema.model_fields, field_name
        assert getattr(config, field_name) == expected, field_name


def test_retired_memory_search_limit_is_absent_from_live_chat_schema() -> None:
    """0031 retires the dead chat setting from schema and serialization."""
    config = KomariChatConfigSchema()

    assert "memory_search_limit" not in KomariChatConfigSchema.model_fields
    assert "memory_search_limit" not in config.model_dump()
    assert not hasattr(config, "memory_search_limit")


def test_phase_b_fields_preserve_validation_and_apply_metadata() -> None:
    # ADR-0018：群聊消息处理开关改为业务命名并即时生效。
    chat_field = KomariChatConfigSchema.model_fields["chat_enabled"]
    assert chat_field.json_schema_extra == {"apply_mode": "immediate"}

    bounds = {
        "knowledge_limit": (0, 11),
        "context_messages_limit": (4, 51),
        "context_max_utf8_bytes": (1023, 131_073),
        "context_max_estimated_tokens": (255, 32_001),
    }
    for field_name, (lower, upper) in bounds.items():
        with pytest.raises(ValidationError):
            KomariChatConfigSchema(**{field_name: lower})
        with pytest.raises(ValidationError):
            KomariChatConfigSchema(**{field_name: upper})



def test_chat_config_interface_has_no_memory_config_dependency() -> None:
    interface_path = KOMARI_CHAT_PLUGIN_DIR / "services" / "config_interface.py"
    source = interface_path.read_text(encoding="utf-8")
    assert "get_memory_config" not in source
    assert "KomariMemoryConfigSchema" not in source
