"""LLM Provider 插件 - 提供统一的 LLM 调用接口（OpenAI 兼容格式）。"""

import time

from nonebot import logger
from nonebot.plugin import PluginMetadata, require

from .config import Config
from .config_schema import DynamicConfigSchema
from .deepseek_client import DeepSeekClient
from .llm_logger import log_llm_call

__plugin_meta__ = PluginMetadata(
    name="llm_provider",
    description="通用 LLM API 提供者（OpenAI 兼容格式），集成 Komari Knowledge 知识库",
    usage="""
    llm_provider = require("llm_provider")

    # 基础用法
    response = await llm_provider.generate_text(
        prompt="你好",
        model="deepseek-chat",
    )

    # 多轮对话（OpenAI messages 格式）
    response = await llm_provider.generate_text_with_messages(
        messages=[
            {"role": "system", "content": "你是助手"},
            {"role": "user", "content": "你好"},
        ],
        model="deepseek-chat",
    )

    # JSON 模式
    response = await llm_provider.generate_text(
        prompt="返回 JSON 格式",
        model="deepseek-chat",
        response_format={"type": "json_object"},
    )
    """,
    config=Config,
)

# 依赖插件
config_manager_plugin = require("config_manager")
knowledge_plugin = require("komari_knowledge")

# 获取配置管理器
config_manager = config_manager_plugin.get_config_manager(
    "llm_provider", DynamicConfigSchema
)


def _get_client() -> DeepSeekClient:
    """获取 LLM 客户端实例。"""
    config = config_manager.get()
    token = config.deepseek_api_token
    if not token:
        raise ValueError("API Token 未配置，请在配置中设置 deepseek_api_token")  # noqa: TRY003
    return DeepSeekClient(
        token,
        timeout_seconds=float(config.deepseek_timeout_seconds),
    )


async def generate_text(
    prompt: str,
    model: str,
    system_instruction: str | None = None,
    temperature: int | None = None,
    max_tokens: int | None = None,
    knowledge_query: str | None = None,
    knowledge_limit: int = 3,
    *,
    enable_knowledge: bool = False,
    response_format: dict | None = None,
    **kwargs,  # noqa: ANN003
) -> str:
    """生成文本（简单 prompt 模式）。

    Args:
        prompt: 用户提示词
        model: 模型名称
        system_instruction: 系统指令
        temperature: 温度参数
        max_tokens: 最大 token 数
        enable_knowledge: 是否启用知识库检索
        knowledge_query: 知识库查询文本
        knowledge_limit: 检索返回的知识数量上限
        response_format: Response format dict
        **kwargs: 其他参数

    Returns:
        生成的文本
    """
    client = _get_client()
    start_time = time.monotonic()

    try:
        # 知识库检索
        knowledge_context = ""
        if enable_knowledge:
            try:
                query = knowledge_query or prompt
                results = await knowledge_plugin.search_knowledge(
                    query, limit=knowledge_limit
                )
                if results:
                    knowledge_context = "\n".join(result.content for result in results)
                    logger.info(f"[LLM Provider] 已检索到 {len(results)} 条相关知识")
            except Exception as e:
                logger.warning(f"[LLM Provider] 知识库检索失败: {e}")

        # 构建系统指令：处理占位符
        placeholder = "{{DYNAMIC_KNOWLEDGE_BASE}}"
        final_system_instruction = (system_instruction or "").replace(
            placeholder, knowledge_context
        )

        result = await client.generate_text(
            prompt=prompt,
            model=model,
            system_instruction=final_system_instruction,
            temperature=temperature,
            max_tokens=max_tokens,
            response_format=response_format,
            **kwargs,
        )
    except Exception as e:
        duration_ms = (time.monotonic() - start_time) * 1000
        await log_llm_call(
            method="generate_text",
            model=model,
            input_data={"prompt": prompt, "system_instruction": system_instruction},
            error=str(e),
            duration_ms=duration_ms,
        )
        logger.error(f"LLM 调用失败: {e}")
        raise
    else:
        duration_ms = (time.monotonic() - start_time) * 1000
        await log_llm_call(
            method="generate_text",
            model=model,
            input_data={
                "prompt": prompt,
                "system_instruction": final_system_instruction,
            },
            output=result,
            duration_ms=duration_ms,
        )
        return result
    finally:
        await client.close()


async def generate_text_with_messages(
    messages: list[dict],
    model: str,
    temperature: float | None = None,
    max_tokens: int | None = None,
    response_format: dict | None = None,
    **kwargs,  # noqa: ANN003
) -> str:
    """使用 OpenAI 格式 messages 生成文本（支持多模态）。

    Args:
        messages: 消息列表 [{role, content}]，content 可以是字符串或数组（OpenAI Vision 格式）
        model: 模型名称
        temperature: 温度参数
        max_tokens: 最大 token 数
        response_format: Response format dict
        **kwargs: 其他参数

    Returns:
        生成的文本
    """
    client = _get_client()
    start_time = time.monotonic()

    try:
        result = await client.generate_text_with_messages(
            messages=messages,
            model=model,
            temperature=temperature,
            max_tokens=max_tokens,
            response_format=response_format,
            **kwargs,
        )
    except Exception as e:
        duration_ms = (time.monotonic() - start_time) * 1000
        await log_llm_call(
            method="generate_text_with_messages",
            model=model,
            input_data=messages,
            error=str(e),
            duration_ms=duration_ms,
        )
        logger.error(f"LLM 调用失败: {e}")
        raise
    else:
        duration_ms = (time.monotonic() - start_time) * 1000
        await log_llm_call(
            method="generate_text_with_messages",
            model=model,
            input_data=messages,
            output=result,
            duration_ms=duration_ms,
        )
        return result
    finally:
        await client.close()


async def test_connection() -> bool:
    """测试 API 连接。

    Returns:
        连接是否成功
    """
    config = config_manager.get()
    token = config.deepseek_api_token
    if not token:
        logger.warning("API Token 未配置，跳过连接测试")
        return False

    client = DeepSeekClient(
        token,
        timeout_seconds=float(config.deepseek_timeout_seconds),
    )
    try:
        return await client.test_connection()
    finally:
        await client.close()
