"""群总结请求场景归类纯结果契约（KOMARIBOT-23）。

本模块零运行时依赖，仅承载三态归类结果与稳定的不可用原因码：
调用方只能得到「命中 / 未命中 / 不可用（带稳定原因码）」三种结果，
结果中不携带场景键、分数、阈值、原始异常或自由文本。

延续 komari_bot/decision/ 共享桶体系，供任意消费方安全 import。
"""

from dataclasses import dataclass
from enum import StrEnum


class SummaryRequestClassificationStatus(StrEnum):
    """群总结请求归类结果的三态状态。"""

    MATCHED = "matched"
    NOT_MATCHED = "not_matched"
    UNAVAILABLE = "unavailable"


class SummaryRequestUnavailableReason(StrEnum):
    """归类不可用的稳定原因码。

    原因码只表达「为什么不可用」的稳定类别，不携带实现细节；
    新增原因码只能追加成员，禁止复用或删除既有成员。
    """

    DECISION_DISABLED = "decision_disabled"
    RUNTIME_UNAVAILABLE = "runtime_unavailable"
    SCENE_DATA_UNAVAILABLE = "scene_data_unavailable"
    EMBEDDING_UNAVAILABLE = "embedding_unavailable"
    CONFIGURATION_INCOMPLETE = "configuration_incomplete"
    # 预留给 KOMARIBOT-24 的 rerank 供应方不可用/失败升级路径；
    # 本票在 rerank 供应方关闭或调用失败时直接使用，不做失败预算与 fallback。
    RERANK_UNAVAILABLE = "rerank_unavailable"


@dataclass(frozen=True, slots=True)
class SummaryRequestClassificationResult:
    """群总结请求场景归类结果。

    仅暴露三态状态与不可用原因码；matched/not_matched 时 reason 恒为 None。
    """

    status: SummaryRequestClassificationStatus
    reason: SummaryRequestUnavailableReason | None

    @classmethod
    def matched(cls) -> "SummaryRequestClassificationResult":
        """构造命中结果。"""
        return cls(
            status=SummaryRequestClassificationStatus.MATCHED,
            reason=None,
        )

    @classmethod
    def not_matched(cls) -> "SummaryRequestClassificationResult":
        """构造未命中结果。"""
        return cls(
            status=SummaryRequestClassificationStatus.NOT_MATCHED,
            reason=None,
        )

    @classmethod
    def unavailable(
        cls,
        reason: SummaryRequestUnavailableReason,
    ) -> "SummaryRequestClassificationResult":
        """构造带稳定原因码的不可用结果。"""
        return cls(
            status=SummaryRequestClassificationStatus.UNAVAILABLE,
            reason=reason,
        )


__all__ = [
    "SummaryRequestClassificationResult",
    "SummaryRequestClassificationStatus",
    "SummaryRequestUnavailableReason",
]
