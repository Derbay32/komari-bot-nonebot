"""兼容层：消息过滤逻辑已迁移至 komari_decision。"""

from komari_bot.plugins.komari_decision.services.message_filter import (
    FilterResult,
    preprocess_message,
)

__all__ = ["FilterResult", "preprocess_message"]
