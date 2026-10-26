"""TSK-206 总结任务在共享 DSH 运行时上的 Python 编排。

Python 冻结预算与历史限制，执行一个 ``TaskKind.SUMMARY`` 任务，提供四个公开
历史/完成回调与一个内部可信分页回调。模型参数不决定群身份、来源标签、时间
范围或覆盖元数据。
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol, cast

from komari_bot.agent import (
    AgentHostError,
    AgentTaskRequest,
    AgentTaskResult,
    JsonValue,
    TaskKind,
)

from .admission_gate import SummaryAdmissionDeniedError, effect_business_admitted
from .history_tools import (
    SummaryToolResult,
    execute_recent_tool,
    execute_topic_tool,
    execute_user_tool,
)
from .summary_candidate import (
    SummaryCandidate,
    SummarySelection,
)
from .summary_task_data import FrozenSummaryTaskConfig, SummaryTaskData

if TYPE_CHECKING:
    from komari_bot.agent import AgentCallbackRequest, AgentHost

TASK_ID_PATTERN = re.compile(r"[A-Za-z0-9_.:-]{1,128}")


class SummaryGenerationError(RuntimeError):
    """The DSH summary task failed for a fixed, already-safe reason."""

    code = "summary_generation_failed"

    def __init__(self, code: str | None = None) -> None:
        if code is not None:
            self.code = code
        super().__init__(self.code)


class SummaryToolArgumentError(RuntimeError):
    """Trusted internal page arguments are invalid (never model-correctable)."""

    code = "invalid_summary_tool_arguments"


class SummaryHistoryProvider(Protocol):
    """The deterministic history business operations for one summary task."""

    async def fetch_recent(self, arguments: Mapping[str, object]) -> SummaryToolResult: ...

    async def fetch_user(self, arguments: Mapping[str, object]) -> SummaryToolResult: ...

    async def fetch_topic(self, arguments: Mapping[str, object]) -> SummaryToolResult: ...


SummaryHistoryProviderFactory = Callable[
    [Any, str, str, FrozenSummaryTaskConfig], SummaryHistoryProvider
]
HostGetter = Callable[[], "AgentHost"]
AdmissionChecker = Callable[[str], bool]


@dataclass(frozen=True, slots=True)
class SummaryGeneration:
    """A verified final tool submission plus the frozen Python selection."""

    summary_text: str
    selection: SummarySelection | None


class DefaultSummaryHistoryProvider:
    """Production history operations bound to one trusted bot/group task."""

    def __init__(
        self,
        *,
        bot: object,
        group_id: str,
        bot_self_id: str,
        config: FrozenSummaryTaskConfig,
    ) -> None:
        self._bot = bot
        self._group_id = group_id
        self._bot_self_id = bot_self_id
        self._config = config

    async def fetch_recent(self, arguments: Mapping[str, object]) -> SummaryToolResult:
        return await execute_recent_tool(
            bot=cast("Any", self._bot),
            group_id=self._group_id,
            bot_self_id=self._bot_self_id,
            batch_size=self._config.fetch_batch_size,
            min_summary_count=self._config.limits.min_count,
            max_summary_count=self._config.limits.max_count,
            history_min_coverage_ratio=self._config.history_min_coverage_ratio,
            arguments=arguments,
        )

    async def fetch_user(self, arguments: Mapping[str, object]) -> SummaryToolResult:
        return await execute_user_tool(
            bot=cast("Any", self._bot),
            group_id=self._group_id,
            bot_self_id=self._bot_self_id,
            batch_size=self._config.fetch_batch_size,
            min_summary_count=self._config.limits.min_count,
            max_summary_count=self._config.limits.max_count,
            summary_tool_scan_limit=self._config.limits.scan_limit,
            history_min_coverage_ratio=self._config.history_min_coverage_ratio,
            arguments=arguments,
        )

    async def fetch_topic(self, arguments: Mapping[str, object]) -> SummaryToolResult:
        return await execute_topic_tool(
            bot=cast("Any", self._bot),
            group_id=self._group_id,
            bot_self_id=self._bot_self_id,
            batch_size=self._config.fetch_batch_size,
            min_summary_count=self._config.limits.min_count,
            max_summary_count=self._config.limits.max_count,
            summary_tool_scan_limit=self._config.limits.scan_limit,
            history_min_coverage_ratio=self._config.history_min_coverage_ratio,
            arguments=arguments,
        )


def _default_host_getter() -> AgentHost:
    from komari_bot.plugins import komari_agent

    return komari_agent.get_agent_host()


def _default_provider_factory(
    bot: object,
    group_id: str,
    bot_self_id: str,
    config: FrozenSummaryTaskConfig,
) -> SummaryHistoryProvider:
    return DefaultSummaryHistoryProvider(
        bot=bot,
        group_id=group_id,
        bot_self_id=bot_self_id,
        config=config,
    )


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _validate_recent_arguments(data: Mapping[str, object]) -> dict[str, object] | None:
    if not set(data) <= {"count", "include_bot_replies"}:
        return None
    count = data.get("count")
    if not _is_int(count):
        return None
    payload: dict[str, object] = {"count": count}
    if "include_bot_replies" in data:
        include = data["include_bot_replies"]
        if not isinstance(include, bool):
            return None
        payload["include_bot_replies"] = include
    return payload


def _validate_user_arguments(data: Mapping[str, object]) -> dict[str, object] | None:
    if not set(data) <= {"count", "user_id", "display_name", "scan_limit"}:
        return None
    count = data.get("count")
    if not _is_int(count):
        return None
    payload: dict[str, object] = {"count": count}
    for name in ("user_id", "display_name"):
        if name in data:
            value = data[name]
            if not isinstance(value, str):
                return None
            payload[name] = value
    if "scan_limit" in data:
        scan_limit = data["scan_limit"]
        if not _is_int(scan_limit):
            return None
        payload["scan_limit"] = scan_limit
    return payload


def _validate_topic_arguments(data: Mapping[str, object]) -> dict[str, object] | None:
    if not set(data) <= {"count", "keywords", "scan_limit"}:
        return None
    count = data.get("count")
    keywords = data.get("keywords")
    if not _is_int(count) or not isinstance(keywords, list):
        return None
    if any(not isinstance(item, str) for item in keywords):
        return None
    payload: dict[str, object] = {"count": count, "keywords": list(keywords)}
    if "scan_limit" in data:
        scan_limit = data["scan_limit"]
        if not _is_int(scan_limit):
            return None
        payload["scan_limit"] = scan_limit
    return payload


def _validate_page_arguments(data: Mapping[str, object]) -> tuple[int, int]:
    if set(data) != {"generation", "offset"}:
        raise SummaryToolArgumentError
    generation = data.get("generation")
    offset = data.get("offset")
    if not _is_int(generation) or not _is_int(offset):
        raise SummaryToolArgumentError
    return cast("int", generation), cast("int", offset)


class DshSummaryService:
    """One summary generation through the production shared ``AgentHost``."""

    def __init__(
        self,
        *,
        host_getter: HostGetter | None = None,
        history_provider_factory: SummaryHistoryProviderFactory | None = None,
        admission_checker: AdmissionChecker | None = None,
    ) -> None:
        self._host_getter = host_getter or _default_host_getter
        self._history_provider_factory = (
            history_provider_factory or _default_provider_factory
        )
        self._admission_checker = admission_checker or (
            lambda group_id: effect_business_admitted(group_id=group_id)
        )

    async def generate(
        self,
        *,
        bot: object,
        group_id: str,
        bot_self_id: str,
        user_request: str,
        request_trace_id: str,
        config: FrozenSummaryTaskConfig,
    ) -> SummaryGeneration:
        if TASK_ID_PATTERN.fullmatch(request_trace_id) is None:
            raise SummaryGenerationError("invalid_trace_id")

        candidate = SummaryCandidate(bot_self_id=bot_self_id)
        provider = self._history_provider_factory(
            bot, group_id, bot_self_id, config
        )
        denied: dict[str, bool] = {"value": False}

        def _admitted() -> bool:
            try:
                allow = bool(self._admission_checker(group_id))
            except asyncio.CancelledError:
                raise
            except Exception:
                allow = False
            if not allow:
                denied["value"] = True
            return allow

        async def admission(_request: AgentCallbackRequest) -> JsonValue:
            return {"allow": _admitted()}

        async def fetch_recent(request: AgentCallbackRequest) -> JsonValue:
            payload = _validate_recent_arguments(request.data)
            if payload is None:
                return _invalid_arguments(candidate)
            try:
                result = await provider.fetch_recent(payload)
            except SummaryAdmissionDeniedError:
                denied["value"] = True
                raise
            candidate.replace_selection(result)
            return cast("JsonValue", candidate.descriptor())

        async def fetch_user(request: AgentCallbackRequest) -> JsonValue:
            payload = _validate_user_arguments(request.data)
            if payload is None:
                return _invalid_arguments(candidate)
            try:
                result = await provider.fetch_user(payload)
            except SummaryAdmissionDeniedError:
                denied["value"] = True
                raise
            candidate.replace_selection(result)
            return cast("JsonValue", candidate.descriptor())

        async def fetch_topic(request: AgentCallbackRequest) -> JsonValue:
            payload = _validate_topic_arguments(request.data)
            if payload is None:
                return _invalid_arguments(candidate)
            try:
                result = await provider.fetch_topic(payload)
            except SummaryAdmissionDeniedError:
                denied["value"] = True
                raise
            candidate.replace_selection(result)
            return cast("JsonValue", candidate.descriptor())

        async def history_page(request: AgentCallbackRequest) -> JsonValue:
            generation, offset = _validate_page_arguments(request.data)
            try:
                allow = bool(self._admission_checker(group_id))
            except asyncio.CancelledError:
                raise
            except Exception:
                allow = False
            if not allow:
                denied["value"] = True
                raise SummaryAdmissionDeniedError
            return cast("JsonValue", candidate.page(generation, offset))

        async def final_summary(request: AgentCallbackRequest) -> JsonValue:
            receipt = candidate.submit_final(request.data)
            return cast("JsonValue", receipt.to_dict())

        task = AgentTaskRequest(
            task_id=request_trace_id,
            kind=TaskKind.SUMMARY,
            task_data=cast(
                "Mapping[str, JsonValue]",
                SummaryTaskData(
                    trace_id=request_trace_id,
                    user_request=user_request,
                    limits=config.limits,
                ).to_dict(),
            ),
            budget=config.budget.to_agent_budget(),
        )
        try:
            result: AgentTaskResult = await self._host_getter().execute(
                task,
                {
                    "admission": admission,
                    "summary.fetch_recent": fetch_recent,
                    "summary.fetch_user": fetch_user,
                    "summary.fetch_topic": fetch_topic,
                    "summary.history_page": history_page,
                    "summary.final_summary": final_summary,
                },
            )
        except asyncio.CancelledError:
            raise
        except AgentHostError as exc:
            if denied["value"]:
                raise SummaryAdmissionDeniedError from None
            raise SummaryGenerationError(exc.code) from None

        summary_text = candidate.verify_host_result(result.result)
        return SummaryGeneration(
            summary_text=summary_text,
            selection=candidate.selection,
        )


def _invalid_arguments(candidate: SummaryCandidate) -> JsonValue:
    return cast(
        "JsonValue",
        {
            "error": "invalid_arguments",
            "generation": candidate.generation,
        },
    )


__all__ = [
    "DefaultSummaryHistoryProvider",
    "DshSummaryService",
    "SummaryGeneration",
    "SummaryGenerationError",
    "SummaryHistoryProvider",
    "SummaryToolArgumentError",
]
