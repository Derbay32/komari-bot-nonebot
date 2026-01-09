"""查询重写服务 - 将当前输入结合历史重写为独立查询。"""

from logging import getLogger

from nonebot.plugin import require

from ..core.retry import retry_async
from ..services.config_interface import get_config
from ..services.redis_manager import MessageSchema

# 依赖 llm_provider 插件
llm_provider = require("llm_provider")

logger = getLogger(__name__)


class QueryRewriteService:
    """查询重写服务。"""

    def __init__(self) -> None:
        """初始化查询重写服务。"""

    def _build_rewrite_prompt(
        self,
        current_query: str,
        conversation_history: list[MessageSchema],
    ) -> str:
        """构建查询重写的 Prompt。

        Args:
            current_query: 当前用户输入
            conversation_history: 最近的历史对话

        Returns:
            重写 Prompt
        """
        # 获取最新配置
        config = get_config()

        # 格式化历史对话（只取最近配置数量的对话作为上下文）
        history_text = ""
        if conversation_history:
            history_lines = []
            # 使用配置中的历史对话数量限制
            limit = min(config.query_rewrite_history_limit, len(conversation_history))
            for msg in conversation_history[-limit:]:
                role = "助手" if msg.is_bot else msg.user_nickname
                history_lines.append(f"{role}: {msg.content}")
            history_text = "\n".join(history_lines)

        # 构建完整的重写指令
        return f"""根据以下对话历史，将用户的最新回复重写为一个语义完整、指代清晰的搜索查询语句。不要回答问题，只需重写。如果话题发生了跳跃，忽略旧历史。注意：输出必须使用简体中文。

对话历史：
{history_text if history_text else "(无历史对话)"}

用户最新回复：{current_query}

重写后的查询："""

    @retry_async(max_attempts=2, base_delay=0.5)
    async def rewrite_query(
        self,
        current_query: str,
        conversation_history: list[MessageSchema],
    ) -> str:
        """重写查询为语义完整的独立语句。

        Args:
            current_query: 当前用户输入
            conversation_history: 最近的历史对话（按时间顺序）

        Returns:
            重写后的查询，失败时返回原始查询
        """
        # 获取最新配置
        config = get_config()

        try:
            # 构建重写 Prompt
            rewrite_prompt = self._build_rewrite_prompt(
                current_query=current_query,
                conversation_history=conversation_history,
            )

            # 调用 LLM 重写（使用总结模型，更快）
            rewritten = await llm_provider.generate_text(
                prompt=rewrite_prompt,
                provider=config.llm_provider,
                model=config.llm_model_summary,
                temperature=0.3,
                max_tokens=256,
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
