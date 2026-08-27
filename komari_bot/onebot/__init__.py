"""OneBot 消息、规则与群任务失败通知共享件。"""

from .group_failure_notify import (
    GroupTaskFailureNotification,
    GroupTaskFailureNotifier,
    ImageFailureDiagnostic,
    InMemoryFailureNotificationCooldown,
    RedisFailureNotificationCooldown,
    image_failure_reason_code,
)
from .onebot_messages import get_user_nickname

__all__ = [
    "GroupTaskFailureNotification",
    "GroupTaskFailureNotifier",
    "ImageFailureDiagnostic",
    "InMemoryFailureNotificationCooldown",
    "RedisFailureNotificationCooldown",
    "get_user_nickname",
    "image_failure_reason_code",
]
