"""LLM Provider 插件 - 提供统一的 LLM 调用接口。"""

from nonebot import logger
from nonebot.plugin import PluginMetadata, require

from .config import Config
from .config_schema import DynamicConfigSchema
from .deepseek_client import DeepSeekClient
from .gemini_client import GeminiClient

__plugin_meta__ = PluginMetadata(
    name="llm_provider",
    description="通用 LLM API 提供者，支持 DeepSeek 和 Gemini，集成 Komari Knowledge 知识库",
    usage="""
    llm_provider = require("llm_provider")

    # 基础用法
    response = await llm_provider.generate_text(
        prompt="你好",
        provider="deepseek",
        system_instruction="你是一个助手",
    )

    # 带知识库检索
    response = await llm_provider.generate_text(
        prompt="小鞠喜欢吃什么？",
        provider="gemini",
        system_instruction="你是小鞠",
        enable_knowledge=True,  # 启用知识库检索
        knowledge_limit=3,  # 检索最多 3 条相关知识

    # Gemini 3 特别说明
    如果使用 gemini 3 以上的 API，你需要使用 thinking_level 参数，而不是 thinking_token。
    thinking_level 参数应当根据对应的 model 传入思考等级，请使用**小写字母**。
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


async def generate_text(
    prompt: str,
    provider: str,
    model: str,
    system_instruction: str | None = None,
    temperature: int | None = None,
    max_tokens: int | None = None,
    knowledge_query: str | None = None,
    knowledge_limit: int = 3,
    *,
    enable_knowledge: bool = False,
    **kwargs,  # noqa: ANN003
) -> str:
    """生成文本。

    Args:
        prompt: 用户提示词
        provider: API 提供商 ("deepseek" 或 "gemini")
        system_instruction: 系统指令
        temperature: 温度参数，None 使用默认值
        max_tokens: 最大 token 数，None 使用默认值
        enable_knowledge: 是否启用知识库检索
        knowledge_query: 知识库查询文本，None 则使用 prompt
        knowledge_limit: 检索返回的知识数量上限
        **kwargs: 其他 provider 特定参数

    Returns:
        生成的文本
    """
    provider = provider.lower()
    config = config_manager.get()

    if provider == "gemini":
        token = config.gemini_api_token
    else:  # 默认 deepseek
        token = config.deepseek_api_token

    if not token:
        raise ValueError(  # noqa:TRY003
            f"{provider.upper()} API Token 未配置，请在配置中设置 {provider}_api_token"
        )

    match provider:
        case "gemini":
            client = GeminiClient(token)
        case "deepseek":
            client = DeepSeekClient(token)
        case _:
            logger.error(
                "未传入 API 渠道或渠道不支持，请检查调用方法是否包含 provider 参数。"
            )
            raise ValueError

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
                else:
                    knowledge_context = ""
            except Exception as e:
                logger.warning(f"[LLM Provider] 知识库检索失败: {e}")

        # 构建系统指令：处理占位符
        placeholder = "{{DYNAMIC_KNOWLEDGE_BASE}}"
        final_system_instruction = (system_instruction or "").replace(
            placeholder, knowledge_context
        )

        result = await client.generate_text(
            prompt=prompt,  # 保持用户原始输入不变
            model=model,
            system_instruction=final_system_instruction,
            temperature=temperature,
            max_tokens=max_tokens,
            **kwargs,
        )
    except Exception as e:
        logger.error(f"LLM 调用失败 ({provider}): {e}")
        raise
    else:
        return result
    finally:
        await client.close()


async def test_connection(provider: str) -> bool:
    """测试指定 provider 的连接。

    Args:
        provider: API 提供商 ("deepseek" 或 "gemini")

    Returns:
        连接是否成功
    """
    provider = provider.lower()
    config = config_manager.get()

    if provider == "gemini":
        token = config.gemini_api_token
    else:
        token = config.deepseek_api_token

    if not token:
        logger.warning(f"{provider.upper()} API Token 未配置，跳过连接测试")
        return False

    client = GeminiClient(token) if provider == "gemini" else DeepSeekClient(token)

    try:
        return await client.test_connection()
    finally:
        await client.close()
