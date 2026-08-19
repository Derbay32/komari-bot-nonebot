"""TSK-197 验收 7 —— 四种关键配置组合的端到端验收矩阵。

``required``/``prompt_guided``（工具调用约束模式）x ``native``/``delegated``
（图片理解模式）的 2x2 组合，全部经公开 debug 生成 seam
（``MessageHandler.generate_debug_reply``）走真实 ``_generate_reply_core``
与真实回复 Agent 工具循环（``generate_reply_with_tools``），以
``final_response`` 完成回复：

- 所有组合都必须经 ``final_response`` 提交成功（裸文本不算成功）；
- ``read_image`` 工具只在 delegated + 有图时暴露，native 绝不暴露；
- ``required`` 每轮向 chat provider 提交 ``tool_choice="required"``，
  ``prompt_guided`` 完全省略 ``tool_choice``；
- 图片输入：native 全量走多模态下载（进入下载器），delegated 任务起点
  零预下载；
- 同一任务内不因图片模式或约束模式切换模型/槽位（主循环恒为 chat 槽位）。

本文件只驱动公开/已确立的测试 seam（debug 入口 + 真实工具循环），不断言
私有 helper；2x2 组合的完整覆盖补齐了既有单维度测试（test_agent_budget
的约束模式、test_image_understanding_mode 的图片模式）未覆盖的交叉面。
"""

from __future__ import annotations

import asyncio
import importlib
from types import SimpleNamespace
from typing import Any

import pytest

from tests.komari_chat.test_agent_budget import (
    _build_chat_config_stub,
    _completion,
    _final_response_completion,
    _ScriptedProvider,
    _tool_call,
    _wire_handler,
)
from tests.komari_chat.test_image_understanding_mode import _vision_stub

message_handler_module = importlib.import_module(
    "komari_bot.plugins.komari_chat.handlers.message_handler"
)

MATRIX = [
    # (tool_call_mode, image_mode)
    ("required", "native"),
    ("required", "delegated"),
    ("prompt_guided", "native"),
    ("prompt_guided", "delegated"),
]

IMAGE_URL = "https://example.com/matrix/a.png"


def _wire_matrix_handler(
    monkeypatch: pytest.MonkeyPatch,
    config: SimpleNamespace,
    provider: _ScriptedProvider,
) -> tuple[Any, list[list[str]]]:
    """布设真实 debug 生成 seam 的依赖，并补上图片模式所需替身。"""
    handler, _search = _wire_handler(monkeypatch, config, provider)
    monkeypatch.setattr(
        message_handler_module,
        "llm_provider_config_manager",
        SimpleNamespace(get=lambda: _vision_stub()),
    )
    download_batches: list[list[str]] = []

    async def _download_images(
        urls: list[str],
        _policy: object,
    ) -> list[str | None]:
        download_batches.append(list(urls))
        return [f"base64:{url}" for url in urls]

    monkeypatch.setattr(
        message_handler_module,
        "download_images_as_base64_aligned",
        _download_images,
    )
    return handler, download_batches


@pytest.mark.parametrize(("tool_call_mode", "image_mode"), MATRIX)
def test_tool_and_image_mode_matrix(
    monkeypatch: pytest.MonkeyPatch,
    tool_call_mode: str,
    image_mode: str,
) -> None:
    """AC7：四种关键配置组合均经 final_response 提交并遵守模式契约。"""
    expect_read_image = image_mode == "delegated"
    expect_tool_choice = tool_call_mode == "required"
    config = _build_chat_config_stub(
        agent_tool_call_mode=tool_call_mode,
        image_understanding_mode=image_mode,
    )
    provider = _ScriptedProvider(
        [
            _completion(
                _tool_call(
                    "record_favorability_delta",
                    '{"delta":0,"reason":"TSK197矩阵"}',
                    {"delta": 0, "reason": "TSK197矩阵"},
                    call_id="call-favor-matrix",
                )
            ),
            _final_response_completion(content="矩阵测试回复"),
        ]
    )
    handler, download_batches = _wire_matrix_handler(
        monkeypatch, config, provider
    )

    result = asyncio.run(
        handler.generate_debug_reply(
            group_id="group-1",
            user_id="user-1",
            user_nickname="测试用户",
            content="矩阵测试",
            image_urls=[IMAGE_URL],
        )
    )

    # 所有组合都经 final_response 提交成功
    assert result.reply == "矩阵测试回复"

    # 同一任务恰好两轮：record_favorability_delta → final_response
    assert len(provider.completion_calls) == 2
    first = provider.completion_calls[0]

    # 工具调用约束模式契约
    if expect_tool_choice:
        assert first["tool_choice"] == "required", (
            f"{tool_call_mode} 模式必须向 provider 提交 tool_choice"
        )
    else:
        assert "tool_choice" not in first, (
            f"{tool_call_mode} 模式不得提交 tool_choice"
        )

    # 工具集合契约：final_response 恒在；read_image 只在 delegated 暴露
    tool_names = {str(tool["function"]["name"]) for tool in first["tools"]}
    assert "final_response" in tool_names, "final_response 必须是成功唯一出口"
    assert ("read_image" in tool_names) is expect_read_image, (
        f"image_mode={image_mode} 的 read_image 暴露与预期不符"
    )

    # 图片输入契约：native 全量下载，delegated 任务起点零预下载
    if image_mode == "native":
        assert download_batches == [[IMAGE_URL]], (
            "native 模式图片必须进入多模态下载器"
        )
    else:
        assert download_batches == [], "delegated 模式任务起点零预下载"
