"""社交时机评分服务（仅用于回复权重）。"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from .config_interface import get_config

if TYPE_CHECKING:
    from .redis_manager import RedisManager


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


class SocialTimingService:
    """根据群聊实时状态计算时机分。"""

    # 固定分项强度（可后续配置化）
    _MENTION_BONUS = 0.20
    _SILENCE_BONUS = 0.20
    _ACTIVITY_MAX_PENALTY = 0.25
    _DIALOGUE_PENALTY = 0.20
    _COOLDOWN_MAX_PENALTY = 0.25

    # 群活跃惩罚拐点
    _ACTIVITY_THRESHOLD = 5
    _ACTIVITY_SLOPE_DENOMINATOR = 10

    def __init__(self, redis: RedisManager) -> None:
        self.redis = redis

    @staticmethod
    def _clamp(value: float, min_value: float = 0.0, max_value: float = 1.0) -> float:
        return max(min_value, min(max_value, value))

    async def score(
        self,
        group_id: str,
        *,
        alias_hit: bool,
        now_ts: float | None = None,
    ) -> TimingScoreBreakdown:
        """计算时机分。

        Args:
            group_id: 群组 ID
            alias_hit: 是否命中机器人别名（用于 cue 加分）
            now_ts: 当前时间戳（可注入用于测试）
        """
        config = get_config()
        now = now_ts if now_ts is not None else time.time()

        # 从缓冲区读取最近消息，并按时间排序
        messages = await self.redis.get_buffer(
            group_id, limit=config.message_buffer_size
        )
        messages.sort(key=lambda x: x.timestamp)

        activity_window_start = now - config.social_window_activity_seconds
        dialogue_window_start = now - config.social_window_dialogue_seconds

        activity_messages = [m for m in messages if m.timestamp >= activity_window_start]
        dialogue_messages = [m for m in messages if m.timestamp >= dialogue_window_start]

        activity_count = len(activity_messages)
        unique_users = len({m.user_id for m in dialogue_messages if not m.is_bot})

        # 1) 被 cue 加分
        mention_bonus = self._MENTION_BONUS if alias_hit else 0.0

        # 2) 冷场加分
        if messages:
            silence_gap = max(0.0, now - messages[-1].timestamp)
        else:
            # 无历史消息视为冷场
            silence_gap = float(config.social_silence_seconds)
        silence_bonus = self._SILENCE_BONUS if silence_gap >= config.social_silence_seconds else 0.0

        # 3) 高活跃惩罚
        if activity_count > self._ACTIVITY_THRESHOLD:
            activity_over = activity_count - self._ACTIVITY_THRESHOLD
            activity_penalty = min(
                self._ACTIVITY_MAX_PENALTY,
                activity_over / self._ACTIVITY_SLOPE_DENOMINATOR,
            )
        else:
            activity_penalty = 0.0

        # 4) 两人对话惩罚（至少有 2 条消息时才施加）
        dialogue_penalty = (
            self._DIALOGUE_PENALTY
            if len(dialogue_messages) >= 2 and unique_users <= 2
            else 0.0
        )

        # 5) 机器人近期发言惩罚
        last_bot_ts: float | None = None
        for msg in reversed(messages):
            if msg.is_bot:
                last_bot_ts = msg.timestamp
                break

        if last_bot_ts is None:
            bot_gap = None
            cooldown_penalty = 0.0
        else:
            bot_gap = max(0.0, now - last_bot_ts)
            if bot_gap < config.social_bot_cooldown_seconds:
                ratio = (config.social_bot_cooldown_seconds - bot_gap) / max(
                    float(config.social_bot_cooldown_seconds), 1.0
                )
                cooldown_penalty = min(self._COOLDOWN_MAX_PENALTY, ratio * self._COOLDOWN_MAX_PENALTY)
            else:
                cooldown_penalty = 0.0

        raw_score = (
            mention_bonus
            + silence_bonus
            - activity_penalty
            - dialogue_penalty
            - cooldown_penalty
        )
        timing_score = self._clamp(raw_score)

        return TimingScoreBreakdown(
            timing_score=timing_score,
            mention_bonus=mention_bonus,
            silence_bonus=silence_bonus,
            activity_penalty=activity_penalty,
            dialogue_penalty=dialogue_penalty,
            cooldown_penalty=cooldown_penalty,
            activity_count=activity_count,
            unique_users=unique_users,
            silence_gap_seconds=silence_gap,
            bot_gap_seconds=bot_gap,
        )
