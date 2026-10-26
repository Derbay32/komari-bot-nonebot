"""TSK-206 总结任务数据与冻结预算。"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from komari_bot.agent import AgentTaskBudget
from komari_bot.plugins.group_history_summary.summary_task_data import (
    SummaryLimits,
    SummaryTaskBudget,
    SummaryTaskConfigError,
    SummaryTaskData,
    freeze_summary_task_config,
)


def _config(**overrides: Any) -> SimpleNamespace:
    values: dict[str, Any] = {
        "summary_default_count": 50,
        "min_summary_count": 10,
        "max_summary_count": 200,
        "summary_tool_scan_limit": 300,
        "summary_planning_round_limit": 3,
        "fetch_batch_size": 50,
        "history_min_coverage_ratio": 0.8,
        "redis_db": 0,
        "summary_lock_ttl_seconds": 300,
        "layout_params": SimpleNamespace(
            model_dump=lambda: {"canvas_width": 1365, "body_size": 30}
        ),
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_limits_and_task_data_use_the_closed_summary_shape() -> None:
    limits = SummaryLimits(
        default_count=30,
        min_count=10,
        max_count=200,
        scan_limit=300,
    )
    data = SummaryTaskData(
        trace_id="group-summary-abc",
        user_request="总结最近消息",
        limits=limits,
    )

    assert data.to_dict() == {
        "schema": 1,
        "request": {
            "source_type": "group_history",
            "source_id": "group-summary-abc",
            "trust_level": "untrusted",
            "content": "总结最近消息",
        },
        "limits": {
            "default_count": 30,
            "min_count": 10,
            "max_count": 200,
            "scan_limit": 300,
        },
    }


def test_budget_maps_round_limit_plus_the_replaced_final_call() -> None:
    budget = SummaryTaskBudget.from_round_limit(3)

    assert budget.rounds == 4
    assert budget.per_round == 1
    assert budget.total == 4
    assert budget.to_agent_budget() == AgentTaskBudget(
        max_rounds=4,
        max_tool_calls_per_round=1,
        max_total_tool_calls=4,
    )


def test_frozen_config_uses_requested_count_and_freezes_layout() -> None:
    config = _config()
    frozen = freeze_summary_task_config(config, requested_count=30)

    assert frozen.limits.default_count == 30
    assert frozen.budget.rounds == 4
    assert frozen.fetch_batch_size == 50
    assert frozen.history_min_coverage_ratio == 0.8
    assert frozen.layout_params == {"canvas_width": 1365, "body_size": 30}
    assert frozen.redis_db == 0
    assert frozen.lock_ttl_seconds == 300

    config.summary_default_count = 90
    config.min_summary_count = 90
    config.max_summary_count = 90
    assert frozen.limits.default_count == 30


def test_frozen_config_rejects_incoherent_limits() -> None:
    with pytest.raises(SummaryTaskConfigError):
        freeze_summary_task_config(
            _config(min_summary_count=50, max_summary_count=20),
            requested_count=None,
        )

    with pytest.raises(SummaryTaskConfigError):
        freeze_summary_task_config(
            _config(summary_tool_scan_limit=100, max_summary_count=200),
            requested_count=None,
        )

    with pytest.raises(SummaryTaskConfigError):
        SummaryTaskBudget.from_round_limit(0)

    with pytest.raises(SummaryTaskConfigError):
        SummaryTaskBudget.from_round_limit(1000)

    with pytest.raises(SummaryTaskConfigError):
        freeze_summary_task_config(
            _config(summary_planning_round_limit=True),
            requested_count=None,
        )


def test_requested_count_outside_limits_is_clamped_for_task_data() -> None:
    frozen = freeze_summary_task_config(_config(), requested_count=5)
    assert frozen.limits.default_count == 10

    frozen = freeze_summary_task_config(_config(), requested_count=500)
    assert frozen.limits.default_count == 200
