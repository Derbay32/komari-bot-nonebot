"""OpenAI 兼容 API 客户端。"""

import json
from typing import Any, Never, cast

from nonebot import logger
from nonebot.plugin import require
from openai import APIConnectionError, APITimeoutError, AsyncOpenAI, OpenAIError

from komari_bot.common.untrusted_context import (
    UntrustedContext,
    apply_llm_security_boundary,
)

from .base_client import (
    BaseLLMClient,
    LLMCompletionResultSchema,
    LLMToolCallFunctionSchema,
    LLMToolCallSchema,
    UnifiedUsageSchema,
)
from .config_schema import DynamicConfigSchema

# 依赖 config_manager 插件
config_manager_plugin = require("config_manager")

# 获取配置管理器
config_manager = config_manager_plugin.get_config_manager(
    "llm_provider", DynamicConfigSchema
)


class OpenAICompatibleClient(BaseLLMClient):
    """OpenAI 兼容 API 客户端。"""

    _INVALID_RESPONSE_MESSAGE = "OpenAI 兼容 API 响应格式异常"

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

    @classmethod
    def _raise_invalid_response(cls) -> "Never":
        """抛出响应格式异常。"""
        raise RuntimeError(cls._INVALID_RESPONSE_MESSAGE)

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
            **kwargs: 其他参数（如 frequency_penalty）

        Returns:
            生成的文本
        """
        config = config_manager.get()
        try:
            reasoning_effort, thinking_disabled, suppress_tool_choice = (
                self._resolve_thinking_params(model, **kwargs)
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

            request_data = {
                "model": model,
                "messages": messages,
                "temperature": temperature
                if temperature is not None
                else config.temperature,
                "max_tokens": max_tokens
                if max_tokens is not None
                else config.max_tokens,
                "frequency_penalty": kwargs.get(
                    "frequency_penalty", config.frequency_penalty
                ),
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

            extra_body: dict[str, Any] = {}
            if thinking_disabled:
                extra_body["thinking"] = {"type": "disabled"}
            extra_params = getattr(config, "extra_params", {})
            if extra_params:
                for key, value in extra_params.items():
                    if key in ("thinking", "enable_thinking") and extra_body:
                        logger.warning("extra_params 中的 {} 与思考模式控制冲突，已忽略", key)
                        continue
                    extra_body[key] = value
            if extra_body:
                logger.debug("注入 OpenAI 兼容 API extra_body 键名: {}", sorted(extra_body))
                request_data["extra_body"] = extra_body

            response = await self.client.chat.completions.create(**request_data)
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
            result = self._build_completion_result(response)
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
        **kwargs,  # noqa: ANN003
    ) -> LLMCompletionResultSchema:
        """使用 OpenAI 格式 messages 直接生成文本（支持多模态）。

        Args:
            messages: 消息列表 [{role, content}]，content 可以是字符串或数组（OpenAI Vision 格式）
            model: 模型名称
            temperature: 温度参数
            max_tokens: 最大 token 数
            response_format: OpenAI 兼容结构化输出参数，非空时会下发到模型
            **kwargs: 其他参数

        Returns:
            生成的文本
        """
        config = config_manager.get()
        try:
            reasoning_effort, thinking_disabled, suppress_tool_choice = (
                self._resolve_thinking_params(model, **kwargs)
            )

            safe_messages = apply_llm_security_boundary(
                messages,
                untrusted_contexts=untrusted_contexts,
            )
            request_data = {
                "model": model,
                "messages": safe_messages,
                "temperature": temperature
                if temperature is not None
                else config.temperature,
                "max_tokens": max_tokens
                if max_tokens is not None
                else config.max_tokens,
                "frequency_penalty": kwargs.get(
                    "frequency_penalty", config.frequency_penalty
                ),
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

            extra_body: dict[str, Any] = {}
            if thinking_disabled:
                extra_body["thinking"] = {"type": "disabled"}
            extra_params = getattr(config, "extra_params", {})
            if extra_params:
                for key, value in extra_params.items():
                    if key in ("thinking", "enable_thinking") and extra_body:
                        logger.warning("extra_params 中的 {} 与思考模式控制冲突，已忽略", key)
                        continue
                    extra_body[key] = value
            if extra_body:
                logger.debug("注入 OpenAI 兼容 API extra_body 键名: {}", sorted(extra_body))
                request_data["extra_body"] = extra_body

            logger.debug(
                f"OpenAI 兼容 API 请求 (messages):\n"
                f"  model: {model}\n"
                f"  messages: {len(safe_messages)} turns\n"
                f"  temperature: {request_data['temperature']}\n"
                f"  max_tokens: {request_data['max_tokens']}\n"
                f"  reasoning_effort: {reasoning_effort}\n"
                f"  thinking_disabled: {thinking_disabled}\n"
                f"  suppress_tool_choice: {suppress_tool_choice}\n"
                f"  frequency_penalty: {request_data['frequency_penalty']}\n"
                f"  tools_count: {len(tools or [])}\n"
                f"  has_response_format: {response_format is not None}\n"
                f"  has_parallel_tool_calls: {parallel_tool_calls is not None}"
            )

            response = await self.client.chat.completions.create(**request_data)

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
            result = self._build_completion_result(response)
            logger.debug(
                "OpenAI 兼容 API 响应: content_chars={} reasoning_chars={} tool_calls={} finish_reason={}",
                len(result.content),
                len(result.reasoning_content or ""),
                len(result.tool_calls),
                result.finish_reason or "-",
            )
            return result

    async def test_connection(self, model: str | None = None) -> bool:
        """测试 API 连接。

        Returns:
            连接是否成功
        """
        config = config_manager.get()
        try:
            await self.client.chat.completions.create(
                model=model or config.model,
                messages=cast(
                    "Any",
                    apply_llm_security_boundary(
                        [{"role": "user", "content": "你好"}]
                    ),
                ),
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
