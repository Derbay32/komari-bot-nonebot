"""LLM Provider 插件 - 提供统一的 LLM 调用接口。"""

from nonebot import logger
from nonebot.plugin import PluginMetadata, require

from .config import Config
from .config_schema import DynamicConfigSchema
from .deepseek_client import DeepSeekClient
from .gemini_client import GeminiClient
from .types import StructuredOutputSchema

__plugin_meta__ = PluginMetadata(
    name="llm_provider",
    description="通用 LLM API 提供者，支持 DeepSeek 和 Gemini，集成 Komari Knowledge 知识库",
    usage="""
    llm_provider = require("llm_provider")

    # 基础用法
    response = await llm_provider.generate_text(
        prompt="你好",
        provider="deepseek",
        model="deepseek-chat",
    )

    # 带知识库检索
    response = await llm_provider.generate_text(
        prompt="小鞠喜欢吃什么？",
        provider="gemini",
        model="gemini-2.5-flash",
        system_instruction="你是小鞠",
        enable_knowledge=True,
        knowledge_limit=3,
    )

    # 结构化输出（Gemini）
    from pydantic import BaseModel

    class SummaryResult(BaseModel):
        summary: str
        entities: list[str]
        importance: int

    response = await llm_provider.generate_text(
        prompt="总结这段对话",
        provider="gemini",
        model="gemini-2.5-flash",
        response_schema=SummaryResult,  # 传入 Pydantic 模型
    )
    result = SummaryResult.model_validate_json(response)

    # 结构化输出（DeepSeek - JSON 模式）
    response = await llm_provider.generate_text(
        prompt="返回 JSON 格式",
        provider="deepseek",
        model="deepseek-chat",
        response_format={"type": "json_object"},
    )

    # Gemini 3 特别说明
    如果使用 gemini 3 以上的 API，你需要使用 thinking_level 参数，而不是 thinking_token。
    thinking_level 参数应当根据对应的 model 传入思考等级，请使用**小写字母**。
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
    # 结构化输出参数（可选）
    response_schema: StructuredOutputSchema | None = None,
    response_json_schema: dict | None = None,
    response_format: dict | None = None,
    **kwargs,  # noqa: ANN003
) -> str:
    """生成文本（支持结构化输出）。

    Args:
        prompt: 用户提示词
        provider: API 提供商 ("deepseek" 或 "gemini")
        model: 模型名称
        system_instruction: 系统指令
        temperature: 温度参数，None 使用默认值
        max_tokens: 最大 token 数，None 使用默认值
        enable_knowledge: 是否启用知识库检索
        knowledge_query: 知识库查询文本，None 则使用 prompt
        knowledge_limit: 检索返回的知识数量上限
        response_schema: Pydantic 模型或 JSON Schema (Gemini/DeepSeek)
        response_json_schema: JSON Schema 字典 (Gemini only)
        response_format: Response format dict (DeepSeek only)
        **kwargs: 其他 provider 特定参数

    Returns:
        生成的文本（使用结构化输出时为 JSON 字符串）
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
            # 结构化输出参数
            response_schema=response_schema,
            response_json_schema=response_json_schema,
            response_format=response_format,
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
