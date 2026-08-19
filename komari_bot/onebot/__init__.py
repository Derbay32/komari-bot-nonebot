"""OneBot 消息、规则与群任务失败通知共享件。"""

from .group_failure_notify import (
    GroupTaskFailureNotification,
    GroupTaskFailureNotifier,
    ImageFailureDiagnostic,
    InMemoryFailureNotificationCooldown,
    RedisFailureNotificationCooldown,
    image_failure_reason_code,
)

__all__ = [
    "GroupTaskFailureNotification",
    "GroupTaskFailureNotifier",
    "ImageFailureDiagnostic",
    "InMemoryFailureNotificationCooldown",
    "RedisFailureNotificationCooldown",
    "image_failure_reason_code",
]
