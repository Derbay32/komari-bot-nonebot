"""Komari Decision 运行时状态模型。"""

from dataclasses import dataclass
from enum import StrEnum


class DecisionRuntimeStatus(StrEnum):
    """判定运行时的三态状态。"""

    READY = "ready"
    DISABLED = "disabled"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class DecisionRuntimeState:
    """判定运行时状态及可观测原因。"""

    status: DecisionRuntimeStatus
    reason: str

    @property
    def is_ready(self) -> bool:
        """当前是否可以执行主动回复判定。"""
        return self.status is DecisionRuntimeStatus.READY

    @classmethod
    def ready(cls, reason: str = "scene runtime 已就绪") -> "DecisionRuntimeState":
        return cls(status=DecisionRuntimeStatus.READY, reason=reason)

    @classmethod
    def disabled(cls, reason: str) -> "DecisionRuntimeState":
        return cls(status=DecisionRuntimeStatus.DISABLED, reason=reason)

    @classmethod
    def failed(cls, reason: str) -> "DecisionRuntimeState":
        return cls(status=DecisionRuntimeStatus.FAILED, reason=reason)


__all__ = ["DecisionRuntimeState", "DecisionRuntimeStatus"]
