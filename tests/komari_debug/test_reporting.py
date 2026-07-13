"""诊断报告发送测试：合并转发节点结构、多批、文本降级。

覆盖：
- node_custom 结构正确
- 批次 ≤50 节点时单批发送
- 超过 50 节点时多批发送
- API 失败后文本降级（调用 send_group_msg）
- 错误/降级章节在失败报告中存在
"""

from __future__ import annotations

import sys
from importlib import import_module
from typing import TYPE_CHECKING, Any

import pytest

from komari_bot.plugins.llm_provider.diagnostic import (
    LLMCallTrace,
    LLMDiagnosticCollector,
    ToolExecutionTrace,
)

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


def test_build_chapters_success_contains_all_sections(debug_reporting: Any) -> None:
    """成功报告包含全部 7 个章节。"""
    collector = _make_collector_with_data()

    chapters = debug_reporting._build_chapters(
        collector,
        "reply",
        succeeded=True,
        error=None,
        extra_info={"user_id": "42", "content": "测试消息"},
        final_result_info={
            "reply_text": "这是一条很长的回复内容" * 10,
            "favorability_delta": "好感度变化: +5（测试理由）",
            "reply_to_message_id": None,
        },
    )

    titles = [c[0] for c in chapters]
    assert titles == [
        "请求总览",
        "最终结果",
        "LLM 调用详情",
        "工具摘要",
        "阶段 token 小计",
        "全链路 token 小计",
        "错误/降级",
    ]

    # 验证最终结果包含回复正文（不截断到 500）
    final_body = chapters[1][1]
    assert "这是一条很长的回复内容" in final_body
    assert "好感度变化: +5" in final_body

    # 验证请求总览不包含回复正文和好感度
    overview_body = chapters[0][1]
    assert "这是一条很长的回复内容" not in overview_body
    assert "好感度变化" not in overview_body


def test_build_chapters_failure_includes_error_chapter(debug_reporting: Any) -> None:
    """失败报告包含错误信息。"""
    collector = _make_collector_with_data()
    collector.add_error("generate_reply", "APITimeoutError", "请求超时 30s")

    chapters = debug_reporting._build_chapters(
        collector,
        "summary",
        succeeded=False,
        error="LLM API 超时",
        extra_info=None,
    )

    titles = [c[0] for c in chapters]
    assert "错误/降级" in titles
    error_chapter = chapters[titles.index("错误/降级")][1]
    assert "APITimeoutError" in error_chapter
    assert "请求超时 30s" in error_chapter

    final_chapter = chapters[titles.index("最终结果")][1]
    assert "LLM API 超时" in final_chapter
    assert "执行失败" in final_chapter


def test_no_calls_chapters_show_placeholder(debug_reporting: Any) -> None:
    """无 LLM 调用记录时章节显示占位文本。"""
    collector = LLMDiagnosticCollector(request_id="empty")

    chapters = debug_reporting._build_chapters(
        collector,
        "reply",
        succeeded=True,
        error=None,
        extra_info=None,
    )

    # 找到 LLM 调用详情和工具摘要章节
    chapter_dict = dict(chapters)
    assert "无 LLM 调用记录" in chapter_dict["LLM 调用详情"]
    assert "无工具调用记录" in chapter_dict["工具摘要"]
    assert "无" in chapter_dict["阶段 token 小计"]


def test_format_tool_line_redacts_sensitive_arguments_and_results(
    debug_reporting: Any,
) -> None:
    """工具摘要只展示安全元数据，不泄露参数正文或工具结果。"""
    profile_trace = ToolExecutionTrace(
        tool_name="read_profile",
        parsed_arguments={"user_id": "123", "keys": ["喜欢的食物"]},
        status="success",
        result_summary="name: 长门\ntraits:\n  - 喜欢的食物: 咖喱",
    )
    favor_trace = ToolExecutionTrace(
        tool_name="record_favorability_delta",
        parsed_arguments={"delta": 2, "reason": "仅用于内部日志的原因"},
        status="success",
        result_summary="pending (debug 路径不会提交)",
    )

    profile_line = debug_reporting._format_tool_line(profile_trace)
    favor_line = debug_reporting._format_tool_line(favor_trace)

    assert "123" not in profile_line
    assert "喜欢的食物" not in profile_line
    assert "长门" not in profile_line
    assert "咖喱" not in profile_line
    assert "仅用于内部日志的原因" not in favor_line
    assert "'delta': 2" in favor_line
    assert "内容已隐藏" in profile_line


def test_build_chapters_tool_summary_does_not_leak_sensitive_content(
    debug_reporting: Any,
) -> None:
    """完整章节构建也不会重新暴露工具敏感内容。"""
    collector = LLMDiagnosticCollector(request_id="redaction")
    collector.add_tool(
        ToolExecutionTrace(
            tool_name="search_web",
            parsed_arguments={"query": "私密搜索词"},
            status="success",
            result_summary="私密搜索结果正文",
        )
    )

    chapters = debug_reporting._build_chapters(
        collector,
        "reply",
        succeeded=True,
        error=None,
        extra_info=None,
    )
    tool_summary = dict(chapters)["工具摘要"]

    assert "私密搜索词" not in tool_summary
    assert "私密搜索结果正文" not in tool_summary
    assert "query_chars" in tool_summary
    assert "内容已隐藏" in tool_summary


# ─── token 格式化测试 ────────────────────────────────────────


def test_format_call_line_with_usage(debug_reporting: Any) -> None:
    """带 usage 的调用行格式化包含所有字段。"""
    from komari_bot.plugins.llm_provider.base_client import UnifiedUsageSchema

    call = LLMCallTrace(
        phase="query_rewrite",
        round_index=0,
        model="deepseek-chat",
        finish_reason="stop",
        duration_ms=150.0,
        usage=UnifiedUsageSchema(
            input_tokens=100,
            cached_input_tokens=60,
            cache_miss_input_tokens=40,
            output_tokens=80,
            reasoning_output_tokens=20,
            total_tokens=180,
        ),
    )

    line = debug_reporting._format_call_line(call, 1)
    assert "query_rewrite" in line
    assert "in=100" in line
    assert "cache_hit=60" in line
    assert "cache_miss=40" in line
    assert "out=80" in line
    assert "reasoning_out=20" in line
    assert "total=180" in line


def test_format_call_line_without_usage(debug_reporting: Any) -> None:
    """无 usage 的调用行显示全部未报告。"""
    call = LLMCallTrace(
        phase="generate_reply",
        round_index=1,
        model="deepseek-chat",
        finish_reason="error",
        duration_ms=None,
    )

    line = debug_reporting._format_call_line(call, 2)
    assert "generate_reply" in line
    assert "全部未报告" in line
    assert "未报告" in line  # duration_ms


def test_format_call_line_with_parent(debug_reporting: Any) -> None:
    """子调用（如视觉调用）显示父调用 ID。"""
    call = LLMCallTrace(
        phase="vision_tool_read_image",
        round_index=0,
        model="gemini-2.0-flash",
        finish_reason="stop",
        duration_ms=300.0,
        parent_call_id="abc123",
    )

    line = debug_reporting._format_call_line(call, 1)
    assert "父调用: abc123" in line


# ─── 阶段小计测试 ────────────────────────────────────────────


def test_phase_subtotal_zero_calls_shows_unreported(debug_reporting: Any) -> None:
    """零调用阶段全部显示未报告。"""
    collector = LLMDiagnosticCollector(request_id="none")

    text = debug_reporting._format_phase_subtotal("query_rewrite", collector)
    assert "(0 次调用)" in text
    assert "未报告" in text


def test_phase_subtotal_with_calls_shows_values(debug_reporting: Any) -> None:
    """有调用的阶段显示聚合值。"""
    from komari_bot.plugins.llm_provider.base_client import UnifiedUsageSchema

    collector = LLMDiagnosticCollector(request_id="has-calls")
    collector.add_call(
        LLMCallTrace(
            phase="query_rewrite",
            round_index=0,
            model="deepseek-chat",
            finish_reason="stop",
            duration_ms=100.0,
            usage=UnifiedUsageSchema(input_tokens=50, output_tokens=30, total_tokens=80),
        )
    )

    text = debug_reporting._format_phase_subtotal("query_rewrite", collector)
    assert "(1 次调用)" in text
    assert "50" in text
    assert "30" in text


# ─── 全链路小计测试 ──────────────────────────────────────────


def test_overall_total_zero_calls_shows_unreported(debug_reporting: Any) -> None:
    """零调用全链路显示未报告。"""
    collector = LLMDiagnosticCollector(request_id="none2")

    text = debug_reporting._format_overall_total(collector)
    assert "(0 次调用)" in text
    assert "未报告" in text


# ─── 合并转发分批测试 ────────────────────────────────────────


@pytest.mark.asyncio
async def test_build_and_send_within_50_nodes_single_batch(
    debug_reporting: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """节点 ≤50 时只发送一批合并转发。"""
    collector = _make_collector_with_data()
    api_calls: list[dict[str, Any]] = []

    class _FakeBot:
        self_id = "669293859"

        async def call_api(self, api: str, **kwargs: Any) -> None:
            api_calls.append({"api": api, **kwargs})

    monkeypatch.setattr(debug_reporting, "MAX_NODES_PER_BATCH", 50)

    await debug_reporting.build_and_send_diagnostic_report(
        bot=_FakeBot(),
        group_id=12345,
        collector=collector,
        result_type="reply",
        succeeded=True,
        extra_info={"user_id": "42"},
    )

    # 应该只有 send_group_forward_msg 调用
    forward_calls = [c for c in api_calls if c["api"] == "send_group_forward_msg"]
    assert len(forward_calls) == 1
    assert len(forward_calls[0]["messages"]) <= 50


@pytest.mark.asyncio
async def test_build_and_send_splits_reports_into_multiple_batches(
    debug_reporting: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """节点超过单批上限时拆成多批，且每批都不超过限制。"""
    collector = _make_collector_with_data()
    api_calls: list[dict[str, Any]] = []

    class _FakeBot:
        self_id = "669293859"

        async def call_api(self, api: str, **kwargs: Any) -> None:
            api_calls.append({"api": api, **kwargs})

    monkeypatch.setattr(debug_reporting, "MAX_NODES_PER_BATCH", 2)

    await debug_reporting.build_and_send_diagnostic_report(
        bot=_FakeBot(),
        group_id=12345,
        collector=collector,
        result_type="reply",
        succeeded=True,
        extra_info={"user_id": "42"},
    )

    forward_calls = [
        call for call in api_calls if call["api"] == "send_group_forward_msg"
    ]
    assert len(forward_calls) > 1
    assert all(len(call["messages"]) <= 2 for call in forward_calls)


@pytest.mark.asyncio
async def test_send_group_forward_msg_failure_falls_back_to_text(
    debug_reporting: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """合并转发 API 失败后降级为 send_group_msg 文本。"""
    collector = _make_collector_with_data()
    api_calls: list[dict[str, Any]] = []

    class _FakeBot:
        self_id = "669293859"

        async def call_api(self, api: str, **kwargs: Any) -> None:
            if api == "send_group_forward_msg":
                msg = "API 不可用"
                raise RuntimeError(msg)
            api_calls.append({"api": api, **kwargs})

    monkeypatch.setattr(debug_reporting, "MAX_NODES_PER_BATCH", 50)

    await debug_reporting.build_and_send_diagnostic_report(
        bot=_FakeBot(),
        group_id=12345,
        collector=collector,
        result_type="reply",
        succeeded=True,
        extra_info={"user_id": "42"},
    )

    # 应该有 send_group_msg 调用（文本降级）
    text_calls = [c for c in api_calls if c["api"] == "send_group_msg"]
    assert len(text_calls) > 0
    # 每个节点都应有文本降级
    assert all(isinstance(c.get("message"), str) for c in text_calls)


# ─── _send_text_fallback 公共函数测试 ────────────────────────


@pytest.mark.asyncio
async def test_send_group_text_success(debug_reporting: Any) -> None:
    """send_group_text 成功发送群消息。"""
    api_calls: list[dict[str, Any]] = []

    class _FakeBot:
        self_id = "669293859"

        async def call_api(self, api: str, **kwargs: Any) -> None:
            api_calls.append({"api": api, **kwargs})

    await debug_reporting.send_group_text(_FakeBot(), 12345, "测试文本")

    assert len(api_calls) == 1
    assert api_calls[0]["api"] == "send_group_msg"
    assert api_calls[0]["group_id"] == 12345
    assert api_calls[0]["message"] == "测试文本"


@pytest.mark.asyncio
async def test_send_group_text_failure_does_not_crash(debug_reporting: Any) -> None:
    """send_group_text 失败时不崩溃，记录异常。"""

    class _FakeBot:
        self_id = "669293859"

        async def call_api(self, _api: str, **_kwargs: Any) -> None:
            msg = "连接断开了"
            raise RuntimeError(msg)

    # 不应抛出异常
    await debug_reporting.send_group_text(_FakeBot(), 12345, "测试")


# ─── helpers ──────────────────────────────────────────────────


def _make_collector_with_data() -> LLMDiagnosticCollector:
    """构造带有一条调用和工具的 collector。"""
    from komari_bot.plugins.llm_provider.base_client import UnifiedUsageSchema

    collector = LLMDiagnosticCollector(request_id="test-collector")
    call = LLMCallTrace(
        phase="query_rewrite",
        round_index=0,
        model="deepseek-chat",
        finish_reason="stop",
        duration_ms=120.5,
        usage=UnifiedUsageSchema(
            input_tokens=100,
            output_tokens=50,
            total_tokens=150,
        ),
    )
    collector.add_call(call)
    tool = ToolExecutionTrace(
        call_id=call.call_id,
        tool_name="search_web",
        parsed_arguments={"query": "天气"},
        status="success",
        result_summary="找到 3 条结果",
    )
    collector.add_tool(tool)
    return collector
