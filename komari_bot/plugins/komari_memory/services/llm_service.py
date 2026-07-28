"""Komari Memory LLM 调用服务，封装 llm_provider 插件。"""

from __future__ import annotations

import json
import re
import time
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from nonebot import logger
from nonebot.plugin import require

from komari_bot.common.untrusted_context import (
    UntrustedContext,
    render_untrusted_context,
)

from ..core.retry import retry_async
from .message_chunking import MEMORY_UNTRUSTED_CONTEXT_MAX_CHARS
from .summary_prompt_template import get_template as get_summary_template
from .summary_prompt_template import render_template as render_summary_template
from .token_counter import estimate_text_tokens

if TYPE_CHECKING:
    from komari_bot.plugins.agent_run_logger.diagnostic import AgentRunCollector

    from ..config_schema import KomariMemoryConfigSchema
    from .redis_manager import MessageSchema

# 依赖 llm_provider 插件
llm_provider = require("llm_provider")

_MAX_SUMMARY_MEMORIES = 8
_SUMMARY_TOKEN_WARNING_THRESHOLD = 32000

_OUTPUT_SUMMARY_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "output_summary_result",
        "description": (
            "输出最终的对话总结结果。将群聊记录按话题或时间段拆分为多段独立记忆，"
            "每段记忆包含简短总结和重要性评分。请在完成所有分析后调用此工具。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "memories": {
                    "type": "array",
                    "description": "对话记忆数组，每条记忆独立存储",
                    "items": {
                        "type": "object",
                        "properties": {
                            "content": {
                                "type": "string",
                                "description": "该段对话的简短总结",
                            },
                            "importance": {
                                "type": "integer",
                                "minimum": 1,
                                "maximum": 5,
                                "description": "该段对话的重要性评分 1-5",
                            },
                        },
                        "required": ["content", "importance"],
                    },
                },
            },
            "required": ["memories"],
        },
    },
}


class _SummaryFallbackError(Exception):
    """内部异常：标记当前总结输出 layer 失败，触发下一层 fallback。"""


def _extract_json_from_markdown(text: str) -> str:
    """从 markdown 代码块中提取 JSON（保留作为降级方案）。"""
    text = text.strip()

    if not text.startswith("```"):
        return text

    pattern = r"```(?:json)?\s*\n([\s\S]*?)\n```"
    match = re.search(pattern, text)
    if match:
        return match.group(1).strip()

    if text.startswith("```"):
        lines = text.split("\n", 1)
        if len(lines) > 1:
            text = lines[1]
        text = text.removesuffix("```")

    return text.strip()


def _extract_tag_content(text: str, tag: str) -> str:
    """从 LLM 回复中提取指定 XML 标签内的内容。"""
    pattern = rf"<{tag}>([\s\S]*)</{tag}>"
    match = re.search(pattern, text)
    if match:
        return match.group(1).strip()

    logger.warning("[KomariMemory] 未找到 <{}> 标签，使用原始回复", tag)
    return re.sub(r"<think>[\s\S]*?</think>", "", text).strip()


def _format_messages_for_summary(
    messages: list["MessageSchema"],
    config: KomariMemoryConfigSchema,
) -> str:
    """将消息缓冲格式化为总结/画像 Agent 共用的群聊记录文本。"""
    lines: list[str] = []
    for message in messages:
        if message.is_bot:
            lines.append(f"[bot] {config.bot_nickname}: {message.content}")
        else:
            lines.append(f"[user_id:{message.user_id}] {message.user_nickname}: {message.content}")
    return "\n".join(lines)


def _build_user_message(
    conversation_text: str,
    participants: list[str],
    display_name_map: dict[str, str],
) -> str:
    """构建与画像 Agent 前缀一致的 user 消息前三段。"""
    parts: list[str] = []
    parts.append(f"【群聊记录】\n{conversation_text}")
    parts.append(f"【参与用户 user_id】\n{json.dumps(participants, ensure_ascii=False)}")
    parts.append(f"【昵称映射】\n{json.dumps(display_name_map, ensure_ascii=False)}")
    return "\n\n".join(parts)


async def _build_summary_messages(
    *,
    conversation_text: str,
    participants: list[str],
    display_name_map: dict[str, str],
) -> list[dict[str, str]]:
    """构建与画像 Agent 共享前缀的三段式总结 messages。"""
    template = await get_summary_template()
    external_context = _build_user_message(
        conversation_text,
        participants,
        display_name_map,
    )
    user_content = (
        render_untrusted_context(
            UntrustedContext(
                source_type="conversation_history",
                source_id="memory-summary-input",
                content=external_context,
                max_chars=MEMORY_UNTRUSTED_CONTEXT_MAX_CHARS,
            )
        )
        + "\n\n请生成对话总结。"
    )
    workflow = render_summary_template(
        template["summary_workflow_system"],
        json_response_example=template["json_response_example"],
    )
    return [
        {"role": "system", "content": template["memory_summary_common_system"]},
        {"role": "user", "content": user_content},
        {"role": "system", "content": workflow},
    ]


def _estimate_messages_tokens(messages: list[dict[str, str]]) -> int:
    """轻量估算 messages 文本 token，用于日志观测。"""
    return sum(estimate_text_tokens(str(message.get("content", ""))) for message in messages)


def _normalize_summary_result(result: dict[str, Any]) -> dict[str, Any]:
    """规范化总结结果，确保 memories 数组格式正确。"""
    memories_raw = result.get("memories")
    if not isinstance(memories_raw, list):
        memories_raw = []

    normalized_memories: list[dict[str, Any]] = []
    seen_contents: set[str] = set()
    for memory in memories_raw:
        if not isinstance(memory, dict):
            continue
        content = str(memory.get("content", "")).strip()
        if len(content) < 8 or content in seen_contents:
            continue
        try:
            importance = int(memory.get("importance", 3))
        except (TypeError, ValueError):
            importance = 3
        seen_contents.add(content)
        normalized_memories.append(
            {
                "content": content,
                "importance": max(1, min(5, importance)),
            }
        )
        if len(normalized_memories) >= _MAX_SUMMARY_MEMORIES:
            break

    return {"memories": normalized_memories}


async def _request_via_json_mode(
    messages: list[dict[str, str]],
    config: KomariMemoryConfigSchema,
    trace_id: str,
    collector: AgentRunCollector | None,
) -> dict[str, Any]:
    """Layer 1：使用 response_format JSON mode 请求结构化总结。"""
    request_data = {
        "messages": messages,
        "model": config.llm_model_summary,
        "temperature": config.llm_temperature_summary,
        "max_tokens": config.llm_max_tokens_summary,
        "thinking_mode": config.llm_thinking_mode_summary,
        "reasoning_effort": config.llm_reasoning_effort_summary,
        "response_format": {"type": "json_object"},
    }
    try:
        if collector is None:
            response = await llm_provider.generate_text_with_messages(
                **request_data,
                request_trace_id=trace_id,
                request_phase="summary_json_mode",
            )
        else:
            completion = await llm_provider.generate_messages_completion(
                **request_data,
                request_trace_id=trace_id,
                request_phase="summary_json_mode",
            )
            from komari_bot.plugins.agent_run_logger.diagnostic import (
                record_completion_call,
            )

            record_completion_call(
                collector,
                phase="summary_json_mode",
                round_index=len(collector.calls),
                method="generate_messages_completion",
                model=config.llm_model_summary,
                request=request_data,
                completion=completion,
            )
            response = completion.content
        parsed = json.loads(response)
    except Exception as exc:
        from komari_bot.plugins.agent_run_logger.diagnostic import record_failed_call

        if not (
            collector is not None
            and collector.calls
            and collector.calls[-1].phase == "summary_json_mode"
            and collector.calls[-1].status == "success"
        ):
            record_failed_call(
                collector,
                phase="summary_json_mode",
                round_index=len(collector.calls) if collector is not None else 0,
                method="generate_messages_completion",
                model=config.llm_model_summary,
                request=request_data,
                error=exc,
            )
        raise _SummaryFallbackError from exc

    if not isinstance(parsed, dict):
        raise _SummaryFallbackError
    result = _normalize_summary_result(parsed)
    if not result.get("memories"):
        raise _SummaryFallbackError
    return result


async def _request_via_tool_calling(
    messages: list[dict[str, str]],
    config: KomariMemoryConfigSchema,
    trace_id: str,
    collector: AgentRunCollector | None,
) -> dict[str, Any]:
    """Layer 2：使用强制 tool calling 引导模型输出结构化总结。"""
    messages_with_tool = [
        *messages,
        {
            "role": "system",
            "content": "请调用 output_summary_result 工具输出最终结果。不要输出任何其他内容。",
        },
    ]
    request_data = {
        "messages": messages_with_tool,
        "model": config.llm_model_summary,
        "temperature": config.llm_temperature_summary,
        "max_tokens": config.llm_max_tokens_summary,
        "tools": [_OUTPUT_SUMMARY_TOOL],
        "tool_choice": {
            "type": "function",
            "function": {"name": "output_summary_result"},
        },
        "parallel_tool_calls": False,
        "thinking_mode": config.llm_thinking_mode_summary,
        "reasoning_effort": config.llm_reasoning_effort_summary,
    }
    try:
        completion = await llm_provider.generate_messages_completion(
            **request_data,
            request_trace_id=trace_id,
            request_phase="summary_tool_calling",
        )
    except Exception as exc:
        from komari_bot.plugins.agent_run_logger.diagnostic import record_failed_call

        record_failed_call(
            collector,
            phase="summary_tool_calling",
            round_index=len(collector.calls) if collector is not None else 0,
            method="generate_messages_completion",
            model=config.llm_model_summary,
            request=request_data,
            error=exc,
        )
        raise _SummaryFallbackError from exc

    from komari_bot.plugins.agent_run_logger.diagnostic import (
        ToolExecutionTrace,
        record_completion_call,
    )

    call_id = record_completion_call(
        collector,
        phase="summary_tool_calling",
        round_index=len(collector.calls) if collector is not None else 0,
        method="generate_messages_completion",
        model=config.llm_model_summary,
        request=request_data,
        completion=completion,
    )

    for tool_call in completion.tool_calls or []:
        tool_started_at = time.monotonic()
        if tool_call.function.name != "output_summary_result":
            if collector is not None:
                collector.add_tool(
                    ToolExecutionTrace(
                        call_id=call_id or "",
                        tool_name=tool_call.function.name,
                        parsed_arguments=tool_call.parsed_arguments or {},
                        status="error",
                        error=f"未知工具: {tool_call.function.name}",
                        duration_ms=(time.monotonic() - tool_started_at) * 1000,
                    )
                )
            continue
        if not tool_call.parsed_arguments:
            if collector is not None:
                collector.add_tool(
                    ToolExecutionTrace(
                        call_id=call_id or "",
                        tool_name=tool_call.function.name,
                        status="error",
                        error="工具参数为空",
                        duration_ms=(time.monotonic() - tool_started_at) * 1000,
                    )
                )
            continue
        result = _normalize_summary_result(tool_call.parsed_arguments)
        if result.get("memories"):
            if collector is not None:
                collector.add_tool(
                    ToolExecutionTrace(
                        call_id=call_id or "",
                        tool_name=tool_call.function.name,
                        parsed_arguments=tool_call.parsed_arguments,
                        status="success",
                        result=result,
                        duration_ms=(time.monotonic() - tool_started_at) * 1000,
                    )
                )
            return result
        if collector is not None:
            collector.add_tool(
                ToolExecutionTrace(
                    call_id=call_id or "",
                    tool_name=tool_call.function.name,
                    parsed_arguments=tool_call.parsed_arguments,
                    status="error",
                    result=result,
                    error="工具结果不含有效记忆",
                    duration_ms=(time.monotonic() - tool_started_at) * 1000,
                )
            )
    raise _SummaryFallbackError


async def _request_via_direct_output(
    messages: list[dict[str, str]],
    config: KomariMemoryConfigSchema,
    trace_id: str,
    collector: AgentRunCollector | None,
) -> dict[str, Any]:
    """Layer 3：直接文本输出，并从 markdown 中提取 JSON。"""
    request_data = {
        "messages": messages,
        "model": config.llm_model_summary,
        "temperature": config.llm_temperature_summary,
        "max_tokens": config.llm_max_tokens_summary,
        "thinking_mode": config.llm_thinking_mode_summary,
        "reasoning_effort": config.llm_reasoning_effort_summary,
    }
    response = ""
    completion: Any | None = None
    try:
        if collector is None:
            response = await llm_provider.generate_text_with_messages(
                **request_data,
                request_trace_id=trace_id,
                request_phase="summary_direct_output",
            )
        else:
            completion = await llm_provider.generate_messages_completion(
                **request_data,
                request_trace_id=trace_id,
                request_phase="summary_direct_output",
            )
    except Exception as exc:
        from komari_bot.plugins.agent_run_logger.diagnostic import record_failed_call

        record_failed_call(
            collector,
            phase="summary_direct_output",
            round_index=len(collector.calls) if collector is not None else 0,
            method="generate_messages_completion",
            model=config.llm_model_summary,
            request=request_data,
            error=exc,
        )
        raise
    if collector is not None:
        assert completion is not None
        from komari_bot.plugins.agent_run_logger.diagnostic import (
            record_completion_call,
        )

        record_completion_call(
            collector,
            phase="summary_direct_output",
            round_index=len(collector.calls),
            method="generate_messages_completion",
            model=config.llm_model_summary,
            request=request_data,
            completion=completion,
        )
        response = completion.content
    json_text = _extract_json_from_markdown(response)
    parsed = json.loads(json_text)
    if not isinstance(parsed, dict):
        msg = "总结模型返回的 JSON 不是对象"
        raise TypeError(msg)
    return _normalize_summary_result(parsed)


async def _request_structured_summary(
    *,
    messages: list[dict[str, str]],
    config: KomariMemoryConfigSchema,
    trace_id: str,
    collector: AgentRunCollector | None,
) -> dict[str, Any]:
    """三层 fallback 的结构化总结输出。"""
    estimated_prompt_tokens = _estimate_messages_tokens(messages)
    logger.info(
        "[KomariMemory] 总结请求追踪: trace_id={} estimated_tokens={}",
        trace_id,
        estimated_prompt_tokens,
    )
    if estimated_prompt_tokens > _SUMMARY_TOKEN_WARNING_THRESHOLD:
        logger.warning(
            "[KomariMemory] 总结输入 token 估算过高: trace_id={} estimated_tokens={}",
            trace_id,
            estimated_prompt_tokens,
        )

    try:
        return await _request_via_json_mode(messages, config, trace_id, collector)
    except _SummaryFallbackError:
        logger.info("[KomariMemory] Layer 1 (json_mode) 失败，降级到 Layer 2 (tool_calling)")

    try:
        return await _request_via_tool_calling(messages, config, trace_id, collector)
    except _SummaryFallbackError:
        logger.info("[KomariMemory] Layer 2 (tool_calling) 失败，降级到 Layer 3 (direct_output)")

    return await _request_via_direct_output(messages, config, trace_id, collector)


@retry_async(max_attempts=3, base_delay=1.0)
async def generate_reply(
    config: KomariMemoryConfigSchema,
    messages: list[dict] | None = None,
    user_message: str = "",
    system_prompt: str = "",
) -> str:
    """生成回复（使用 OpenAI messages 格式，带重试机制，支持多模态）。"""
    if messages is not None:
        raw_response = await llm_provider.generate_text_with_messages(
            messages=messages,
            model=config.llm_model_chat,
            temperature=config.llm_temperature_chat,
            max_tokens=config.llm_max_tokens_chat,
            thinking_mode=config.llm_thinking_mode_chat,
            reasoning_effort=config.llm_reasoning_effort_chat,
            request_phase="memory_reply",
        )
    else:
        raw_response = await llm_provider.generate_text(
            prompt=user_message,
            model=config.llm_model_chat,
            system_instruction=system_prompt,
            temperature=config.llm_temperature_chat,
            max_tokens=config.llm_max_tokens_chat,
            thinking_mode=config.llm_thinking_mode_chat,
            reasoning_effort=config.llm_reasoning_effort_chat,
            request_phase="memory_reply",
        )

    return _extract_tag_content(raw_response, config.response_tag)


@retry_async(max_attempts=3, base_delay=1.0)
async def summarize_conversation(
    messages: list["MessageSchema"],
    config: KomariMemoryConfigSchema,
    *,
    participants: list[str],
    display_name_map: dict[str, str],
    collector: AgentRunCollector | None = None,
) -> dict[str, Any]:
    """总结对话为多段独立记忆（oneshot，带三层 fallback + 重试）。"""
    trace_id = f"memsum-{uuid4().hex[:8]}"
    group_id = messages[0].group_id if messages else "-"
    conversation_text = _format_messages_for_summary(messages, config)
    summary_messages = await _build_summary_messages(
        conversation_text=conversation_text,
        participants=participants,
        display_name_map=display_name_map,
    )
    logger.info(
        "[KomariMemory] 总结追踪开始: trace_id={} group={} messages={}",
        trace_id,
        group_id,
        len(messages),
    )
    if not messages:
        return {"memories": []}

    return await _request_structured_summary(
        messages=summary_messages,
        config=config,
        trace_id=trace_id,
        collector=collector,
    )
