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
