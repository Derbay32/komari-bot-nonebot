"""角色绑定命令处理器。"""

from nonebot import on_command
from nonebot.adapters.onebot.v11 import Bot, Message, MessageEvent
from nonebot.params import CommandArg

from . import get_binding_manager

# /bind set <角色名> 或 /bind set <用户ID> <角色名>
bind_set = on_command(("bind", "set"), priority=10, block=True)


@bind_set.handle()
async def handle_set(
    bot: Bot, event: MessageEvent, args: Message = CommandArg()
) -> None:
    """处理设置绑定命令。

    Args:
        bot: Bot 实例
        event: 消息事件
        args: 命令参数
    """
    manager = get_binding_manager()
    user_id = str(event.user_id)

    # 提取参数
    arg_text = args.extract_plain_text().strip()

    if not arg_text:
        await bind_set.finish("❌ 请提供角色名\n用法: /bind set <角色名>")

    # 检查是否是 SUPERUSER（通过检查 session）
    bot_info = await bot.get_login_info()
    is_su = user_id == str(bot_info["user_id"])

    # SUPERUSER 可以指定目标用户: /bind set <用户ID> <角色名>
    if is_su:
        parts = arg_text.split(maxsplit=1)
        if len(parts) == 2 and parts[0].isdigit():
            target_user_id = parts[0]
            character_name = parts[1]
            await manager.set_character_name(target_user_id, character_name)
            await bind_set.finish(
                f"✅ 已为用户 {target_user_id} 设置角色名为 {character_name}"
            )
        else:
            # SUPERUSER 没有指定用户ID，则设置自己的
            character_name = arg_text
            await manager.set_character_name(user_id, character_name)
            await bind_set.finish(f"✅ 已设置您的角色名为 {character_name}")
    else:
        # 普通用户只能设置自己的
        character_name = arg_text
        await manager.set_character_name(user_id, character_name)
        await bind_set.finish(f"✅ 已设置您的角色名为 {character_name}")


# /bind del [用户ID]
bind_del = on_command(("bind", "del"), priority=10, block=True)


@bind_del.handle()
async def handle_del(
    bot: Bot, event: MessageEvent, args: Message = CommandArg()
) -> None:
    """处理删除绑定命令。

    Args:
        bot: Bot 实例
        event: 消息事件
        args: 命令参数
    """
    manager = get_binding_manager()
    user_id = str(event.user_id)

    # 提取参数
    arg_text = args.extract_plain_text().strip()

    # 检查是否是 SUPERUSER
    bot_info = await bot.get_login_info()
    is_su = user_id == str(bot_info["user_id"])

    if is_su and arg_text and arg_text.isdigit():
        # SUPERUSER 删除指定用户的绑定
        target_user_id = arg_text
        success = await manager.remove_character_name(target_user_id)
        if success:
            await bind_del.finish(f"✅ 已删除用户 {target_user_id} 的角色绑定")
        else:
            await bind_del.finish(f"❌ 用户 {target_user_id} 没有角色绑定")
    else:
        # 普通用户删除自己的绑定
        success = await manager.remove_character_name(user_id)
        if success:
            await bind_del.finish("✅ 已删除您的角色绑定")
        else:
            await bind_del.finish("❌ 您还没有设置角色绑定")


# /bind list
bind_list = on_command(("bind", "list"), priority=10, block=True)


@bind_list.handle()
async def handle_list(bot: Bot, event: MessageEvent) -> None:
    """处理查看绑定列表命令。

    Args:
        bot: Bot 实例
        event: 消息事件
    """
    manager = get_binding_manager()
    user_id = str(event.user_id)

    # 检查是否是 SUPERUSER
    bot_info = await bot.get_login_info()
    is_su = user_id == str(bot_info["user_id"])

    bindings = manager.list_bindings()

    if not bindings:
        await bind_list.finish("📋 当前没有任何角色绑定")

    if is_su:
        # SUPERUSER 可以看到所有绑定
        lines = ["📋 所有角色绑定列表："]
        for uid, name in bindings.items():
            lines.append(f"  {uid}: {name}")
        await bind_list.finish("\n".join(lines))
    # 普通用户只能看到自己的绑定
    elif user_id in bindings:
        await bind_list.finish(f"📋 您的角色绑定: {bindings[user_id]}")
    else:
        await bind_list.finish("❌ 您还没有设置角色绑定")
