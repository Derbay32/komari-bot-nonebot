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
    # 仅表达「配置与提供者均启用 rerank 时的预期调用故障」
    # （远程服务/响应校验/传输超时/未初始化）；提供者明确关闭时走真实余弦模式。
    RERANK_UNAVAILABLE = "rerank_unavailable"
    # KOMARIBOT-24：rerank 供应方可降级失败累计达到阈值，需要升级诊断的窄标记；
    # 达到阈值后计数保留，后续失败仍返回本原因码。
    RERANK_FAILURE_BUDGET_EXHAUSTED = "rerank_failure_budget_exhausted"
    # KOMARIBOT-24：rerank 已失败但失败预算存储（Redis）不可用，禁止走 fallback。
    FAILURE_BUDGET_UNAVAILABLE = "failure_budget_unavailable"


@dataclass(frozen=True, slots=True)
class SummaryRequestClassificationResult:
    """群总结请求场景归类结果。

    仅暴露三态状态与不可用原因码；强制状态/原因不变量：
    UNAVAILABLE 必须携带 reason，MATCHED/NOT_MATCHED 不得携带 reason。
    """

    status: SummaryRequestClassificationStatus
    reason: SummaryRequestUnavailableReason | None

    def __post_init__(self) -> None:
        """校验状态/原因不变量，非法组合抛 ValueError。"""
        if self.status is SummaryRequestClassificationStatus.UNAVAILABLE:
            if self.reason is None:
                msg = "不可用结果必须携带原因码"
                raise ValueError(msg)
        elif self.reason is not None:
            msg = "命中或未命中结果不能携带原因码"
            raise ValueError(msg)

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
