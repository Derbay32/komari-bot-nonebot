"""按钮权限一次性实验探针（throwaway prototype，TSK-297）。

独立入口，不加载任何业务插件、不访问 PG/Redis，仅用于在真实 QQ 客户端
对比三种按钮 ``action.permission`` 配置的可点击性。详细说明见同目录
``PROTOTYPE-PERMISSION.md``。
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import nonebot
from nonebot import logger, on_message
from nonebot.adapters.qq import Adapter as QQAdapter
from nonebot.adapters.qq.config import BotInfo, Intents
from nonebot.adapters.qq.event import GroupAtMessageCreateEvent
from nonebot.adapters.qq.message import Message, MessageSegment
from nonebot.adapters.qq.models import (
    Action,
    Button,
    InlineKeyboard,
    InlineKeyboardRow,
    MessageKeyboard,
    Permission,
    RenderData,
)
from pydantic import BaseModel

from komari_bot.core.nonebot_compat import install_nonebot_forwardref_compatibility

CONFIG_ENV = "PERMISSION_PROBE_CONFIG"
COMMAND = "/permtest"
INSPECT_USER = "prototype-user"
UNSUPPORT_TIPS = "当前客户端不支持按钮，请升级 QQ 后重试"

PROBE_BODY = (
    "**按钮权限实验（一次性探针）**\n"
    "本消息携带 3 个按钮，仅 `action.permission` 不同，其余参数完全一致：\n"
    "- A 仅本人：`type=0`，指定名单为触发者本人\n"
    "- B 空名单：`type=0`，指定名单为空\n"
    "- C 所有人：`type=2`\n\n"
    "请依次点击三个按钮观察是否可点。"
    "点击只会把对应命令填入输入框，需要你手动发送后服务端才会返回回执。"
)

RECEIPTS: dict[str, str] = {
    "A": "回执 A：命令已到达服务端（不代表按钮点击已生效）。",
    "B": "回执 B：命令已到达服务端（不代表按钮点击已生效）。",
    "C": "回执 C：命令已到达服务端（不代表按钮点击已生效）。",
}


class _ProbeConfig(BaseModel):
    """显式 JSON 配置文件的唯一来源；不读取 .env。"""

    qq_bots: list[BotInfo]
    qq_is_sandbox: bool = False
    probe_group_openid: str


def _button(button_id: str, label: str, data: str, permission: Permission) -> Button:
    """三个按钮共用同一渲染风格与动作类型，只有权限不同。"""
    return Button(
        id=button_id,
        render_data=RenderData(label=label, visited_label=label, style=0),
        action=Action(
            type=2,
            permission=permission,
            data=data,
            reply=False,
            enter=False,
            unsupport_tips=UNSUPPORT_TIPS,
        ),
    )


def build_probe_message(user_openid: str) -> Message:
    """构造唯一一条 Markdown 实验消息；触发者 OpenID 为空时拒绝构造。"""
    if not user_openid:
        raise ValueError("probe requires a non-empty triggering user openid")  # noqa: TRY003
    keyboard = MessageKeyboard(
        content=InlineKeyboard(
            rows=[
                InlineKeyboardRow(
                    buttons=[
                        _button(
                            "A",
                            "A 仅本人",
                            "/permtest A",
                            Permission(type=0, specify_user_ids=[user_openid]),
                        ),
                        _button(
                            "B",
                            "B 空名单",
                            "/permtest B",
                            Permission(type=0, specify_user_ids=[]),
                        ),
                        _button("C", "C 所有人", "/permtest C", Permission(type=2)),
                    ]
                )
            ]
        )
    )
    message = Message()
    message += MessageSegment.markdown(PROBE_BODY)
    message += MessageSegment.keyboard(keyboard)
    return message


def inspect_payload() -> str:
    """离线打印同一构造函数产出的完整合成载荷（使用 dummy 用户）。"""
    message = build_probe_message(INSPECT_USER)
    return json.dumps(
        [
            {
                "type": segment.type,
                "data": {
                    key: (
                        value.model_dump(mode="json", exclude_none=True)
                        if isinstance(value, BaseModel)
                        else value
                    )
                    for key, value in segment.data.items()
                },
            }
            for segment in message
        ],
        ensure_ascii=False,
        indent=2,
    )


def _command_text(content: str) -> str | None:
    """按词法返回去掉前导空白后的 ``/permtest`` 命令文本。"""
    text = content.lstrip()
    if not text.startswith(COMMAND):
        return None
    if len(text) > len(COMMAND) and not text[len(COMMAND)].isspace():
        return None
    return text


def _load_probe_config() -> _ProbeConfig:
    path = os.environ.get(CONFIG_ENV, "").strip()
    if not path:
        raise RuntimeError(f"missing {CONFIG_ENV} pointing to the private probe config JSON")  # noqa: TRY003
    with Path(path).open(encoding="utf-8") as file:
        raw = json.load(file)
    config = _ProbeConfig.model_validate(raw)
    if not config.probe_group_openid.strip():
        raise RuntimeError("probe config requires a non-empty probe_group_openid")  # noqa: TRY003
    if not config.qq_bots:
        raise RuntimeError("probe config requires at least one QQ bot entry")  # noqa: TRY003
    return config


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


def main() -> None:
    """离线检查或启动探针服务；模块级不做任何 NoneBot 初始化。"""
    if "--inspect" in sys.argv[1:]:
        print(inspect_payload())  # noqa: T201
        return

    probe_config = _load_probe_config()
    install_nonebot_forwardref_compatibility()
    nonebot.init(
        driver="~fastapi+~httpx+~websockets",
        port=8080,
        log_level="INFO",
        _env_file=None,
    )
    driver = nonebot.get_driver()
    extra_config = driver.config.model_extra
    if extra_config is None:
        raise RuntimeError("nonebot config is not extensible")  # noqa: TRY003
    extra_config["qq_is_sandbox"] = probe_config.qq_is_sandbox
    extra_config["qq_bots"] = [
        bot.model_copy(update={"intent": _minimal_qq_intents()}).model_dump()
        for bot in probe_config.qq_bots
    ]
    driver.register_adapter(QQAdapter)
    probe_group_openid = probe_config.probe_group_openid.strip()

    matcher = on_message(priority=5, block=False)

    @matcher.handle()
    async def handle_probe(event: GroupAtMessageCreateEvent) -> None:
        if type(event) is not GroupAtMessageCreateEvent:
            return
        if event.group_openid != probe_group_openid:
            return
        text = _command_text(event.content if isinstance(event.content, str) else "")
        if text is None:
            return
        args = text[len(COMMAND) :].split()
        if not args:
            user_openid = event.get_user_id()
            if not user_openid:
                logger.warning("[PermProbe] 触发者身份为空，按 fail-closed 放弃发送实验消息")
                return
            try:
                await matcher.send(build_probe_message(user_openid))
            except Exception:
                logger.exception("[PermProbe] 实验消息发送结果不确定，不补发")
            return
        if len(args) == 1 and args[0] in RECEIPTS:
            await matcher.send(MessageSegment.text(RECEIPTS[args[0]]))
        # 其他子命令一律静默忽略

    logger.info("[PermProbe] 探针已启动，仅监听已配置实验群")
    nonebot.run()


if __name__ == "__main__":
    main()
