"""容器内 NoneBot 初始化入口。"""

import nonebot
from nonebot import logger
from nonebot.adapters.onebot.v11 import Adapter as OneBotV11Adapter
from nonebot.adapters.qq import Adapter as QQAdapter
from nonebot.adapters.qq.config import BotInfo, Intents
from pydantic import BaseModel, ConfigDict, Field

from komari_bot.core.nonebot_compat import install_nonebot_forwardref_compatibility

install_nonebot_forwardref_compatibility()
nonebot.init()

driver = nonebot.get_driver()
driver.register_adapter(OneBotV11Adapter)


class _QQStartupConfig(BaseModel):
    """启动时读取的 QQ 账号与取证身份映射。"""

    model_config = ConfigDict(hide_input_in_errors=True)

    qq_is_sandbox: bool = False
    qq_bots: list[BotInfo] = Field(default_factory=list)
    qq_official_bot_qq_by_app: dict[str, object] = Field(default_factory=dict)


def _minimal_qq_intents() -> Intents:
    return Intents(
        guilds=False,
        guild_members=False,
        guild_messages=False,
        guild_message_reactions=False,
        direct_message=False,
        open_forum_event=False,
        audio_live_member=False,
        group_members=False,
        c2c_group_at_messages=True,
        interaction=False,
        message_audit=False,
        forum_event=False,
        audio_action=False,
        at_messages=False,
    )


def _register_qq_adapter_if_configured() -> None:
    """Register QQ only when configured, with a closed minimal intent set."""
    try:
        startup_config = nonebot.get_plugin_config(_QQStartupConfig)
    except Exception:
        logger.error("[QQ] invalid startup configuration; adapter registration aborted")
        raise RuntimeError("invalid QQ startup configuration") from None  # noqa: TRY003
    if not startup_config.qq_bots:
        return
    normalized_bots = [
        bot.model_copy(update={"intent": _minimal_qq_intents()})
        for bot in startup_config.qq_bots
    ]
    # The QQ adapter reads its own Config through get_plugin_config a second
    # time.  Publish only normalized BotInfo values through the driver config;
    # secrets and identity mappings remain configuration data, never log data.
    extra_config = driver.config.model_extra
    if extra_config is None:
        logger.error("[QQ] invalid startup configuration; adapter registration aborted")
        raise RuntimeError("invalid QQ startup configuration") from None  # noqa: TRY003
    try:
        extra_config["qq_is_sandbox"] = startup_config.qq_is_sandbox
        extra_config["qq_bots"] = [bot.model_dump() for bot in normalized_bots]
    except Exception:
        logger.error("[QQ] invalid startup configuration; adapter registration aborted")
        raise RuntimeError("invalid QQ startup configuration") from None  # noqa: TRY003
    driver.register_adapter(QQAdapter)


_register_qq_adapter_if_configured()

nonebot.load_builtin_plugins("echo")
# nonebot_plugin_orm 由 pyproject [tool.nonebot.plugins] 声明，经 load_from_toml
# 加载：v2.0.0 起连接池/会话生命周期/Alembic 迁移由该插件托管
nonebot.load_from_toml("pyproject.toml")

if __name__ == "__main__":
    nonebot.run()
