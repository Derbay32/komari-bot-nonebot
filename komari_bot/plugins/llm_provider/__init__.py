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
    )
    """,
    config=Config,
)

# 依赖 config_manager 插件
config_manager_plugin = require("config_manager")

# 获取配置管理器
config_manager = config_manager_plugin.get_config_manager("llm_provider", DynamicConfigSchema)


# 按理说防注入函数一定会有输入值，否则就没有必要构造防注入提示词
def _build_safe_prompt(
    user_input: str | tuple
) -> str:
    """构建防注入的用户指令。

    将用户侧传递的 prompt 与防注入指令合并。

    Args:
        user_input: 用户自定义的输入

    Returns:
        合并后的最终 user 提示词
    """
    # 打标签告诉ai这是用户输入内容
    # 如果插件侧提供了用户输入内容，则构造防注入格式输入
    if isinstance(user_input, tuple):
        final_prompt = (
            f"{user_input[0].replace("|", f"<user_input>{user_input[1]}</user_input>\n", 1)}"
            )
        return final_prompt

    return user_input


async def generate_text(
    prompt: str,
    provider: str,
    system_instruction: str | None = None,
    temperature: int | None = None,
    max_tokens: int | None = None,
    enable_knowledge: bool = False,
    knowledge_query: str | None = None,
    knowledge_limit: int = 3,
    **kwargs,
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
        raise ValueError(f"{provider.upper()} API Token 未配置，请在配置中设置 {provider}_api_token")

    if provider == "gemini":
        client = GeminiClient(token)
    else:
        client = DeepSeekClient(token)

    try:
        # 知识库检索
        knowledge_context = ""
        if enable_knowledge:
            try:
                knowledge_plugin = require("komari_memory")
                query = knowledge_query or prompt
                results = await knowledge_plugin.search_memory(query, limit=knowledge_limit)

                if results:
                    knowledge_context = "\n".join(result.content for result in results)
                    logger.info(f"[LLM Provider] 已检索到 {len(results)} 条相关知识")
                else:
                    knowledge_context = ""
            except Exception as e:
                logger.warning(f"[LLM Provider] 知识库检索失败: {e}")

        # 构建系统指令：处理占位符
        placeholder = "{{DYNAMIC_KNOWLEDGE_BASE}}"
        final_system_instruction = (system_instruction or "").replace(placeholder, knowledge_context)

        safe_prompt = _build_safe_prompt(prompt) if prompt else prompt
        result = await client.generate_text(
            prompt=safe_prompt,  # 保持用户原始输入不变
            system_instruction=final_system_instruction,
            temperature=temperature,
            max_tokens=max_tokens,
            **kwargs,
        )
        return result
    except Exception as e:
        logger.error(f"LLM 调用失败 ({provider}): {e}")
        raise
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

    if provider == "gemini":
        client = GeminiClient(token)
    else:
        client = DeepSeekClient(token)

    try:
        return await client.test_connection()
    finally:
        await client.close()
