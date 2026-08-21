"""统一群聊准入的冻结契约类型。

本模块只承载准入模块对外的契约身份：三个 wire 枚举、原因码与问题码
闭集，以及两个 frozen/slots/keyword-only 契约 dataclass。不包含任何运行
时状态、策略解释或 I/O。

原因码与问题码闭集是真实类型契约：``typing.get_type_hints`` 解析出的
``Literal`` args 与冻结集精确相等。见 ADR-0012 与 CONTEXT.md「群聊准入」。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Literal, TypeAlias


class AdmissionIntent(StrEnum):
    """调用方在准入时点声明的群行为目的。"""

    BUSINESS = "business"
    FACT_FINALIZATION = "fact_finalization"
    TECHNICAL_CLEANUP = "technical_cleanup"


class AdmissionQualification(StrEnum):
    """准入裁决授予的行为资格。"""

    BUSINESS = "business"
    FACT_FINALIZATION = "fact_finalization"
    TECHNICAL_CLEANUP = "technical_cleanup"
    REJECTED = "rejected"


class AdmissionRuntimeStatus(StrEnum):
    """准入运行时状态。"""

    READY = "ready"
    DEGRADED = "degraded"
    FAILED = "failed"


# 原因码/问题码闭集必须保留传统 TypeAlias：PEP 695 ``type`` 别名不会被
# ``typing.get_type_hints`` 展开为 Literal，无法作为字段注解的真实闭集契约。
AdmissionReasonCode: TypeAlias = Literal[  # noqa: UP040
    "policy_admitted",
    "policy_restricted",
    "group_attribution_unavailable",
    "effective_policy_unavailable",
    "fact_finalization_granted",
    "technical_cleanup_granted",
    "private_input_rejected",
]

AdmissionProblemCode: TypeAlias = Literal[  # noqa: UP040
    "storage_unavailable",
    "stored_policy_invalid",
    "snapshot_publish_failed",
    "internal_error",
]


@dataclass(frozen=True, slots=True, kw_only=True)
class AdmissionResult:
    """一次准入裁决的结果。

    ``effective_revision`` 是裁决时实际生效的策略修订，进程从未建立合法
    策略时为 ``None``；``reason_code`` 只取冻结闭集内的机器分类，不携带
    策略正文、群号或异常正文。
    """

    qualification: AdmissionQualification
    effective_revision: int | None
    reason_code: AdmissionReasonCode


@dataclass(frozen=True, slots=True, kw_only=True)
class AdmissionRuntimeState:
    """准入运行时健康状态的进程内不可变投影。

    ``configured_revision`` 是运行时已接纳的最高持久修订；
    ``effective_revision`` 是裁决实际生效的策略修订。两者不同表示更高持
    久修订未通过校验编译，运行时正按最近有效策略（LKG）裁决。
    """

    status: AdmissionRuntimeStatus
    problem_code: AdmissionProblemCode | None
    configured_revision: int | None
    effective_revision: int | None
    using_last_known_good: bool

    @property
    def is_ready(self) -> bool:
        """运行时是否持有合法的最新策略修订；DEGRADED 不是 ready。"""
        return self.status is AdmissionRuntimeStatus.READY
