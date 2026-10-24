"""判定配置退役契约（TSK-190）：场景/总结字段与两个管理分区不再存在。

替换原 ``test_summary_classification_config.py``。0025 退役 38 个判定配置
字段与 ``chat_scene`` / ``summary_classification`` 两个管理分区后，这里守住
「删除面」与「保留面」两侧：

- 删除面：38 个字段与两个分区号不出现在 Schema 与分区元数据中；
- 保留面：13 个保留字段、Jev 校验、``startup_required=False``、别名解析与
  新初始化默认启用（plugin_enable=true）逐项保持。

期望的删除集合是显式测试数据（``tests.db.tsk190_legacy_config_support``
的冻结清单），不来自被测 Schema 或迁移源文本。
"""

from __future__ import annotations

from typing import Any, cast

import pytest
from pydantic import ValidationError

from komari_bot.config.typed_config import (
    ensure_typed_config_model,
    get_config_section_metadata,
)
from tests.db.tsk190_legacy_config_support import (
    RETAINED_DECISION_CONFIG_FIELDS,
    RETIRED_DECISION_CONFIG_FIELDS,
)

STORAGE_FIELDS = frozenset({"id", "revision", "updated_at"})
RETIRED_SECTION_IDS = ("chat_scene", "summary_classification")
SURVIVING_SECTIONS = [
    ("jev_request", "Jev 请求", 30),
    ("jev_execution", "Jev 执行", 40),
]
JEV_REQUEST_FIELDS = (
    "jev_base_url",
    "jev_api_key",
    "jev_model",
    "jev_http_timeout_seconds",
    "jev_request_timeout_seconds",
)
JEV_EXECUTION_FIELDS = (
    "max_concurrent_decisions",
    "max_queued_messages_per_group",
    "queue_timeout_seconds",
    "reference_timeout_seconds",
    "optional_reply_probability",
    "decision_log_retention_days",
)


def _decision_schema() -> type[Any]:
    schema = ensure_typed_config_model("komari_decision")
    assert schema is not None
    return cast("type[Any]", schema)


def test_decision_config_declares_only_surviving_sections() -> None:
    """管理分区只剩 Jev 请求与 Jev 执行，淘汰分区不再暴露。"""
    metadata = get_config_section_metadata(_decision_schema())

    assert [
        (section.section_id, section.display_name, section.order)
        for section in metadata.sections
    ] == SURVIVING_SECTIONS

    declared = {section.section_id for section in metadata.sections}
    for section_id in RETIRED_SECTION_IDS:
        assert section_id not in declared

    for field_name in JEV_REQUEST_FIELDS:
        assert metadata.field_section_ids[field_name] == "jev_request"
    for field_name in JEV_EXECUTION_FIELDS:
        assert metadata.field_section_ids[field_name] == "jev_execution"
    # 保留但未归入任何新分区的字段不携带失效分区号
    assert metadata.field_section_ids["plugin_enable"] is None
    assert metadata.field_section_ids["bot_aliases"] is None


def test_retired_decision_fields_are_gone_from_schema() -> None:
    """38 个退役字段不再出现在模型字段集合中。"""
    fields = set(_decision_schema().model_fields)

    assert set(RETIRED_DECISION_CONFIG_FIELDS) & fields == set()
    assert fields == STORAGE_FIELDS | set(RETAINED_DECISION_CONFIG_FIELDS)
    assert len(RETIRED_DECISION_CONFIG_FIELDS) == 38


def test_retained_fields_keep_their_values_and_validators() -> None:
    """保留字段仍可构造，Jev 校验器与别名解析保持原有严格性。"""
    config = _decision_schema()()

    assert config.jev_model == "jev-1.14.0"
    assert config.jev_base_url == ""
    assert config.jev_api_key == ""
    assert config.jev_http_timeout_seconds == 3.0
    assert config.jev_request_timeout_seconds == 3.0
    assert config.max_concurrent_decisions == 4
    assert config.max_queued_messages_per_group == 5
    assert config.queue_timeout_seconds == 3.0
    assert config.reference_timeout_seconds == 3.0
    assert config.optional_reply_probability == 0.5
    assert config.decision_log_retention_days == 7

    # 校验器仍在配置写入边界生效（删除字段不得顺带带走 Jev 校验）
    for rejected in (
        {"jev_base_url": "ftp://example.invalid"},
        {"jev_base_url": "https://user:pw@example.invalid"},
        {"jev_api_key": "has space"},
        {"jev_model": "   "},
        {"jev_http_timeout_seconds": 0},
        {"jev_request_timeout_seconds": float("inf")},
        {"queue_timeout_seconds": 0},
        {"reference_timeout_seconds": -1.0},
    ):
        with pytest.raises(ValidationError):
            _decision_schema()(**rejected)


def test_new_configuration_is_enabled_with_accurate_descriptions() -> None:
    """新初始化默认启用，且保留字段说明描述新判定行为。"""
    schema = _decision_schema()
    config = schema()

    assert schema.model_fields["plugin_enable"].default is True
    assert config.plugin_enable is True
    # 判定配置缺失只降级主动回复判定，不阻塞进程启动
    assert schema.startup_required is False  # type: ignore[attr-defined]

    enable_description = str(schema.model_fields["plugin_enable"].description)
    assert "Jev" in enable_description
    assert "未配置" in enable_description
    assert "仅响应显式 @" not in enable_description

    alias_description = str(schema.model_fields["bot_aliases"].description)
    assert "长度例外" in alias_description
    assert "确定性" in alias_description
    # 别名仍是可运维的输入事实，默认值不变
    assert config.bot_aliases == ["小鞠", "小鞠知花", "komari"]
    assert schema(bot_aliases="小鞠,komari").bot_aliases == ["小鞠", "komari"]
