"""社交时机评分契约：时机分解结果。"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class TimingScoreBreakdown:
    """时机评分分解结果。"""

    timing_score: float
    mention_bonus: float
    silence_bonus: float
    activity_penalty: float
    dialogue_penalty: float
    cooldown_penalty: float
    activity_count: int
    unique_users: int
    silence_gap_seconds: float
    bot_gap_seconds: float | None


__all__ = ["TimingScoreBreakdown"]
