from nonebot import logger, on_command
from nonebot.adapters.onebot.v11 import Bot, MessageEvent
from nonebot.exception import FinishedException
from nonebot.params import Command
from nonebot.permission import SUPERUSER
from nonebot.plugin import PluginMetadata, require

from .config_schema import DynamicConfigSchema

# 依赖用户数据插件
user_data_plugin = require("user_data")
# 依赖配置管理插件
config_manager_plugin = require("config_manager")
# 依赖权限管理插件
permission_manager_plugin = require("permission_manager")
# 依赖角色名绑定插件
character_binding = require("character_binding")

# 导入用户数据插件函数，如果插件未加载则设为 None
try:
    generate_or_update_favorability = user_data_plugin.generate_or_update_favorability
    format_favor_response = user_data_plugin.format_favor_response
except AttributeError:
    logger.error("无法导入user_data插件的函数，请确保用户数据插件已正确安装")
    generate_or_update_favorability = None
    format_favor_response = None

__plugin_meta__ = PluginMetadata(
    name="jrhg",
    description="今日好感插件，提供每日好感度查询和白名单管理",
    usage="""
    .jrhg - 查询今日好感度
    .jrhg on/off - 管理员控制插件开关
    """,
)

# 初始化配置管理器
config_manager = config_manager_plugin.get_config_manager("jrhg", DynamicConfigSchema)

# 主jrhg指令注册，使用动态权限检查
jrhg = on_command("jrhg", priority=10, block=True)

# JRHG开关指令注册，权限SUPERUSER
manage = on_command(
    ("jrhg", "on"),
    aliases={("jrhg", "off"), ("jrhg", "status")},
    permission=SUPERUSER,
    priority=5,
    block=True,
)


@manage.handle()
async def jrhg_switch(cmd: tuple[str, ...] = Command()) -> None:
    """处理插件开关命令"""
    _, action = cmd
    config = config_manager.get()
    match action:
        case "status":
            # 显示插件状态信息
            permission_info = permission_manager_plugin.format_permission_info(config)
            (
                _plugin_status,
                status_desc,
            ) = await permission_manager_plugin.check_plugin_status(config)

            # 获取用户数据插件状态
            user_data_status = (
                "🟢 正常" if generate_or_update_favorability else "🔴 异常"
            )

            message = (
                f"JRHG插件状态:\n"
                f"插件: {status_desc}\n"
                f"用户数据插件: {user_data_status}\n"
                f"{permission_info}"
            )
            await manage.finish(message)

        case "on" | "off":
            # 切换插件开关
            new_status = action == "on"
            old_status = config.plugin_enable

            if old_status == new_status:
                await manage.finish(f"插件已经是{'开启' if new_status else '关闭'}状态")

            # 持久化到 JSON
            config_manager.update_field("plugin_enable", new_status)

            status_text = "开启" if new_status else "关闭"
            await manage.finish(f"JRHG插件已{status_text}")

        case _:
            await manage.finish("未知操作，请使用 on/off/status")


@jrhg.handle()
async def jrhg_function(
    bot: Bot,
    event: MessageEvent,
) -> None:
    """处理jrhg主命令"""
    # 获取用户信息
    user_id = event.get_user_id()
    user_nickname = (
        (event.sender.nickname or event.sender.card or user_id)
        if event.sender
        else user_id
    )
    username = character_binding.get_character_name(user_id, user_nickname)

    # 使用运行时配置进行权限检查
    can_use, reason = await permission_manager_plugin.check_runtime_permission(
        bot, event, config_manager.get()
    )
    if not can_use:
        logger.info(f"用户 {username}({user_id}) 请求被拒绝，原因：{reason}")
        await jrhg.finish(f"❌ {reason}")

    try:
        # 检查依赖插件是否可用
        if not generate_or_update_favorability or not format_favor_response:
            await jrhg.finish("❌ 用户数据插件不可用，请联系管理员")

        # 获取或生成好感度
        logger.info(f"用户 {username}({user_id}) 请求好感度问候")

        favor_result = await generate_or_update_favorability(user_id)

        if favor_result.is_new_day:
            logger.info(
                f"为用户 {username} 生成新的每日好感度: {favor_result.daily_favor}"
            )

        final_response = await format_favor_response(
            ai_response="",
            user_nickname=username,
            daily_favor=favor_result.daily_favor,
        )

        await jrhg.finish(final_response)

    except Exception as e:
        if not isinstance(e, FinishedException):
            logger.exception(f"处理jrhg命令时发生错误: {e}")


# 插件生命周期管理
async def on_startup() -> None:
    """插件启动时的初始化"""
    try:
        # 检查用户数据插件
        if not generate_or_update_favorability:
            logger.error("用户数据插件不可用，JRHG插件将无法正常工作")
        else:
            logger.info("用户数据插件可用")

    except Exception as e:
        logger.error(f"JRHG插件启动时发生错误: {e}")


async def on_shutdown() -> None:
    """插件关闭时的清理"""
    logger.info("JRHG插件已关闭")


# 导出生命周期函数
__plugin_startup__ = on_startup
__plugin_shutdown__ = on_shutdown
