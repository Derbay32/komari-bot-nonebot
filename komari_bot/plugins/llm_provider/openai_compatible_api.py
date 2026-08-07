"""OpenAI 兼容 API 客户端（Chat Completions / Responses 双协议 + 流式聚合）。"""

import inspect
import json
from typing import Any, Never, cast

from nonebot import logger
from nonebot.plugin import require
from openai import APIConnectionError, APITimeoutError, AsyncOpenAI, OpenAIError

from komari_bot.llm.llm_protocol import DEFAULT_REQUEST_API, RequestApi
from komari_bot.llm.untrusted_context import (
    UntrustedContext,
    apply_llm_security_boundary,
)

from .base_client import (
    CONTINUATION_METADATA_KEY,
    BaseLLMClient,
    LLMCompletionResultSchema,
    LLMProviderContinuationSchema,
    LLMToolCallFunctionSchema,
    LLMToolCallSchema,
    UnifiedUsageSchema,
)
from .config_schema import (
    DynamicConfigSchema,
    get_unsupported_extra_param_keys,
)

# 依赖 config_manager 插件
config_manager_plugin = require("config_manager")

# 获取配置管理器
config_manager = config_manager_plugin.get_config_manager(
    "llm_provider", DynamicConfigSchema
)

# extra_params 白名单中有正式 Responses 映射、可直通的采样键；
# 其余白名单键（logprobs/min_p/presence_penalty/repetition_penalty/seed/stop/top_k）
# 在 Responses 协议下静默忽略——已确认的设计取舍，不告警。
_RESPONSES_PASSTHROUGH_EXTRA_KEYS = frozenset({"top_p", "top_logprobs"})

_RESPONSES_INCOMPLETE_LENGTH = "max_output_tokens"
_RESPONSES_INCOMPLETE_CONTENT_FILTER = "content_filter"


class OpenAICompatibleClient(BaseLLMClient):
    """OpenAI 兼容 API 客户端。"""

    _INVALID_RESPONSE_MESSAGE = "OpenAI 兼容 API 响应格式异常"
    _INVALID_RESPONSES_STATUS_MESSAGE = "Responses API 响应状态异常"

    def __init__(
        self,
        api_token: str,
        base_url: str,
        timeout_seconds: float = 300.0,
    ) -> None:
        """初始化客户端。

        Args:
            api_token: OpenAI 兼容 API Token
            base_url: OpenAI 兼容 API Base URL
            timeout_seconds: 请求总超时时间（秒）
        """
        self.api_token = api_token
        self.timeout_seconds = timeout_seconds
        self.base_url = base_url
        self.client = AsyncOpenAI(
            api_key=api_token,
            base_url=base_url,
            timeout=timeout_seconds,
        )

    @staticmethod
    def _is_deepseek_v4_model(model: str) -> bool:
        """判断是否 deepseek-v4 系模型（默认开启思考，需主动关闭）。"""
        return "deepseek-v4" in model.lower()

    @staticmethod
    def _resolve_thinking_params(
        model: str,
        **kwargs: object,
    ) -> tuple[str | None, bool, bool]:
        """解析思考模式相关参数。

        thinking_mode 与 reasoning_effort 均由调用方通过 kwargs 传入（per-call），
        不再读取任何全局配置。

        Returns:
            (reasoning_effort, thinking_disabled, suppress_tool_choice)
            - reasoning_effort: 非 deepseek-v4 系且思考开启时返回 effort 值，否则 None
            - thinking_disabled: deepseek-v4 系且思考关闭时返回 True（注入 thinking:disabled）
            - suppress_tool_choice: 思考模式启用时返回 True（跳过 tool_choice 注入）
        """
        thinking_mode = bool(kwargs.get("thinking_mode", False))
        raw_effort = kwargs.get("reasoning_effort", "")
        is_v4 = OpenAICompatibleClient._is_deepseek_v4_model(model)

        if is_v4:
            thinking_disabled = not thinking_mode
            reasoning_effort = None
        else:
            thinking_disabled = False
            if thinking_mode:
                effort = str(raw_effort).strip() if raw_effort is not None else ""
                reasoning_effort = effort or None
            else:
                reasoning_effort = None

        suppress_tool_choice = thinking_mode
        return reasoning_effort, thinking_disabled, suppress_tool_choice

    @staticmethod
    def _resolve_request_mode(
        config: object,
        request_api: RequestApi | None,
        *,
        stream_enabled: bool | None,
    ) -> tuple[RequestApi, bool]:
        """解析请求模式：None 分量回退默认槽位配置快照。"""
        resolved_api: RequestApi = (
            request_api
            if request_api is not None
            else cast(
                "RequestApi",
                getattr(config, "request_api", DEFAULT_REQUEST_API),
            )
        )
        if resolved_api not in ("chat_completions", "responses"):
            msg = f"未知请求 API: {resolved_api}"
            raise ValueError(msg)
        resolved_stream = (
            bool(stream_enabled)
            if stream_enabled is not None
            else bool(getattr(config, "stream_enabled", False))
        )
        return resolved_api, resolved_stream

    @staticmethod
    def _build_extra_body(config: object, *, thinking_disabled: bool) -> dict[str, Any]:
        """构造受白名单约束的 extra_body，避免覆盖正式请求字段。"""
        extra_body: dict[str, Any] = {}
        if thinking_disabled:
            extra_body["thinking"] = {"type": "disabled"}

        extra_params = getattr(config, "extra_params", {})
        if not isinstance(extra_params, dict):
            msg = "extra_params 必须是对象"
            raise TypeError(msg)
        unsupported = get_unsupported_extra_param_keys(extra_params)
        if unsupported:
            msg = f"extra_params 包含不允许的键: {', '.join(unsupported)}"
            raise ValueError(msg)
        extra_body.update(extra_params)
        return extra_body

    @classmethod
    def _raise_invalid_response(cls) -> "Never":
        """抛出响应格式异常。"""
        raise RuntimeError(cls._INVALID_RESPONSE_MESSAGE)

    @classmethod
    def _raise_invalid_responses_status(cls, status: str) -> "Never":
        """抛出稳定的 Responses 终态异常，进入现有请求失败链路。"""
        raise RuntimeError(  # noqa: TRY003
            f"{cls._INVALID_RESPONSES_STATUS_MESSAGE}: {status}"
        )

    @staticmethod
    def _safe_get_int(obj: Any, key: str) -> int | None:
        """从对象、字典或 model_extra 中安全提取整数值。"""
        val = OpenAICompatibleClient._safe_get_value(obj, key)
        if val is None:
            return None
        try:
            return int(val)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _safe_get_value(obj: Any, key: str) -> Any:
        """从对象、普通字典或 Pydantic model_extra 中安全取值。"""
        if isinstance(obj, dict):
            val = obj.get(key)
            model_extra = obj.get("model_extra")
        else:
            val = getattr(obj, key, None)
            model_extra = getattr(obj, "model_extra", None)
        if val is None and isinstance(model_extra, dict):
            val = model_extra.get(key)
        return val

    @staticmethod
    def _extract_unified_usage(response: Any) -> UnifiedUsageSchema | None:
        """从 OpenAI 兼容响应中安全提取统一用量信息。

        支持标准 OpenAI 格式、DeepSeek 扩展字段（prompt_cache_hit_tokens /
        prompt_cache_miss_tokens）和 Pydantic model_extra。
        缺失或异常字段不影响解析，对应位置保留 None。
        """
        usage_obj = OpenAICompatibleClient._safe_get_value(response, "usage")
        if usage_obj is None:
            return None

        try:
            input_tokens = OpenAICompatibleClient._safe_get_int(
                usage_obj, "prompt_tokens"
            )
            output_tokens = OpenAICompatibleClient._safe_get_int(
                usage_obj, "completion_tokens"
            )
            total_tokens = OpenAICompatibleClient._safe_get_int(
                usage_obj, "total_tokens"
            )

            # 缓存命中：优先 DeepSeek prompt_cache_hit_tokens，
            # 不存在时回退 OpenAI prompt_tokens_details.cached_tokens
            cached_input_tokens = OpenAICompatibleClient._safe_get_int(
                usage_obj, "prompt_cache_hit_tokens"
            )
            if cached_input_tokens is None:
                details = OpenAICompatibleClient._safe_get_value(
                    usage_obj, "prompt_tokens_details"
                )
                if details is not None:
                    cached_input_tokens = OpenAICompatibleClient._safe_get_int(
                        details, "cached_tokens"
                    )

            # 缓存未命中：仅 DeepSeek 报告
            cache_miss_input_tokens = OpenAICompatibleClient._safe_get_int(
                usage_obj, "prompt_cache_miss_tokens"
            )

            # 推理输出 token
            reasoning_output_tokens = None
            completion_details = OpenAICompatibleClient._safe_get_value(
                usage_obj, "completion_tokens_details"
            )
            if completion_details is not None:
                reasoning_output_tokens = OpenAICompatibleClient._safe_get_int(
                    completion_details, "reasoning_tokens"
                )

            return UnifiedUsageSchema(
                input_tokens=input_tokens,
                cached_input_tokens=cached_input_tokens,
                cache_miss_input_tokens=cache_miss_input_tokens,
                output_tokens=output_tokens,
                reasoning_output_tokens=reasoning_output_tokens,
                total_tokens=total_tokens,
            )
        except Exception:
            logger.warning(
                "[LLM Provider] usage 提取异常，已忽略", exc_info=True
            )
            return None

    @staticmethod
    def _parse_tool_arguments(raw_arguments: str) -> dict[str, Any] | None:
        """安全解析工具参数 JSON。"""
        if not raw_arguments.strip():
            return {}
        try:
            parsed = json.loads(raw_arguments)
        except json.JSONDecodeError:
            return None
        return parsed if isinstance(parsed, dict) else None

    # ==================== Chat Completions 路径 ====================

    @staticmethod
    def _strip_internal_metadata(
        messages: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """剥离消息上的内部元数据（`_` 前缀键），永不发往上游端点。"""
        return [
            {key: value for key, value in message.items() if not key.startswith("_")}
            for message in messages
        ]

    def _build_chat_request_data(
        self,
        *,
        config: object,
        model: str,
        safe_messages: list[dict[str, Any]],
        temperature: float | None,
        max_tokens: int | None,
        frequency_penalty: float,
        response_format: dict | None,
        tools: list[dict[str, Any]] | None,
        tool_choice: str | dict[str, Any] | None,
        parallel_tool_calls: bool | None,
        reasoning_effort: str | None,
        thinking_disabled: bool,
        suppress_tool_choice: bool,
    ) -> dict[str, Any]:
        """构造 Chat Completions 请求体（非流式行为逐字节保持）。"""
        request_data: dict[str, Any] = {
            "model": model,
            "messages": self._strip_internal_metadata(safe_messages),
            "temperature": temperature
            if temperature is not None
            else config.temperature,  # type: ignore[attr-defined]
            "max_tokens": max_tokens
            if max_tokens is not None
            else config.max_tokens,  # type: ignore[attr-defined]
            "frequency_penalty": frequency_penalty,
        }

        if response_format is not None:
            request_data["response_format"] = response_format

        if tools is not None:
            request_data["tools"] = tools
        if tool_choice is not None and not suppress_tool_choice:
            request_data["tool_choice"] = tool_choice
        elif tool_choice is not None and suppress_tool_choice:
            logger.warning(
                "思考模式启用，已跳过 tool_choice 注入 (model={}, tool_choice={})",
                model,
                tool_choice,
            )
        if parallel_tool_calls is not None:
            request_data["parallel_tool_calls"] = parallel_tool_calls

        if reasoning_effort is not None:
            request_data["reasoning_effort"] = reasoning_effort

        extra_body = self._build_extra_body(
            config,
            thinking_disabled=thinking_disabled,
        )
        if extra_body:
            logger.debug("注入 OpenAI 兼容 API extra_body 键名: {}", sorted(extra_body))
            request_data["extra_body"] = extra_body

        return request_data

    def _build_completion_result(self, response: Any) -> LLMCompletionResultSchema:
        """将 OpenAI 兼容响应转换为统一结果。"""
        if not getattr(response, "choices", None):
            logger.error(
                "OpenAI 兼容 API 响应格式异常: response_type={}",
                type(response).__name__,
            )
            self._raise_invalid_response()

        choice = response.choices[0]
        message = getattr(choice, "message", None)
        if message is None:
            logger.error(
                "OpenAI 兼容 API 响应缺少 message: response_type={}",
                type(response).__name__,
            )
            self._raise_invalid_response()

        content = getattr(message, "content", None) or ""
        reasoning_content = getattr(message, "reasoning_content", None)
        if reasoning_content is None:
            message_extra = getattr(message, "model_extra", None)
            if isinstance(message_extra, dict):
                reasoning_content = message_extra.get("reasoning_content")
        if reasoning_content is not None:
            reasoning_content = str(reasoning_content).strip() or None
        tool_calls: list[LLMToolCallSchema] = []
        raw_tool_calls = getattr(message, "tool_calls", None) or []
        for raw_tool_call in raw_tool_calls:
            function = getattr(raw_tool_call, "function", None)
            function_name = str(getattr(function, "name", "")).strip()
            raw_arguments = str(getattr(function, "arguments", "") or "")
            if not function_name:
                continue
            tool_calls.append(
                LLMToolCallSchema(
                    id=getattr(raw_tool_call, "id", None),
                    type=str(getattr(raw_tool_call, "type", "function") or "function"),
                    function=LLMToolCallFunctionSchema(
                        name=function_name,
                        arguments=raw_arguments,
                    ),
                    raw_arguments=raw_arguments,
                    parsed_arguments=self._parse_tool_arguments(raw_arguments),
                )
            )

        finish_reason = getattr(choice, "finish_reason", None)
        usage = self._extract_unified_usage(response)
        return LLMCompletionResultSchema(
            content=content.strip(),
            reasoning_content=reasoning_content,
            tool_calls=tool_calls,
            finish_reason=str(finish_reason) if finish_reason is not None else None,
            usage=usage,
        )

    @staticmethod
    async def _close_stream(stream: Any) -> None:
        """尽力关闭 SDK stream；关闭失败不遮蔽主流程异常。"""
        close = getattr(stream, "close", None)
        if not callable(close):
            return
        try:
            result = close()
            if inspect.isawaitable(result):
                await result
        except Exception as exc:
            logger.warning(
                "[LLM Provider] 关闭上游流失败: error_type={}",
                type(exc).__name__,
            )

    async def _call_chat_completion_stream(
        self,
        request_data: dict[str, Any],
    ) -> LLMCompletionResultSchema:
        """消费 Chat Completions SSE 流并聚合为统一完成结果。

        流正常迭代结束即终态；任何异常或取消都会丢弃部分聚合结果，
        部分结果永不构造成成功完成对象。
        """
        request = {
            **request_data,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        stream = await self.client.chat.completions.create(**request)

        content_parts: list[str] = []
        reasoning_parts: list[str] = []
        tool_slots: dict[int, dict[str, Any]] = {}
        finish_reason: str | None = None
        usage_holder: Any = None
        try:
            async for chunk in stream:
                if self._safe_get_value(chunk, "usage") is not None:
                    usage_holder = chunk
                choices = self._safe_get_value(chunk, "choices") or []
                if not choices:
                    continue
                choice = choices[0]
                delta = self._safe_get_value(choice, "delta")
                if delta is not None:
                    text = self._safe_get_value(delta, "content")
                    if text:
                        content_parts.append(str(text))
                    reasoning = self._safe_get_value(delta, "reasoning_content")
                    if reasoning:
                        reasoning_parts.append(str(reasoning))
                    for tool_delta in (
                        self._safe_get_value(delta, "tool_calls") or []
                    ):
                        raw_index = self._safe_get_value(tool_delta, "index")
                        index = raw_index if isinstance(raw_index, int) else 0
                        slot = tool_slots.setdefault(
                            index,
                            {
                                "id": None,
                                "type": "function",
                                "name_parts": [],
                                "argument_parts": [],
                            },
                        )
                        tool_id = self._safe_get_value(tool_delta, "id")
                        if tool_id:
                            slot["id"] = str(tool_id)
                        tool_type = self._safe_get_value(tool_delta, "type")
                        if tool_type:
                            slot["type"] = str(tool_type)
                        function = self._safe_get_value(tool_delta, "function")
                        if function is not None:
                            name_part = self._safe_get_value(function, "name")
                            if name_part:
                                slot["name_parts"].append(str(name_part))
                            argument_part = self._safe_get_value(
                                function, "arguments"
                            )
                            if argument_part:
                                slot["argument_parts"].append(str(argument_part))
                chunk_finish = self._safe_get_value(choice, "finish_reason")
                if chunk_finish:
                    finish_reason = str(chunk_finish)
        finally:
            await self._close_stream(stream)

        tool_calls: list[LLMToolCallSchema] = []
        for index in sorted(tool_slots):
            slot = tool_slots[index]
            function_name = "".join(slot["name_parts"]).strip()
            if not function_name:
                continue
            raw_arguments = "".join(slot["argument_parts"])
            tool_calls.append(
                LLMToolCallSchema(
                    id=slot["id"],
                    type=slot["type"],
                    function=LLMToolCallFunctionSchema(
                        name=function_name,
                        arguments=raw_arguments,
                    ),
                    raw_arguments=raw_arguments,
                    parsed_arguments=self._parse_tool_arguments(raw_arguments),
                )
            )

        usage = (
            self._extract_unified_usage(usage_holder)
            if usage_holder is not None
            else None
        )
        reasoning_content = "".join(reasoning_parts).strip() or None
        return LLMCompletionResultSchema(
            content="".join(content_parts).strip(),
            reasoning_content=reasoning_content,
            tool_calls=tool_calls,
            finish_reason=finish_reason,
            usage=usage,
        )

    # ==================== Responses 路径 ====================

    @staticmethod
    def _to_responses_user_item(content: Any) -> list[dict[str, Any]]:
        """把 user 消息内容翻译为 Responses input_text / input_image 条目。"""
        if isinstance(content, str):
            return [
                {
                    "role": "user",
                    "content": [{"type": "input_text", "text": content}],
                }
            ]
        if not isinstance(content, list):
            return []
        parts: list[dict[str, Any]] = []
        for part in content:
            if not isinstance(part, dict):
                continue
            part_type = part.get("type")
            if part_type == "text":
                parts.append(
                    {"type": "input_text", "text": str(part.get("text", ""))}
                )
            elif part_type == "image_url":
                image_url = part.get("image_url")
                if isinstance(image_url, dict):
                    url = str(image_url.get("url", ""))
                else:
                    url = str(image_url or "")
                if url:
                    parts.append({"type": "input_image", "image_url": url})
        return [{"role": "user", "content": parts}]

    def _to_responses_input_items(
        self,
        message: dict[str, Any],
    ) -> list[dict[str, Any]]:
        """把统一消息结构翻译为 Responses input 条目列表。"""
        role = message.get("role")

        if role == "user":
            return self._to_responses_user_item(message.get("content"))

        if role == "assistant":
            continuation = message.get(CONTINUATION_METADATA_KEY)
            if continuation is not None:
                # 校验协议并原样展开 output items，避免与重建的
                # assistant 文本/工具调用重复。
                continuation_api = self._safe_get_value(continuation, "api")
                if continuation_api != "responses":
                    self._raise_invalid_responses_status(
                        f"continuation 协议不匹配: {continuation_api}"
                    )
                output_items = (
                    self._safe_get_value(continuation, "output_items") or []
                )
                return [
                    self._serialize_output_item(item) for item in output_items
                ]

            items: list[dict[str, Any]] = []
            content = message.get("content")
            if isinstance(content, str) and content:
                items.append(
                    {
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": content}],
                    }
                )
            elif isinstance(content, list):
                texts = [
                    str(part.get("text", ""))
                    for part in content
                    if isinstance(part, dict) and part.get("type") == "text"
                ]
                if texts:
                    items.append(
                        {
                            "role": "assistant",
                            "content": [
                                {"type": "output_text", "text": "\n".join(texts)}
                            ],
                        }
                    )
            for tool_call in message.get("tool_calls") or []:
                if not isinstance(tool_call, dict):
                    continue
                function = tool_call.get("function")
                function = function if isinstance(function, dict) else {}
                function_name = str(function.get("name", "")).strip()
                if not function_name:
                    continue
                items.append(
                    {
                        "type": "function_call",
                        "call_id": str(tool_call.get("id") or function_name),
                        "name": function_name,
                        "arguments": str(function.get("arguments", "") or ""),
                    }
                )
            return items

        if role == "tool":
            output = message.get("content")
            if isinstance(output, list):
                output = "\n".join(
                    str(part.get("text", ""))
                    for part in output
                    if isinstance(part, dict) and part.get("type") == "text"
                )
            return [
                {
                    "type": "function_call_output",
                    "call_id": str(message.get("tool_call_id", "")),
                    "output": str(output or ""),
                }
            ]

        logger.warning(
            "[LLM Provider] Responses 输入转换跳过未知角色: role={}",
            role,
        )
        return []

    @staticmethod
    def _flatten_responses_tool(tool: dict[str, Any]) -> dict[str, Any]:
        """把 Chat 格式的函数工具扁平化为 Responses 格式。

        保留调用方显式 strict 值，未提供时默认 strict=false，
        避免现有 JSON Schema 被强制严格模式拒绝。
        """
        if tool.get("type") == "function":
            function = tool.get("function")
            if isinstance(function, dict):
                flattened: dict[str, Any] = {
                    "type": "function",
                    "name": function.get("name", ""),
                }
                if function.get("description") is not None:
                    flattened["description"] = function["description"]
                if function.get("parameters") is not None:
                    flattened["parameters"] = function["parameters"]
                flattened["strict"] = function.get("strict", False)
                return flattened
            flattened = dict(tool)
            flattened.setdefault("strict", False)
            return flattened
        return dict(tool)

    @staticmethod
    def _translate_responses_tool_choice(
        tool_choice: str | dict[str, Any],
    ) -> Any:
        """把 Chat 格式 tool_choice 翻译为 Responses 格式。"""
        if isinstance(tool_choice, str):
            return tool_choice
        if isinstance(tool_choice, dict) and tool_choice.get("type") == "function":
            function = tool_choice.get("function")
            if isinstance(function, dict) and function.get("name"):
                return {"type": "function", "name": function["name"]}
        return tool_choice

    @staticmethod
    def _translate_responses_text_format(
        response_format: dict[str, Any],
    ) -> dict[str, Any]:
        """把 Chat response_format 翻译为 Responses text.format。"""
        format_type = response_format.get("type")
        if format_type == "json_schema":
            schema_payload = response_format.get("json_schema")
            schema_payload = schema_payload if isinstance(schema_payload, dict) else {}
            translated: dict[str, Any] = {
                "type": "json_schema",
                "name": schema_payload.get("name", "response"),
                "schema": schema_payload.get("schema", {}),
                "strict": schema_payload.get("strict", False),
            }
            if schema_payload.get("description") is not None:
                translated["description"] = schema_payload["description"]
            return {"format": translated}
        if format_type in ("json_object", "text"):
            return {"format": {"type": format_type}}
        return {"format": dict(response_format)}

    def _build_responses_request_data(
        self,
        *,
        config: object,
        model: str,
        safe_messages: list[dict[str, Any]],
        temperature: float | None,
        max_tokens: int | None,
        response_format: dict | None,
        tools: list[dict[str, Any]] | None,
        tool_choice: str | dict[str, Any] | None,
        parallel_tool_calls: bool | None,
        reasoning_effort: str | None,
        suppress_tool_choice: bool,
    ) -> dict[str, Any]:
        """把统一请求翻译为 Responses 协议请求体。

        安全边界已在 safe_messages 中完成排序；全部 system 内容按原顺序
        合并到 instructions，不可信上下文仅以 user 数据身份进入 input。
        固定 store=false；不使用 previous_response_id / conversation / 托管工具。
        frequency_penalty、DeepSeek thinking extra body 与不兼容的
        extra_params 键静默忽略（不发送、不偷渡、不告警）。
        """
        instructions_parts: list[str] = []
        input_items: list[dict[str, Any]] = []
        for message in safe_messages:
            if message.get("role") == "system":
                content = message.get("content")
                if isinstance(content, str) and content:
                    instructions_parts.append(content)
                continue
            input_items.extend(self._to_responses_input_items(message))

        request_data: dict[str, Any] = {
            "model": model,
            "instructions": "\n\n".join(instructions_parts),
            "input": input_items,
            "store": False,
            "temperature": temperature
            if temperature is not None
            else config.temperature,  # type: ignore[attr-defined]
            "max_output_tokens": max_tokens
            if max_tokens is not None
            else config.max_tokens,  # type: ignore[attr-defined]
        }

        if tools is not None:
            request_data["tools"] = [
                self._flatten_responses_tool(tool) for tool in tools
            ]
            if tools:
                # 存在函数工具时请求加密推理内容，支持无状态推理续接
                request_data["include"] = ["reasoning.encrypted_content"]
        if tool_choice is not None and not suppress_tool_choice:
            request_data["tool_choice"] = self._translate_responses_tool_choice(
                tool_choice
            )
        elif tool_choice is not None and suppress_tool_choice:
            logger.warning(
                "思考模式启用，已跳过 tool_choice 注入 (model={}, tool_choice={})",
                model,
                tool_choice,
            )
        if parallel_tool_calls is not None:
            request_data["parallel_tool_calls"] = parallel_tool_calls

        if response_format is not None:
            request_data["text"] = self._translate_responses_text_format(
                response_format
            )

        if reasoning_effort is not None:
            request_data["reasoning"] = {"effort": reasoning_effort}

        extra_params = getattr(config, "extra_params", {})
        if not isinstance(extra_params, dict):
            msg = "extra_params 必须是对象"
            raise TypeError(msg)
        unsupported = get_unsupported_extra_param_keys(extra_params)
        if unsupported:
            msg = f"extra_params 包含不允许的键: {', '.join(unsupported)}"
            raise ValueError(msg)
        for key in sorted(extra_params):
            if key in _RESPONSES_PASSTHROUGH_EXTRA_KEYS:
                request_data[key] = extra_params[key]

        return request_data

    @staticmethod
    def _serialize_output_item(item: Any) -> dict[str, Any]:
        """把 Responses output item 序列化为可回传的 plain dict。"""
        serialized = OpenAICompatibleClient._serialize_json_value(item)
        return serialized if isinstance(serialized, dict) else {"value": serialized}

    @staticmethod
    def _serialize_json_value(value: Any) -> Any:
        """递归序列化 SDK 对象 / 字典 / 列表为 JSON 兼容结构。"""
        if value is None or isinstance(value, (str, int, float, bool)):
            return value
        if isinstance(value, dict):
            return {
                str(key): OpenAICompatibleClient._serialize_json_value(val)
                for key, val in value.items()
            }
        if isinstance(value, (list, tuple)):
            return [
                OpenAICompatibleClient._serialize_json_value(item)
                for item in value
            ]
        model_dump = getattr(value, "model_dump", None)
        if callable(model_dump):
            try:
                return OpenAICompatibleClient._serialize_json_value(
                    model_dump(mode="json")
                )
            except Exception:
                pass
        if hasattr(value, "__dict__"):
            return {
                key: OpenAICompatibleClient._serialize_json_value(val)
                for key, val in vars(value).items()
                if not key.startswith("_")
            }
        return str(value)

    def _extract_responses_usage(self, response: Any) -> UnifiedUsageSchema | None:
        """从 Responses 终态对象提取统一用量；未报告则为 None。"""
        usage_obj = self._safe_get_value(response, "usage")
        if usage_obj is None:
            return None
        try:
            cached_input_tokens = None
            input_details = self._safe_get_value(usage_obj, "input_tokens_details")
            if input_details is not None:
                cached_input_tokens = self._safe_get_int(
                    input_details, "cached_tokens"
                )
            reasoning_output_tokens = None
            output_details = self._safe_get_value(usage_obj, "output_tokens_details")
            if output_details is not None:
                reasoning_output_tokens = self._safe_get_int(
                    output_details, "reasoning_tokens"
                )
            return UnifiedUsageSchema(
                input_tokens=self._safe_get_int(usage_obj, "input_tokens"),
                cached_input_tokens=cached_input_tokens,
                output_tokens=self._safe_get_int(usage_obj, "output_tokens"),
                reasoning_output_tokens=reasoning_output_tokens,
                total_tokens=self._safe_get_int(usage_obj, "total_tokens"),
            )
        except Exception:
            logger.warning(
                "[LLM Provider] Responses usage 提取异常，已忽略", exc_info=True
            )
            return None

    def _parse_responses_response(
        self,
        response: Any,
    ) -> LLMCompletionResultSchema:
        """Responses 终态归一化（流式与非流式共用同一解析器）。

        - completed + 函数调用 → finish_reason="tool_calls"；普通 completed → "stop"
        - 合法 incomplete：max_output_tokens → "length"；content_filter → "content_filter"
        - refusal 文本并入 content 并标记 content_filter（可交付内容而非异常）
        - failed / cancelled / 非预期 queued / in_progress / 未知 incomplete 原因
          → 丢弃部分结果并抛稳定 provider 异常
        """
        status = str(self._safe_get_value(response, "status") or "")
        finish_override: str | None = None
        if status == "completed":
            finish_override = None
        elif status == "incomplete":
            incomplete_details = self._safe_get_value(response, "incomplete_details")
            reason = str(self._safe_get_value(incomplete_details, "reason") or "")
            if reason == _RESPONSES_INCOMPLETE_LENGTH:
                finish_override = "length"
            elif reason == _RESPONSES_INCOMPLETE_CONTENT_FILTER:
                finish_override = "content_filter"
            else:
                self._raise_invalid_responses_status(f"incomplete:{reason or '-'}")
        else:
            self._raise_invalid_responses_status(status or "missing")

        content_parts: list[str] = []
        refusal_seen = False
        tool_calls: list[LLMToolCallSchema] = []
        output_items = list(self._safe_get_value(response, "output") or [])
        for item in output_items:
            item_type = self._safe_get_value(item, "type")
            if item_type == "message":
                for part in self._safe_get_value(item, "content") or []:
                    part_type = self._safe_get_value(part, "type")
                    if part_type == "output_text":
                        text = self._safe_get_value(part, "text")
                        if text:
                            content_parts.append(str(text))
                    elif part_type == "refusal":
                        refusal_text = self._safe_get_value(part, "refusal")
                        if refusal_text:
                            content_parts.append(str(refusal_text))
                        refusal_seen = True
            elif item_type == "function_call":
                function_name = str(self._safe_get_value(item, "name") or "").strip()
                if not function_name:
                    continue
                raw_arguments = str(self._safe_get_value(item, "arguments") or "")
                tool_calls.append(
                    LLMToolCallSchema(
                        id=self._safe_get_value(item, "call_id")
                        or self._safe_get_value(item, "id"),
                        type="function",
                        function=LLMToolCallFunctionSchema(
                            name=function_name,
                            arguments=raw_arguments,
                        ),
                        raw_arguments=raw_arguments,
                        parsed_arguments=self._parse_tool_arguments(raw_arguments),
                    )
                )

        if finish_override is not None:
            finish_reason = finish_override
        elif tool_calls:
            finish_reason = "tool_calls"
        elif refusal_seen:
            finish_reason = "content_filter"
        else:
            finish_reason = "stop"

        continuation = LLMProviderContinuationSchema(
            api="responses",
            output_items=[
                self._serialize_output_item(item) for item in output_items
            ],
        )
        return LLMCompletionResultSchema(
            content="".join(content_parts).strip(),
            reasoning_content=None,
            tool_calls=tool_calls,
            finish_reason=finish_reason,
            usage=self._extract_responses_usage(response),
            continuation=continuation,
        )

    async def _call_responses_stream(
        self,
        request_data: dict[str, Any],
    ) -> LLMCompletionResultSchema:
        """消费 Responses SSE 事件流至终态并用共用解析器归一化。

        流 error 事件、断流或无终态结束时丢弃部分结果并抛稳定异常。
        """
        request = {**request_data, "stream": True}
        stream = await self.client.responses.create(**request)

        final_response: Any = None
        try:
            async for event in stream:
                event_type = str(self._safe_get_value(event, "type") or "")
                if event_type in (
                    "response.completed",
                    "response.incomplete",
                    "response.failed",
                ):
                    final_response = self._safe_get_value(event, "response")
                    break
                if event_type in ("error", "response.error"):
                    self._raise_invalid_responses_status("stream_error")
        finally:
            await self._close_stream(stream)

        if final_response is None:
            self._raise_invalid_responses_status("stream_missing_terminal")
        return self._parse_responses_response(final_response)

    # ==================== 统一调度 ====================

    async def _dispatch_request(
        self,
        *,
        config: object,
        model: str,
        safe_messages: list[dict[str, Any]],
        temperature: float | None,
        max_tokens: int | None,
        frequency_penalty: float,
        response_format: dict | None,
        tools: list[dict[str, Any]] | None,
        tool_choice: str | dict[str, Any] | None,
        parallel_tool_calls: bool | None,
        reasoning_effort: str | None,
        thinking_disabled: bool,
        suppress_tool_choice: bool,
        request_api: RequestApi,
        stream_enabled: bool,
    ) -> LLMCompletionResultSchema:
        """按请求 API 与流式开关路由到具体的请求路径。"""
        if request_api == "responses":
            request_data = self._build_responses_request_data(
                config=config,
                model=model,
                safe_messages=safe_messages,
                temperature=temperature,
                max_tokens=max_tokens,
                response_format=response_format,
                tools=tools,
                tool_choice=tool_choice,
                parallel_tool_calls=parallel_tool_calls,
                reasoning_effort=reasoning_effort,
                suppress_tool_choice=suppress_tool_choice,
            )
            if stream_enabled:
                return await self._call_responses_stream(request_data)
            response = await self.client.responses.create(**request_data)
            return self._parse_responses_response(response)

        request_data = self._build_chat_request_data(
            config=config,
            model=model,
            safe_messages=safe_messages,
            temperature=temperature,
            max_tokens=max_tokens,
            frequency_penalty=frequency_penalty,
            response_format=response_format,
            tools=tools,
            tool_choice=tool_choice,
            parallel_tool_calls=parallel_tool_calls,
            reasoning_effort=reasoning_effort,
            thinking_disabled=thinking_disabled,
            suppress_tool_choice=suppress_tool_choice,
        )
        if stream_enabled:
            return await self._call_chat_completion_stream(request_data)
        response = await self.client.chat.completions.create(**request_data)
        return self._build_completion_result(response)

    async def generate_text(
        self,
        prompt: str,
        model: str,
        system_instruction: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        response_format: dict | None = None,
        *,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | dict[str, Any] | None = None,
        parallel_tool_calls: bool | None = None,
        untrusted_contexts: list[UntrustedContext] | None = None,
        request_api: RequestApi | None = None,
        stream_enabled: bool | None = None,
        **kwargs,  # noqa: ANN003
    ) -> LLMCompletionResultSchema:
        """生成文本（支持 JSON 模式）。

        Args:
            prompt: 用户提示词
            model: 模型名称
            system_instruction: 系统指令
            temperature: 温度参数
            max_tokens: 最大 token 数
            response_format: OpenAI 兼容结构化输出参数，非空时会下发到模型
            request_api: 请求 API（None 时解析默认槽位配置）
            stream_enabled: 是否启用流式传输（None 时解析默认槽位配置）
            **kwargs: 其他参数（如 frequency_penalty）

        Returns:
            生成的文本
        """
        config = config_manager.get()
        try:
            reasoning_effort, thinking_disabled, suppress_tool_choice = (
                self._resolve_thinking_params(model, **kwargs)
            )
            resolved_api, resolved_stream = self._resolve_request_mode(
                config, request_api, stream_enabled=stream_enabled
            )
            logger.debug(
                f"OpenAI 兼容 API 请求:\n"
                f"  model: {model}\n"
                f"  temperature: {temperature if temperature is not None else config.temperature}\n"
                f"  max_tokens: {max_tokens if max_tokens is not None else config.max_tokens}\n"
                f"  reasoning_effort: {reasoning_effort}\n"
                f"  thinking_disabled: {thinking_disabled}\n"
                f"  suppress_tool_choice: {suppress_tool_choice}\n"
                f"  frequency_penalty: {kwargs.get('frequency_penalty', config.frequency_penalty)}\n"
                f"  request_api: {resolved_api}\n"
                f"  stream_enabled: {resolved_stream}\n"
                f"  prompt_chars: {len(prompt)}\n"
                f"  system_instruction_chars: {len(system_instruction or '')}\n"
                f"  tools_count: {len(tools or [])}\n"
                f"  has_response_format: {response_format is not None}"
            )
            messages: list[dict[str, Any]] = []
            if system_instruction:
                messages.append({"role": "system", "content": system_instruction})
            messages.append({"role": "user", "content": prompt})
            messages = apply_llm_security_boundary(
                messages,
                untrusted_contexts=untrusted_contexts,
            )

            result = await self._dispatch_request(
                config=config,
                model=model,
                safe_messages=messages,
                temperature=temperature,
                max_tokens=max_tokens,
                frequency_penalty=kwargs.get(
                    "frequency_penalty", config.frequency_penalty
                ),
                response_format=response_format,
                tools=tools,
                tool_choice=tool_choice,
                parallel_tool_calls=parallel_tool_calls,
                reasoning_effort=reasoning_effort,
                thinking_disabled=thinking_disabled,
                suppress_tool_choice=suppress_tool_choice,
                request_api=resolved_api,
                stream_enabled=resolved_stream,
            )
        except APITimeoutError:
            logger.error("OpenAI 兼容 API 请求超时")
            raise
        except APIConnectionError as exc:
            logger.error(
                "OpenAI 兼容 API 网络错误: error_type={}",
                type(exc).__name__,
            )
            raise
        except OpenAIError as exc:
            status_code = getattr(exc, "status_code", None)
            logger.error(
                "OpenAI 兼容 API 调用失败: error_type={} status_code={}",
                type(exc).__name__,
                status_code if isinstance(status_code, int) else "-",
            )
            raise
        except Exception as exc:
            logger.error(
                "OpenAI 兼容 API 未知错误: error_type={}",
                type(exc).__name__,
            )
            raise
        else:
            logger.debug(
                "OpenAI 兼容 API 响应: content_chars={} reasoning_chars={} tool_calls={} finish_reason={}",
                len(result.content),
                len(result.reasoning_content or ""),
                len(result.tool_calls),
                result.finish_reason or "-",
            )
            return result

    async def generate_text_with_messages(
        self,
        messages: list[dict],
        model: str,
        temperature: float | None = None,
        max_tokens: int | None = None,
        response_format: dict | None = None,
        *,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | dict[str, Any] | None = None,
        parallel_tool_calls: bool | None = None,
        untrusted_contexts: list[UntrustedContext] | None = None,
        request_api: RequestApi | None = None,
        stream_enabled: bool | None = None,
        **kwargs,  # noqa: ANN003
    ) -> LLMCompletionResultSchema:
        """使用 OpenAI 格式 messages 直接生成文本（支持多模态）。

        Args:
            messages: 消息列表 [{role, content}]，content 可以是字符串或数组（OpenAI Vision 格式）
            model: 模型名称
            temperature: 温度参数
            max_tokens: 最大 token 数
            response_format: OpenAI 兼容结构化输出参数；非空时会下发到模型
            request_api: 请求 API（None 时解析默认槽位配置）
            stream_enabled: 是否启用流式传输（None 时解析默认槽位配置）
            **kwargs: 其他参数

        Returns:
            生成的文本
        """
        config = config_manager.get()
        try:
            reasoning_effort, thinking_disabled, suppress_tool_choice = (
                self._resolve_thinking_params(model, **kwargs)
            )
            resolved_api, resolved_stream = self._resolve_request_mode(
                config, request_api, stream_enabled=stream_enabled
            )

            safe_messages = apply_llm_security_boundary(
                messages,
                untrusted_contexts=untrusted_contexts,
            )
            logger.debug(
                f"OpenAI 兼容 API 请求 (messages):\n"
                f"  model: {model}\n"
                f"  messages: {len(safe_messages)} turns\n"
                f"  temperature: {temperature if temperature is not None else config.temperature}\n"
                f"  max_tokens: {max_tokens if max_tokens is not None else config.max_tokens}\n"
                f"  reasoning_effort: {reasoning_effort}\n"
                f"  thinking_disabled: {thinking_disabled}\n"
                f"  suppress_tool_choice: {suppress_tool_choice}\n"
                f"  frequency_penalty: {kwargs.get('frequency_penalty', config.frequency_penalty)}\n"
                f"  request_api: {resolved_api}\n"
                f"  stream_enabled: {resolved_stream}\n"
                f"  tools_count: {len(tools or [])}\n"
                f"  has_response_format: {response_format is not None}\n"
                f"  has_parallel_tool_calls: {parallel_tool_calls is not None}"
            )

            result = await self._dispatch_request(
                config=config,
                model=model,
                safe_messages=safe_messages,
                temperature=temperature,
                max_tokens=max_tokens,
                frequency_penalty=kwargs.get(
                    "frequency_penalty", config.frequency_penalty
                ),
                response_format=response_format,
                tools=tools,
                tool_choice=tool_choice,
                parallel_tool_calls=parallel_tool_calls,
                reasoning_effort=reasoning_effort,
                thinking_disabled=thinking_disabled,
                suppress_tool_choice=suppress_tool_choice,
                request_api=resolved_api,
                stream_enabled=resolved_stream,
            )

        except APITimeoutError:
            logger.error("OpenAI 兼容 API 请求超时")
            raise
        except APIConnectionError as exc:
            logger.error(
                "OpenAI 兼容 API 网络错误: error_type={}",
                type(exc).__name__,
            )
            raise
        except OpenAIError as exc:
            status_code = getattr(exc, "status_code", None)
            logger.error(
                "OpenAI 兼容 API 调用失败: error_type={} status_code={}",
                type(exc).__name__,
                status_code if isinstance(status_code, int) else "-",
            )
            raise
        else:
            logger.debug(
                "OpenAI 兼容 API 响应: content_chars={} reasoning_chars={} tool_calls={} finish_reason={}",
                len(result.content),
                len(result.reasoning_content or ""),
                len(result.tool_calls),
                result.finish_reason or "-",
            )
            return result

    async def test_connection(self, model: str | None = None) -> bool:
        """测试 API 连接（只验证默认槽位的请求 API + 流式组合）。

        Returns:
            连接是否成功
        """
        config = config_manager.get()
        resolved_api, resolved_stream = self._resolve_request_mode(
            config, None, stream_enabled=None
        )
        try:
            safe_messages = apply_llm_security_boundary(
                [{"role": "user", "content": "你好"}]
            )
            if resolved_api == "responses":
                request_data = self._build_responses_request_data(
                    config=config,
                    model=model or config.model,
                    safe_messages=safe_messages,
                    temperature=0.1,
                    max_tokens=16,
                    response_format=None,
                    tools=None,
                    tool_choice=None,
                    parallel_tool_calls=None,
                    reasoning_effort=None,
                    suppress_tool_choice=False,
                )
                if resolved_stream:
                    await self._call_responses_stream(request_data)
                else:
                    response = await self.client.responses.create(**request_data)
                    self._parse_responses_response(response)
            elif resolved_stream:
                request_data = self._build_chat_request_data(
                    config=config,
                    model=model or config.model,
                    safe_messages=safe_messages,
                    temperature=0.1,
                    max_tokens=10,
                    frequency_penalty=0.0,
                    response_format=None,
                    tools=None,
                    tool_choice=None,
                    parallel_tool_calls=None,
                    reasoning_effort=None,
                    thinking_disabled=False,
                    suppress_tool_choice=False,
                )
                await self._call_chat_completion_stream(request_data)
            else:
                await self.client.chat.completions.create(
                    model=model or config.model,
                    messages=cast("Any", safe_messages),
                    temperature=0.1,
                    max_tokens=10,
                )
        except Exception as exc:
            logger.error(
                "OpenAI 兼容 API 连接测试失败: error_type={}",
                type(exc).__name__,
            )
            return False
        else:
            return True

    async def close(self) -> None:
        """关闭客户端。"""
        await self.client.close()
