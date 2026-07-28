"""角色绑定命令处理器。"""

from __future__ import annotations

from dataclasses import dataclass

from nonebot import on_command

# NoneBot 会在运行时解析处理函数注解，这里必须导入真实类型。
from nonebot.adapters.onebot.v11 import Message, MessageEvent  # noqa: TC002
from nonebot.params import CommandArg, Depends
from nonebot.permission import SUPERUSER

from .manager import CharacterBindingManager, get_manager


@dataclass(slots=True)
class BindSetRequest:
    """设置绑定请求。"""

    operator_user_id: str
    target_user_id: str
    character_name: str
    specified_target: bool = False


@dataclass(slots=True)
class BindDeleteRequest:
    """删除绑定请求。"""

    operator_user_id: str
    target_user_id: str
    specified_target: bool = False


def get_event_user_id(event: MessageEvent) -> str:
    """获取事件发送者 ID。"""
    return event.get_user_id()


def get_command_text(args: Message = CommandArg()) -> str:
    """提取纯文本命令参数。"""
    return args.extract_plain_text().strip()


def parse_self_bind_set_request(
    user_id: str = Depends(get_event_user_id),
    arg_text: str = Depends(get_command_text),
) -> BindSetRequest:
    """解析普通用户设置绑定请求。"""
    return BindSetRequest(
        operator_user_id=user_id,
        target_user_id=user_id,
        character_name=arg_text,
    )


def parse_superuser_bind_set_request(
    user_id: str = Depends(get_event_user_id),
    arg_text: str = Depends(get_command_text),
) -> BindSetRequest:
    """解析超级用户设置绑定请求。"""
    parts = arg_text.split(maxsplit=1)
    if len(parts) == 2 and parts[0].isdigit():
        return BindSetRequest(
            operator_user_id=user_id,
            target_user_id=parts[0],
            character_name=parts[1],
            specified_target=True,
        )

    return BindSetRequest(
        operator_user_id=user_id,
        target_user_id=user_id,
        character_name=arg_text,
    )


def parse_self_bind_delete_request(
    user_id: str = Depends(get_event_user_id),
) -> BindDeleteRequest:
    """解析普通用户删除绑定请求。"""
    return BindDeleteRequest(
        operator_user_id=user_id,
        target_user_id=user_id,
    )


def parse_superuser_bind_delete_request(
    user_id: str = Depends(get_event_user_id),
    arg_text: str = Depends(get_command_text),
) -> BindDeleteRequest:
    """解析超级用户删除绑定请求。"""
    if arg_text.isdigit():
        return BindDeleteRequest(
            operator_user_id=user_id,
            target_user_id=arg_text,
            specified_target=True,
        )

    return BindDeleteRequest(
        operator_user_id=user_id,
        target_user_id=user_id,
    )


# /bind - 显示使用说明
bind = on_command("bind", priority=10, block=True)


@bind.handle()
async def handle_bind_help(event: MessageEvent) -> None:
    """处理 /bind 命令，显示使用说明。

    Args:
        event: 消息事件
    """
    user_id = str(event.user_id)
    manager = get_manager()

    # 检查用户是否有绑定
    has_binding = manager.has_binding(user_id)
    binding_info = ""
    if has_binding:
        character_name = manager.get_character_name(user_id)
        binding_info = f"\n📋 您当前的角色绑定: {character_name}"

    help_text = (
        "🎭 角色绑定命令说明：\n"
        "━━━━━━━━━━━━━━━\n"
        "• .bind set <角色名>\n"
        "  设置您的角色绑定\n"
        "• .bind del\n"
        "  删除您的角色绑定\n"
        "• .bind list\n"
        "  查看您的角色绑定\n"
        "━━━━━━━━━━━━━━━"
        f"{binding_info}"
    )

    await bind.finish(help_text)


# /bind set <角色名> 或 /bind set <用户ID> <角色名>
bind_set_superuser = on_command(
    ("bind", "set"), permission=SUPERUSER, priority=9, block=True
)
bind_set = on_command(("bind", "set"), priority=10, block=True)


@bind_set_superuser.handle()
async def handle_set_superuser(
    request: BindSetRequest = Depends(parse_superuser_bind_set_request),
    manager: CharacterBindingManager = Depends(get_manager),
) -> None:
    """处理超级用户设置绑定命令。"""
    if not request.character_name:
        await bind_set_superuser.finish(
            "❌ 请提供角色名\n"
            "用法: .bind set <角色名>\n"
            "SUPERUSER 用法: .bind set <用户ID> <角色名>"
        )

    await manager.set_character_name(request.target_user_id, request.character_name)

    if request.specified_target:
        await bind_set_superuser.finish(
            f"✅ 已为用户 {request.target_user_id} 设置角色名为 "
            f"{request.character_name}"
        )

    await bind_set_superuser.finish(f"✅ 已设置您的角色名为 {request.character_name}")


@bind_set.handle()
async def handle_set(
    request: BindSetRequest = Depends(parse_self_bind_set_request),
    manager: CharacterBindingManager = Depends(get_manager),
) -> None:
    """处理普通用户设置绑定命令。"""
    if not request.character_name:
        await bind_set.finish("❌ 请提供角色名\n用法: .bind set <角色名>")

    await manager.set_character_name(request.target_user_id, request.character_name)
    await bind_set.finish(f"✅ 已设置您的角色名为 {request.character_name}")


# /bind del [用户ID]
bind_del_superuser = on_command(
    ("bind", "del"), permission=SUPERUSER, priority=9, block=True
)
bind_del = on_command(("bind", "del"), priority=10, block=True)


@bind_del_superuser.handle()
async def handle_del_superuser(
    request: BindDeleteRequest = Depends(parse_superuser_bind_delete_request),
    manager: CharacterBindingManager = Depends(get_manager),
) -> None:
    """处理超级用户删除绑定命令。"""
    success = await manager.remove_character_name(request.target_user_id)

    if request.specified_target:
        if success:
            await bind_del_superuser.finish(
                f"✅ 已删除用户 {request.target_user_id} 的角色绑定"
            )
        else:
            await bind_del_superuser.finish(
                f"❌ 用户 {request.target_user_id} 没有角色绑定"
            )

    if success:
        await bind_del_superuser.finish("✅ 已删除您的角色绑定")

    await bind_del_superuser.finish("❌ 您还没有设置角色绑定")


@bind_del.handle()
async def handle_del(
    request: BindDeleteRequest = Depends(parse_self_bind_delete_request),
    manager: CharacterBindingManager = Depends(get_manager),
) -> None:
    """处理普通用户删除绑定命令。"""
    success = await manager.remove_character_name(request.target_user_id)
    if success:
        await bind_del.finish("✅ 已删除您的角色绑定")
    else:
        await bind_del.finish("❌ 您还没有设置角色绑定")


# /bind list
bind_list_superuser = on_command(
    ("bind", "list"), permission=SUPERUSER, priority=9, block=True
)
bind_list = on_command(("bind", "list"), priority=10, block=True)


@bind_list_superuser.handle()
async def handle_list_superuser(
    manager: CharacterBindingManager = Depends(get_manager),
) -> None:
    """处理超级用户查看绑定列表命令。"""
    bindings = manager.list_bindings()

    if not bindings:
        await bind_list_superuser.finish("📋 当前没有任何角色绑定")

    lines = ["📋 所有角色绑定列表："]
    for uid, name in bindings.items():
        lines.append(f"  {uid}: {name}")
    await bind_list_superuser.finish("\n".join(lines))


@bind_list.handle()
async def handle_list(
    user_id: str = Depends(get_event_user_id),
    manager: CharacterBindingManager = Depends(get_manager),
) -> None:
    """处理普通用户查看绑定列表命令。"""
    bindings = manager.list_bindings()

    if not bindings:
        await bind_list.finish("📋 当前没有任何角色绑定")

    if user_id in bindings:
        await bind_list.finish(f"📋 您的角色绑定: {bindings[user_id]}")

    await bind_list.finish("❌ 您还没有设置角色绑定")
