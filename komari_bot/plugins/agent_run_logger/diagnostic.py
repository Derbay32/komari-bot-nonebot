"""请求级 Agent Run 采集模型与调试聚合。"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from .sanitizer import sanitize_log_value

AgentRunType = Literal["chat_reply", "scheduled_summary", "group_history_summary"]
AgentRunOrigin = Literal["normal", "debug"]
AgentRunStatus = Literal["success", "error", "cancelled"]


class LLMCallTrace(BaseModel):
    """Agent Run 内的一次逻辑 LLM 调用。"""

    call_id: str = Field(default_factory=lambda: uuid.uuid4().hex[:12])
    parent_call_id: str | None = None
    phase: str = ""
    round_index: int = 0
    method: str = ""
    model: str = ""
    started_at: str = Field(
        default_factory=lambda: datetime.now().astimezone().isoformat()
    )
    status: Literal["success", "error"] = "success"
    request: dict[str, Any] = Field(default_factory=dict)
    response: dict[str, Any] | None = None
    error: dict[str, Any] | None = None
    finish_reason: str | None = None
    duration_ms: float | None = None
    usage: Any | None = None


class ToolExecutionTrace(BaseModel):
    """Agent Run 内的一次工具执行。"""

    call_id: str = ""
    tool_name: str = ""
    parsed_arguments: dict[str, Any] = Field(default_factory=dict)
    status: str = "pending"
    result: Any = None
    error: str | None = None
    duration_ms: float | None = None
    error_summary: str | None = None
    result_summary: str | None = None


class _PhaseAggregation(BaseModel):
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


class _OverallAggregation(_PhaseAggregation):
    pass


class AgentRunNotFinishedError(RuntimeError):
    """尚未结束的收集器不能构建落盘记录。"""


def _usage_value(usage: object, field: str) -> Any:
    if isinstance(usage, dict):
        return usage.get(field)
    return getattr(usage, field, None)


class AgentRunCollector:
    """显式下传的单任务 Agent Run 收集器。"""

    def __init__(
        self,
        request_id: str | None = None,
        *,
        run_type: AgentRunType = "chat_reply",
        task_kind: str = "chat_reply",
        origin: AgentRunOrigin = "normal",
        input_data: object = None,
        persist: bool = False,
    ) -> None:
        self.request_id = request_id or uuid.uuid4().hex[:12]
        self.run_id = uuid.uuid4().hex
        self.run_type = run_type
        self.task_kind = task_kind
        self.origin = origin
        self.input_data = sanitize_log_value(input_data)
        self.persist = persist
        self.started_at = datetime.now().astimezone()
        self.finished_at: datetime | None = None
        self.status: AgentRunStatus | None = None
        self.output_data: Any = None
        self.final_error: dict[str, str] | None = None
        self.calls: list[LLMCallTrace] = []
        self.tools: list[ToolExecutionTrace] = []
        self.errors: list[dict[str, str]] = []
        self._finalized = False

    @property
    def finalized(self) -> bool:
        return self._finalized

    def add_call(self, call: LLMCallTrace) -> None:
        call.request = sanitize_log_value(call.request)
        if call.response is not None:
            call.response = sanitize_log_value(call.response)
        if call.error is not None:
            call.error = sanitize_log_value(call.error)
        self.calls.append(call)

    def add_tool(self, tool: ToolExecutionTrace) -> None:
        tool.parsed_arguments = sanitize_log_value(tool.parsed_arguments)
        tool.result = sanitize_log_value(tool.result)
        self.tools.append(tool)

    def add_error(self, phase: str, error_type: str, message: str) -> None:
        self.errors.append(
            {"phase": phase, "type": error_type, "message": message}
        )

    def set_input_data(self, value: object) -> None:
        """在任务取得 processing 快照后补全权威业务输入。"""
        self.input_data = sanitize_log_value(value)

    def get_phase_calls(self, phase: str) -> list[LLMCallTrace]:
        return [call for call in self.calls if call.phase == phase]

    def _aggregate_usage_fields(
        self,
        calls: list[LLMCallTrace],
    ) -> tuple[dict[str, int], dict[str, bool], int]:
        fields = (
            "input_tokens",
            "cached_input_tokens",
            "cache_miss_input_tokens",
            "output_tokens",
            "reasoning_output_tokens",
            "total_tokens",
        )
        totals: dict[str, int] = dict.fromkeys(fields, 0)
        complete: dict[str, bool] = dict.fromkeys(fields, True)
        for call in calls:
            usage = call.usage
            if usage is None:
                for field in fields:
                    complete[field] = False
                continue
            for field in fields:
                value = _usage_value(usage, field)
                if value is None:
                    complete[field] = False
                    continue
                try:
                    totals[field] += int(value)
                except (TypeError, ValueError):
                    complete[field] = False
        return totals, complete, len(calls)

    @staticmethod
    def _build_aggregation(
        model: type[_PhaseAggregation],
        totals: dict[str, int],
        complete: dict[str, bool],
        call_count: int,
    ) -> _PhaseAggregation:
        return model(
            **totals,
            call_count=call_count,
            **{f"{field}_complete": value for field, value in complete.items()},
        )

    def aggregate_phase(self, phase: str) -> _PhaseAggregation:
        totals, complete, count = self._aggregate_usage_fields(
            self.get_phase_calls(phase)
        )
        return self._build_aggregation(_PhaseAggregation, totals, complete, count)

    def aggregate_overall(self) -> _OverallAggregation:
        totals, complete, count = self._aggregate_usage_fields(self.calls)
        result = self._build_aggregation(_OverallAggregation, totals, complete, count)
        return _OverallAggregation.model_validate(result.model_dump())

    def mark_finished(
        self,
        *,
        status: AgentRunStatus,
        output: object = None,
        error: BaseException | str | None = None,
    ) -> bool:
        """结束收集，返回本次调用是否首次完成。"""
        if self._finalized:
            return False
        self._finalized = True
        self.status = status
        self.finished_at = datetime.now().astimezone()
        self.output_data = sanitize_log_value(output)
        if error is not None:
            self.final_error = {
                "type": type(error).__name__ if isinstance(error, BaseException) else "Error",
                "message": str(error),
            }
        return True

    def build_record(self) -> dict[str, Any]:
        if not self._finalized or self.finished_at is None or self.status is None:
            raise AgentRunNotFinishedError
        aggregate = self.aggregate_overall().model_dump()
        methods = sorted({call.method for call in self.calls if call.method})
        models = sorted({call.model for call in self.calls if call.model})
        return sanitize_log_value(
            {
                "schema_version": 3,
                "run_id": self.run_id,
                "trace_id": self.request_id,
                "run_type": self.run_type,
                "task_kind": self.task_kind,
                "origin": self.origin,
                "status": self.status,
                "started_at": self.started_at.isoformat(),
                "finished_at": self.finished_at.isoformat(),
                "duration_ms": round(
                    (self.finished_at - self.started_at).total_seconds() * 1000,
                    2,
                ),
                "input": self.input_data,
                "output": self.output_data,
                "error": self.final_error,
                "rounds": [call.model_dump(mode="json") for call in self.calls],
                "tool_executions": [
                    tool.model_dump(mode="json") for tool in self.tools
                ],
                "errors": self.errors,
                "models": models,
                "methods": methods,
                "usage": aggregate,
            }
        )


LLMDiagnosticCollector = AgentRunCollector


def completion_response_payload(completion: object) -> dict[str, Any]:
    """从 provider completion 结果提取完整、可序列化响应。"""
    tool_calls = getattr(completion, "tool_calls", []) or []
    return {
        "content": getattr(completion, "content", ""),
        "reasoning_content": getattr(completion, "reasoning_content", None),
        "tool_calls": [
            (
                tool_call.model_dump(mode="json", exclude_none=True)
                if callable(getattr(tool_call, "model_dump", None))
                else sanitize_log_value(tool_call)
            )
            for tool_call in tool_calls
        ],
        "finish_reason": getattr(completion, "finish_reason", None),
        "usage": sanitize_log_value(getattr(completion, "usage", None)),
    }


def record_completion_call(
    collector: AgentRunCollector | None,
    *,
    phase: str,
    round_index: int,
    method: str,
    model: str,
    request: dict[str, Any],
    completion: object,
    parent_call_id: str | None = None,
    call_id: str | None = None,
) -> str | None:
    """把一次成功 completion 追加到收集器。"""
    if collector is None:
        return None
    trace = LLMCallTrace(
        call_id=call_id or uuid.uuid4().hex[:12],
        parent_call_id=parent_call_id,
        phase=phase,
        round_index=round_index,
        method=method,
        model=model,
        request=request,
        response=completion_response_payload(completion),
        finish_reason=getattr(completion, "finish_reason", None),
        duration_ms=getattr(completion, "duration_ms", None),
        usage=getattr(completion, "usage", None),
    )
    collector.add_call(trace)
    return trace.call_id


def record_failed_call(
    collector: AgentRunCollector | None,
    *,
    phase: str,
    round_index: int,
    method: str,
    model: str,
    request: dict[str, Any],
    error: BaseException,
    parent_call_id: str | None = None,
) -> None:
    """把一次失败的逻辑 LLM 调用追加到收集器。"""
    if collector is None:
        return
    collector.add_call(
        LLMCallTrace(
            parent_call_id=parent_call_id,
            phase=phase,
            round_index=round_index,
            method=method,
            model=model,
            status="error",
            request=request,
            error={"type": type(error).__name__, "message": str(error)},
        )
    )
