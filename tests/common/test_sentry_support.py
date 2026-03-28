"""Sentry 辅助函数测试。"""

from __future__ import annotations

import logging
from dataclasses import dataclass
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
    get_ignored_sentry_exceptions,
    sentry_before_send,
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


def test_build_sentry_init_options_builds_logging_integration_and_filters() -> None:
    captured_logging_kwargs: dict[str, int] = {}

    def _logging_integration_factory(*, level: int, event_level: int) -> dict[str, int]:
        captured_logging_kwargs["level"] = level
        captured_logging_kwargs["event_level"] = event_level
        return {"level": level, "event_level": event_level}

    config = _DummySentryConfig()

    options = build_sentry_init_options(
        config=config,
        dsn="https://example@sentry.invalid/1",
        resolve_level=lambda level_name, default: getattr(logging, level_name, default),
        logging_integration_factory=_logging_integration_factory,
        asyncio_integration_factory=lambda: "asyncio",
        fastapi_integration_factory=lambda: "fastapi",
        starlette_integration_factory=lambda: "starlette",
        environ={"ENVIRONMENT": "prod"},
    )

    assert captured_logging_kwargs == {
        "level": logging.INFO,
        "event_level": logging.ERROR,
    }
    assert options["environment"] == "prod"
    assert options["release"] is None
    assert options["before_send"] is sentry_before_send
    assert options["ignore_errors"] == list(get_ignored_sentry_exceptions())
    assert options["integrations"] == [
        {"level": logging.INFO, "event_level": logging.ERROR},
        "asyncio",
        "fastapi",
        "starlette",
    ]
