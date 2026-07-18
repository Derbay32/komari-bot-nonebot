"""Sentry 辅助函数测试。"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from functools import partial
from types import SimpleNamespace
from typing import Any, cast

from nonebot.exception import (
    FinishedException,
    PausedException,
    RejectedException,
    StopPropagation,
    TypeMisMatch,
)

from komari_bot.common.sentry_support import (
    build_sentry_init_options,
    ensure_sentry_privacy_hooks,
    get_ignored_sentry_exceptions,
    sentry_before_breadcrumb,
    sentry_before_send,
    sentry_before_send_log,
    sentry_before_send_transaction,
)


def _build_type_mismatch() -> TypeMisMatch:
    param = cast(
        "Any",
        SimpleNamespace(
            name="event",
            _type_display=lambda: "GroupMessageEvent",
        ),
    )
    return TypeMisMatch(param, "private_event")


@dataclass(slots=True)
class _DummySentryConfig:
    environment: str = ""
    release: str = ""
    debug: bool = False
    error_sample_rate: float = 1.0
    traces_sample_rate: float = 0.2
    profiles_sample_rate: float = 0.0
    attach_stacktrace: bool = True
    send_default_pii: bool = False
    max_breadcrumbs: int = 100
    breadcrumb_level: str = "INFO"
    sentry_logs_level: str = "INFO"
    event_level: str = "ERROR"


def test_sentry_before_send_drops_nonebot_control_flow_exceptions() -> None:
    for error in (
        StopPropagation(),
        PausedException(),
        RejectedException(),
        FinishedException(),
    ):
        assert sentry_before_send({}, {"exc_info": (type(error), error, None)}) is None


def test_sentry_before_send_keeps_business_and_type_mismatch_errors() -> None:
    type_mismatch = _build_type_mismatch()

    assert sentry_before_send(
        {"id": "1"}, {"exc_info": (TypeMisMatch, type_mismatch, None)}
    ) == {"id": "1"}
    assert sentry_before_send(
        {"id": "2"}, {"exc_info": (RuntimeError, RuntimeError("boom"), None)}
    ) == {"id": "2"}


def test_sentry_before_breadcrumb_hides_message_and_unsafe_data() -> None:
    breadcrumb = sentry_before_breadcrumb(
        {
            "type": "log",
            "level": "info",
            "category": "komari.chat",
            "message": "breadcrumb-content-canary",
            "data": {
                "user_id": "breadcrumb-user-canary",
                "query": "breadcrumb-query-canary",
                "code.function.name": "process_message",
                "status_code": 200,
            },
        },
        {},
    )

    assert breadcrumb["message"] == "[breadcrumb 正文已隐藏，字符数=25]"
    assert breadcrumb["data"] == {
        "code.function.name": "process_message",
        "status_code": 200,
    }
    assert "breadcrumb-content-canary" not in str(breadcrumb)
    assert "breadcrumb-user-canary" not in str(breadcrumb)
    assert "breadcrumb-query-canary" not in str(breadcrumb)


def test_sentry_before_send_log_hides_body_and_interpolation_parameters() -> None:
    sanitized = sentry_before_send_log(
        {
            "severity_text": "WARN",
            "severity_number": 13,
            "body": "log-body-canary",
            "attributes": {
                "sentry.message.parameter.0": "log-argument-canary",
                "logger.name": "komari.test",
                "code.line.number": 42,
            },
        },
        {},
    )

    assert sanitized["body"] == "[日志正文已隐藏，字符数=15]"
    assert sanitized["attributes"] == {
        "logger.name": "komari.test",
        "code.line.number": 42,
    }
    assert "log-body-canary" not in str(sanitized)
    assert "log-argument-canary" not in str(sanitized)


def test_sentry_before_send_removes_business_content_and_user_context() -> None:
    event = {
        "message": "event-message-canary",
        "logentry": {
            "message": "logentry-message-canary",
            "params": ["logentry-param-canary"],
        },
        "exception": {
            "values": [
                {
                    "type": "RuntimeError",
                    "value": "exception-value-canary",
                    "stacktrace": {
                        "frames": [
                            {
                                "function": "process_message",
                                "vars": {"content": "frame-variable-canary"},
                            }
                        ]
                    },
                }
            ]
        },
        "threads": {
            "values": [
                {
                    "stacktrace": {
                        "frames": [{"vars": {"query": "thread-variable-canary"}}]
                    }
                }
            ]
        },
        "request": {
            "method": "post",
            "url": "https://example.invalid/user/request-url-canary",
            "query_string": "request-query-canary",
            "data": "request-data-canary",
        },
        "breadcrumbs": {
            "values": [
                {
                    "message": "event-breadcrumb-canary",
                    "data": {"content": "event-breadcrumb-data-canary"},
                }
            ]
        },
        "contexts": {
            "trace": {"trace_id": "safe-trace-id"},
            "response": {"data": "response-context-canary"},
        },
        "tags": {
            "component": "chat",
            "user_id": "tag-user-canary",
            "topic": "tag-topic-canary",
        },
        "user": {"id": "event-user-canary"},
        "extra": {"prompt": "event-extra-canary"},
        "server_name": "server-name-canary",
    }

    sanitized = sentry_before_send(event, {})

    assert sanitized is not None
    assert sanitized is event
    assert sanitized["request"] == {"method": "POST"}
    assert sanitized["tags"] == {"component": "chat"}
    assert sanitized["contexts"] == {"trace": {"trace_id": "safe-trace-id"}}
    assert "user" not in sanitized
    assert "extra" not in sanitized
    exception_frame = sanitized["exception"]["values"][0]["stacktrace"]["frames"][0]
    thread_frame = sanitized["threads"]["values"][0]["stacktrace"]["frames"][0]
    assert "vars" not in exception_frame
    assert "vars" not in thread_frame

    serialized = str(sanitized)
    for canary in (
        "event-message-canary",
        "logentry-message-canary",
        "logentry-param-canary",
        "exception-value-canary",
        "frame-variable-canary",
        "thread-variable-canary",
        "request-url-canary",
        "request-query-canary",
        "request-data-canary",
        "event-breadcrumb-canary",
        "event-breadcrumb-data-canary",
        "response-context-canary",
        "tag-user-canary",
        "tag-topic-canary",
        "event-user-canary",
        "event-extra-canary",
        "server-name-canary",
    ):
        assert canary not in serialized


def test_sentry_before_send_keeps_explicit_user_context_when_pii_is_enabled() -> None:
    event = {"user": {"id": "explicit-user"}}

    assert sentry_before_send(event, {}, allow_user_context=True) == event


def test_sentry_transaction_hook_hides_transaction_spans_and_request_content() -> None:
    event = {
        "transaction": "GET /users/transaction-name-canary",
        "transaction_info": {"source": "route"},
        "request": {
            "method": "get",
            "url": "https://example.invalid/request-url-canary",
            "query_string": "request-query-canary",
            "headers": {"authorization": "request-header-canary"},
        },
        "spans": [
            {
                "trace_id": "safe-trace-id",
                "span_id": "safe-span-id",
                "op": "http.client",
                "description": "SELECT span-description-canary",
                "data": {
                    "http.method": "POST",
                    "http.response.status_code": 200,
                    "db.statement": "span-data-canary",
                    "http.request.header.authorization": "span-header-canary",
                },
                "tags": {
                    "component": "database",
                    "user_id": "span-tag-canary",
                },
            }
        ],
        "measurements": {"measurement-canary": {"value": 1.0}},
        "breadcrumbs": {
            "values": [{"message": "transaction-breadcrumb-canary"}]
        },
    }

    sanitized = sentry_before_send_transaction(event, {})

    assert sanitized["request"] == {"method": "GET"}
    assert sanitized["transaction_info"] == {"source": "route"}
    assert "measurements" not in sanitized
    assert sanitized["spans"][0]["data"] == {
        "http.method": "POST",
        "http.response.status_code": 200,
    }
    assert sanitized["spans"][0]["tags"] == {"component": "database"}
    serialized = str(sanitized)
    for canary in (
        "transaction-name-canary",
        "request-url-canary",
        "request-query-canary",
        "request-header-canary",
        "span-description-canary",
        "span-data-canary",
        "span-header-canary",
        "span-tag-canary",
        "measurement-canary",
        "transaction-breadcrumb-canary",
    ):
        assert canary not in serialized


def test_ensure_sentry_privacy_hooks_composes_external_hooks_and_is_idempotent() -> (
    None
):
    external_calls: list[str] = []

    def _external_hook(
        payload: dict[str, Any],
        _hint: dict[str, Any],
    ) -> dict[str, Any]:
        external_calls.append("called")
        payload["extra"] = {"secret": "external-hook-canary"}
        return payload

    client = SimpleNamespace(
        options={
            "before_send": _external_hook,
            "before_breadcrumb": _external_hook,
            "before_send_transaction": _external_hook,
            "before_send_log": _external_hook,
            "ignore_errors": [ValueError],
        }
    )

    ensure_sentry_privacy_hooks(client, allow_user_context=False)
    installed_hooks = {
        name: client.options[name]
        for name in (
            "before_send",
            "before_breadcrumb",
            "before_send_transaction",
            "before_send_log",
        )
    }
    ensure_sentry_privacy_hooks(client, allow_user_context=False)

    assert installed_hooks == {
        name: client.options[name] for name in installed_hooks
    }
    assert ValueError in client.options["ignore_errors"]
    assert set(get_ignored_sentry_exceptions()).issubset(
        client.options["ignore_errors"]
    )

    sanitized_event = client.options["before_send"](
        {"message": "event-canary"},
        {},
    )
    sanitized_breadcrumb = client.options["before_breadcrumb"](
        {"message": "breadcrumb-canary"},
        {},
    )
    sanitized_transaction = client.options["before_send_transaction"](
        {"transaction": "transaction-canary"},
        {},
    )
    sanitized_log = client.options["before_send_log"](
        {"body": "log-canary"},
        {},
    )

    assert external_calls == ["called"] * 4
    assert "external-hook-canary" not in str(sanitized_event)
    assert "breadcrumb-canary" not in str(sanitized_breadcrumb)
    assert "transaction-canary" not in str(sanitized_transaction)
    assert "log-canary" not in str(sanitized_log)


def test_build_sentry_init_options_builds_log_integrations_and_filters() -> None:
    captured_logging_kwargs: dict[str, int] = {}
    captured_loguru_kwargs: dict[str, int] = {}

    def _logging_integration_factory(
        *,
        sentry_logs_level: int,
        level: int,
        event_level: int,
    ) -> dict[str, int]:
        captured_logging_kwargs["sentry_logs_level"] = sentry_logs_level
        captured_logging_kwargs["level"] = level
        captured_logging_kwargs["event_level"] = event_level
        return {
            "sentry_logs_level": sentry_logs_level,
            "level": level,
            "event_level": event_level,
        }

    def _loguru_integration_factory(
        *,
        sentry_logs_level: int,
        level: int,
        event_level: int,
    ) -> dict[str, int]:
        captured_loguru_kwargs["sentry_logs_level"] = sentry_logs_level
        captured_loguru_kwargs["level"] = level
        captured_loguru_kwargs["event_level"] = event_level
        return {
            "sentry_logs_level": sentry_logs_level,
            "level": level,
            "event_level": event_level,
        }

    config = _DummySentryConfig()

    options = build_sentry_init_options(
        config=config,
        dsn="https://example@sentry.invalid/1",
        resolve_level=lambda level_name, default: getattr(logging, level_name, default),
        logging_integration_factory=_logging_integration_factory,
        loguru_integration_factory=_loguru_integration_factory,
        asyncio_integration_factory=lambda: "asyncio",
        fastapi_integration_factory=lambda: "fastapi",
        starlette_integration_factory=lambda: "starlette",
        environ={"ENVIRONMENT": "prod"},
    )

    assert captured_logging_kwargs == {
        "sentry_logs_level": logging.INFO,
        "level": logging.INFO,
        "event_level": logging.ERROR,
    }
    assert captured_loguru_kwargs == {
        "sentry_logs_level": logging.INFO,
        "level": logging.INFO,
        "event_level": logging.ERROR,
    }
    assert options["environment"] == "prod"
    assert options["release"] is None
    assert options["enable_logs"] is True
    before_send = options["before_send"]
    assert isinstance(before_send, partial)
    assert before_send.func is sentry_before_send
    assert before_send.keywords == {"allow_user_context": False}
    assert options["before_breadcrumb"] is sentry_before_breadcrumb
    before_send_transaction = options["before_send_transaction"]
    assert isinstance(before_send_transaction, partial)
    assert before_send_transaction.func is sentry_before_send_transaction
    assert before_send_transaction.keywords == {"allow_user_context": False}
    assert options["before_send_log"] is sentry_before_send_log
    assert options["ignore_errors"] == list(get_ignored_sentry_exceptions())
    assert options["integrations"] == [
        {
            "sentry_logs_level": logging.INFO,
            "level": logging.INFO,
            "event_level": logging.ERROR,
        },
        {
            "sentry_logs_level": logging.INFO,
            "level": logging.INFO,
            "event_level": logging.ERROR,
        },
        "asyncio",
        "fastapi",
        "starlette",
    ]
