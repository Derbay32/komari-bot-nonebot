"""群聊总结组合配置约束测试。"""

import pytest
from pydantic import ValidationError

from komari_bot.plugins.group_history_summary.config_schema import (
    DynamicConfigSchema,
    LayoutParamsSchema,
)


def test_summary_counts_must_form_ordered_range() -> None:
    with pytest.raises(ValidationError, match="min_summary_count <= default"):
        DynamicConfigSchema(
            min_summary_count=100,
            summary_default_count=50,
            max_summary_count=200,
        )

    with pytest.raises(ValidationError, match="不能小于 max_summary_count"):
        DynamicConfigSchema(
            min_summary_count=10,
            summary_default_count=50,
            max_summary_count=400,
            summary_tool_scan_limit=300,
        )


def test_layout_rejects_text_regions_outside_canvas() -> None:
    with pytest.raises(ValidationError, match="正文横向区域超出画布"):
        LayoutParamsSchema(
            canvas_width=600,
            body_x=500,
            body_max_width=200,
        )

    with pytest.raises(ValidationError, match="正文起点与字号组合超出画布"):
        LayoutParamsSchema(
            canvas_height=300,
            body_y=290,
            body_size=30,
        )


def test_config_schema_request_protocol_defaults() -> None:
    config = DynamicConfigSchema()

    assert config.summary_planning_request_api == "chat_completions"
    assert config.summary_planning_stream_enabled is False
    assert config.summary_request_api == "chat_completions"
    assert config.summary_stream_enabled is False


def test_config_schema_accepts_responses_request_api() -> None:
    config = DynamicConfigSchema(
        summary_planning_request_api="responses",
        summary_planning_stream_enabled=True,
        summary_request_api="responses",
        summary_stream_enabled=True,
    )

    assert config.summary_planning_request_api == "responses"
    assert config.summary_planning_stream_enabled is True
    assert config.summary_request_api == "responses"
    assert config.summary_stream_enabled is True


def test_config_schema_rejects_invalid_request_api() -> None:
    with pytest.raises(ValidationError):
        DynamicConfigSchema(summary_planning_request_api="websocket")  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        DynamicConfigSchema(summary_request_api="websocket")  # type: ignore[arg-type]
