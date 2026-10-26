"""查询重写服务 - 仅重写当前用户输入。"""

from nonebot import logger
from nonebot.plugin import require

from komari_bot.core.retry import retry_async

from .config_interface import get_config

# 依赖 llm_provider 插件
require("llm_provider")

from komari_bot.plugins import llm_provider


class QueryRewriteService:
    """查询重写服务。"""

    def __init__(self) -> None:
        """初始化查询重写服务。"""

    def _build_rewrite_prompt(
        self,
        current_query: str,
    ) -> str:
        """构建查询重写的 Prompt。

        Args:
            current_query: 当前用户输入

        Returns:
            重写 Prompt
        """
        return f"""请将以下用户输入重写为一个语义完整、表达自然的句子。不要回答问题，只需重写。若原句已经清晰，可保持原意做最小修改。注意：输出必须使用简体中文。

用户输入：{current_query}"""

    @retry_async(max_attempts=2, base_delay=0.5)
    async def _generate_rewritten_completion(
        self,
        *,
        rewrite_prompt: str,
        model: str,
        thinking_mode: bool,
        reasoning_effort: str,
        request_api: str = "chat_completions",
        stream_enabled: bool = False,
        request_trace_id: str | None = None,
    ) -> str:
        """调用 LLM 生成重写后的查询。"""
        request_data = {
            "prompt": rewrite_prompt,
            "model": model,
            "temperature": 0.3,
            "max_tokens": 256,
            "thinking_mode": thinking_mode,
            "reasoning_effort": reasoning_effort,
            "request_api": request_api,
            "stream_enabled": stream_enabled,
        }
        result = await llm_provider.generate_completion(
            **request_data,
            request_phase="query_rewrite",
            request_trace_id=request_trace_id or "",
        )

        return result.content

    async def rewrite_query(
        self,
        current_query: str,
        *,
        request_trace_id: str | None = None,
    ) -> str:
        """重写查询为语义完整的独立语句。

        Args:
            current_query: 当前用户输入
            request_trace_id: 普通运行日志的请求追踪 ID

        Returns:
            重写后的查询，失败时返回原始查询
        """
        if not current_query.strip():
            return current_query

        # 获取最新配置
        config = get_config()

        try:
            # 构建重写 Prompt
            rewrite_prompt = self._build_rewrite_prompt(
                current_query=current_query,
            )

            # 调用 LLM 重写（使用查询重写专用槽位）
            rewritten = await self._generate_rewritten_completion(
                rewrite_prompt=rewrite_prompt,
                model=config.query_rewrite_model,
                thinking_mode=config.query_rewrite_thinking_mode,
                reasoning_effort=config.query_rewrite_reasoning_effort,
                request_api=config.query_rewrite_request_api,
                stream_enabled=config.query_rewrite_stream_enabled,
                request_trace_id=request_trace_id,
            )
        except Exception as e:
            # 降级：返回原始查询
            logger.warning(
                f"[QueryRewrite] 重写失败，使用原始查询: {e}",
                exc_info=True,
            )
            return current_query

        # 清理结果
        rewritten_clean = rewritten.strip()

        # 简单验证：重写结果不应为空且不能过长
        if not rewritten_clean or len(rewritten_clean) > 200:
            logger.warning(
                f"[QueryRewrite] 重写结果异常，使用原始查询: '{rewritten_clean}'"
            )
            return current_query

        logger.info(
            f"[QueryRewrite] 重写成功: '{current_query[:30]}...' -> "
            f"'{rewritten_clean[:30]}...'"
        )
        return rewritten_clean
