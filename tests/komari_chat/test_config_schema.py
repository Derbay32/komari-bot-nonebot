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

    assert set(EXPECTED_AGENT_BUDGET_DEFAULTS) <= set(KomariChatConfigSchema.model_fields)
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


def test_agent_tool_call_mode_is_typed_enum_with_default_required() -> None:
    """AC1：agent_tool_call_mode 是强类型枚举，默认 required。"""
    assert "agent_tool_call_mode" in KomariChatConfigSchema.model_fields
    schema = KomariChatConfigSchema.model_json_schema()
    property_schema = schema["properties"]["agent_tool_call_mode"]
    assert property_schema.get("enum") == list(EXPECTED_AGENT_TOOL_CALL_MODE_CHOICES)
    assert (
        KomariChatConfigSchema.model_fields["agent_tool_call_mode"].default
        == EXPECTED_AGENT_TOOL_CALL_MODE_DEFAULT
    )


@pytest.mark.parametrize("mode", EXPECTED_AGENT_TOOL_CALL_MODE_CHOICES)
def test_agent_tool_call_mode_accepts_enum_values(mode: str) -> None:
    """AC1：两个枚举值都可配置。"""
    config = KomariChatConfigSchema(agent_tool_call_mode=mode)
    assert config.model_dump()["agent_tool_call_mode"] == mode


@pytest.mark.parametrize(
    "invalid",
    [
        "auto",
        "none",
        "None",
        "required ",
        "Required",
        "prompt_guided_extra",
        "",
    ],
)
def test_agent_tool_call_mode_rejects_invalid_values(invalid: str) -> None:
    """AC1：枚举之外的值必须被 Pydantic 拒绝。"""
    with pytest.raises(ValidationError):
        KomariChatConfigSchema(agent_tool_call_mode=invalid)


def test_agent_tool_call_mode_apply_mode_is_immediate() -> None:
    """TSK-193：工具约束模式按管理元数据即时生效（无重启/重建）。"""
    field = KomariChatConfigSchema.model_fields["agent_tool_call_mode"]
    extra = field.json_schema_extra
    if isinstance(extra, dict) and "apply_mode" in extra:
        assert extra["apply_mode"] == "immediate"
    # 模型级默认 apply_mode 也必须保持 immediate，字段未声明覆盖时同样即时生效
    assert KomariChatConfigSchema.model_config.get("json_schema_extra") == {
        "default_apply_mode": "immediate"
    }


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
