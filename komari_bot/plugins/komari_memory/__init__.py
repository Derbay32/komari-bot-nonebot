"""
Komari Memory 常识库插件。

提供 Bot 人物设定和世界知识的混合检索功能。

## 使用方式

### 在其他插件中调用

```python
from nonebot.plugin import require

memory_plugin = require("komari_memory")

# 检索相关知识
results = await memory_plugin.search_memory("小鞠喜欢什么？")
for result in results:
    print(f"{result.category}: {result.content}")
```

### WebUI

Bot 启动时自动启动 WebUI 管理界面（可在配置中设置端口）。
"""
import subprocess
from pathlib import Path

from nonebot import get_driver, logger
from nonebot.plugin import PluginMetadata, require

from .config_schema import DynamicConfigSchema
from .engine import SearchResult, initialize_engine, get_engine

# 依赖 config_manager 插件
config_manager_plugin = require("config_manager")

# 获取配置管理器
config_manager = config_manager_plugin.get_config_manager(
    "komari_memory", DynamicConfigSchema
)

__plugin_meta__ = PluginMetadata(
    name="komari_memory",
    description="小鞠常识库 - 提供人物设定和世界知识的混合检索",
    usage="""
    在其他插件中引用：
    memory_plugin = require("komari_memory")
    results = await memory_plugin.search_memory("查询文本")
    """,
    config=DynamicConfigSchema,
)

driver = get_driver()

# Streamlit 进程
_streamlit_process: subprocess.Popen | None = None


@driver.on_startup
async def on_startup():
    """Bot 启动时初始化常识库引擎并启动 WebUI。"""
    global _streamlit_process
    config = config_manager.get()

    if not config.plugin_enable:
        logger.info("[Komari Memory] 插件未启用，跳过初始化")
        return

    if not config.pg_user or not config.pg_password:
        logger.warning(
            "[Komari Memory] 数据库用户名或密码未配置，跳过初始化。"
            "请在配置中设置 pg_user 和 pg_password"
        )
        return

    try:
        await initialize_engine()
        logger.info("[Komari Memory] 插件启动完成")
    except Exception as e:
        logger.error(f"[Komari Memory] 初始化失败: {e}")
        return

    # 启动 WebUI
    if config.webui_enabled:
        webui_path = Path(__file__).parent / "webui.py"
        try:
            _streamlit_process = subprocess.Popen(
                [
                    "streamlit",
                    "run",
                    str(webui_path),
                    "--server.port",
                    str(config.webui_port),
                    "--server.headless",
                    "true",
                ]
            )
            logger.info(
                f"[Komari Memory] WebUI 已启动: http://localhost:{config.webui_port}"
            )
        except Exception as e:
            logger.error(f"[Komari Memory] WebUI 启动失败: {e}")


@driver.on_shutdown
async def on_shutdown():
    """Bot 关闭时清理资源。"""
    global _streamlit_process

    # 关闭 WebUI
    if _streamlit_process:
        try:
            _streamlit_process.terminate()
            _streamlit_process.wait(timeout=5)
            logger.info("[Komari Memory] WebUI 已关闭")
        except subprocess.TimeoutExpired:
            _streamlit_process.kill()
            logger.warning("[Komari Memory] WebUI 强制关闭")
        except Exception as e:
            logger.error(f"[Komari Memory] WebUI 关闭失败: {e}")
        finally:
            _streamlit_process = None

    # 关闭数据库连接
    engine = get_engine()
    if engine:
        await engine.close()
        logger.info("[Komari Memory] 插件已关闭")


async def search_memory(query: str, limit: int | None = None) -> list[SearchResult]:
    """
    检索相关知识。

    这是供其他插件调用的主要接口。

    Args:
        query: 查询文本
        limit: 最大返回数量，None 使用配置默认值

    Returns:
        检索结果列表

    Example:
        >>> results = await search_memory("小鞠喜欢吃什么？")
        >>> for r in results:
        ...     print(f"[{r.source}] {r.content}")
    """
    engine = get_engine()
    if engine is None:
        logger.warning("[Komari Memory] 引擎未初始化")
        return []

    config = config_manager.get()
    if not config.plugin_enable:
        return []

    return await engine.search(query, limit)


async def add_knowledge(
    content: str,
    keywords: list[str],
    category: str = "general",
    notes: str | None = None,
) -> int:
    """
    添加知识到数据库。

    Args:
        content: 知识内容
        keywords: 关键词列表
        category: 分类
        notes: 备注

    Returns:
        新知识的 ID
    """
    engine = get_engine()
    if engine is None:
        raise RuntimeError("常识库引擎未初始化")

    return await engine.add_knowledge(content, keywords, category, notes)


async def get_all_knowledge() -> list[dict]:
    """获取所有知识（用于管理界面）。"""
    engine = get_engine()
    if engine is None:
        raise RuntimeError("常识库引擎未初始化")

    return await engine.get_all_knowledge()


async def delete_knowledge(kid: int) -> bool:
    """删除知识。

    Args:
        kid: 知识 ID

    Returns:
        是否删除成功
    """
    engine = get_engine()
    if engine is None:
        raise RuntimeError("常识库引擎未初始化")

    return await engine.delete_knowledge(kid)
