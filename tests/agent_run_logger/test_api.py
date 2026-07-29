"""Agent Run 日志管理 API 测试。"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

import pytest
from fastapi import FastAPI

from komari_bot.plugins.agent_run_logger.api import (
    API_PREFIX,
    register_agent_run_log_api,
)

if TYPE_CHECKING:
    from nonebug import App


class _FakeReader:
    def __init__(self) -> None:
        self.list_calls: list[dict[str, object]] = []

    async def list_runs(self, **kwargs: object) -> tuple[list[dict[str, Any]], int]:
        self.list_calls.append(dict(kwargs))
        return (
            [
                {
                    "run_id": "run-1",
                    "trace_id": "trace-1",
                    "date": "2026-07-22",
                    "run_type": "chat_reply",
                    "task_kind": "chat_reply",
                    "origin": "normal",
                    "status": "success",
                    "started_at": "2026-07-22T10:00:00+08:00",
                    "finished_at": "2026-07-22T10:00:01+08:00",
                    "duration_ms": 1000.0,
                    "models": ["deepseek-chat"],
                    "methods": ["generate_messages_completion"],
                    "round_count": 2,
                    "tool_count": 1,
                    "usage": {"total_tokens": 20},
                    "input_preview": "完整输入预览",
                    "output_preview": "完整输出预览",
                    "error_preview": "",
                }
            ],
            1,
        )

    async def get_run(self, run_id: str) -> dict[str, Any] | None:
        if run_id == "missing":
            return None
        return {
            "schema_version": 3,
            "run_id": run_id,
            "trace_id": "trace-1",
            "run_type": "chat_reply",
            "task_kind": "chat_reply",
            "origin": "normal",
            "status": "success",
            "started_at": "2026-07-22T10:00:00+08:00",
            "finished_at": "2026-07-22T10:00:01+08:00",
            "duration_ms": 1000.0,
            "input": {"message": "完整输入"},
            "output": {"reply": "完整输出"},
            "rounds": [{"response": {"reasoning_content": "完整思考"}}],
            "tool_executions": [],
            "errors": [],
            "models": ["deepseek-chat"],
            "methods": ["generate_messages_completion"],
            "usage": {"total_tokens": 20},
        }

def _build_app(reader: _FakeReader | None) -> FastAPI:
    api_app = FastAPI()
    register_agent_run_log_api(
        api_app,
        api_token=[
            {
                "credential_id": "agent-run-reader",
                "token": "secret-token-00000000",
                "permissions": ["agent_run_logs:read"],
            }
        ],
        allowed_origins=["https://ui.example.com"],
        reader_getter=lambda: reader,
    )
    return api_app


@pytest.mark.asyncio
async def test_routes_require_permission_token_and_support_cors(app: App) -> None:
    async with app.test_server(asgi=cast("Any", _build_app(_FakeReader()))) as ctx:
        client = ctx.get_client()
        unauthorized = await client.get(f"{API_PREFIX}/runs")
        preflight = await client.options(
            f"{API_PREFIX}/runs",
            headers={
                "Origin": "https://ui.example.com",
                "Access-Control-Request-Method": "GET",
            },
        )

    assert unauthorized.status_code == 401
    assert preflight.status_code == 200
    assert preflight.headers["access-control-allow-origin"] == (
        "https://ui.example.com"
    )


@pytest.mark.asyncio
async def test_canonical_routes_forward_combined_filters_and_return_full_detail(
    app: App,
) -> None:
    reader = _FakeReader()
    headers = {"Authorization": "Bearer secret-token-00000000"}
    path = (
        f"{API_PREFIX}/runs?date=2026-07-22&days=3&run_type=chat_reply"
        "&task_kind=chat_reply&origin=normal&trace_id=trace-1&status=success"
        "&model=deepseek-chat&method=generate_messages_completion&limit=5&offset=1"
    )
    async with app.test_server(asgi=cast("Any", _build_app(reader))) as ctx:
        client = ctx.get_client()
        listed = await client.get(path, headers=headers)
        detail = await client.get(f"{API_PREFIX}/runs/run-1", headers=headers)
        missing = await client.get(f"{API_PREFIX}/runs/missing", headers=headers)

    assert listed.status_code == 200
    assert listed.json()["items"][0]["run_id"] == "run-1"
    assert reader.list_calls == [
        {
            "log_date": "2026-07-22",
            "days": 3,
            "run_type": "chat_reply",
            "task_kind": "chat_reply",
            "origin": "normal",
            "trace_id": "trace-1",
            "status": "success",
            "model": "deepseek-chat",
            "method": "generate_messages_completion",
            "limit": 5,
            "offset": 1,
        }
    ]
    assert detail.status_code == 200
    assert detail.json()["rounds"][0]["response"]["reasoning_content"] == (
        "完整思考"
    )
    assert missing.status_code == 404


@pytest.mark.asyncio
async def test_routes_return_503_without_reader(app: App) -> None:
    async with app.test_server(asgi=cast("Any", _build_app(None))) as ctx:
        response = await ctx.get_client().get(
            f"{API_PREFIX}/runs",
            headers={"Authorization": "Bearer secret-token-00000000"},
        )
    assert response.status_code == 503
