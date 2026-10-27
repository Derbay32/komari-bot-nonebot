"""TSK-206 总结任务数据、冻结预算与配置快照。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, cast

from komari_bot.agent import AgentTaskBudget

MAX_SUMMARY_BUDGET_VALUE = 1000
MIN_SUMMARY_ROUND_LIMIT = 1
MAX_SUMMARY_ROUND_LIMIT = MAX_SUMMARY_BUDGET_VALUE - 1


class SummaryTaskConfigError(RuntimeError):
    """总结任务数据或预算无法从配置冻结。"""

    code = "invalid_summary_task_config"


def _int_value(value: object, *, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise SummaryTaskConfigError
    if value < minimum or value > maximum:
        raise SummaryTaskConfigError
    return value


@dataclass(frozen=True, slots=True)
class SummaryLimits:
    """Frozen history-count and scan limits for one summary task."""

    default_count: int
    min_count: int
    max_count: int
    scan_limit: int

    def __post_init__(self) -> None:
        min_count = _int_value(self.min_count, minimum=1, maximum=MAX_SUMMARY_BUDGET_VALUE)
        max_count = _int_value(self.max_count, minimum=1, maximum=MAX_SUMMARY_BUDGET_VALUE)
        default_count = _int_value(
            self.default_count,
            minimum=1,
            maximum=MAX_SUMMARY_BUDGET_VALUE,
        )
        scan_limit = _int_value(
            self.scan_limit,
            minimum=1,
            maximum=MAX_SUMMARY_BUDGET_VALUE,
        )
        if min_count > max_count:
            raise SummaryTaskConfigError
        if not min_count <= default_count <= max_count:
            raise SummaryTaskConfigError
        if scan_limit < max_count:
            raise SummaryTaskConfigError

    def to_dict(self) -> dict[str, int]:
        return {
            "default_count": self.default_count,
            "min_count": self.min_count,
            "max_count": self.max_count,
            "scan_limit": self.scan_limit,
        }


@dataclass(frozen=True, slots=True)
class SummaryTaskBudget:
    """The shared DSH task budget mapped from the summary round setting."""

    rounds: int
    per_round: int
    total: int

    @classmethod
    def from_round_limit(cls, round_limit: object) -> SummaryTaskBudget:
        rounds = _int_value(
            round_limit,
            minimum=MIN_SUMMARY_ROUND_LIMIT,
            maximum=MAX_SUMMARY_ROUND_LIMIT,
        )
        return cls(rounds=rounds + 1, per_round=1, total=rounds + 1)

    def to_agent_budget(self) -> AgentTaskBudget:
        return AgentTaskBudget(
            max_rounds=self.rounds,
            max_tool_calls_per_round=self.per_round,
            max_total_tool_calls=self.total,
        )


@dataclass(frozen=True, slots=True)
class FrozenSummaryTaskConfig:
    """Every config primitive the summary task uses, copied before first await."""

    budget: SummaryTaskBudget
    limits: SummaryLimits
    fetch_batch_size: int
    history_min_coverage_ratio: float
    layout_params: dict[str, Any]
    redis_db: int
    lock_ttl_seconds: int


@dataclass(frozen=True, slots=True)
class SummaryTaskData:
    """Closed task-data envelope rendered by the DSH summary preset."""

    trace_id: str
    user_request: str
    limits: SummaryLimits

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": 1,
            "request": {
                "source_type": "group_history",
                "source_id": self.trace_id,
                "trust_level": "untrusted",
                "content": self.user_request,
            },
            "limits": self.limits.to_dict(),
        }


def _config_value(config: object, name: str) -> object:
    try:
        return getattr(config, name)
    except AttributeError:
        raise SummaryTaskConfigError from None


def _coverage_ratio(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SummaryTaskConfigError
    ratio = float(value)
    if not 0.0 <= ratio <= 1.0:
        raise SummaryTaskConfigError
    return ratio


def _layout_params(config: object) -> dict[str, Any]:
    layout = _config_value(config, "layout_params")
    try:
        dumped = cast("Any", layout).model_dump()
    except AttributeError:
        if isinstance(layout, Mapping):
            return dict(layout)
        raise SummaryTaskConfigError from None
    if not isinstance(dumped, Mapping):
        raise SummaryTaskConfigError
    return dict(dumped)


def freeze_summary_task_config(
    config: object,
    *,
    requested_count: int | None,
) -> FrozenSummaryTaskConfig:
    """Freeze once at task start; no later config mutation affects this task."""
    min_count = _int_value(
        _config_value(config, "min_summary_count"),
        minimum=1,
        maximum=MAX_SUMMARY_BUDGET_VALUE,
    )
    max_count = _int_value(
        _config_value(config, "max_summary_count"),
        minimum=1,
        maximum=MAX_SUMMARY_BUDGET_VALUE,
    )
    configured_default = _int_value(
        _config_value(config, "summary_default_count"),
        minimum=1,
        maximum=MAX_SUMMARY_BUDGET_VALUE,
    )
    if requested_count is not None:
        requested = _int_value(
            requested_count,
            minimum=1,
            maximum=MAX_SUMMARY_BUDGET_VALUE,
        )
        configured_default = requested
    default_count = max(min_count, min(max_count, configured_default))
    limits = SummaryLimits(
        default_count=default_count,
        min_count=min_count,
        max_count=max_count,
        scan_limit=_int_value(
            _config_value(config, "summary_tool_scan_limit"),
            minimum=1,
            maximum=MAX_SUMMARY_BUDGET_VALUE,
        ),
    )
    budget = SummaryTaskBudget.from_round_limit(
        _config_value(config, "summary_planning_round_limit")
    )
    fetch_batch_size = _int_value(
        _config_value(config, "fetch_batch_size"),
        minimum=1,
        maximum=200,
    )
    redis_db = _int_value(
        _config_value(config, "redis_db"),
        minimum=0,
        maximum=15,
    )
    lock_ttl_seconds = _int_value(
        _config_value(config, "summary_lock_ttl_seconds"),
        minimum=1,
        maximum=3600,
    )
    return FrozenSummaryTaskConfig(
        budget=budget,
        limits=limits,
        fetch_batch_size=fetch_batch_size,
        history_min_coverage_ratio=_coverage_ratio(
            _config_value(config, "history_min_coverage_ratio")
        ),
        layout_params=_layout_params(config),
        redis_db=redis_db,
        lock_ttl_seconds=lock_ttl_seconds,
    )


__all__ = [
    "MAX_SUMMARY_BUDGET_VALUE",
    "FrozenSummaryTaskConfig",
    "SummaryLimits",
    "SummaryTaskBudget",
    "SummaryTaskConfigError",
    "SummaryTaskData",
    "freeze_summary_task_config",
]
