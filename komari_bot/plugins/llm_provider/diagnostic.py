"""LLM 请求级诊断模型，供调试插件、聊天和总结共同使用。

设计原则：
- 每个 debug 命令显式创建 LLMDiagnosticCollector 实例并向下传递。
- 正常业务调用传 None，不产生诊断开销。
- 绝不使用全局状态或线程本地变量。
- 聚合报告分别标记各字段是否完整（所有调用都报告了该字段），
  不能用缺失调用的 0 冒充完整总计。
- collector 不保存原始 prompt、response body、画像等敏感正文。
"""

from __future__ import annotations

import uuid
from typing import Any

from pydantic import BaseModel, Field

from .base_client import UnifiedUsageSchema  # noqa: TC001


class LLMCallTrace(BaseModel):
    """单次 LLM 调用的诊断记录。"""

    call_id: str = Field(default_factory=lambda: uuid.uuid4().hex[:12])
    parent_call_id: str | None = Field(default=None, description="父调用 ID")
    phase: str = Field(default="", description="调用阶段标识")
    round_index: int = Field(default=0, description="阶段内轮次序号（0-based）")
    model: str = Field(default="", description="使用的模型名")
    finish_reason: str | None = Field(default=None, description="结束原因")
    duration_ms: float | None = Field(default=None, description="调用耗时（毫秒）")
    usage: UnifiedUsageSchema | None = Field(
        default=None, description="后端实际返回的用量信息"
    )


class ToolExecutionTrace(BaseModel):
    """单次工具执行的诊断记录。"""

    call_id: str = Field(default="", description="所属 LLM 调用 ID")
    tool_name: str = Field(default="", description="工具名称")
    parsed_arguments: dict[str, Any] = Field(
        default_factory=dict, description="安全解析后的参数（不含敏感内容）"
    )
    status: str = Field(
        default="pending", description="执行状态：pending | success | error | skipped"
    )
    error_summary: str | None = Field(
        default=None, description="错误摘要（截断后的错误信息）"
    )
    result_summary: str | None = Field(
        default=None, description="结果摘要（不含完整历史、画像或搜索正文）"
    )


class _PhaseAggregation(BaseModel):
    """单阶段 token 聚合结果。"""

    input_tokens: int = 0
    cached_input_tokens: int = 0
    cache_miss_input_tokens: int = 0
    output_tokens: int = 0
    reasoning_output_tokens: int = 0
    total_tokens: int = 0
    call_count: int = 0
    input_tokens_complete: bool = True
    cached_input_tokens_complete: bool = True
    cache_miss_input_tokens_complete: bool = True
    output_tokens_complete: bool = True
    reasoning_output_tokens_complete: bool = True
    total_tokens_complete: bool = True


class _OverallAggregation(BaseModel):
    """全链路 token 聚合结果。"""

    input_tokens: int = 0
    cached_input_tokens: int = 0
    cache_miss_input_tokens: int = 0
    output_tokens: int = 0
    reasoning_output_tokens: int = 0
    total_tokens: int = 0
    call_count: int = 0
    input_tokens_complete: bool = True
    cached_input_tokens_complete: bool = True
    cache_miss_input_tokens_complete: bool = True
    output_tokens_complete: bool = True
    reasoning_output_tokens_complete: bool = True
    total_tokens_complete: bool = True


class LLMDiagnosticCollector:
    """请求级 LLM 诊断收集器。

    每个 debug 命令显式创建一个实例并向下传递；
    正常业务调用传 None，不会产生诊断开销。

    不保存原始 prompt、response body、画像内容、搜索正文等敏感信息。
    即使后续阶段抛错，已完成调用仍保留在 collector 中，供失败报告使用。
    """

    def __init__(self, request_id: str | None = None) -> None:
        self.request_id: str = request_id or uuid.uuid4().hex[:12]
        self.calls: list[LLMCallTrace] = []
        self.tools: list[ToolExecutionTrace] = []
        self.errors: list[dict[str, str]] = []

    def add_call(self, call: LLMCallTrace) -> None:
        """记录一次 LLM 调用。"""
        self.calls.append(call)

    def add_tool(self, tool: ToolExecutionTrace) -> None:
        """记录一次工具执行。"""
        self.tools.append(tool)

    def add_error(self, phase: str, error_type: str, message: str) -> None:
        """记录一次非致命错误。"""
        self.errors.append(
            {"phase": phase, "type": error_type, "message": message}
        )

    def get_phase_calls(self, phase: str) -> list[LLMCallTrace]:
        """获取指定阶段的所有调用记录。"""
        return [c for c in self.calls if c.phase == phase]

    def _aggregate_usage_fields(
        self,
        calls: list[LLMCallTrace],
    ) -> tuple[dict[str, int], dict[str, bool], int]:
        """对一组调用的 usage 字段进行聚合，返回 (合计值, 完整性标记, 调用次数)。"""
        fields: tuple[str, ...] = (
            "input_tokens",
            "cached_input_tokens",
            "cache_miss_input_tokens",
            "output_tokens",
            "reasoning_output_tokens",
            "total_tokens",
        )
        totals: dict[str, int] = {f: 0 for f in fields}  # noqa: C420
        complete: dict[str, bool] = {f: True for f in fields}  # noqa: C420
        call_count = 0

        for call in calls:
            call_count += 1
            u = call.usage
            if u is None:
                for f in fields:
                    complete[f] = False
                continue

            for f in fields:
                val = getattr(u, f, None)
                if val is None:
                    complete[f] = False
                else:
                    try:
                        totals[f] += int(val)
                    except (TypeError, ValueError):
                        complete[f] = False

        return totals, complete, call_count

    def aggregate_phase(self, phase: str) -> _PhaseAggregation:
        """按阶段聚合 token 用量，缺失字段标注为不完整。"""
        phase_calls = self.get_phase_calls(phase)
        totals, complete, call_count = self._aggregate_usage_fields(phase_calls)
        return _PhaseAggregation(
            input_tokens=totals["input_tokens"],
            cached_input_tokens=totals["cached_input_tokens"],
            cache_miss_input_tokens=totals["cache_miss_input_tokens"],
            output_tokens=totals["output_tokens"],
            reasoning_output_tokens=totals["reasoning_output_tokens"],
            total_tokens=totals["total_tokens"],
            call_count=call_count,
            input_tokens_complete=complete["input_tokens"],
            cached_input_tokens_complete=complete["cached_input_tokens"],
            cache_miss_input_tokens_complete=complete["cache_miss_input_tokens"],
            output_tokens_complete=complete["output_tokens"],
            reasoning_output_tokens_complete=complete["reasoning_output_tokens"],
            total_tokens_complete=complete["total_tokens"],
        )

    def aggregate_overall(self) -> _OverallAggregation:
        """全链路聚合 token 用量，缺失字段标注为不完整。"""
        totals, complete, call_count = self._aggregate_usage_fields(self.calls)
        return _OverallAggregation(
            input_tokens=totals["input_tokens"],
            cached_input_tokens=totals["cached_input_tokens"],
            cache_miss_input_tokens=totals["cache_miss_input_tokens"],
            output_tokens=totals["output_tokens"],
            reasoning_output_tokens=totals["reasoning_output_tokens"],
            total_tokens=totals["total_tokens"],
            call_count=call_count,
            input_tokens_complete=complete["input_tokens"],
            cached_input_tokens_complete=complete["cached_input_tokens"],
            cache_miss_input_tokens_complete=complete["cache_miss_input_tokens"],
            output_tokens_complete=complete["output_tokens"],
            reasoning_output_tokens_complete=complete["reasoning_output_tokens"],
            total_tokens_complete=complete["total_tokens"],
        )
