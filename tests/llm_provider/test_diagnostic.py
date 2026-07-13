"""LLM 诊断模型与 collector 测试。"""

from __future__ import annotations

from typing import Any

from komari_bot.plugins.llm_provider.base_client import (
    UnifiedUsageSchema,
)
from komari_bot.plugins.llm_provider.diagnostic import (
    LLMCallTrace,
    LLMDiagnosticCollector,
    ToolExecutionTrace,
)


class TestUnifiedUsageSchema:
    """UnifiedUsageSchema 单元测试。"""

    def test_all_fields_default_to_none(self) -> None:
        usage = UnifiedUsageSchema()
        assert usage.input_tokens is None
        assert usage.cached_input_tokens is None
        assert usage.cache_miss_input_tokens is None
        assert usage.output_tokens is None
        assert usage.reasoning_output_tokens is None
        assert usage.total_tokens is None

    def test_partial_fields(self) -> None:
        usage = UnifiedUsageSchema(
            input_tokens=100,
            output_tokens=50,
            total_tokens=150,
        )
        assert usage.input_tokens == 100
        assert usage.cached_input_tokens is None
        assert usage.cache_miss_input_tokens is None
        assert usage.output_tokens == 50
        assert usage.reasoning_output_tokens is None
        assert usage.total_tokens == 150

    def test_full_deepseek_usage(self) -> None:
        usage = UnifiedUsageSchema(
            input_tokens=200,
            cached_input_tokens=120,
            cache_miss_input_tokens=80,
            output_tokens=300,
            reasoning_output_tokens=50,
            total_tokens=500,
        )
        assert usage.input_tokens == 200
        assert usage.cached_input_tokens == 120
        assert usage.cache_miss_input_tokens == 80
        assert usage.output_tokens == 300
        assert usage.reasoning_output_tokens == 50
        assert usage.total_tokens == 500

    def test_model_dump_excludes_none_by_default(self) -> None:
        usage = UnifiedUsageSchema(input_tokens=10, output_tokens=5)
        d = usage.model_dump(exclude_none=True)
        assert "input_tokens" in d
        assert "output_tokens" in d
        assert "cached_input_tokens" not in d
        assert "cache_miss_input_tokens" not in d

    def test_model_dump_serializable(self) -> None:
        usage = UnifiedUsageSchema(input_tokens=1, total_tokens=1)
        d = usage.model_dump()
        assert isinstance(d, dict)
        assert d["input_tokens"] == 1


class TestLLMCallTrace:
    """LLMCallTrace 单元测试。"""

    def test_defaults(self) -> None:
        trace = LLMCallTrace()
        assert len(trace.call_id) == 12
        assert trace.parent_call_id is None
        assert trace.phase == ""
        assert trace.round_index == 0
        assert trace.usage is None
        assert trace.duration_ms is None

    def test_full_construction(self) -> None:
        usage = UnifiedUsageSchema(input_tokens=10, total_tokens=10)
        trace = LLMCallTrace(
            parent_call_id="parent-1",
            phase="query_rewrite",
            round_index=0,
            model="deepseek-chat",
            finish_reason="stop",
            duration_ms=123.4,
            usage=usage,
        )
        assert trace.parent_call_id == "parent-1"
        assert trace.phase == "query_rewrite"
        assert trace.round_index == 0
        assert trace.model == "deepseek-chat"
        assert trace.finish_reason == "stop"
        assert trace.duration_ms == 123.4
        assert trace.usage == usage

    def test_unique_call_ids(self) -> None:
        t1 = LLMCallTrace()
        t2 = LLMCallTrace()
        assert t1.call_id != t2.call_id


class TestToolExecutionTrace:
    """ToolExecutionTrace 单元测试。"""

    def test_defaults(self) -> None:
        tool = ToolExecutionTrace()
        assert tool.call_id == ""
        assert tool.tool_name == ""
        assert tool.parsed_arguments == {}
        assert tool.status == "pending"
        assert tool.error_summary is None
        assert tool.result_summary is None

    def test_full_construction(self) -> None:
        tool = ToolExecutionTrace(
            call_id="call-1",
            tool_name="search_web",
            parsed_arguments={"query": "天气"},
            status="success",
            result_summary="搜索结果共3条",
        )
        assert tool.call_id == "call-1"
        assert tool.tool_name == "search_web"
        assert tool.parsed_arguments == {"query": "天气"}
        assert tool.status == "success"
        assert tool.result_summary == "搜索结果共3条"
        assert tool.error_summary is None


class TestLLMDiagnosticCollector:
    """LLMDiagnosticCollector 单元测试。"""

    def _make_usage(self, **kwargs: int) -> UnifiedUsageSchema:
        return UnifiedUsageSchema(**kwargs)

    def _make_call(
        self, phase: str, usage: UnifiedUsageSchema | None, **kwargs: Any
    ) -> LLMCallTrace:
        kw: dict[str, Any] = {"phase": phase, "usage": usage}
        kw.update(kwargs)
        return LLMCallTrace(**kw)

    def test_add_call_and_get_phase_calls(self) -> None:
        collector = LLMDiagnosticCollector()
        c1 = self._make_call("rewrite", self._make_usage(input_tokens=10, total_tokens=10))
        c2 = self._make_call("reply", self._make_usage(input_tokens=20, total_tokens=20))
        collector.add_call(c1)
        collector.add_call(c2)

        assert len(collector.calls) == 2
        assert len(collector.get_phase_calls("rewrite")) == 1
        assert len(collector.get_phase_calls("reply")) == 1
        assert len(collector.get_phase_calls("nonexistent")) == 0

    def test_add_tool(self) -> None:
        collector = LLMDiagnosticCollector()
        tool = ToolExecutionTrace(call_id="call-1", tool_name="read_profile")
        collector.add_tool(tool)
        assert len(collector.tools) == 1
        assert collector.tools[0].tool_name == "read_profile"

    def test_add_error(self) -> None:
        collector = LLMDiagnosticCollector()
        collector.add_error("reply", "APITimeoutError", "请求超时")
        assert len(collector.errors) == 1
        assert collector.errors[0]["phase"] == "reply"
        assert collector.errors[0]["type"] == "APITimeoutError"

    def test_aggregate_phase_all_reported(self) -> None:
        collector = LLMDiagnosticCollector()
        collector.add_call(
            self._make_call(
                "rewrite",
                self._make_usage(input_tokens=10, output_tokens=5, total_tokens=15),
            )
        )
        collector.add_call(
            self._make_call(
                "rewrite",
                self._make_usage(input_tokens=20, output_tokens=8, total_tokens=28),
            )
        )

        agg = collector.aggregate_phase("rewrite")
        assert agg.call_count == 2
        assert agg.input_tokens == 30
        assert agg.output_tokens == 13
        assert agg.total_tokens == 43
        assert agg.input_tokens_complete is True
        # 后端未报告的字段标记为不完整，避免用 0 冒充完整总计
        assert agg.cached_input_tokens_complete is False
        assert agg.cache_miss_input_tokens_complete is False
        assert agg.reasoning_output_tokens_complete is False
        assert agg.output_tokens_complete is True
        assert agg.total_tokens_complete is True

    def test_aggregate_phase_with_deepseek_cache_fields(self) -> None:
        collector = LLMDiagnosticCollector()
        collector.add_call(
            self._make_call(
                "reply",
                self._make_usage(
                    input_tokens=100,
                    cached_input_tokens=60,
                    cache_miss_input_tokens=40,
                    output_tokens=50,
                    reasoning_output_tokens=20,
                    total_tokens=150,
                ),
            )
        )

        agg = collector.aggregate_phase("reply")
        assert agg.input_tokens == 100
        assert agg.cached_input_tokens == 60
        assert agg.cache_miss_input_tokens == 40
        assert agg.output_tokens == 50
        assert agg.reasoning_output_tokens == 20
        assert agg.total_tokens == 150
        assert agg.cached_input_tokens_complete is True
        assert agg.cache_miss_input_tokens_complete is True
        assert agg.reasoning_output_tokens_complete is True

    def test_aggregate_phase_usage_is_none_marks_all_incomplete(self) -> None:
        collector = LLMDiagnosticCollector()
        collector.add_call(self._make_call("rewrite", None))

        agg = collector.aggregate_phase("rewrite")
        assert agg.call_count == 1
        assert agg.input_tokens == 0
        assert agg.input_tokens_complete is False
        assert agg.cached_input_tokens_complete is False
        assert agg.output_tokens_complete is False
        assert agg.reasoning_output_tokens_complete is False

    def test_aggregate_phase_partial_fields_marks_only_missing_incomplete(self) -> None:
        collector = LLMDiagnosticCollector()
        collector.add_call(
            self._make_call(
                "reply",
                self._make_usage(input_tokens=10, output_tokens=5, total_tokens=15),
            )
        )
        collector.add_call(
            self._make_call(
                "reply",
                self._make_usage(
                    input_tokens=20,
                    cached_input_tokens=10,
                    output_tokens=8,
                    total_tokens=28,
                ),
            )
        )

        agg = collector.aggregate_phase("reply")
        assert agg.call_count == 2
        assert agg.input_tokens == 30
        assert agg.output_tokens == 13
        assert agg.total_tokens == 43
        assert agg.input_tokens_complete is True
        assert agg.output_tokens_complete is True
        assert agg.total_tokens_complete is True
        # cached_input_tokens: 第一次调用未报告 → incomplete
        assert agg.cached_input_tokens_complete is False
        assert agg.cached_input_tokens == 10

    def test_aggregate_overall_all_reported(self) -> None:
        collector = LLMDiagnosticCollector()
        collector.add_call(
            self._make_call(
                "rewrite",
                self._make_usage(input_tokens=5, total_tokens=5),
            )
        )
        collector.add_call(
            self._make_call(
                "reply_round_0",
                self._make_usage(
                    input_tokens=100,
                    cached_input_tokens=50,
                    cache_miss_input_tokens=50,
                    output_tokens=200,
                    reasoning_output_tokens=30,
                    total_tokens=300,
                ),
            )
        )

        agg = collector.aggregate_overall()
        assert agg.call_count == 2
        assert agg.input_tokens == 105
        assert agg.cached_input_tokens == 50
        assert agg.cache_miss_input_tokens == 50
        assert agg.output_tokens == 200
        assert agg.reasoning_output_tokens == 30
        assert agg.total_tokens == 305
        assert agg.input_tokens_complete is True
        assert agg.total_tokens_complete is True

    def test_aggregate_overall_mixed_completeness(self) -> None:
        collector = LLMDiagnosticCollector()
        collector.add_call(self._make_call("rewrite", None))
        collector.add_call(
            self._make_call(
                "reply",
                self._make_usage(input_tokens=10, output_tokens=5, total_tokens=15),
            )
        )

        agg = collector.aggregate_overall()
        assert agg.call_count == 2
        assert agg.input_tokens == 10
        assert agg.output_tokens == 5
        assert agg.total_tokens == 15
        # 第一调用无 usage → 所有字段 incomplete
        assert agg.input_tokens_complete is False
        assert agg.output_tokens_complete is False
        assert agg.total_tokens_complete is False

    def test_no_global_state(self) -> None:
        c1 = LLMDiagnosticCollector()
        c2 = LLMDiagnosticCollector()
        c1.add_call(self._make_call("rewrite", self._make_usage(input_tokens=10, total_tokens=10)))
        c2.add_call(self._make_call("reply", self._make_usage(input_tokens=20, total_tokens=20)))

        assert len(c1.calls) == 1
        assert len(c2.calls) == 1
        assert c1.get_phase_calls("rewrite")[0].usage is not None
        assert c1.get_phase_calls("rewrite")[0].usage.input_tokens == 10  # type: ignore[union-attr]
        assert c2.get_phase_calls("reply")[0].usage is not None
        assert c2.get_phase_calls("reply")[0].usage.input_tokens == 20  # type: ignore[union-attr]

    def test_calls_persist_after_error_injection(self) -> None:
        collector = LLMDiagnosticCollector()
        collector.add_call(
            self._make_call("rewrite", self._make_usage(input_tokens=10, total_tokens=10))
        )
        collector.add_error("rewrite", "ValueError", "解析失败")

        try:
            raise RuntimeError("后续阶段崩溃")  # noqa: TRY301
        except RuntimeError:
            pass

        collector.add_call(
            self._make_call("reply", self._make_usage(input_tokens=20, total_tokens=20))
        )

        assert len(collector.calls) == 2
        assert len(collector.errors) == 1
        assert collector.errors[0]["type"] == "ValueError"

    def test_empty_collector_aggregation(self) -> None:
        collector = LLMDiagnosticCollector()
        agg = collector.aggregate_overall()
        assert agg.call_count == 0
        assert agg.input_tokens == 0
        assert agg.input_tokens_complete is True

        phase_agg = collector.aggregate_phase("nonexistent")
        assert phase_agg.call_count == 0
        assert phase_agg.input_tokens_complete is True
