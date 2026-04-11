"""LLM Provider API 路由测试。"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from fastapi import FastAPI

from komari_bot.plugins.llm_provider.api import API_PREFIX, register_llm_provider_api

if TYPE_CHECKING:
    from nonebug import App


def _with_query(path: str, **params: object) -> str:
    query = "&".join(
        f"{key}={value}" for key, value in params.items() if value is not None
    )
    return f"{path}?{query}" if query else path


class _FakeReader:
    def __init__(self) -> None:
        self.list_calls: list[dict[str, object]] = []
        self.detail_calls: list[tuple[str, int]] = []

    async def list_logs(self, **kwargs: object) -> tuple[list[dict[str, object]], int]:
        self.list_calls.append(dict(kwargs))
        return (
            [
                {
                    "date": "2026-04-10",
                    "line_number": 1,
                    "timestamp": "2026-04-10T11:00:00+00:00",
                    "method": "generate_text_with_messages",
                    "model": "deepseek-chat",
                    "trace_id": "chat-1",
                    "phase": "reply",
                    "duration_ms": 123.4,
                    "status": "success",
                    "input_preview": "hello",
                    "output_preview": "<content>你好</content>",
                    "error_preview": "",
                }
            ],
            1,
        )

    async def get_log(self, *, date: str, line_number: int) -> dict[str, object] | None:
        self.detail_calls.append((date, line_number))
        if line_number == 99:
            return None
        return {
            "date": date,
            "line_number": line_number,
            "timestamp": "2026-04-10T11:00:00+00:00",
            "method": "generate_text_with_messages",
            "model": "deepseek-chat",
            "trace_id": "chat-1",
            "phase": "reply",
            "duration_ms": 123.4,
            "status": "success",
            "input_preview": "hello",
            "output_preview": "<content>你好</content>",
            "error_preview": "",
            "input": {"trace_id": "chat-1"},
            "output": "<content>你好</content>",
            "error": None,
        }


def _build_app(reader: _FakeReader | None) -> FastAPI:
    api_app = FastAPI()
    register_llm_provider_api(
        api_app,
        api_token="secret-token",
        allowed_origins=["https://ui.example.com"],
        reader_getter=lambda: reader,
    )
    return api_app


@pytest.mark.asyncio
async def test_llm_provider_routes_require_token_and_handle_cors(app: App) -> None:
    async with app.test_server(asgi=_build_app(_FakeReader())) as ctx:
        client = ctx.get_client()
        unauthorized = await client.get(f"{API_PREFIX}/reply-logs")
        assert unauthorized.status_code == 401

        preflight = await client.options(
            f"{API_PREFIX}/reply-logs",
            headers={
                "Origin": "https://ui.example.com",
                "Access-Control-Request-Method": "GET",
            },
        )
        assert preflight.status_code == 200
        assert (
            preflight.headers["access-control-allow-origin"]
            == "https://ui.example.com"
        )


@pytest.mark.asyncio
async def test_llm_provider_routes_support_list_and_detail_filters(app: App) -> None:
    reader = _FakeReader()
    headers = {"Authorization": "Bearer secret-token"}

    async with app.test_server(asgi=_build_app(reader)) as ctx:
        client = ctx.get_client()
        listed = await client.get(
            _with_query(
                f"{API_PREFIX}/reply-logs",
                date="2026-04-10",
                days=3,
                trace_id="chat-1",
                model="deepseek-chat",
                method="generate_text_with_messages",
                status="success",
                limit=5,
                offset=1,
            ),
            headers=headers,
        )
        detail = await client.get(
            f"{API_PREFIX}/reply-logs/2026-04-10/1",
            headers=headers,
        )
        missing = await client.get(
            f"{API_PREFIX}/reply-logs/2026-04-10/99",
            headers=headers,
        )

    assert listed.status_code == 200
    assert listed.json()["items"][0]["trace_id"] == "chat-1"
    assert reader.list_calls == [
        {
            "date": "2026-04-10",
            "days": 3,
            "trace_id": "chat-1",
            "model": "deepseek-chat",
            "method": "generate_text_with_messages",
            "status": "success",
            "limit": 5,
            "offset": 1,
        }
    ]
    assert detail.status_code == 200
    assert detail.json()["output"] == "<content>你好</content>"
    assert missing.status_code == 404


@pytest.mark.asyncio
async def test_llm_provider_routes_return_503_when_reader_unavailable(app: App) -> None:
    async with app.test_server(asgi=_build_app(None)) as ctx:
        client = ctx.get_client()
        response = await client.get(
            f"{API_PREFIX}/reply-logs",
            headers={"Authorization": "Bearer secret-token"},
        )

    assert response.status_code == 503
    assert "读取器未初始化" in response.json()["detail"]


@pytest.mark.asyncio
async def test_llm_provider_routes_report_validation_errors(app: App) -> None:
    async with app.test_server(asgi=_build_app(_FakeReader())) as ctx:
        client = ctx.get_client()
        response = await client.get(
            _with_query(f"{API_PREFIX}/reply-logs", date="bad-date"),
            headers={"Authorization": "Bearer secret-token"},
        )

    assert response.status_code == 422
