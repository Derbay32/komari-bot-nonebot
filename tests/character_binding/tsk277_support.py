"""TSK-277 测试共享件：窄 seam 守卫、权威文案常量、可替换协调器与载荷读取。

本模块只承载测试语义，不实现任何生产行为。所有定稿文案逐字取自
``/private/tmp/tsk277-authoritative-views.json``（视图 6a9ec36d / 6a9eb957 /
6a9eb9c2 / 6a9ebece / 6a9ebf29 / 6a9ec12f / 6a9ec1bf / 6a9ec2bc / 6a9ec423）。
"""

from __future__ import annotations

import importlib
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, cast

import pytest

from komari_bot.plugins.group_admission import (
    QQAdmissionToken,
    QQBindClaim,
    QQEffectDecision,
    QQInitialBindRequest,
    QQVerifiedBindingSession,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from nonebot.adapters.qq.event import GroupAtMessageCreateEvent

WIZARD_MODULE = "komari_bot.plugins.character_binding.wizard"
HANDLER_MODULE = "komari_bot.plugins.character_binding.qq_commands"
WIZARD_CONTRACT = (
    "BindingWizard",
    "WizardScope",
    "WizardSessionView",
    "WizardReply",
    "WizardButton",
    "WizardStep",
    "BindingCommitOutcomeUnknownError",
    "get_binding_wizard",
    "set_binding_wizard",
)

SESSION_TTL = timedelta(minutes=10)

# ---- 权威定稿文案（逐字） ----
CHALLENGE_BODY = (
    "正在确认你的本群身份。\n"
    "会话码：{session}\n"
    "请点击“继续绑定”进入下一步。\n"
    "本次绑定流程有效期为 10 分钟，可随时取消。"
)
NOT_READY = "身份确认尚未完成，请稍后点击“继续绑定”。"
NAME_INPUT = "请填写本群角色名。\n\n长度为 **1–64 个字符**，同群不可重名。"  # noqa: RUF001
LEGACY_CHOICE = "你之前的角色名是：**{name}**。\n\n是否在本群继续使用？"
BINDING_CONFIRM = "本群角色名：**{name}**\n\n仅对当前群生效。\n\n确认绑定？"
BIND_SUCCESS = "已完成绑定。\n\n本群角色名：**{name}**。"
EXISTING_BINDING = "本群角色名：**{name}**\n\n仅对当前群生效。"
RENAME_CONFIRM = "将本群角色名从“{old}”改为“{new}”。\n确认修改？"
RENAME_SUCCESS = "本群角色名已修改为：{name}。"
UNBIND_CONFIRM = (
    "将清除你的本群角色名“{name}”。\n"
    "两个机器人入口已确认的账号关联会保留。\n"
    "其他群不受影响；当前对局仍使用原名字。\n"
    "确认解绑？"
)
UNBIND_SUCCESS = "已解除本群角色名绑定。\n再次开局或加入前，请通过 /bind 设置角色名。"
CANCELLED = "已取消本次操作，原有绑定未变更。"
EXPIRED = "本次操作已失效，请重新运行 /bind。"
NAME_FORMAT_ERROR = "请输入 1–64 个字符的角色名。"  # noqa: RUF001
NAME_CONTROL_ERROR = "角色名不能包含换行、控制字符或零宽字符，请换一个名字。"
NAME_DUPLICATE_ERROR = "这个角色名在本群已被使用，请换一个名字。"
BAD_COMMAND = "绑定命令格式不正确，请发送 /bind 查看当前步骤。"
NOT_YOUR_FLOW = "这不是你的绑定流程，请通过 /bind 发起自己的操作。"
WRONG_GROUP = "请在发起本次绑定的群内继续操作。"
WRONG_STEP = "当前步骤不支持这个操作，请发送 /bind 查看当前步骤。"
NO_LEGACY = "没有可沿用的旧角色名，请填写本群角色名。"
NO_CHARACTER_NAME = "你还没有设置本群角色名，请通过 /bind 完成绑定。"
GROUP_UNCONFIRMED = "暂时无法确认本群信息，请稍后重新运行 /bind。"
GROUP_CONFLICT = "本群的机器人身份关联存在冲突，请联系管理员处理。"
MEMBER_CONFLICT = "你的本群账号关联存在冲突，请联系管理员处理。"
IDENTITY_UNCONFIRMED = "暂时无法完成身份验证，请稍后重新运行 /bind。"

CONTINUE_BUTTON = "继续绑定"
CANCEL_BUTTON = "取消"
FILL_NAME_BUTTON = "填写名字"
REUSE_BUTTON = "沿用旧名"
REFILL_BUTTON = "重新填写"
CONFIRM_BUTTON = "确认绑定"
MODIFY_NAME_BUTTON = "修改名字"
RENAME_BUTTON = "改名"
UNBIND_BUTTON = "解绑"

APP_ID = "app-tsk277"
SECOND_APP_ID = "app-tsk277-2"
GROUP_OPENID = "group-openid-tsk277"
SECOND_GROUP_OPENID = "group-openid-tsk277-2"
MEMBER_OPENID = "member-openid-tsk277"
SECOND_MEMBER_OPENID = "member-openid-tsk277-2"
GROUP_ID = 277001
SECOND_GROUP_ID = 277002
MEMBER_QQ = 277002
SECOND_MEMBER_QQ = 277003
OFFICIAL_BOT_QQ = "9277001"
BASE_TIME = datetime(2026, 9, 8, 12, 0, tzinfo=UTC)


@dataclass
class FrozenClock:
    """可控 UTC 时钟，避免真实等待 TTL 边界。"""

    current: datetime = field(default_factory=lambda: BASE_TIME)

    def __call__(self) -> datetime:
        return self.current

    def advance(self, delta: timedelta) -> None:
        self.current += delta


def require_wizard_contract(*names: str) -> Any:
    """加载真实 wizard 模块；生产 seam 缺失时给出明确业务 RED 而不是 import 崩。"""
    try:
        module = importlib.import_module(WIZARD_MODULE)
    except ModuleNotFoundError as error:
        pytest.fail(f"TSK-277 生产 seam 缺失：{WIZARD_MODULE}（{error}）")
    missing = [name for name in (names or WIZARD_CONTRACT) if not hasattr(module, name)]
    if missing:
        pytest.fail(f"TSK-277 wizard 公共契约缺失：{missing}")
    return module


def freeze_qq_now(
    monkeypatch: pytest.MonkeyPatch,
    clock: Callable[[], datetime],
) -> None:
    """把 group_admission.qq 的时间源与测试时钟统一。

    生产 qq.py 用真实墙钟判定 challenge/verified session TTL；测试若只冻结
    coordinator 时钟会让 claim 立即过期。统一到同一可控时钟，夹具才确定。
    """
    qq_module = importlib.import_module("komari_bot.plugins.group_admission.qq")
    monkeypatch.setattr(qq_module, "_now", clock)


def make_claim(
    *,
    clock: FrozenClock,
    session_code: str = "qq277-session",
    app_id: str = APP_ID,
    group_openid: str = GROUP_OPENID,
    member_openid: str = MEMBER_OPENID,
    qq_message_id: str = "qq277-msg-1",
    is_new: bool = True,
    ttl: timedelta = SESSION_TTL,
) -> QQBindClaim:
    return QQBindClaim(
        session_code=session_code,
        app_id=app_id,
        group_openid=group_openid,
        member_openid=member_openid,
        qq_message_id=qq_message_id,
        connection_generation=0,
        expires_at=clock() + ttl,
        is_new=is_new,
    )


def make_verified(
    *,
    clock: FrozenClock,
    session_code: str = "qq277-session",
    app_id: str = APP_ID,
    group_openid: str = GROUP_OPENID,
    member_openid: str = MEMBER_OPENID,
    qq_message_id: str = "qq277-msg-1",
    group_id: int = GROUP_ID,
    member_qq: int = MEMBER_QQ,
    ttl: timedelta = SESSION_TTL,
) -> QQVerifiedBindingSession:
    return QQVerifiedBindingSession(
        session_code=session_code,
        app_id=app_id,
        group_openid=group_openid,
        member_openid=member_openid,
        qq_message_id=qq_message_id,
        group_id=group_id,
        member_qq=member_qq,
        connection_generation=0,
        expires_at=clock() + ttl,
    )


def make_token(
    *,
    scope: str,
    app_id: str = APP_ID,
    group_openid: str = GROUP_OPENID,
    member_openid: str = MEMBER_OPENID,
    qq_message_id: str = "qq277-msg-1",
    group_id: int | None = None,
    member_qq: int | None = None,
    claim: QQBindClaim | None = None,
    verified_session: QQVerifiedBindingSession | None = None,
) -> QQAdmissionToken:
    return QQAdmissionToken(
        scope=cast("Any", scope),
        app_id=app_id,
        group_openid=group_openid,
        member_openid=member_openid,
        qq_message_id=qq_message_id,
        group_id=group_id,
        member_qq=member_qq,
        effective_policy_revision=1,
        connection_generation=0,
        claim=claim,
        verified_session=verified_session,
    )


class FakeCoordinator:
    """wizard 依赖的协调器端口替身，记录调用并按脚本返回资格。"""

    def __init__(
        self,
        *,
        claim: QQBindClaim | None = None,
        verified: QQVerifiedBindingSession | None = None,
        recheck_allowed: bool = True,
        allow_first_rechecks: int | None = None,
    ) -> None:
        self.claim = claim
        self.verified = verified
        self.recheck_allowed = recheck_allowed
        self.allow_first_rechecks = allow_first_rechecks
        self.recheck_script: list[bool] = []
        self.claim_calls: list[QQInitialBindRequest] = []
        self.recheck_calls: list[tuple[QQAdmissionToken, str]] = []
        self.cancelled: list[str] = []
        self.session_resolver_calls: list[tuple[str, str, str]] = []

    async def claim_initial_bind(
        self,
        request: QQInitialBindRequest,
    ) -> QQBindClaim | None:
        self.claim_calls.append(request)
        return self.claim

    async def resolve_verified_binding_session(
        self,
        app_id: str,
        group_openid: str,
        member_openid: str,
    ) -> QQVerifiedBindingSession | None:
        self.session_resolver_calls.append((app_id, group_openid, member_openid))
        return self.verified

    async def recheck(
        self,
        token: QQAdmissionToken,
        *,
        effect: str,
    ) -> QQEffectDecision:
        self.recheck_calls.append((token, effect))
        if self.recheck_script:
            allowed = self.recheck_script.pop(0)
        elif self.allow_first_rechecks is not None:
            allowed = len(self.recheck_calls) <= self.allow_first_rechecks
        else:
            allowed = self.recheck_allowed
        return QQEffectDecision(
            allowed=allowed,
            effect=cast("Any", effect),
            reason_code="policy_admitted" if allowed else "policy_restricted",
            effective_policy_revision=1,
        )

    async def cancel(self, session_code: str) -> None:
        self.cancelled.append(session_code)


def make_event(
    *,
    content: str,
    message_id: str,
    app_id: str = APP_ID,
    group_openid: str = GROUP_OPENID,
    member_openid: str = MEMBER_OPENID,
) -> GroupAtMessageCreateEvent:
    from tests.group_admission.qq_admission_support import make_group_at

    del app_id
    return make_group_at(
        content=content,
        group_openid=group_openid,
        member_openid=member_openid,
        message_id=message_id,
    )


def buttons_of(reply: Any) -> list[tuple[str, str, int]]:
    """把 WizardReply 的键盘拍平成 (label, command, type) 列表。"""
    rows = getattr(reply, "keyboard", ()) or ()
    return [
        (str(button.label), str(button.command), 2)
        for row in rows
        for button in row
    ]


def payload_buttons(payload: dict[str, Any]) -> list[tuple[str, str, int]]:
    """从真实 QQ ``post_group_messages`` 载荷读取按钮 (label, data, type)。"""
    keyboard = payload.get("keyboard")
    content = getattr(keyboard, "content", None)
    rows = getattr(content, "rows", None) or []
    result: list[tuple[str, str, int]] = []
    for row in rows:
        for button in getattr(row, "buttons", None) or []:
            render_data = getattr(button, "render_data", None)
            action = getattr(button, "action", None)
            result.append(
                (
                    str(getattr(render_data, "label", "")),
                    str(getattr(action, "data", "")),
                    int(getattr(action, "type", 0) or 0),
                )
            )
    return result


def markdown_content(payload: dict[str, Any]) -> str:
    markdown = payload.get("markdown")
    return str(getattr(markdown, "content", "") or "")


def reference_message_id(payload: dict[str, Any]) -> str | None:
    reference = payload.get("message_reference")
    if reference is None:
        return None
    return str(getattr(reference, "message_id", "") or "")


__all__ = [
    "APP_ID",
    "BAD_COMMAND",
    "BASE_TIME",
    "BINDING_CONFIRM",
    "BIND_SUCCESS",
    "CANCELLED",
    "CANCEL_BUTTON",
    "CHALLENGE_BODY",
    "CONFIRM_BUTTON",
    "CONTINUE_BUTTON",
    "EXISTING_BINDING",
    "EXPIRED",
    "FILL_NAME_BUTTON",
    "GROUP_CONFLICT",
    "GROUP_ID",
    "GROUP_OPENID",
    "GROUP_UNCONFIRMED",
    "HANDLER_MODULE",
    "IDENTITY_UNCONFIRMED",
    "LEGACY_CHOICE",
    "MEMBER_CONFLICT",
    "MEMBER_OPENID",
    "MEMBER_QQ",
    "MODIFY_NAME_BUTTON",
    "NAME_CONTROL_ERROR",
    "NAME_DUPLICATE_ERROR",
    "NAME_FORMAT_ERROR",
    "NAME_INPUT",
    "NOT_READY",
    "NOT_YOUR_FLOW",
    "NO_CHARACTER_NAME",
    "NO_LEGACY",
    "OFFICIAL_BOT_QQ",
    "REFILL_BUTTON",
    "RENAME_BUTTON",
    "RENAME_CONFIRM",
    "RENAME_SUCCESS",
    "REUSE_BUTTON",
    "SECOND_APP_ID",
    "SECOND_GROUP_ID",
    "SECOND_GROUP_OPENID",
    "SECOND_MEMBER_OPENID",
    "SECOND_MEMBER_QQ",
    "SESSION_TTL",
    "UNBIND_BUTTON",
    "UNBIND_CONFIRM",
    "UNBIND_SUCCESS",
    "WIZARD_CONTRACT",
    "WIZARD_MODULE",
    "WRONG_GROUP",
    "WRONG_STEP",
    "FakeCoordinator",
    "FrozenClock",
    "buttons_of",
    "freeze_qq_now",
    "make_claim",
    "make_event",
    "make_token",
    "make_verified",
    "markdown_content",
    "payload_buttons",
    "reference_message_id",
    "require_wizard_contract",
]
