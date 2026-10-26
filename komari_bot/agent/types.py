"""Typed, framework-neutral DSH task boundary."""

from __future__ import annotations

import copy
import json
import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Final

MAX_TASK_ID_LENGTH: Final = 128
MAX_CALLBACK_NAME_LENGTH: Final = 64
MAX_TASK_DATA_BYTES: Final = 512 * 1024
MAX_CALLBACK_DATA_BYTES: Final = 256 * 1024
MAX_RESULT_BYTES: Final = 512 * 1024
MAX_BUDGET_VALUE: Final = 1000

_TASK_ID_PATTERN: Final = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
_CALLBACK_NAME_PATTERN: Final = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")
_ERROR_CODE_PATTERN: Final = re.compile(r"^[a-z][a-z0-9_]{0,63}$")

type JsonValue = bool | int | float | str | list[JsonValue] | dict[str, JsonValue] | None


class TaskKind(StrEnum):
    """Trusted business task type; the only preset selector the protocol carries."""

    CHAT = "chat"
    SUMMARY = "summary"


class HostState(StrEnum):
    """Lifecycle of the one long-lived DSH child process."""

    NEW = "new"
    STARTING = "starting"
    READY = "ready"
    UNAVAILABLE = "unavailable"
    CLOSING = "closing"
    CLOSED = "closed"


class ReadinessState(StrEnum):
    """Public readiness, distinguishing unconfigured from unavailable."""

    READY = "ready"
    UNCONFIGURED_READY = "unconfigured_ready"
    UNAVAILABLE = "unavailable"


class AgentHostError(RuntimeError):
    """Base class for failures at the DSH task boundary."""

    code: str = "host_error"


class AgentHostUnavailableError(AgentHostError):
    """The child process is not usable; no task can start or finish."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class AgentProtocolError(AgentHostError):
    """The peer sent a frame that violates the versioned envelope contract."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class AgentTaskError(AgentHostError):
    """A task failed for a stable, already-sanitized reason code."""

    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(message or code)
        self.code = code
        self.safe_message = message or "The task failed."


def validate_error_code(code: str) -> bool:
    """Return whether a stable machine code is safe to relay."""
    return _ERROR_CODE_PATTERN.fullmatch(code) is not None


def validate_json_value(value: object, *, depth: int = 0) -> None:
    """Validate one lossless JSON value without coercing it."""
    if depth > 64:
        msg = "json value is too deep"
        raise ValueError(msg)
    if value is None or isinstance(value, bool):
        return
    if isinstance(value, (int, float)):
        if isinstance(value, float) and not (-float("inf") < value < float("inf")):
            msg = "json number is not finite"
            raise ValueError(msg)
        return
    if isinstance(value, str):
        return
    if isinstance(value, list):
        for item in value:
            validate_json_value(item, depth=depth + 1)
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                msg = "json object key is not a string"
                raise TypeError(msg)
            validate_json_value(item, depth=depth + 1)
        return
    msg = f"unsupported json value: {type(value).__name__}"
    raise TypeError(msg)


def json_payload(value: JsonValue) -> bytes:
    """Serialize one JSON value with deterministic, non-NaN encoding."""
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")


def copy_json_object(value: Mapping[str, Any], *, max_bytes: int) -> dict[str, JsonValue]:
    """Deep-copy and validate one protocol object against a byte budget."""
    detached = copy.deepcopy(dict(value))
    validate_json_value(detached)
    encoded = json.dumps(detached, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
    if len(encoded) > max_bytes:
        msg = "json object exceeds the protocol budget"
        raise ValueError(msg)
    return detached


def _validate_budget_value(name: str, value: object) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        msg = f"{name} must be an integer"
        raise TypeError(msg)
    if value < 1 or value > MAX_BUDGET_VALUE:
        msg = f"{name} must be between 1 and {MAX_BUDGET_VALUE}"
        raise ValueError(msg)
    return value


@dataclass(frozen=True, slots=True)
class AgentTaskBudget:
    """Business budget frozen at task creation."""

    max_rounds: int
    max_tool_calls_per_round: int
    max_total_tool_calls: int

    def __post_init__(self) -> None:
        _validate_budget_value("max_rounds", self.max_rounds)
        _validate_budget_value("max_tool_calls_per_round", self.max_tool_calls_per_round)
        _validate_budget_value("max_total_tool_calls", self.max_total_tool_calls)

    def to_protocol(self) -> dict[str, Any]:
        """Return the closed JSON shape carried on the wire."""
        return {
            "max_rounds": self.max_rounds,
            "max_tool_calls_per_round": self.max_tool_calls_per_round,
            "max_total_tool_calls": self.max_total_tool_calls,
        }


@dataclass(frozen=True, slots=True)
class AgentTaskRequest:
    """Trusted task input; never carries model messages, tools, or credentials."""

    task_id: str
    kind: TaskKind
    task_data: Mapping[str, JsonValue]
    budget: AgentTaskBudget

    def __post_init__(self) -> None:
        if _TASK_ID_PATTERN.fullmatch(self.task_id) is None:
            msg = "task_id must match the protocol identifier pattern"
            raise ValueError(msg)
        if not isinstance(self.kind, TaskKind):
            msg = "kind must be a trusted TaskKind"
            raise TypeError(msg)
        copied = copy_json_object(self.task_data, max_bytes=MAX_TASK_DATA_BYTES)
        object.__setattr__(self, "task_data", copied)

    def to_protocol(self) -> dict[str, JsonValue]:
        """Return the request payload accepted by the Node host."""
        return {
            "task_id": self.task_id,
            "kind": self.kind.value,
            "task_data": dict(self.task_data),
            "budget": self.budget.to_protocol(),
        }


@dataclass(frozen=True, slots=True)
class AgentTaskResult:
    """Structured business result filled by a successful project tool callback."""

    task_id: str
    kind: TaskKind
    result: Mapping[str, JsonValue]


@dataclass(frozen=True, slots=True)
class AgentCallbackRequest:
    """One Python callback invocation bound to its live trusted task."""

    name: str
    data: Mapping[str, JsonValue]
    task: AgentTaskRequest

    def __post_init__(self) -> None:
        if _CALLBACK_NAME_PATTERN.fullmatch(self.name) is None:
            msg = "callback name must match the protocol identifier pattern"
            raise ValueError(msg)
        copied = copy_json_object(self.data, max_bytes=MAX_CALLBACK_DATA_BYTES)
        object.__setattr__(self, "data", copied)


type AgentCallbackHandler = Callable[[AgentCallbackRequest], Awaitable[JsonValue]]


@dataclass(frozen=True, slots=True)
class HostReadiness:
    """Public display of the child's model-readiness distinction."""

    state: ReadinessState
    bindings: Mapping[TaskKind, bool]

    @property
    def available(self) -> bool:
        """Whether a task can be submitted at all."""
        return self.state is not ReadinessState.UNAVAILABLE

    @property
    def model_configured(self) -> bool:
        """Whether both trusted task bindings are complete."""
        return all(self.bindings.values())


__all__ = [
    "MAX_CALLBACK_DATA_BYTES",
    "MAX_CALLBACK_NAME_LENGTH",
    "MAX_RESULT_BYTES",
    "MAX_TASK_DATA_BYTES",
    "MAX_TASK_ID_LENGTH",
    "AgentCallbackHandler",
    "AgentCallbackRequest",
    "AgentHostError",
    "AgentHostUnavailableError",
    "AgentProtocolError",
    "AgentTaskBudget",
    "AgentTaskError",
    "AgentTaskRequest",
    "AgentTaskResult",
    "HostReadiness",
    "HostState",
    "JsonValue",
    "ReadinessState",
    "TaskKind",
    "copy_json_object",
    "json_payload",
    "validate_error_code",
    "validate_json_value",
]
