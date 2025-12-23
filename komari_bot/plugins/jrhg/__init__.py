from nonebot import logger
from nonebot.plugin import PluginMetadata, require
from nonebot import on_command
from nonebot.permission import SUPERUSER
from nonebot.params import CommandArg, Command
from nonebot.adapters.onebot.v11 import Bot, MessageEvent, Message
from nonebot.exception import FinishedException

from .config import Config
from .config_manager import get_config_manager, ConfigManager
from .config_schemas import DynamicConfigSchema
from .permissions import (
    get_user_nickname,
    check_plugin_status,
    format_permission_info,
    check_runtime_permission,
)
from .deepseek_client import get_client, close_client

# 依赖用户数据插件
user_data_plugin = require("user_data")
# 导入函数，如果插件未加载则设为 None
try:
    generate_or_update_favorability = user_data_plugin.generate_or_update_favorability
    format_favor_response = user_data_plugin.format_favor_response
except AttributeError:
    logger.error("无法导入user_data插件的函数，请确保用户数据插件已正确安装")
    generate_or_update_favorability = None
    format_favor_response = None

__plugin_meta__ = PluginMetadata(
    name="jrhg",
    description="今日好感插件，基于DeepSeek API生成个性化问候，支持好感度系统和白名单管理",
    usage="/jrhg - 获取今日好感问候\n/jrhg on/off - 管理员控制插件开关",
    config=Config,
)

# 初始化配置管理器
config_manager: ConfigManager = get_config_manager()
dynamic_config: DynamicConfigSchema = config_manager.initialize()

# 主jrhg指令注册，使用动态权限检查
jrhg = on_command(
    "jrhg",
    priority=10,
    block=True
)

# JRHG开关指令注册，权限SUPERUSER
manage = on_command(
    ("jrhg", "on"),
    aliases={("jrhg", "off"), ("jrhg", "status")},
    permission=SUPERUSER,
    priority=5,
    block=True
)


@manage.handle()
async def jrhg_switch(bot: Bot, event: MessageEvent, cmd: tuple[str, ...] = Command()):
    """处理插件开关命令"""
    _, action = cmd

    if action == "status":
        # 显示插件状态信息
        permission_info = format_permission_info(dynamic_config)
        plugin_status, status_desc = await check_plugin_status(dynamic_config)

        # 获取用户数据插件状态
        user_data_status = "🟢 正常" if generate_or_update_favorability else "🔴 异常"

        message = (
            f"JRHG插件状态:\n"
            f"插件: {status_desc}\n"
            f"用户数据插件: {user_data_status}\n"
            f"{permission_info}"
        )
        await manage.finish(message)

    elif action in ["on", "off"]:
        # 切换插件开关
        new_status = action == "on"
        old_status = dynamic_config.jrhg_plugin_enable

        if old_status == new_status:
            await manage.finish(f"插件已经是{'开启' if new_status else '关闭'}状态")

        # 持久化到 JSON
        config_manager.update_field("jrhg_plugin_enable", new_status)
        # 更新本地引用
        dynamic_config.jrhg_plugin_enable = new_status

        status_text = "开启" if new_status else "关闭"
        await manage.finish(f"JRHG插件已{status_text}")

    else:
        await manage.finish("未知操作，请使用 on/off/status")


@jrhg.handle()
async def jrhg_function(bot: Bot, event: MessageEvent, args: Message = CommandArg()):
    """处理jrhg主命令"""
    # 使用运行时配置进行权限检查
    can_use, reason = await check_runtime_permission(bot, event, config_manager)
    if not can_use:
        await jrhg.finish(f"❌ {reason}")

    try:
        # 检查依赖插件是否可用
        if not generate_or_update_favorability or not format_favor_response:
            await jrhg.finish("❌ 用户数据插件不可用，请联系管理员")

        # 获取用户信息
        user_id = event.get_user_id()
        group_id = getattr(event, 'group_id', user_id)  # 如果是私聊，使用用户ID作为群组ID
        user_nickname = get_user_nickname(event)

        # 获取或生成好感度
        logger.info(f"用户 {user_nickname}({user_id}) 在群 {group_id} 请求好感度问候")

        favor_result = await generate_or_update_favorability(user_id, str(group_id))

        if favor_result.is_new_day:
            logger.info(f"为用户 {user_nickname} 生成新的每日好感度: {favor_result.daily_favor}")

        # 获取DeepSeek客户端并生成问候
        client = get_client(dynamic_config)

        # 如果有额外参数，作为自定义消息传递给AI
        custom_message = args.extract_plain_text().strip() if args else None

        ai_response = await client.generate_greeting(
            user_nickname=user_nickname,
            daily_favor=favor_result.daily_favor,
            custom_message=custom_message
        )

        # 格式化最终回复
        final_response = await format_favor_response(
            ai_response=ai_response,
            user_nickname=user_nickname,
            daily_favor=favor_result.daily_favor
        )

        await jrhg.finish(final_response)

    except Exception as e:
        if not isinstance(e, FinishedException):
            logger.error(f"处理jrhg命令时发生错误: {e}")
            await jrhg.finish("❌ 处理请求时发生错误，请稍后重试")


# 插件生命周期管理
async def on_startup():
    """插件启动时的初始化"""
    try:
        # 测试DeepSeek API连接
        client = get_client(dynamic_config)
        connection_ok = await client.test_connection()

        if connection_ok:
            logger.info("JRHG插件启动成功，DeepSeek API连接正常")
        else:
            logger.warning("JRHG插件启动成功，但DeepSeek API连接测试失败")

        # 检查用户数据插件
        if not generate_or_update_favorability:
            logger.error("用户数据插件不可用，JRHG插件将无法正常工作")
        else:
            logger.info("用户数据插件可用")

    except Exception as e:
        logger.error(f"JRHG插件启动时发生错误: {e}")


async def on_shutdown():
    """插件关闭时的清理"""
    try:
        await close_client()
        logger.info("JRHG插件已关闭")
    except Exception as e:
        logger.error(f"关闭JRHG插件时发生错误: {e}")


# 导出生命周期函数
__plugin_startup__ = on_startup
__plugin_shutdown__ = on_shutdown
