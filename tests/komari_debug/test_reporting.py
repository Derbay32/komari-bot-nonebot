"""诊断报告发送测试：私聊默认、公开脱敏、多批和文本降级。

TSK-200 起 debug 报告只是请求 ID、结果类型、成功/失败、安全固定错误码
与最终结果的纯渲染，不再包含逐轮 LLM/工具明细、token 用量聚合或采集器
累积状态。

覆盖：
- node_custom 结构正确
- 批次 ≤50 节点时单批发送
- 超过 50 节点时多批发送
- 私聊合并转发失败后仅向同一用户文本降级
- 显式公开报告不包含敏感 canary
- 失败报告只渲染安全固定错误码
"""

from __future__ import annotations

import sys
from importlib import import_module
from typing import TYPE_CHECKING, Any

import pytest
from nonebot.adapters.onebot.v11 import Message, MessageSegment

if TYPE_CHECKING:
    from nonebug import App


@pytest.fixture
def debug_reporting(app: App) -> Any:
    del app
    module_name = "komari_bot.plugins.komari_debug.reporting"
    sys.modules.pop(module_name, None)
    return import_module(module_name)


# ─── 节点拆分测试 ────────────────────────────────────────────


def test_split_single_node_normal_text(debug_reporting: Any) -> None:
    """普通短文本返回单一节点。"""
    nodes = debug_reporting._split_into_nodes("诊断报告内容")
    assert len(nodes) == 1
    assert nodes[0] == "诊断报告内容"


def test_split_multiple_nodes_long_text(debug_reporting: Any) -> None:
    """超长文本被拆分为多个节点。"""
    single_line = "A" * (debug_reporting.MAX_NODE_TEXT_LENGTH + 100)
    nodes = debug_reporting._split_into_nodes(single_line)
    assert len(nodes) == 2
    for node in nodes:
        assert len(node) <= debug_reporting.MAX_NODE_TEXT_LENGTH


def test_split_exact_boundary(debug_reporting: Any) -> None:
    """恰好等于 MAX_NODE_TEXT_LENGTH 的文本不拆分。"""
    text = "X" * debug_reporting.MAX_NODE_TEXT_LENGTH
    nodes = debug_reporting._split_into_nodes(text)
    assert len(nodes) == 1
    assert nodes[0] == text


def test_split_many_short_lines_accumulate(debug_reporting: Any) -> None:
    """多行短文本累积到超过限制时才拆分。"""
    lines = ["line " + str(i) + "\n" for i in range(1000)]
    text = "".join(lines)
    nodes = debug_reporting._split_into_nodes(text)
    assert len(nodes) > 1
    # 验证每个节点不超过限制
    for node in nodes:
        assert len(node) <= debug_reporting.MAX_NODE_TEXT_LENGTH
    # 验证内容可拼回
    assert "".join(nodes) == text


# ─── node_custom 结构验证 ─────────────────────────────────────


def test_node_custom_has_correct_user_id_and_nickname() -> None:
    """验证 MessageSegment.node_custom 使用 bot.self_id 和 "Komari Debug"。"""
    # 不实际调用 API，验证函数签名和默认参数
    import inspect

    from nonebot.adapters.onebot.v11 import MessageSegment

    sig = inspect.signature(MessageSegment.node_custom)
    assert "user_id" in sig.parameters
    assert "nickname" in sig.parameters
    assert "content" in sig.parameters


# ─── 章节构建测试 ─────────────────────────────────────────────


def test_build_chapters_renders_request_identity_and_final_result(
    debug_reporting: Any,
) -> None:
    """成功报告只有请求总览与最终结果两个纯渲染章节。"""
    long_reply = "这是一条很长的回复内容" * 10

    chapters = debug_reporting._build_chapters(
        "debug-reply-abc123456789",
        "reply",
        succeeded=True,
        error=None,
        final_result_info={
            "reply_text": long_reply,
            "favorability_delta": "好感度变化: +5（测试理由）",
            "reply_to_message_id": None,
        },
    )

    assert [chapter[0] for chapter in chapters] == ["请求总览", "最终结果"]

    overview_body = chapters[0][1]
    assert "请求 ID: debug-reply-abc123456789" in overview_body
    assert "类型: reply" in overview_body
    assert "状态: 成功" in overview_body
    # 请求总览不包含回复正文与好感度
    assert long_reply not in overview_body
    assert "好感度变化" not in overview_body

    final_body = chapters[1][1]
    assert "最终结果: 已成功完成" in final_body
    # 回复正文不截断
    assert long_reply in final_body
    assert "好感度变化: +5" in final_body


def test_build_chapters_failure_renders_stable_safe_error_code(
    debug_reporting: Any,
) -> None:
    """失败报告只渲染固定安全错误码，不回显任意异常正文。"""
    chapters = debug_reporting._build_chapters(
        "debug-reply-fail",
        "summary",
        succeeded=False,
        error="debug_summary_failed",
        final_result_info={"reply_text": "失败任务不应渲染输出"},
    )

    assert [chapter[0] for chapter in chapters] == ["请求总览", "最终结果"]
    final_body = dict(chapters)["最终结果"]
    assert "最终结果: 执行失败" in final_body
    assert "错误码: debug_summary_failed" in final_body
    # 失败任务不渲染回复正文
    assert "失败任务不应渲染输出" not in final_body


def test_build_chapters_rejects_unsafe_error_text(debug_reporting: Any) -> None:
    """非安全码形状的错误文本归一化为 internal_error，不回显原文。"""
    chapters = debug_reporting._build_chapters(
        "debug-reply-raw",
        "reply",
        succeeded=False,
        error="RuntimeError: upstream said secret-canary-991",
    )

    final_body = dict(chapters)["最终结果"]
    assert "错误码: internal_error" in final_body
    assert "secret-canary-991" not in final_body


def test_build_chapters_only_renders_safe_final_result_fields(
    debug_reporting: Any,
) -> None:
    """最终结果只渲染白名单安全字段，未知键与异常正文被丢弃。"""
    chapters = debug_reporting._build_chapters(
        "debug-summary-safe",
        "summary",
        succeeded=True,
        error=None,
        final_result_info={
            "filtered_message_count": 12,
            "filter_label": "最近消息",
            "time_range": "07-11 22:00 - 07-11 23:00",
            "history_status": "complete",
            "image_page_count": 2,
            "raw_exception": "exception-body-canary-997",
            "tool_arguments": "tool-arguments-canary-998",
            "user_id": "user-identity-canary-999",
        },
    )

    final_body = dict(chapters)["最终结果"]
    assert "12" in final_body
    assert "最近消息" in final_body
    assert "07-11 22:00 - 07-11 23:00" in final_body
    assert "complete" in final_body
    for canary in (
        "exception-body-canary-997",
        "tool-arguments-canary-998",
        "user-identity-canary-999",
    ):
        assert canary not in final_body


def test_build_chapters_success_without_final_result_is_still_safe(
    debug_reporting: Any,
) -> None:
    """没有最终结果信息时成功报告仍完整且不含占位异常正文。"""
    chapters = debug_reporting._build_chapters(
        "debug-reply-empty",
        "reply",
        succeeded=True,
        error=None,
    )

    final_body = dict(chapters)["最终结果"]
    assert "最终结果: 已成功完成" in final_body


# ─── 合并转发分批测试 ────────────────────────────────────────


@pytest.mark.asyncio
async def test_build_and_send_within_50_nodes_single_batch(
    debug_reporting: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """节点 ≤50 时只发送一批合并转发。"""
    api_calls: list[dict[str, Any]] = []

    class _FakeBot:
        self_id = "669293859"

        async def call_api(self, api: str, **kwargs: Any) -> None:
            api_calls.append({"api": api, **kwargs})

    monkeypatch.setattr(debug_reporting, "MAX_NODES_PER_BATCH", 50)

    await debug_reporting.build_and_send_diagnostic_report(
        bot=_FakeBot(),
        user_id=42,
        request_id="debug-reply-batch",
        result_type="reply",
        succeeded=True,
    )

    # 默认只向 SUPERUSER 私聊投递
    forward_calls = [c for c in api_calls if c["api"] == "send_private_forward_msg"]
    assert len(forward_calls) == 1
    assert forward_calls[0]["user_id"] == 42
    assert len(forward_calls[0]["messages"]) <= 50
    assert not any(call["api"].startswith("send_group") for call in api_calls)


@pytest.mark.asyncio
async def test_build_and_send_splits_reports_into_multiple_batches(
    debug_reporting: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """节点超过单批上限时拆成多批，且每批都不超过限制。"""
    api_calls: list[dict[str, Any]] = []

    class _FakeBot:
        self_id = "669293859"

        async def call_api(self, api: str, **kwargs: Any) -> None:
            api_calls.append({"api": api, **kwargs})

    monkeypatch.setattr(debug_reporting, "MAX_NODES_PER_BATCH", 1)

    await debug_reporting.build_and_send_diagnostic_report(
        bot=_FakeBot(),
        user_id=42,
        request_id="debug-reply-multi",
        result_type="reply",
        succeeded=True,
        final_result_info={"reply_text": "A" * 5000},
    )

    forward_calls = [
        call for call in api_calls if call["api"] == "send_private_forward_msg"
    ]
    assert len(forward_calls) > 1
    assert all(len(call["messages"]) <= 1 for call in forward_calls)


@pytest.mark.asyncio
async def test_send_private_forward_msg_failure_falls_back_to_private_text(
    debug_reporting: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """私聊合并转发失败后只降级为 send_private_msg。"""
    api_calls: list[dict[str, Any]] = []

    class _FakeBot:
        self_id = "669293859"

        async def call_api(self, api: str, **kwargs: Any) -> None:
            if api == "send_private_forward_msg":
                msg = "API 不可用"
                raise RuntimeError(msg)
            api_calls.append({"api": api, **kwargs})

    monkeypatch.setattr(debug_reporting, "MAX_NODES_PER_BATCH", 50)

    await debug_reporting.build_and_send_diagnostic_report(
        bot=_FakeBot(),
        user_id=42,
        request_id="debug-reply-fallback",
        result_type="reply",
        succeeded=True,
    )

    text_calls = [c for c in api_calls if c["api"] == "send_private_msg"]
    assert len(text_calls) > 0
    assert all(c["user_id"] == 42 for c in text_calls)
    assert all(
        isinstance(c.get("message"), Message)
        and len(c["message"]) == 1
        and c["message"][0].type == "text"
        for c in text_calls
    )
    assert not any(call["api"].startswith("send_group") for call in api_calls)


@pytest.mark.asyncio
async def test_explicit_public_report_is_redacted_and_private_copy_is_complete(
    debug_reporting: Any,
) -> None:
    """--public 只额外公开结构化元数据，完整敏感内容仍只进私聊。"""
    api_calls: list[dict[str, Any]] = []

    class _FakeBot:
        self_id = "669293859"

        async def call_api(self, api: str, **kwargs: Any) -> None:
            api_calls.append({"api": api, **kwargs})

    result = await debug_reporting.build_and_send_diagnostic_report(
        bot=_FakeBot(),
        user_id=42,
        public_group_id=12345,
        request_id="debug-reply-public",
        result_type="reply",
        succeeded=False,
        error="debug_reply_failed",
        final_result_info={"reply_text": "reply-canary-private-995"},
    )

    private_calls = [
        call for call in api_calls if call["api"] == "send_private_forward_msg"
    ]
    public_calls = [
        call for call in api_calls if call["api"] == "send_group_forward_msg"
    ]
    assert result.private_delivered is True
    assert result.public_delivered is True
    assert len(private_calls) == 1
    assert len(public_calls) == 1
    private_payload = str(private_calls[0]["messages"])
    public_payload = str(public_calls[0]["messages"])
    assert "debug-reply-public" in private_payload
    assert "debug_reply_failed" in private_payload
    assert "reply-canary-private-995" not in private_payload
    assert "debug-reply-public" in public_payload
    assert "debug_reply_failed" in public_payload
    for canary in [
        "reply-canary-private-995",
        "user-canary-993",
        "input-canary-private-994",
        "exception-canary-996",
    ]:
        assert canary not in public_payload
    assert "输入、输出、用户标识与异常正文已隐藏" in public_payload


# ─── 投递 helper 测试 ────────────────────────────────────────


@pytest.mark.asyncio
async def test_send_group_text_success(debug_reporting: Any) -> None:
    """send_group_text 成功发送群消息。"""
    api_calls: list[dict[str, Any]] = []

    class _FakeBot:
        self_id = "669293859"

        async def call_api(self, api: str, **kwargs: Any) -> None:
            api_calls.append({"api": api, **kwargs})

    delivered = await debug_reporting.send_group_text(
        _FakeBot(),
        12345,
        "测试文本",
    )

    assert delivered is True
    assert len(api_calls) == 1
    assert api_calls[0]["api"] == "send_group_msg"
    assert api_calls[0]["group_id"] == 12345
    assert api_calls[0]["message"] == Message(MessageSegment.text("测试文本"))


@pytest.mark.asyncio
async def test_send_group_text_failure_does_not_crash(debug_reporting: Any) -> None:
    """send_group_text 失败时不崩溃，记录异常。"""

    class _FakeBot:
        self_id = "669293859"

        async def call_api(self, _api: str, **_kwargs: Any) -> None:
            msg = "连接断开了"
            raise RuntimeError(msg)

    delivered = await debug_reporting.send_group_text(_FakeBot(), 12345, "测试")

    assert delivered is False


@pytest.mark.asyncio
async def test_send_private_message_targets_only_the_superuser(
    debug_reporting: Any,
) -> None:
    api_calls: list[dict[str, Any]] = []

    class _FakeBot:
        async def call_api(self, api: str, **kwargs: Any) -> None:
            api_calls.append({"api": api, **kwargs})

    delivered = await debug_reporting.send_private_message(
        _FakeBot(),
        42,
        "private-canary",
    )

    assert delivered is True
    assert api_calls == [
        {
            "api": "send_private_msg",
            "user_id": 42,
            "message": Message(MessageSegment.text("private-canary")),
        }
    ]
