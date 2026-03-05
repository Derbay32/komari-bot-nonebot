"""兼容层：社交时机评分逻辑已迁移至 komari_decision。"""

from komari_bot.plugins.komari_decision.services.social_timing_service import (
    SocialTimingService,
    TimingScoreBreakdown,
)

__all__ = ["SocialTimingService", "TimingScoreBreakdown"]
