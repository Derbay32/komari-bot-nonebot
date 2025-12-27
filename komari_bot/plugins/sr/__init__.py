from nonebot import get_plugin_config, on_command, logger
from nonebot.plugin import PluginMetadata, require
from nonebot.params import Command, CommandArg
from nonebot.adapters.onebot.v11 import Bot, MessageEvent, Message
from nonebot.exception import FinishedException

from random import randint

from .config import Config
from .config_schema import DynamicConfigSchema

__plugin_meta__ = PluginMetadata(
    name="sr",
    description="",
    usage="",
    config=Config,
)

config = get_plugin_config(Config)

# 依赖配置管理插件
config_manager_plugin = require("config_manager")
# 依赖权限管理插件
permission_manager_plugin = require("permission_manager")

# 初始化配置管理器
config_manager = config_manager_plugin.get_config_manager("sr", DynamicConfigSchema)
dynamic_config: DynamicConfigSchema = config_manager.initialize()

# 注册 sr 主指令
sr = on_command(
    "sr",
    priority=10,
    block=True
)

# 注册 sr 自定义指令
sr_custom = on_command(
    ("sr", "list"),
    aliases={("sr", "add"), ("sr", "del"), ("sr", "undo")},
    priority=7,
    block=True
)

# 注册 sr 管理指令
sr_manage = on_command(
    ("sr", "status"),
    aliases={("sr", "on"),("sr", "off")},
    priority=5,
    block=True
)

@sr_manage.handle()
async def sr_switch(
    bot: Bot,
    event: MessageEvent,
    cmd: tuple[str, ...]= Command()
    ):

    _, action = cmd

    if action == "status":
        # 显示插件状态信息
        permission_info = permission_manager_plugin.format_permission_info(dynamic_config)
        plugin_status, status_desc = await permission_manager_plugin.check_plugin_status(dynamic_config)

        message = (
            f"SR 插件状态: {status_desc}"
        )
        await sr_manage.finish(message)

    elif action == ["on", "off"]:
        # 切换插件开关
        new_status = action == "on"
        old_status = dynamic_config.plugin_enable

        if old_status == new_status:
            await sr_manage.finish(f"插件已经是{'开启' if new_status else '关闭'}状态")

        # 持久化到 JSON
        config_manager.update_field("plugin_enable", new_status)
        # 更新本地引用∂
        dynamic_config.plugin_enable = new_status

        status_text = "开启" if new_status else "关闭"
        await sr_manage.finish(f"SR 插件已{status_text}")

    else:
        await sr_manage.finish("未知操作，请使用 on/off/status")

@sr.handle()
async def sr_function(
    bot: Bot,
    event: MessageEvent,
    args: Message = CommandArg()
    ):

    # 获取用户信息
    user_id = event.get_user_id()
    user_nickname = permission_manager_plugin.get_user_nickname(event)

    # 使用运行时配置进行权限检查
    can_use, reason = await permission_manager_plugin.check_runtime_permission(bot, event, config_manager)
    if not can_use:
        logger.info(f"用户 {user_nickname}({user_id}) 请求被拒绝，原因：{reason}。")
        await sr.finish(f"❌ {reason}")

    try:
        # 如果有额外参数，作为自定义消息加入最终回复
        custom_message = args.extract_plain_text().strip() if args else None

        # 获取神人榜与其长度
        sr_list = dynamic_config.sr_list
        sr_num = len(sr_list)

        #
        sr_target = randint(0, sr_num - 1)

        # 格式化最终回复
        if custom_message:
            response = (
                f"{user_nickname}抽取：\n"
                f"{custom_message}——"
                f"{sr_target - 1}. {sr_list[sr_target]}"
            )
        else:
            response = (
                f"{user_nickname}抽到的神人是——\n"
                f"{sr_target - 1}. {sr_list[sr_target]}"
            )

        await sr.finish(response)

    except Exception as e:
        if not isinstance(e, FinishedException):
            logger.error(f"处理 sr 命令时发生错误: {e}")
            await sr.finish("❌ 处理请求时发生错误，请稍后重试")

@sr_custom.handle()
async def sr_usrcustom(
    bot: Bot,
    event: MessageEvent,
    cmd: tuple[str, ...] = Command(),
    args: Message = CommandArg()
    ):

    # 初始化命令层
    _, action = cmd

    # 获取用户信息
    user_id = event.get_user_id()
    user_nickname = permission_manager_plugin.get_user_nickname(event)

    # 使用运行时配置进行权限检查
    can_use, reason = await permission_manager_plugin.check_runtime_permission(bot, event, config_manager)
    if not can_use:
        logger.info(f"用户 {user_nickname}({user_id}) 请求被拒绝，原因：{reason}。")
        await sr.finish(f"❌ {reason}")

    try:
        if action == "list":
            # 获取神人榜
            sr_list = dynamic_config.sr_list

            if not sr_list:
                await sr_custom.finish("神人榜为空，使用 /sr add 添加神人")
                return

            # 解析页码参数
            page_str = args.extract_plain_text().strip() if args else ""
            page = int(page_str) if page_str.isdigit() else 1

            chunk_size = dynamic_config.list_chunk_size
            total_pages = (len(sr_list) + chunk_size - 1) // chunk_size

            if page < 1 or page > total_pages:
                await sr_custom.finish(f"页码无效，共 {total_pages} 页")
                return

            start_idx = (page - 1) * chunk_size
            end_idx = min(start_idx + chunk_size, len(sr_list))
            page_items = sr_list[start_idx:end_idx]

            content = "\n".join(
                f"{start_idx + i + 1}. {item}"
                for i, item in enumerate(page_items)
            )

            if total_pages == 1:
                await sr_custom.finish(
                    f"目前神人榜内共{len(sr_list)}位神人"
                    f"神人榜列表：\n"
                    f"{content}"
                )
            else:
                await sr_custom.finish(
                    f"目前神人榜内共{len(sr_list)}位神人"
                    f"神人榜列表(第{page}/{total_pages}页)：\n"
                    f"{content}"
                )

    except Exception as e:
        if not isinstance(e, FinishedException):
            logger.error(f"处理 sr 命令时发生错误: {e}")
            await sr.finish("❌ 处理请求时发生错误，请稍后重试")