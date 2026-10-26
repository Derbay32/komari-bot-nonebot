"""Versioned newline-delimited JSON framing for the private child pipe."""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final

if TYPE_CHECKING:
    from collections.abc import Mapping

from .types import (
    AgentProtocolError,
    AgentTaskRequest,
    JsonValue,
    TaskKind,
    json_payload,
    validate_error_code,
    validate_json_value,
)

PROTOCOL_VERSION: Final = 1
MAX_FRAME_BYTES: Final = 1024 * 1024
MAX_ID_LENGTH: Final = 128
MAX_ERROR_MESSAGE_LENGTH: Final = 200
MAX_READY_REASON_LENGTH: Final = 200

_ID_PATTERN: Final = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_CALLBACK_NAME_PATTERN: Final = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")


def validate_frame_id(value: object) -> str:
    """Validate one correlation identifier."""
    if not isinstance(value, str) or _ID_PATTERN.fullmatch(value) is None:
        raise AgentProtocolError("invalid_id")
    return value


def encode_frame(kind: str, frame_id: str, payload: Mapping[str, JsonValue]) -> bytes:
    """Serialize one client frame and enforce the finite frame budget."""
    validate_frame_id(frame_id)
    value: dict[str, JsonValue] = {
        "v": PROTOCOL_VERSION,
        "kind": kind,
        "id": frame_id,
        "payload": dict(payload),
    }
    encoded = json_payload(value)
    if len(encoded) > MAX_FRAME_BYTES:
        raise AgentProtocolError("frame_too_large")
    return encoded + b"\n"


def encode_request(request_id: str, request: AgentTaskRequest) -> bytes:
    """Serialize one trusted task request."""
    return encode_frame("request", request_id, request.to_protocol())


def encode_callback_result_success(callback_id: str, result: JsonValue) -> bytes:
    """Serialize one successful Python callback value."""
    validate_json_value(result)
    if len(json_payload(result)) > MAX_FRAME_BYTES:
        raise AgentProtocolError("callback_result_too_large")
    return encode_frame("callback_result", callback_id, {"ok": True, "result": result})


def encode_callback_result_error(callback_id: str, code: str) -> bytes:
    """Serialize one sanitized callback failure."""
    if not validate_error_code(code):
        code = "callback_failed"
    return encode_frame("callback_result", callback_id, {"ok": False, "error": {"code": code, "message": "The callback failed."}})


def encode_cancel(request_id: str) -> bytes:
    """Serialize one task cancellation request."""
    return encode_frame("cancel", request_id, {"reason": "cancelled"})


def encode_shutdown() -> bytes:
    """Serialize the bounded process shutdown request."""
    return encode_frame("shutdown", "shutdown", {"reason": "shutdown"})


async def read_frame(reader: asyncio.StreamReader) -> bytes | None:
    """Read one newline-delimited frame with a finite line bound.

    A partial line at EOF is a protocol violation, not a silent end.
    """
    try:
        line = await reader.readuntil(b"\n")
    except asyncio.IncompleteReadError as exc:
        if exc.partial:
            raise AgentProtocolError("unterminated_frame") from exc
        return None
    except asyncio.LimitOverrunError as exc:
        raise AgentProtocolError("frame_too_large") from exc
    except ValueError as exc:
        raise AgentProtocolError("frame_too_large") from exc
    if len(line) > MAX_FRAME_BYTES:
        raise AgentProtocolError("frame_too_large")
    if not line.strip():
        raise AgentProtocolError("empty_frame")
    return line


@dataclass(frozen=True, slots=True)
class ReadyFrame:
    """The child's one startup readiness announcement."""

    state: str
    protocol_version: int
    host_version: str
    bindings: Mapping[TaskKind, bool]
    reason: str | None


@dataclass(frozen=True, slots=True)
class ResultFrame:
    """Terminal outcome of one request."""

    request_id: str
    ok: bool
    result: Mapping[str, JsonValue] | None
    error_code: str | None
    error_message: str | None


@dataclass(frozen=True, slots=True)
class CallbackFrame:
    """One project-tool callback invocation."""

    callback_id: str
    request_id: str
    task_id: str
    name: str
    data: Mapping[str, JsonValue]


type ServerFrame = ReadyFrame | ResultFrame | CallbackFrame


def _exact_keys(value: Mapping[str, Any], keys: tuple[str, ...], code: str) -> None:
    if set(value) != set(keys):
        raise AgentProtocolError(code)


def _parse_ready(payload: Mapping[str, Any]) -> ReadyFrame:
    _exact_keys(payload, ("state", "protocol_version", "host_version", "bindings"), "invalid_ready_payload")
    state = payload["state"]
    if state not in {"ready", "unavailable"}:
        raise AgentProtocolError("invalid_ready_state")
    version = payload["protocol_version"]
    if not isinstance(version, int) or isinstance(version, bool) or version != PROTOCOL_VERSION:
        raise AgentProtocolError("unsupported_protocol_version")
    host_version = payload["host_version"]
    if not isinstance(host_version, str) or not host_version or len(host_version) > 64:
        raise AgentProtocolError("invalid_host_version")
    bindings = payload["bindings"]
    if not isinstance(bindings, dict):
        raise AgentProtocolError("invalid_bindings")
    _exact_keys(bindings, ("chat", "summary"), "invalid_bindings")
    parsed_bindings: dict[TaskKind, bool] = {}
    for kind in TaskKind:
        value = bindings[kind.value]
        if not isinstance(value, bool):
            raise AgentProtocolError("invalid_bindings")
        parsed_bindings[kind] = value
    reason = payload.get("reason")
    if reason is not None and (not isinstance(reason, str) or len(reason) > MAX_READY_REASON_LENGTH):
        raise AgentProtocolError("invalid_ready_reason")
    return ReadyFrame(
        state=state,
        protocol_version=version,
        host_version=host_version,
        bindings=parsed_bindings,
        reason=reason,
    )


def _parse_result(request_id: str, payload: Mapping[str, Any]) -> ResultFrame:
    ok = payload.get("ok")
    if not isinstance(ok, bool):
        raise AgentProtocolError("invalid_result_payload")
    if ok:
        _exact_keys(payload, ("ok", "result"), "invalid_result_payload")
        result = payload["result"]
        if not isinstance(result, dict):
            raise AgentProtocolError("invalid_result_value")
        validate_json_value(result)
        if len(json_payload(result)) > MAX_FRAME_BYTES:
            raise AgentProtocolError("result_too_large")
        return ResultFrame(request_id=request_id, ok=True, result=result, error_code=None, error_message=None)
    _exact_keys(payload, ("ok", "error"), "invalid_result_payload")
    error = payload["error"]
    if not isinstance(error, dict):
        raise AgentProtocolError("invalid_error_payload")
    _exact_keys(error, ("code", "message"), "invalid_error_payload")
    code = error["code"]
    message = error["message"]
    if not isinstance(code, str) or not validate_error_code(code):
        raise AgentProtocolError("invalid_error_code")
    if not isinstance(message, str) or len(message) > MAX_ERROR_MESSAGE_LENGTH:
        raise AgentProtocolError("invalid_error_message")
    return ResultFrame(request_id=request_id, ok=False, result=None, error_code=code, error_message=message)


def _parse_callback(callback_id: str, payload: Mapping[str, Any]) -> CallbackFrame:
    _exact_keys(payload, ("request_id", "task_id", "name", "data"), "invalid_callback_payload")
    request_id = validate_frame_id(payload["request_id"])
    task_id = payload["task_id"]
    if not isinstance(task_id, str) or not task_id or len(task_id) > MAX_ID_LENGTH:
        raise AgentProtocolError("invalid_task_id")
    name = payload["name"]
    if not isinstance(name, str) or _CALLBACK_NAME_PATTERN.fullmatch(name) is None:
        raise AgentProtocolError("invalid_callback_name")
    data = payload["data"]
    if not isinstance(data, dict):
        raise AgentProtocolError("invalid_callback_data")
    validate_json_value(data)
    if len(json_payload(data)) > MAX_FRAME_BYTES:
        raise AgentProtocolError("callback_data_too_large")
    return CallbackFrame(
        callback_id=callback_id,
        request_id=request_id,
        task_id=task_id,
        name=name,
        data=data,
    )


def decode_server_frame(line: bytes) -> ServerFrame:
    """Parse one JSON line from the child; closed kinds only."""
    try:
        parsed: object = json.loads(line)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AgentProtocolError("invalid_json") from exc
    if not isinstance(parsed, dict):
        raise AgentProtocolError("invalid_envelope")
    _exact_keys(parsed, ("v", "kind", "id", "payload"), "invalid_envelope")
    if parsed["v"] != PROTOCOL_VERSION:
        raise AgentProtocolError("unsupported_protocol_version")
    frame_id = validate_frame_id(parsed["id"])
    kind = parsed["kind"]
    payload = parsed["payload"]
    if not isinstance(payload, dict):
        raise AgentProtocolError("invalid_payload")
    match kind:
        case "ready":
            if frame_id != "ready":
                raise AgentProtocolError("invalid_ready_id")
            return _parse_ready(payload)
        case "result":
            return _parse_result(frame_id, payload)
        case "callback":
            return _parse_callback(frame_id, payload)
        case _:
            raise AgentProtocolError("unknown_frame_kind")


__all__ = [
    "MAX_FRAME_BYTES",
    "PROTOCOL_VERSION",
    "CallbackFrame",
    "ReadyFrame",
    "ResultFrame",
    "ServerFrame",
    "decode_server_frame",
    "encode_callback_result_error",
    "encode_callback_result_success",
    "encode_cancel",
    "encode_frame",
    "encode_request",
    "encode_shutdown",
    "read_frame",
    "validate_frame_id",
]
