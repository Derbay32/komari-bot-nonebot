"""用户画像维护 Agent 编排服务。"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from nonebot import logger
from nonebot.plugin import require

from komari_bot.common.memory_agent_locks import (
    MemoryAgentLockScope,
    acquire_memory_agent_lock,
)
from komari_bot.common.profile_operations import ProfileOperation

from ..services.summary_prompt_template import (
    get_template as get_summary_template,
)
from ..services.summary_prompt_template import (
    render_template as render_summary_template,
)
from .redis_staging import ProfileStaging
from .tool_definitions import PROFILE_AGENT_TOOLS

if TYPE_CHECKING:
    import redis.asyncio as aioredis

    from ..config_schema import KomariMemoryConfigSchema
    from ..services.memory_service import MemoryService

llm_provider = require("llm_provider")


@dataclass(frozen=True)
class ProfileAgentResult:
    """画像 Agent 运行结果。"""

    committed_count: int
    staged_count: int
    summary: str
    status: str
    changed_user_ids: set[str] = field(default_factory=set)


async def run_profile_agent(
    *,
    redis: aioredis.Redis,
    memory: MemoryService,
    group_id: str,
    conversation_text: str,
    participants: list[str],
    display_name_map: dict[str, str],
    bot_user_ids: set[str],
    config: KomariMemoryConfigSchema,
    trace_id: str,
) -> ProfileAgentResult:
    """运行画像维护 Agent，并在程序侧自动提交暂存区。"""
    session_id = f"{trace_id}:{uuid4().hex[:8]}"
    staging = ProfileStaging(
        redis,
        session_id,
        group_id,
        memory,
        ttl_seconds=config.memory_agent_staging_ttl_seconds,
    )
    async with acquire_memory_agent_lock(
        memory.pg_pool,
        scope=MemoryAgentLockScope.PROFILE_GROUP,
        group_id=group_id,
        trace_id=trace_id,
        timeout_seconds=config.memory_agent_lock_timeout_seconds,
    ):
        try:
            return await _run_profile_agent_locked(
                staging=staging,
                conversation_text=conversation_text,
                participants=participants,
                display_name_map=display_name_map,
                bot_user_ids=bot_user_ids,
                config=config,
                trace_id=trace_id,
            )
        except Exception:
            await staging.discard()
            logger.exception(
                "[KomariMemory] 画像 Agent 执行失败，已丢弃暂存区: trace_id={} group={}",
                trace_id,
                group_id,
            )
            raise


async def _run_profile_agent_locked(
    *,
    staging: ProfileStaging,
    conversation_text: str,
    participants: list[str],
    display_name_map: dict[str, str],
    bot_user_ids: set[str],
    config: KomariMemoryConfigSchema,
    trace_id: str,
) -> ProfileAgentResult:
    messages = _build_initial_messages(
        conversation_text=conversation_text,
        participants=participants,
        display_name_map=display_name_map,
        bot_user_ids=bot_user_ids,
        config=config,
    )
    tool_calls_used = 0
    read_profiles_used = 0
    write_operations_used = 0
    last_content = ""

    for round_index in range(config.memory_agent_max_rounds):
        completion = await llm_provider.generate_messages_completion(
            messages=messages,
            model=config.llm_model_summary,
            temperature=config.llm_temperature_summary,
            max_tokens=config.llm_max_tokens_summary,
            tools=PROFILE_AGENT_TOOLS,
            tool_choice="auto",
            parallel_tool_calls=False,
            request_trace_id=trace_id,
            request_phase="profile_agent",
            request_round_index=round_index + 1,
        )
        last_content = str(completion.content or "").strip()
        if not completion.tool_calls:
            break

        tool_calls_used += len(completion.tool_calls)
        if tool_calls_used > config.memory_agent_max_tool_calls:
            await staging.discard()
            return ProfileAgentResult(
                committed_count=0,
                staged_count=0,
                summary="画像 Agent 超出工具调用预算，已丢弃暂存区",
                status="discarded",
            )

        messages.append(_build_assistant_tool_call_message(completion))
        for tool_call in completion.tool_calls:
            tool_name = tool_call.function.name
            arguments = tool_call.parsed_arguments or {}
            result: dict[str, Any]
            match tool_name:
                case "read_profile":
                    read_profiles_used += 1
                    if read_profiles_used > config.memory_agent_max_read_profiles:
                        result = {"status": "error", "message": "读取画像次数超过预算"}
                    else:
                        result = await _execute_read_profile(staging, arguments)
                case "write_profile":
                    operations = _parse_operations(arguments.get("operations"))
                    write_operations_used += len(operations)
                    if write_operations_used > config.memory_agent_max_write_operations:
                        result = {"status": "error", "message": "写入画像操作数超过预算"}
                    else:
                        result = (await staging.stage(operations)).to_dict()
                case "preview_profile":
                    result = (await staging.preview()).to_dict()
                case _:
                    result = {"status": "error", "message": f"未知工具: {tool_name}"}
            messages.append(_build_tool_result_message(tool_call, result))
    else:
        preview = await staging.preview()
        await staging.discard()
        return ProfileAgentResult(
            committed_count=0,
            staged_count=preview.staged_count,
            summary="画像 Agent 超出最大轮数，已丢弃暂存区",
            status="discarded",
        )

    preview = await staging.preview()
    if preview.staged_count <= 0:
        await staging.discard()
        return ProfileAgentResult(
            committed_count=0,
            staged_count=0,
            summary=last_content or "暂存区为空，无画像操作提交",
            status="nothing_to_commit",
        )

    commit_result = await staging.commit()
    return ProfileAgentResult(
        committed_count=commit_result.committed_count,
        staged_count=preview.staged_count,
        summary=last_content or commit_result.summary,
        status=commit_result.status,
        changed_user_ids=commit_result.changed_user_ids,
    )


def _build_initial_messages(
    *,
    conversation_text: str,
    participants: list[str],
    display_name_map: dict[str, str],
    bot_user_ids: set[str],
    config: KomariMemoryConfigSchema,
) -> list[dict[str, Any]]:
    template = get_summary_template()
    workflow = render_summary_template(
        template["profile_agent_workflow_system"],
        bot_user_ids=", ".join(sorted(bot_user_ids)) or "无",
        profile_trait_limit=config.profile_trait_limit,
    )
    user_content = (
        f"【群聊记录】\n{conversation_text}\n\n"
        f"【参与用户 user_id】\n{json.dumps(participants, ensure_ascii=False)}\n\n"
        f"【昵称映射】\n{json.dumps(display_name_map, ensure_ascii=False)}\n\n"
        "请维护用户画像。"
    )
    return [
        {"role": "system", "content": template["memory_summary_common_system"]},
        {"role": "user", "content": user_content},
        {"role": "system", "content": workflow},
    ]


async def _execute_read_profile(
    staging: ProfileStaging,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    user_id = str(arguments.get("user_id", "")).strip()
    if not user_id:
        return {"status": "error", "message": "缺少 user_id"}
    keys_raw = arguments.get("keys")
    keys = [str(key).strip() for key in keys_raw if str(key).strip()] if isinstance(keys_raw, list) else None
    include_staged = bool(arguments.get("include_staged", False))
    return (await staging.read_profile(user_id, keys, include_staged=include_staged)).to_dict()


def _parse_operations(raw: Any) -> list[ProfileOperation]:
    if not isinstance(raw, list):
        return []
    operations: list[ProfileOperation] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        operation = ProfileOperation.from_mapping(item)
        if operation is not None:
            operations.append(operation)
    return operations


def _build_assistant_tool_call_message(completion: Any) -> dict[str, Any]:
    tool_calls = [
        {
            "id": tool_call.id or f"call_{uuid4().hex[:8]}",
            "type": "function",
            "function": {
                "name": tool_call.function.name,
                "arguments": tool_call.raw_arguments or tool_call.function.arguments,
            },
        }
        for tool_call in completion.tool_calls
    ]
    return {
        "role": "assistant",
        "content": completion.content or "",
        "tool_calls": tool_calls,
    }


def _build_tool_result_message(tool_call: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {
        "role": "tool",
        "tool_call_id": tool_call.id or "",
        "name": tool_call.function.name,
        "content": json.dumps(result, ensure_ascii=False),
    }
