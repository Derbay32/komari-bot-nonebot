"""komari_chat 请求模式透传与 continuation 附着验收测试（Ticket 03/07）。"""

from __future__ import annotations

import asyncio
from importlib import import_module
from types import SimpleNamespace
from typing import Any

llm_service_module = import_module("komari_bot.plugins.komari_chat.services.llm_service")
vision_service_module = import_module("komari_bot.plugins.komari_chat.services.vision_service")
query_rewrite_module = import_module(
    "komari_bot.plugins.komari_chat.services.query_rewrite_service"
)
base_client_module = import_module("komari_bot.plugins.llm_provider.base_client")

CONTINUATION_KEY = base_client_module.CONTINUATION_METADATA_KEY


class _RecordingProvider:
    """记录 generate_messages_completion 调用并按队列返回 completion。"""

    def __init__(self) -> None:
        self.completion_calls: list[dict[str, Any]] = []
        self.text_calls: list[dict[str, Any]] = []
        self.completions: list[Any] = []

    async def generate_messages_completion(self, **kwargs: Any) -> Any:
        self.completion_calls.append(kwargs)
        return self.completions.pop(0)

    async def generate_text(self, **kwargs: Any) -> str:
        self.text_calls.append(kwargs)
        return '{"summary": "测试总结内容", "entities": [], "user_interactions": [], "importance": 2}'


def _tool_call(
    name: str,
    arguments: str,
    parsed_arguments: dict[str, Any],
    *,
    call_id: str = "call-1",
) -> Any:
    return base_client_module.LLMToolCallSchema(
        id=call_id,
        type="function",
        function=base_client_module.LLMToolCallFunctionSchema(
            name=name,
            arguments=arguments,
        ),
        raw_arguments=arguments,
        parsed_arguments=parsed_arguments,
    )


def _final_response_completion(content: str = "好呀") -> Any:
    return base_client_module.LLMCompletionResultSchema(
        content="",
        tool_calls=[
            _tool_call(
                "final_response",
                "{}",
                {
                    "content": content,
                    "interaction_history": {
                        "event": "打招呼",
                        "result": "回应",
                        "emotion": "开心",
                    },
                },
            )
        ],
        finish_reason="tool_calls",
    )


def _build_config(**overrides: Any) -> SimpleNamespace:
    values: dict[str, Any] = {
        "llm_model_chat": "chat-model",
        "llm_temperature_chat": 0.7,
        "llm_max_tokens_chat": 1024,
        "llm_thinking_mode_chat": False,
        "llm_reasoning_effort_chat": "",
        "llm_model_summary": "summary-model",
        "llm_temperature_summary": 0.3,
        "llm_max_tokens_summary": 2048,
        "llm_thinking_mode_summary": False,
        "llm_reasoning_effort_summary": "",
        "llm_request_api_chat": "chat_completions",
        "llm_stream_enabled_chat": False,
        "llm_request_api_summary": "chat_completions",
        "llm_stream_enabled_summary": False,
        "bot_nickname": "小鞠",
        # TSK-192：回复 Agent 预算默认值（与配置 Schema 一致）
        "agent_max_rounds": 10,
        "agent_max_tool_calls_per_round": 4,
        "agent_max_total_tool_calls": 20,
        # TSK-193：工具调用约束模式（与配置 Schema 默认值一致；
        # from_config 不再对缺字段回退隐藏默认，fixture 必须显式提供）
        "agent_tool_call_mode": "required",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_tool_loop_passes_chat_slot_request_mode(monkeypatch: Any) -> None:
    """工具循环 chat 槽位显式透传 request_api/stream_enabled。"""
    provider = _RecordingProvider()
    provider.completions = [_final_response_completion()]
    monkeypatch.setattr(llm_service_module, "llm_provider", provider)

    asyncio.run(
        llm_service_module.generate_reply(
            config=_build_config(
                llm_request_api_chat="responses",
                llm_stream_enabled_chat=True,
            ),
            messages=[{"role": "user", "content": "你好"}],
        )
    )

    assert len(provider.completion_calls) == 1
    call = provider.completion_calls[0]
    assert call["request_api"] == "responses"
    assert call["stream_enabled"] is True


def test_tool_loop_uses_vision_slot_request_mode(monkeypatch: Any) -> None:
    """启用 read_image 工具时改用 vision 槽位参数。"""
    provider = _RecordingProvider()
    provider.completions = [_final_response_completion()]
    monkeypatch.setattr(llm_service_module, "llm_provider", provider)

    read_images_calls: list[dict[str, Any]] = []

    async def _fake_read_images(*_args: Any, **kwargs: Any) -> list[str]:
        read_images_calls.append(kwargs)
        return ["描述"]

    monkeypatch.setattr(llm_service_module, "read_images", _fake_read_images)

    asyncio.run(
        llm_service_module.generate_reply_with_tools(
            config=_build_config(
                llm_request_api_chat="chat_completions",
                llm_stream_enabled_chat=False,
            ),
            messages=[{"role": "user", "content": "看图"}],
            tools=[llm_service_module.READ_IMAGE_TOOL],
            base64_images=["data:image/png;base64,AAAA"],
            vision_model="vision-model",
            vision_request_api="responses",
            vision_stream_enabled=True,
        )
    )

    call = provider.completion_calls[0]
    assert call["model"] == "vision-model"
    assert call["request_api"] == "responses"
    assert call["stream_enabled"] is True


def test_tool_loop_read_image_tool_forwards_vision_mode(monkeypatch: Any) -> None:
    """read_image 业务工具把 vision 槽位模式传给视觉服务。"""
    provider = _RecordingProvider()
    provider.completions = [
        base_client_module.LLMCompletionResultSchema(
            content="",
            tool_calls=[_tool_call("read_image", '{"image_index": 0}', {"image_index": 0})],
            finish_reason="tool_calls",
        ),
        _final_response_completion(),
    ]
    monkeypatch.setattr(llm_service_module, "llm_provider", provider)

    read_images_calls: list[dict[str, Any]] = []

    async def _fake_read_images(*_args: Any, **kwargs: Any) -> list[str]:
        read_images_calls.append(kwargs)
        return ["一只猫"]

    monkeypatch.setattr(llm_service_module, "read_images", _fake_read_images)

    asyncio.run(
        llm_service_module.generate_reply_with_tools(
            config=_build_config(),
            messages=[{"role": "user", "content": "看图"}],
            tools=[llm_service_module.READ_IMAGE_TOOL],
            base64_images=["data:image/png;base64,AAAA"],
            vision_model="vision-model",
            vision_request_api="responses",
            vision_stream_enabled=True,
        )
    )

    assert len(read_images_calls) == 1
    assert read_images_calls[0]["request_api"] == "responses"
    assert read_images_calls[0]["stream_enabled"] is True


def test_tool_loop_freezes_request_mode_across_rounds(monkeypatch: Any) -> None:
    """任务内冻结：同一任务多轮循环的模式配置保持一致。"""
    provider = _RecordingProvider()
    provider.completions = [
        base_client_module.LLMCompletionResultSchema(
            content="",
            tool_calls=[_tool_call("search_web", '{"query": "x"}', {"query": "x"})],
            finish_reason="tool_calls",
        ),
        _final_response_completion(),
    ]
    monkeypatch.setattr(llm_service_module, "llm_provider", provider)

    async def _fake_search(*_args: Any, **_kwargs: Any) -> str:
        return "搜索结果"

    monkeypatch.setattr(
        llm_service_module,
        "komari_search",
        SimpleNamespace(search_web=_fake_search),
    )

    config = _build_config(
        llm_request_api_chat="responses",
        llm_stream_enabled_chat=True,
    )
    original_execute = llm_service_module._execute_business_tool

    async def _mutating_execute(**kwargs: Any) -> Any:
        # 模拟任务执行期间运维改了配置
        config.llm_request_api_chat = "chat_completions"
        config.llm_stream_enabled_chat = False
        return await original_execute(**kwargs)

    monkeypatch.setattr(
        llm_service_module, "_execute_business_tool", _mutating_execute
    )

    asyncio.run(
        llm_service_module.generate_reply_with_tools(
            config=config,
            messages=[{"role": "user", "content": "查一下"}],
            tools=[llm_service_module.SEARCH_WEB_TOOL],
        )
    )

    assert len(provider.completion_calls) == 2
    modes = [
        (call["request_api"], call["stream_enabled"])
        for call in provider.completion_calls
    ]
    # 第二轮仍使用任务开始时冻结的 responses 模式
    assert modes == [("responses", True), ("responses", True)]


def test_tool_loop_attaches_continuation_to_assistant_message(monkeypatch: Any) -> None:
    """Ticket 07：工具循环 assistant 消息携带 continuation 内部元数据。"""
    continuation = base_client_module.LLMProviderContinuationSchema(
        api="responses",
        output_items=[{"type": "function_call", "call_id": "call-1"}],
    )
    provider = _RecordingProvider()
    provider.completions = [
        base_client_module.LLMCompletionResultSchema(
            content="",
            tool_calls=[_tool_call("read_image", '{"image_index": 0}', {"image_index": 0})],
            finish_reason="tool_calls",
            continuation=continuation,
        ),
        _final_response_completion(),
    ]
    monkeypatch.setattr(llm_service_module, "llm_provider", provider)

    async def _fake_read_images(*_args: Any, **_kwargs: Any) -> list[str]:
        return ["一只猫"]

    monkeypatch.setattr(llm_service_module, "read_images", _fake_read_images)

    asyncio.run(
        llm_service_module.generate_reply_with_tools(
            config=_build_config(llm_request_api_chat="responses"),
            messages=[{"role": "user", "content": "看图"}],
            tools=[llm_service_module.READ_IMAGE_TOOL],
            base64_images=["data:image/png;base64,AAAA"],
            vision_model="vision-model",
            vision_request_api="responses",
        )
    )

    assert len(provider.completion_calls) == 2
    second_round_messages = provider.completion_calls[1]["messages"]
    assistant_messages = [
        message for message in second_round_messages if message["role"] == "assistant"
    ]
    assert len(assistant_messages) == 1
    assert assistant_messages[0][CONTINUATION_KEY]["api"] == "responses"
    assert assistant_messages[0][CONTINUATION_KEY]["output_items"] == [
        {"type": "function_call", "call_id": "call-1"}
    ]


def test_tool_loop_no_tool_retry_discards_content_and_continuation(
    monkeypatch: Any,
) -> None:
    """TSK-193：无 tool_calls 的 completion 正文与 continuation 不进入下一轮。

    旧行为：正文与续接信息作为 assistant 消息回填并附着 continuation；
    新契约：只保留最后有效上下文 + 代码拥有的短纠错 user 指令。
    """
    continuation = base_client_module.LLMProviderContinuationSchema(
        api="responses",
        output_items=[{"type": "message", "id": "msg_1"}],
    )
    provider = _RecordingProvider()
    provider.completions = [
        base_client_module.LLMCompletionResultSchema(
            content="让我想想",
            tool_calls=[],
            finish_reason="stop",
            continuation=continuation,
        ),
        _final_response_completion(),
    ]
    monkeypatch.setattr(llm_service_module, "llm_provider", provider)

    asyncio.run(
        llm_service_module.generate_reply(
            config=_build_config(llm_request_api_chat="responses"),
            messages=[{"role": "user", "content": "你好"}],
        )
    )

    assert len(provider.completion_calls) == 2
    second_round_messages = provider.completion_calls[1]["messages"]
    assistant_messages = [
        message for message in second_round_messages if message["role"] == "assistant"
    ]
    assert assistant_messages == [], "裸文本轮不得作为 assistant 消息进入下一轮"
    assert not any(
        CONTINUATION_KEY in message for message in second_round_messages
    ), "裸文本 continuation 不得进入下一轮 request messages"
    correction = [
        message
        for message in second_round_messages
        if message.get("role") == "user"
    ]
    assert correction, "裸文本轮后必须追加代码拥有的短纠错 user 指令"
    assert "必须调用" in str(correction[-1].get("content", ""))


def test_tool_loop_empty_content_retry_discards_continuation(
    monkeypatch: Any,
) -> None:
    """TSK-193：空内容的无工具输出携带的 continuation 也不得进入下一轮。"""
    continuation = base_client_module.LLMProviderContinuationSchema(
        api="responses",
        output_items=[{"type": "reasoning", "id": "rs_1"}],
    )
    provider = _RecordingProvider()
    provider.completions = [
        base_client_module.LLMCompletionResultSchema(
            content="",
            tool_calls=[],
            finish_reason="stop",
            continuation=continuation,
        ),
        _final_response_completion(),
    ]
    monkeypatch.setattr(llm_service_module, "llm_provider", provider)

    asyncio.run(
        llm_service_module.generate_reply(
            config=_build_config(llm_request_api_chat="responses"),
            messages=[{"role": "user", "content": "你好"}],
        )
    )

    assert len(provider.completion_calls) == 2
    second_round_messages = provider.completion_calls[1]["messages"]
    assert not any(
        CONTINUATION_KEY in message for message in second_round_messages
    ), "空内容无工具轮的 continuation 不得进入下一轮"


def test_tool_loop_discards_reasoning_content_of_no_tool_completion(
    monkeypatch: Any,
) -> None:
    """TSK-193：无 tool_calls 的 reasoning 正文也不得进入下一轮上下文。"""
    provider = _RecordingProvider()
    provider.completions = [
        base_client_module.LLMCompletionResultSchema(
            content="",
            reasoning_content="秘密推理内容",
            tool_calls=[],
            finish_reason="stop",
        ),
        _final_response_completion(),
    ]
    monkeypatch.setattr(llm_service_module, "llm_provider", provider)

    asyncio.run(
        llm_service_module.generate_reply(
            config=_build_config(),
            messages=[{"role": "user", "content": "你好"}],
        )
    )

    assert len(provider.completion_calls) == 2
    second_round_messages = provider.completion_calls[1]["messages"]
    rendered = str(second_round_messages)
    assert "秘密推理内容" not in rendered, "裸文本轮的 reasoning 不得进入下一轮"


def test_summarize_conversation_passes_summary_slot_mode(monkeypatch: Any) -> None:
    """聊天记忆总结显式透传 summary 槽位模式。"""
    provider = _RecordingProvider()
    monkeypatch.setattr(llm_service_module, "llm_provider", provider)

    message = SimpleNamespace(
        user_id="10001",
        user_nickname="阿明",
        group_id="114514",
        content="周末一起吃拉面吧",
        is_bot=False,
        message_id="1",
        timestamp=1.0,
    )

    asyncio.run(
        llm_service_module.summarize_conversation(
            messages=[message],
            config=_build_config(
                llm_request_api_summary="responses",
                llm_stream_enabled_summary=True,
            ),
        )
    )

    assert len(provider.text_calls) == 1
    call = provider.text_calls[0]
    assert call["request_api"] == "responses"
    assert call["stream_enabled"] is True


def test_query_rewrite_passes_summary_slot_mode(monkeypatch: Any) -> None:
    """查询重写使用 summary 槽位模式。"""
    captured: list[dict[str, Any]] = []

    class _Provider:
        async def generate_completion(self, **kwargs: Any) -> SimpleNamespace:
            captured.append(kwargs)
            return SimpleNamespace(content="重写后的查询", reasoning_content=None)

    monkeypatch.setattr(query_rewrite_module, "llm_provider", _Provider())
    monkeypatch.setattr(
        query_rewrite_module,
        "get_memory_config",
        lambda: SimpleNamespace(
            llm_model_summary="summary-model",
            llm_thinking_mode_summary=False,
            llm_reasoning_effort_summary="",
            llm_request_api_summary="responses",
            llm_stream_enabled_summary=True,
        ),
    )

    service = query_rewrite_module.QueryRewriteService()
    result = asyncio.run(service.rewrite_query("最近那个新番怎么样"))

    assert result == "重写后的查询"
    assert len(captured) == 1
    assert captured[0]["request_api"] == "responses"
    assert captured[0]["stream_enabled"] is True


def test_vision_service_passes_vision_slot_mode(monkeypatch: Any) -> None:
    """视觉服务单图读取显式透传 vision 槽位模式。"""
    captured: list[dict[str, Any]] = []

    class _Provider:
        async def generate_messages_completion(self, **kwargs: Any) -> SimpleNamespace:
            captured.append(kwargs)
            return SimpleNamespace(
                content="一只猫",
                tool_calls=[],
                finish_reason="stop",
                usage=None,
                duration_ms=1.0,
                continuation=None,
            )

    monkeypatch.setattr(vision_service_module, "llm_provider", _Provider())
    monkeypatch.setattr(
        vision_service_module,
        "llm_provider_config_manager",
        SimpleNamespace(get=lambda: SimpleNamespace(api_token="token")),
    )

    # TSK-190：视觉描述 Prompt 经 chat Prompt 公开 loader seam 注入非空
    # 测试值，无 DB 冷启动不再触碰 PostgreSQL。兼容 vision_service 本地
    # get_template 绑定与 ``prompt_template.get_template`` 属性访问两种风格。
    prompt_template_module = import_module(
        "komari_bot.plugins.komari_chat.services.prompt_template"
    )

    async def _fake_vision_description_template() -> dict[str, str]:
        return {"vision_description_prompt": "TEST-VISION-DESCRIPTION-PROMPT"}

    monkeypatch.setattr(
        prompt_template_module,
        "get_template",
        _fake_vision_description_template,
    )
    if hasattr(vision_service_module, "get_template"):
        monkeypatch.setattr(
            vision_service_module,
            "get_template",
            _fake_vision_description_template,
        )

    descriptions = asyncio.run(
        vision_service_module.read_images(
            ["data:image/png;base64,AAAA"],
            vision_model="vision-model",
            request_api="responses",
            stream_enabled=True,
        )
    )

    assert descriptions == ["一只猫"]
    assert len(captured) == 1
    assert captured[0]["request_api"] == "responses"
    assert captured[0]["stream_enabled"] is True
    content = captured[0]["messages"][0]["content"]
    assert content[0]["type"] == "text"
    assert content[0]["text"] == "TEST-VISION-DESCRIPTION-PROMPT"
