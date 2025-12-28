from nonebot import get_plugin_config, get_driver, logger
from nonebot.plugin import PluginMetadata, require

import aiohttp

from .config import Config as GlitchtipConfig

__plugin_meta__ = PluginMetadata(
    name="glitchtip_heartbeat",
    description="Glitchtip 心跳检测插件，在 OneBot v11 连接时定期发送心跳",
    usage="自动运行，无需手动操作",
    config=GlitchtipConfig,
)

plugin_config = get_plugin_config(GlitchtipConfig).glitchtip_heartbeat
driver = get_driver()

# 尝试加载 nonebot_plugin_apscheduler
_scheduler = None
try:
    _scheduler = require("nonebot_plugin_apscheduler").scheduler
except Exception:
    _scheduler = None

# 心跳任务ID
_HEARTBEAT_JOB_ID = "glitchtip_heartbeat"

# 连接状态标记
_is_connected = False


async def _send_heartbeat() -> None:
    """发送心跳请求到 Glitchtip"""
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(plugin_config.url) as response:
                if response.status == 200:
                    logger.debug("Glitchtip 心跳发送成功")
                else:
                    logger.warning(f"Glitchtip 心跳返回状态码: {response.status}")
    except Exception as e:
        logger.error(f"发送 Glitchtip 心跳时出错: {e}")


async def on_bot_connect(bot) -> None:
    """Bot 连接时启动心跳任务"""
    global _is_connected
    if _is_connected:
        return

    # 检查插件开关
    if not plugin_config.enabled:
        logger.info("Glitchtip 心跳检测插件已禁用，跳过启动")
        return

    _is_connected = True
    logger.info(f"Bot {bot.self_id} 已连接，启动 Glitchtip 心跳检测")

    if _scheduler:
        # 立即发送一次心跳
        await _send_heartbeat()

        # 添加定时心跳任务
        _scheduler.add_job(
            _send_heartbeat,
            "interval",
            seconds=plugin_config.interval,
            id=_HEARTBEAT_JOB_ID,
            replace_existing=True,
        )
        logger.info(f"Glitchtip 心跳任务已启动 (间隔: {plugin_config.interval}秒)")
    else:
        logger.warning("scheduler 不可用，无法启动定时心跳任务")


async def on_bot_disconnect(bot) -> None:
    """Bot 断开连接时停止心跳任务"""
    global _is_connected
    if not _is_connected:
        return

    _is_connected = False
    logger.info(f"Bot {bot.self_id} 已断开连接，停止 Glitchtip 心跳检测")

    if _scheduler:
        try:
            _scheduler.remove_job(_HEARTBEAT_JOB_ID)
            logger.info("Glitchtip 心跳任务已停止")
        except Exception as e:
            logger.warning(f"移除心跳任务时出错: {e}")


# 注册 bot 连接/断开钩子
driver.on_bot_connect(on_bot_connect)
driver.on_bot_disconnect(on_bot_disconnect)
