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
- 图片输入：native 全量走多模态下载并作为 ``image_url`` 部件交给主
  provider（工具集不暴露 ``read_image``）；delegated 任务起点零预下载，
  scripted provider 真实提出 ``read_image(image_index=0)``，经真实工具循环
  与真实任务级图片会话执行——安全下载恰好一次、视觉子调用恰好一次、工具
  结果以 ``vision`` 不可信上下文回流主循环、Agent Run 记录 read_image
  成功 trace——再继续 ``record_favorability_delta`` 与 ``final_response``；
- 同一任务内不因图片模式或约束模式切换模型/槽位（主循环恒为 chat 槽位）。

本文件只驱动公开/已确立的测试 seam（debug 入口 + 真实工具循环 + 稳定
网络/视觉边界替身），不断言私有 helper；2x2 组合的完整覆盖补齐了既有单
维度测试（test_agent_budget 的约束模式、test_image_understanding_mode
的图片模式）未覆盖的交叉面。
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

image_downloader_module = importlib.import_module(
    "komari_bot.plugins.komari_chat.services.image_downloader"
)
image_reading_session_module = importlib.import_module(
    "komari_bot.plugins.komari_chat.services.image_reading_session"
)
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
VISION_DESCRIPTION = "这是一张测试图片的视觉描述"
#: 与来源 URL 完全无关的固定安全图片 data URI（1x1 PNG 测试 payload）。
#: native/delegated 下载替身统一返回它；旧实现把原始 URL 直接拼进
#: "base64:" 前缀，把来源 URL 泄漏进所谓安全图片数据，与既定安全边界相反。
SAFE_IMAGE_DATA_URI = (
    "data:image/png;base64,"
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="
)


def _matrix_build_prompt(image_mode: str) -> Any:
    """native 把 base64 图片嵌入多模态 (user) 消息；delegated 只回纯文本。"""

    async def _build_prompt(**kwargs: object) -> list[dict[str, object]]:
        if image_mode == "native":
            raw_urls = kwargs.get("image_urls")
            raw_reply_urls = kwargs.get("reply_image_urls")
            image_urls: list[str] = []
            if isinstance(raw_urls, list):
                image_urls.extend(str(url) for url in raw_urls)
            if isinstance(raw_reply_urls, list):
                image_urls.extend(str(url) for url in raw_reply_urls)
            content: list[dict[str, object]] = [
                {"type": "text", "text": "矩阵测试"}
            ]
            content.extend(
                {"type": "image_url", "image_url": {"url": url}}
                for url in image_urls
            )
            return [{"role": "user", "content": content}]
        return [{"role": "user", "content": "矩阵测试"}]

    return _build_prompt


def _wire_matrix_handler(
    monkeypatch: pytest.MonkeyPatch,
    config: SimpleNamespace,
    provider: _ScriptedProvider,
    *,
    image_mode: str,
) -> tuple[
    Any,
    list[list[str]],
    list[str],
    list[tuple[list[str], dict[str, Any]]],
]:
    """布设真实 debug 生成 seam 的依赖，并补上图片模式所需稳定边界替身。

    Returns:
        (handler, native_download_batches, delegated_downloads, vision_calls)
    """
    handler, _search = _wire_handler(monkeypatch, config, provider)
    monkeypatch.setattr(
        message_handler_module,
        "llm_provider_config_manager",
        SimpleNamespace(get=lambda: _vision_stub()),
    )
    monkeypatch.setattr(
        message_handler_module,
        "build_prompt",
        _matrix_build_prompt(image_mode),
    )
    # native 批量下载边界（任务起点全量下载，交给多模态输入）
    native_download_batches: list[list[str]] = []

    async def _download_images(
        urls: list[str],
        _policy: object,
    ) -> list[str | None]:
        native_download_batches.append(list(urls))
        return [SAFE_IMAGE_DATA_URI for _ in urls]

    monkeypatch.setattr(
        message_handler_module,
        "download_images_as_base64_aligned",
        _download_images,
    )
    # delegated 懒下载边界（真实 ImageReadingSession 内部的安全下载器；
    # 替换类方法后经实例访问会绑定 self，签名需保留 self）
    delegated_downloads: list[str] = []

    async def _delegated_download(self: object, url: str) -> str | None:
        del self
        delegated_downloads.append(url)
        return SAFE_IMAGE_DATA_URI

    monkeypatch.setattr(
        image_downloader_module.ImageDownloadSession,
        "download",
        _delegated_download,
    )
    # delegated 视觉子调用边界（真实会话 read() → vision_service.read_images）
    vision_calls: list[tuple[list[str], dict[str, Any]]] = []

    async def _fake_read_images(
        base64_images: list[str],
        **kwargs: object,
    ) -> list[str]:
        vision_calls.append((list(base64_images), dict(kwargs)))
        return [VISION_DESCRIPTION]

    monkeypatch.setattr(
        image_reading_session_module,
        "read_images",
        _fake_read_images,
    )
    return handler, native_download_batches, delegated_downloads, vision_calls


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
    if expect_read_image:
        # delegated：模型先真实提出 read_image(image_index=0)，经工具循环
        # 执行图片会话后再继续好感度与最终回复。
        steps = [
            _completion(
                _tool_call(
                    "read_image",
                    '{"image_index":0}',
                    {"image_index": 0},
                    call_id="call-image-matrix",
                )
            ),
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
    else:
        steps = [
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
    provider = _ScriptedProvider(steps)
    handler, native_download_batches, delegated_downloads, vision_calls = (
        _wire_matrix_handler(
            monkeypatch,
            config,
            provider,
            image_mode=image_mode,
        )
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

    # 主循环轮次：delegated 多一轮 read_image；native 只有好感度→最终回复
    expected_rounds = 3 if expect_read_image else 2
    assert len(provider.completion_calls) == expected_rounds, (
        f"image_mode={image_mode} 主循环轮次不符"
    )

    # 工具调用约束模式契约（每一轮都成立）
    for call in provider.completion_calls:
        if expect_tool_choice:
            assert call["tool_choice"] == "required", (
                f"{tool_call_mode} 模式必须向 provider 提交 tool_choice"
            )
        else:
            assert "tool_choice" not in call, (
                f"{tool_call_mode} 模式不得提交 tool_choice"
            )

    first = provider.completion_calls[0]
    tool_names = {str(tool["function"]["name"]) for tool in first["tools"]}
    assert "final_response" in tool_names, "final_response 必须是成功唯一出口"
    assert ("read_image" in tool_names) is expect_read_image, (
        f"image_mode={image_mode} 的 read_image 暴露与预期不符"
    )

    if image_mode == "native":
        # native：图片作为多模态输入交给主 provider，工具集不暴露 read_image
        assert native_download_batches == [[IMAGE_URL]], (
            "native 模式图片必须进入多模态下载器"
        )
        assert delegated_downloads == [], "native 不得触达 delegated 懒下载器"
        assert vision_calls == [], "native 不得调用视觉子服务"

        user_message = first["messages"][0]
        parts = user_message["content"]
        assert isinstance(parts, list), (
            "native 带图请求的 user 消息必须为多模态部件数组"
        )
        image_parts = [
            part for part in parts if part.get("type") == "image_url"
        ]
        assert image_parts, "主 provider 必须收到 image_url 多模态部件"
        assert [part["image_url"]["url"] for part in image_parts] == [
            SAFE_IMAGE_DATA_URI
        ], "主 provider 收到的图片必须精确等于安全 data URI（下载器产物，与来源 URL 无关）"

        # 扫描 native 全部轮次 provider messages：原始 URL 不得出现在任何
        # 消息中（主 provider 只应收到与来源 URL 无关的安全 data URI）
        native_rendered = "\n".join(
            str(message)
            for call in provider.completion_calls
            for message in call["messages"]
        )
        assert IMAGE_URL not in native_rendered, (
            "native 主循环 messages 不得泄漏原始 URL"
        )
    else:
        # delegated：read_image 经真实工具循环与真实图片会话执行
        assert native_download_batches == [], (
            "delegated 任务起点零预下载（原生批量下载器不触达）"
        )
        assert delegated_downloads == [IMAGE_URL], (
            "read_image 必须经安全下载器恰好下载一次原始图片"
        )
        assert len(vision_calls) == 1, "read_image 必须触发恰好一次视觉子调用"
        assert vision_calls[0][0] == [SAFE_IMAGE_DATA_URI], (
            "视觉子调用必须收到安全 data URI 输入（下载器产物，与来源 URL 无关）"
        )

        # 主循环 messages 不得出现原始 URL / base64 / 安全 data URI
        # （TSK-195 稳定索引边界：主 Agent 只能看稳定索引与视觉描述）。
        # 扫描全部轮次（不能只扫 first call），防止后续轮次泄漏。
        all_rendered = "\n".join(
            str(message)
            for call in provider.completion_calls
            for message in call["messages"]
        )
        assert IMAGE_URL not in all_rendered, "delegated 主循环不得泄漏原始 URL"
        assert "base64:" not in all_rendered, "delegated 主循环不得泄漏 base64"
        assert SAFE_IMAGE_DATA_URI not in all_rendered, (
            "delegated 主循环不得出现安全图片 data URI（只能看稳定索引与视觉描述）"
        )

        # read_image 工具结果以 vision 不可信上下文回流主循环
        second = provider.completion_calls[1]
        tool_messages = [
            message
            for message in second["messages"]
            if message.get("role") == "tool"
        ]
        vision_results = [
            message
            for message in tool_messages
            if 'source_type="vision"' in str(message.get("content", ""))
        ]
        assert len(vision_results) == 1, (
            "read_image 工具结果必须以 vision 来源回流主循环"
        )
        assert VISION_DESCRIPTION in str(
            vision_results[0].get("content", "")
        ), "工具结果必须携带视觉描述正文"

        # Agent Run 工具 trace 保留 read_image 成功执行记录
        record = result.collector.build_record()
        read_image_traces = [
            trace
            for trace in record["tool_executions"]
            if trace["tool_name"] == "read_image"
        ]
        assert len(read_image_traces) == 1, (
            "Agent Run 必须记录 read_image 工具执行 trace"
        )
        assert read_image_traces[0]["status"] == "success"
        assert VISION_DESCRIPTION in str(read_image_traces[0]["result"])
