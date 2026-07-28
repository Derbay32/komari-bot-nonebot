from random import randint

from nonebot import get_driver, get_plugin_config, logger, on_command
from nonebot.adapters.onebot.v11 import Bot, Message, MessageEvent
from nonebot.exception import FinishedException
from nonebot.params import Command, CommandArg
from nonebot.permission import SUPERUSER
from nonebot.plugin import PluginMetadata, require

from komari_bot.common.onebot_messages import plain_text_message

from .commands import AddCommand, DeleteCommand
from .config import Config
from .config_schema import DynamicConfigSchema
from .redis_undo_stack import (
    close_redis,
    peek_undo,
    pop_undo_if_token,
    push_undo,
)

__plugin_meta__ = PluginMetadata(
    name="sr",
    description="神人榜随机抽选插件",
    usage="""
    核心指令：.sr
    .sr 随机从神人榜内抽取一个
    .sr <任意内容> 以指定内容为判定，随机从神人榜内抽取一个
    .sr add 向神人榜内添加神人
    .sr del 删除神人榜内的指定神人
    .sr undo 撤销上次的添加/删除操作
    .sr list 查看目前神人榜内的神人
    以下为管理员指令：
    .sr status 查看插件运行情况
    .sr on/off 开关本插件
    """,
    config=Config,
)

config = get_plugin_config(Config)

# 依赖配置管理插件
config_manager_plugin = require("config_manager")
# 依赖权限管理插件
permission_manager_plugin = require("permission_manager")
# 依赖用户名绑定插件
character_binding = require("character_binding")

# 初始化配置管理器
config_manager = config_manager_plugin.get_config_manager("sr", DynamicConfigSchema)
driver = get_driver()


async def _record_undo_or_warn(user_id: str, command: object, result: str) -> str:
    """保存撤销记录；主操作成功后 Redis 故障只追加明确警告。"""
    try:
        await push_undo(user_id, command, config_manager)
    except Exception:
        logger.exception("SR 主操作已成功，但撤销记录写入 Redis 失败")
        return (
            f"{result}\n"
            "⚠️ 操作已生效，但撤销记录保存失败，本次操作无法通过 undo 撤销"
        )
    return result


async def _undo_latest(user_id: str) -> str:
    """按 peek→配置 CAS→条件 pop 顺序撤销最近一次操作。"""
    try:
        cmd_data = await peek_undo(user_id, config_manager)
    except Exception:
        logger.exception("读取 SR 撤销记录失败")
        return "❌ 暂时无法读取撤销记录，请稍后重试"
    if cmd_data is None:
        return "❌ 没有可撤销的操作"

    if cmd_data["type"] == "AddCommand":
        last_cmd = AddCommand.from_dict(cmd_data, config_manager)
    else:
        last_cmd = DeleteCommand.from_dict(cmd_data, config_manager)

    result = await last_cmd.undo()
    if not result.startswith("↩️"):
        return result

    try:
        popped = await pop_undo_if_token(
            user_id,
            cmd_data["token"],
            config_manager,
        )
    except Exception:
        logger.exception("SR 撤销已生效，但 Redis 栈确认失败")
        return (
            f"{result}\n"
            "⚠️ 撤销已生效，但撤销记录确认失败；重复 undo 可能只返回状态冲突"
        )
    if not popped:
        return (
            f"{result}\n"
            "⚠️ 撤销已生效，但撤销栈已被并发更新，旧记录未被错误弹出"
        )
    return result


@driver.on_shutdown
async def on_shutdown() -> None:
    """关闭撤销栈 Redis 客户端。"""
    await close_redis()

# 注册 sr 主指令
sr = on_command("sr", priority=10, block=True)

# 注册 sr 自定义指令
sr_custom = on_command(
    ("sr", "list"),
    aliases={("sr", "add"), ("sr", "del"), ("sr", "undo")},
    priority=7,
    block=True,
)

# 注册 sr 管理指令
sr_manage = on_command(
    ("sr", "status"), aliases={("sr", "on"), ("sr", "off")}, priority=5, block=True
)


@sr_manage.handle()
async def sr_switch(
    bot: Bot,
    event: MessageEvent,
    cmd: tuple[str, ...] = Command(),
) -> None:
    if not await SUPERUSER(bot, event):
        await sr_manage.finish("❌ 仅限 SUPERUSER 使用")

    _, action = cmd
    config = await config_manager.get_async()

    match action:
        case "status":
            # 显示插件状态信息
            permission_info = permission_manager_plugin.format_permission_info(config)
            await sr_manage.finish(plain_text_message(f"SR {permission_info}"))

        case "on" | "off":
            # 切换插件开关
            new_status = action == "on"
            old_status = config.plugin_enable

            if old_status == new_status:
                await sr_manage.finish(
                    plain_text_message(
                        f"插件已经是{'开启' if new_status else '关闭'}状态"
                    )
                )

            await config_manager.update_field_async("plugin_enable", new_status)

            status_text = "开启" if new_status else "关闭"
            await sr_manage.finish(plain_text_message(f"SR 插件已{status_text}"))

        case _:
            await sr_manage.finish("未知操作，请使用 on/off/status")


@sr.handle()
async def sr_function(
    bot: Bot, event: MessageEvent, args: Message = CommandArg()
) -> None:
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
        bot, event, await config_manager.get_async()
    )
    if not can_use:
        logger.info(f"用户 {username}({user_id}) 请求被拒绝，原因：{reason}。")
        await sr.finish(plain_text_message(f"❌ {reason}"))

    try:
        # 如果有额外参数，作为自定义消息加入最终回复
        custom_message = args.extract_plain_text().strip() if args else None

        # 获取神人榜与其长度
        config = await config_manager.get_async()
        sr_list = config.sr_list
        sr_num = len(sr_list)
        if not sr_list:
            await sr.finish("神人榜为空，请先配置名单")
            return

        sr_target = randint(0, sr_num - 1)

        # 格式化最终回复
        if custom_message:
            response = (
                f"{username}抽取：\n"
                f"{custom_message}——\n"
                f"{sr_target + 1}. {sr_list[sr_target]}"
            )
        else:
            response = (
                f"{username}抽到的神人是——\n{sr_target + 1}. {sr_list[sr_target]}"
            )

        await sr.finish(plain_text_message(response))

    except Exception as e:
        if not isinstance(e, FinishedException):
            logger.error(f"处理 sr 命令时发生错误: {e}")
            await sr.finish("❌ 处理请求时发生错误，请稍后重试")


@sr_custom.handle()
async def sr_usrcustom(
    bot: Bot,
    event: MessageEvent,
    cmd: tuple[str, ...] = Command(),
    args: Message = CommandArg(),
) -> None:
    # 初始化命令层
    _, action = cmd

    # 获取用户信息
    user_id = event.get_user_id()
    user_nickname = permission_manager_plugin.get_user_nickname(event)

    # 使用运行时配置进行权限检查
    can_use, reason = await permission_manager_plugin.check_runtime_permission(
        bot, event, await config_manager.get_async()
    )
    if not can_use:
        logger.info(f"用户 {user_nickname}({user_id}) 请求被拒绝，原因：{reason}。")
        await sr.finish(plain_text_message(f"❌ {reason}"))

    try:
        match action:
            case "list":
                # 获取神人榜
                config = await config_manager.get_async()
                sr_list = config.sr_list

                if not sr_list:
                    await sr_custom.finish("神人榜为空，使用 .sr add 添加神人")
                    return

                # 解析页码参数
                page_str = args.extract_plain_text().strip() if args else ""
                page = int(page_str) if page_str.isdigit() else 1

                chunk_size = config.list_chunk_size
                total_pages = (len(sr_list) + chunk_size - 1) // chunk_size

                if page < 1 or page > total_pages:
                    await sr_custom.finish(
                        plain_text_message(f"页码无效，共 {total_pages} 页")
                    )
                    return

                start_idx = (page - 1) * chunk_size
                end_idx = min(start_idx + chunk_size, len(sr_list))
                page_items = sr_list[start_idx:end_idx]

                content = "\n".join(
                    f"{start_idx + i + 1}. {item}" for i, item in enumerate(page_items)
                )

                if total_pages == 1:
                    await sr_custom.finish(
                        plain_text_message(
                            f"目前神人榜内共{len(sr_list)}位神人神人榜列表：\n{content}"
                        )
                    )
                else:
                    await sr_custom.finish(
                        plain_text_message(
                            f"目前神人榜内共{len(sr_list)}位神人\n"
                            f"神人榜列表(第{page}/{total_pages}页)：\n"
                            f"{content}"
                        )
                    )
            case "add":
                args_text = args.extract_plain_text().strip()
                if not args_text:
                    await sr_custom.finish("❌ 请输入要添加的神人名称")

                cmd_obj = AddCommand(item=args_text, config_manager=config_manager)

                result = await cmd_obj.execute()

                if not result.startswith("❌"):
                    result = await _record_undo_or_warn(user_id, cmd_obj, result)

                await sr_custom.finish(plain_text_message(result))

            case "del":
                args_text = args.extract_plain_text().strip()
                if not args_text:
                    await sr_custom.finish("❌ 请输入要删除的神人名称或序号")

                # 判断是序号还是名称
                if args_text.isdigit():
                    cmd_obj = DeleteCommand(
                        index=int(args_text), config_manager=config_manager
                    )
                else:
                    cmd_obj = DeleteCommand(
                        item=args_text, config_manager=config_manager
                    )

                result = await cmd_obj.execute()

                if not result.startswith("❌"):
                    result = await _record_undo_or_warn(user_id, cmd_obj, result)

                await sr_custom.finish(plain_text_message(result))

            case "undo":
                result = await _undo_latest(user_id)
                await sr_custom.finish(plain_text_message(result))

    except Exception as e:
        if not isinstance(e, FinishedException):
            logger.error(f"处理 sr 命令时发生错误: {e}")
            await sr.finish("❌ 处理请求时发生错误，请稍后重试")
