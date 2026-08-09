"""OneBot 消息、规则与群任务失败通知共享件。"""

from .group_failure_notify import (
    GroupTaskFailureNotification,
    GroupTaskFailureNotifier,
    InMemoryFailureNotificationCooldown,
    RedisFailureNotificationCooldown,
)

__all__ = [
    "GroupTaskFailureNotification",
    "GroupTaskFailureNotifier",
    "InMemoryFailureNotificationCooldown",
    "RedisFailureNotificationCooldown",
]
