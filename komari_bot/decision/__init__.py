"""Komari Decision 判定契约共享包。

延续 ADR-0005 的共享桶体系（参照 komari_bot/llm/ 的既有手法），承载判定领域的
全部契约纯件：运行时状态、判定输出、时机评分分解与消息过滤结果，并提供
DecisionEngineProtocol 供消费方注解引擎实例。

聊天宽重排候选/结果/异常契约已随 KOMARIBOT-27 退役：聊天专用类型只保留在
komari_decision 深 implementation 内部，本包不承载。

本包零依赖 nonebot / redis / 插件内部类型，可被任意消费方安全 import。
"""

from .decision_engine import CallIntent, DecisionOutcome, MemoryAction, ReplyReason
from .engine_protocol import DecisionEngineProtocol
from .message_filter import FilterResult
from .runtime_state import DecisionRuntimeState, DecisionRuntimeStatus
from .social_timing_service import TimingScoreBreakdown
from .summary_request_classification import (
    SummaryRequestClassificationResult,
    SummaryRequestClassificationStatus,
    SummaryRequestUnavailableReason,
)

__all__ = [
    "CallIntent",
    "DecisionEngineProtocol",
    "DecisionOutcome",
    "DecisionRuntimeState",
    "DecisionRuntimeStatus",
    "FilterResult",
    "MemoryAction",
    "ReplyReason",
    "SummaryRequestClassificationResult",
    "SummaryRequestClassificationStatus",
    "SummaryRequestUnavailableReason",
    "TimingScoreBreakdown",
]
