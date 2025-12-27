import time

from nonebot import logger
from nonebot.plugin import PluginMetadata, require
from nonebot import on_command
from nonebot.permission import SUPERUSER
from nonebot.params import CommandArg, Command
from nonebot.adapters.onebot.v11 import Bot, MessageEvent, Message
from nonebot.exception import FinishedException

from .config import Config
from .config_schemas import DynamicConfigSchema

# 依赖用户数据插件
user_data_plugin = require("user_data")
# 依赖配置管理插件
config_manager_plugin = require("config_manager")
# 依赖权限管理插件
permission_manager_plugin = require("permission_manager")
# 依赖 LLM Provider 插件
llm_provider = require("llm_provider")

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
    usage="/jrhg - 获取今日好感问候\n/jrhg on/off - 管理员控制插件开关",
    config=Config,
)

# 初始化配置管理器
config_manager = config_manager_plugin.get_config_manager("jrhg", DynamicConfigSchema)
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


def _build_favor_prompt(daily_favor: int, user_nickname: str) -> str:
    """根据好感度构建系统提示词。"""
    base_prompt = dynamic_config.default_prompt

    # 根据好感度添加具体的态度指导
    if daily_favor <= 20:
        attitude_guide = f"你对{user_nickname}的好感度很低({daily_favor}/100)，请用非常冷淡、疏远的语气回应。"
    elif daily_favor <= 40:
        attitude_guide = f"你对{user_nickname}的好感度较低({daily_favor}/100)，请用冷淡、有距离感的语气回应。"
    elif daily_favor <= 60:
        attitude_guide = f"你对{user_nickname}的好感度一般({daily_favor}/100)，请用中性、礼貌的语气回应。"
    elif daily_favor <= 80:
        attitude_guide = f"你对{user_nickname}的好感度较高({daily_favor}/100)，请用友好、热情的语气回应。"
    else:
        attitude_guide = f"你对{user_nickname}的好感度非常高({daily_favor}/100)，请用非常热情、亲密的语气回应。"

    return f"{base_prompt}\n\n{attitude_guide}\n\n请直接生成打招呼的内容，不要提及好感度数值。"


def _get_fallback_response(daily_favor: int, user_nickname: str) -> str:
    """获取备用回复（当 API 调用失败时使用）。"""
    if daily_favor <= 20:
        return f"咦！？去、去死！"
    elif daily_favor <= 40:
        return f"唔诶，{user_nickname}！？怎、怎么是你…!?（后退）。"
    elif daily_favor <= 60:
        return f"不、不过是区区{user_nickname}，可、可别得意忘形了。"
    elif daily_favor <= 80:
        return f"{user_nickname}，你、你来啦，今天要不要，一、一起看书……？"
    else:
        return f"只、只是有一点点在意你哦……唔，{user_nickname}，你就是这点不、不行啦！"


@manage.handle()
async def jrhg_switch(bot: Bot, event: MessageEvent, cmd: tuple[str, ...] = Command()):
    """处理插件开关命令"""
    _, action = cmd

    if action == "status":
        # 显示插件状态信息
        permission_info = permission_manager_plugin.format_permission_info(dynamic_config)
        plugin_status, status_desc = await permission_manager_plugin.check_plugin_status(dynamic_config)

        # 获取用户数据插件状态
        user_data_status = "🟢 正常" if generate_or_update_favorability else "🔴 异常"

        # 获取 LLM Provider 状态
        llm_provider_name = dynamic_config.api_provider.upper()
        llm_ok = await llm_provider.test_connection(dynamic_config.api_provider)
        llm_status = "🟢 正常" if llm_ok else "🔴 异常"

        message = (
            f"JRHG插件状态:\n"
            f"插件: {status_desc}\n"
            f"用户数据插件: {user_data_status}\n"
            f"LLM Provider ({llm_provider_name}): {llm_status}\n"
            f"{permission_info}"
        )
        await manage.finish(message)

    elif action in ["on", "off"]:
        # 切换插件开关
        new_status = action == "on"
        old_status = dynamic_config.plugin_enable

        if old_status == new_status:
            await manage.finish(f"插件已经是{'开启' if new_status else '关闭'}状态")

        # 持久化到 JSON
        config_manager.update_field("plugin_enable", new_status)
        # 更新本地引用
        dynamic_config.plugin_enable = new_status

        status_text = "开启" if new_status else "关闭"
        await manage.finish(f"JRHG插件已{status_text}")

    else:
        await manage.finish("未知操作，请使用 on/off/status")


@jrhg.handle()
async def jrhg_function(bot: Bot, event: MessageEvent, args: Message = CommandArg()):
    """处理jrhg主命令"""
    # 获取用户信息
    user_id = event.get_user_id()
    user_nickname = permission_manager_plugin.get_user_nickname(event)
    favor_result = None  # 初始化以避免异常处理中未绑定

    # 使用运行时配置进行权限检查
    can_use, reason = await permission_manager_plugin.check_runtime_permission(bot, event, config_manager)
    if not can_use:
        logger.info(f"用户 {user_nickname}({user_id}) 请求被拒绝，原因：{reason}")
        await jrhg.finish(f"❌ {reason}")

    try:
        # 检查依赖插件是否可用
        if not generate_or_update_favorability or not format_favor_response:
            await jrhg.finish("❌ 用户数据插件不可用，请联系管理员")

        # 获取或生成好感度
        logger.info(f"用户 {user_nickname}({user_id}) 请求好感度问候")

        favor_result = await generate_or_update_favorability(user_id)

        if favor_result.is_new_day:
            logger.info(f"为用户 {user_nickname} 生成新的每日好感度: {favor_result.daily_favor}")

        # 构建提示词
        system_prompt = _build_favor_prompt(favor_result.daily_favor, user_nickname)

        # 如果有额外参数，作为自定义消息传递给AI
        custom_message = args.extract_plain_text().strip() if args else None
        now_time = time.strftime("%A %Y-%m-%d %H:%M", time.localtime())

        if custom_message:
            user_message = f"现在的时间是{now_time}。用户{user_nickname}对你说：{custom_message}，请回应他。"
        else:
            user_message = f"现在的时间是{now_time}。请向用户{user_nickname}打个招呼。"

        # 调用 LLM Provider
        ai_response = await llm_provider.generate_text(
            prompt=user_message,
            provider=dynamic_config.api_provider,
            system_instruction=system_prompt,
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
            # 返回备用回复
            if favor_result:
                fallback = _get_fallback_response(favor_result.daily_favor, user_nickname)
            else:
                fallback = "发生错误，请稍后重试"
            await jrhg.finish(fallback)


# 插件生命周期管理
async def on_startup():
    """插件启动时的初始化"""
    try:
        # 测试 LLM API 连接
        connection_ok = await llm_provider.test_connection(dynamic_config.api_provider)

        provider = dynamic_config.api_provider.upper()
        if connection_ok:
            logger.info(f"JRHG插件启动成功，{provider} API连接正常")
        else:
            logger.warning(f"JRHG插件启动成功，但{provider} API连接测试失败")

        # 检查用户数据插件
        if not generate_or_update_favorability:
            logger.error("用户数据插件不可用，JRHG插件将无法正常工作")
        else:
            logger.info("用户数据插件可用")

    except Exception as e:
        logger.error(f"JRHG插件启动时发生错误: {e}")


async def on_shutdown():
    """插件关闭时的清理"""
    logger.info("JRHG插件已关闭")


# 导出生命周期函数
__plugin_startup__ = on_startup
__plugin_shutdown__ = on_shutdown
