from typing import Any

from nonebot import logger, on_command
from nonebot.adapters.onebot.v11 import Bot, MessageEvent
from nonebot.exception import FinishedException
from nonebot.params import Command
from nonebot.permission import SUPERUSER
from nonebot.plugin import PluginMetadata, require

from .config_schema import DynamicConfigSchema
from .llm_service import generate_reply
from .prompt_builder import build_prompt

# 依赖用户数据插件
user_data_plugin = require("user_data")
# 依赖配置管理插件
config_manager_plugin = require("config_manager")
# 依赖权限管理插件
permission_manager_plugin = require("permission_manager")
# 依赖记忆插件
komari_memory_plugin = require("komari_memory")
# 依赖 LLM Provider 插件
llm_provider = require("llm_provider")
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
    description="今日好感插件，基于 LLM API 生成个性化问候，支持好感度系统和白名单管理",
    usage=".jrhg - 获取今日好感问候\n/jrhg on/off - 管理员控制插件开关",
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


def _get_response(daily_favor: int, user_nickname: str) -> str:
    """获取回复"""
    match daily_favor:
        case df if df <= 20:
            return "咦！？去、去死！"
        case df if df <= 40:
            return f"唔诶，{user_nickname}！？怎、怎么是你…!?（后退）。"
        case df if df <= 60:
            return f"不、不过是区区{user_nickname}，可、可别得意忘形了。"
        case df if df <= 80:
            return f"{user_nickname}，你、你来啦，今天要不要，一、一起看书……？"
        case _:
            return (
                f"只、只是有一点点在意你哦……唔，{user_nickname}，你就是这点不、不行啦！"
            )


async def _load_interaction_history(
    *,
    user_id: str,
    group_id: str | None,
) -> dict[str, Any] | None:
    """读取用户互动历史，失败时返回 None。"""
    if not group_id:
        return None

    get_plugin_manager = getattr(komari_memory_plugin, "get_plugin_manager", None)
    if not callable(get_plugin_manager):
        return None

    try:
        manager: Any = get_plugin_manager()
        memory_service: Any = (
            None if manager is None else getattr(manager, "memory", None)
        )
        if memory_service is None:
            return None

        interaction_history = await memory_service.get_interaction_history(
            user_id=user_id,
            group_id=group_id,
        )
        return interaction_history if isinstance(interaction_history, dict) else None
    except Exception:
        logger.warning(
            "[JRHG] 读取互动历史失败，改用空历史占位: user={} group={}",
            user_id,
            group_id,
            exc_info=True,
        )
        return None


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
    raw_group_id = getattr(event, "group_id", None)
    group_id = str(raw_group_id) if raw_group_id is not None else None
    favor_result = None  # 初始化以避免异常处理中未绑定

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

        interaction_history = await _load_interaction_history(
            user_id=user_id,
            group_id=group_id,
        )
        response = _get_response(favor_result.daily_favor, username)
        prompt_messages = build_prompt(
            daily_favor=favor_result.daily_favor,
            interaction_history=interaction_history,
        )

        try:
            generated_response = await generate_reply(
                messages=prompt_messages,
                request_trace_id=f"jrhg-{event.message_id}",
            )
        except Exception as llm_error:
            logger.warning(
                "[JRHG] LLM 生成失败，使用固定文案兜底: user={} group={} has_history={} error={}",
                username,
                group_id or "-",
                interaction_history is not None,
                llm_error,
                exc_info=True,
            )
        else:
            if generated_response.strip():
                response = generated_response.strip()
            else:
                logger.warning(
                    "[JRHG] LLM 返回空回复，使用固定文案兜底: user={} group={} has_history={}",
                    username,
                    group_id or "-",
                    interaction_history is not None,
                )

        # 格式化最终回复
        final_response = await format_favor_response(
            ai_response=response,
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
