"""QQ 群角色绑定向导：会话、视图、发送前重审与最终原子提交。

向导只消费 274 写入共享 ``state`` 的 ``QQAdmissionToken``，不重复初始 claim，
也不自行解释准入策略。临时会话由向导拥有，正式写入复用 271
``BindingTransaction`` 与 276 共享组锁，并在同一事务内重读 canonical 名字，
避免提交结果不确定后的旧确认覆盖后续合法维护结果。
"""

from __future__ import annotations

import secrets
from collections import OrderedDict, deque
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Literal, Protocol

from nonebot.adapters.qq.event import GroupAtMessageCreateEvent

from komari_bot.db.group_transaction_locks import lock_group_scope
from komari_bot.plugins.group_admission import QQInitialBindRequest

from .manager import (
    CharacterNameValidationError,
    validate_character_name,
)
from .transaction import (
    BindingConflictError,
    BindingPersistenceError,
    BindingTransaction,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Sequence

    from sqlalchemy.ext.asyncio import AsyncSession

    from komari_bot.plugins.group_admission import (
        QQAdmissionToken,
        QQBindClaim,
        QQEffectDecision,
        QQVerifiedBindingSession,
    )

    from .manager import CharacterBindingManager

SESSION_TTL: timedelta = timedelta(minutes=10)
_MAX_SEEN_MESSAGES: int = 1024
_MAX_COMPLETED_REPLIES: int = 32
_MAX_SESSIONS: int = 512
_COMMAND_PREFIX: str = "/bind"

WizardStep = Literal[
    "challenge_pending",
    "legacy_choice",
    "name_input",
    "binding_confirm",
    "rename_confirm",
    "unbind_confirm",
    "completed",
]
WizardOperation = Literal["bind", "rename", "unbind"]

# ---- 权威定稿文案（逐字取自 Huly 最终视图，不得改写） ----
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

_NAME_STEPS: frozenset[WizardStep] = frozenset(
    {"legacy_choice", "name_input", "binding_confirm", "rename_confirm"}
)
_CONFIRM_STEPS: frozenset[WizardStep] = frozenset(
    {"binding_confirm", "rename_confirm", "unbind_confirm"}
)


class BindingCommitOutcomeUnknownError(RuntimeError):
    """最终提交结果不确定：既不能报告成功，也不能断言失败。"""


@dataclass(frozen=True, slots=True)
class WizardScope:
    """一个草稿的身份作用域。"""

    app_id: str
    group_openid: str
    member_openid: str


@dataclass(frozen=True, slots=True)
class WizardButton:
    """QQ 键盘按钮：action.type=2 只填入真实命令。"""

    label: str
    command: str


@dataclass(frozen=True, slots=True)
class WizardReply:
    """一条待发送的可见回复。"""

    body: str
    keyboard: tuple[tuple[WizardButton, ...], ...]
    reply_to_message_id: str | None


@dataclass(frozen=True, slots=True)
class WizardSessionView:
    """向导临时会话的不可变只读视图。"""

    session_code: str
    step: WizardStep
    operation: WizardOperation
    scope: WizardScope
    character_name: str | None
    created_at: datetime
    expires_at: datetime
    challenge_sent: bool
    completed: bool


@dataclass(slots=True)
class _Session:
    session_code: str
    scope: WizardScope
    operation: WizardOperation
    step: WizardStep
    created_at: datetime
    expires_at: datetime
    character_name: str | None = None
    previous_name: str | None = None
    challenge_sent: bool = False
    challenge_mode: Literal["unmapped", "business"] | None = None
    expected_group_id: int | None = None
    expected_member_qq: int | None = None
    completed: bool = False

    def view(self) -> WizardSessionView:
        return WizardSessionView(
            session_code=self.session_code,
            step=self.step,
            operation=self.operation,
            scope=self.scope,
            character_name=self.character_name,
            created_at=self.created_at,
            expires_at=self.expires_at,
            challenge_sent=self.challenge_sent,
            completed=self.completed,
        )


@dataclass(frozen=True, slots=True)
class _ConfirmSnapshot:
    """confirm 进入 await 前冻结的不可变草稿快照。"""

    scope: WizardScope
    session_code: str
    operation: WizardOperation
    character_name: str | None
    previous_name: str | None
    expected_group_id: int | None
    expected_member_qq: int | None
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class _CompletedReply:
    """已完成回复的 canonical 校验与待清理信息。"""

    session_code: str
    scope: WizardScope
    operation: WizardOperation
    character_name: str | None


def _escape_name(name: str) -> str:
    """把名字渲染为普通文本，避免注入 Markdown 排版或真实 mention。"""
    escaped = name.replace("\\", "\\\\")
    for marker in ("*", "_", "`", "~", "|"):
        escaped = escaped.replace(marker, f"\\{marker}")
    return escaped.replace("<", "&lt;").replace(">", "&gt;")


def bind_command_text(content: str) -> str | None:
    """按词法返回去掉前导空白后的 ``/bind`` 命令文本。

    命令边界以空白分隔：``/bind``、``/bind <子命令>`` 与 ``" /bind "`` 是命令；
    ``/bindfoo`` 不是命令，必须静默。只去除命令前的空白，不改动子命令正文。
    """
    text = content.lstrip()
    if not text.startswith(_COMMAND_PREFIX):
        return None
    if len(text) > len(_COMMAND_PREFIX) and not text[len(_COMMAND_PREFIX)].isspace():
        return None
    return text


def _conflict_text(error: BindingConflictError) -> str:
    message = str(error)
    if "member" in message:
        return MEMBER_CONFLICT
    if "角色名" in message:
        return NAME_DUPLICATE_ERROR
    if "group" in message or "群" in message:
        return GROUP_CONFLICT
    return IDENTITY_UNCONFIRMED


class BindingCoordinatorPort(Protocol):
    """向导依赖的最小协调器端口（真实 QQBindingCoordinator 已具备）。"""

    async def claim_initial_bind(
        self,
        request: QQInitialBindRequest,
    ) -> QQBindClaim | None: ...

    async def resolve_verified_binding_session(
        self,
        app_id: str,
        group_openid: str,
        member_openid: str,
    ) -> QQVerifiedBindingSession | None: ...

    async def recheck(
        self,
        token: QQAdmissionToken,
        *,
        effect: str,
    ) -> QQEffectDecision: ...

    async def cancel(self, session_code: str) -> None: ...


class BindingWizard:
    """拥有 QQ 绑定向导临时会话与最终事务边界。"""

    def __init__(
        self,
        *,
        coordinator: BindingCoordinatorPort,
        session_factory: Callable[[], AsyncSession],
        clock: Callable[[], datetime],
        manager: CharacterBindingManager | None = None,
        legacy_loader: Callable[[str], Awaitable[str | None]] | None = None,
    ) -> None:
        self._coordinator = coordinator
        self._session_factory = session_factory
        self._clock = clock
        self._manager = manager
        self._legacy_loader = legacy_loader
        self._sessions: dict[WizardScope, _Session] = {}
        self._by_code: dict[str, _Session] = {}
        self._seen_messages: OrderedDict[tuple[str, str, str, str], None] = (
            OrderedDict()
        )
        self._completed_replies: deque[tuple[WizardReply, _CompletedReply]] = deque(
            maxlen=_MAX_COMPLETED_REPLIES
        )
        self._active = True
        self._generation = 0

    # ---- 基础设施 ----

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None:
            raise ValueError("clock must return a timezone-aware datetime")  # noqa: TRY003
        return value.astimezone(UTC)

    async def _recheck(self, token: QQAdmissionToken) -> bool:
        try:
            decision = await self._coordinator.recheck(token, effect=token.scope)
        except Exception:
            return False
        return bool(decision.allowed)

    def _build(
        self,
        event: GroupAtMessageCreateEvent,
        body: str,
        keyboard: Sequence[Sequence[WizardButton]] = (),
    ) -> WizardReply:
        return WizardReply(
            body=body,
            keyboard=tuple(tuple(row) for row in keyboard),
            reply_to_message_id=event.id,
        )

    async def _reply(
        self,
        token: QQAdmissionToken,
        event: GroupAtMessageCreateEvent,
        body: str,
        keyboard: Sequence[Sequence[WizardButton]] = (),
    ) -> WizardReply | None:
        if not await self._recheck(token):
            return None
        return self._build(event, body, keyboard)

    def _new_code(self) -> str:
        for _attempt in range(8):
            code = f"qq277-{secrets.token_hex(8)}"
            if code not in self._by_code:
                return code
        raise RuntimeError("unable to allocate a wizard session code")  # noqa: TRY003

    def _store(self, session: _Session) -> None:
        previous = self._sessions.get(session.scope)
        if previous is not None and previous.session_code != session.session_code:
            self._by_code.pop(previous.session_code, None)
        self._sessions[session.scope] = session
        self._by_code[session.session_code] = session

    async def _drop(self, session: _Session) -> None:
        if self._sessions.get(session.scope) is session:
            self._sessions.pop(session.scope, None)
        if self._by_code.get(session.session_code) is session:
            self._by_code.pop(session.session_code, None)
        await self._coordinator.cancel(session.session_code)

    def _mark_seen(self, message_key: tuple[str, str, str, str]) -> None:
        self._seen_messages[message_key] = None
        if len(self._seen_messages) > _MAX_SEEN_MESSAGES:
            self._seen_messages.popitem(last=False)

    async def _read_canonical(self, scope: WizardScope) -> str | None:
        if self._manager is not None:
            cached = self._manager.get_qq_character_name(
                app_id=scope.app_id,
                group_openid=scope.group_openid,
                member_openid=scope.member_openid,
            )
            if cached is not None:
                return cached
        else:
            return None
        session = self._session_factory()
        try:
            row = await BindingTransaction(session).resolve_member(
                app_id=scope.app_id,
                group_openid=scope.group_openid,
                member_openid=scope.member_openid,
            )
        except Exception:
            return None
        finally:
            with suppress(Exception):
                await session.close()
        return row.character_name if row is not None else None

    async def _legacy_candidate(self, member_qq: int | None) -> str | None:
        if member_qq is None:
            return None
        if self._legacy_loader is not None:
            try:
                return await self._legacy_loader(str(member_qq))
            except Exception:
                return None
        if self._manager is not None:
            try:
                return await self._manager.get_legacy_character_name(str(member_qq))
            except Exception:
                return None
        return None

    async def _resolve_evidence(
        self,
        scope: WizardScope,
    ) -> QQVerifiedBindingSession | None:
        try:
            return await self._coordinator.resolve_verified_binding_session(
                scope.app_id,
                scope.group_openid,
                scope.member_openid,
            )
        except Exception:
            return None

    @staticmethod
    def _verified_identity(token: QQAdmissionToken) -> tuple[int, int] | None:
        if token.verified_session is not None:
            return (token.verified_session.group_id, token.verified_session.member_qq)
        if (
            token.scope == "business"
            and isinstance(token.group_id, int)
            and token.group_id > 0
            and isinstance(token.member_qq, int)
            and token.member_qq > 0
        ):
            return (token.group_id, token.member_qq)
        return None

    # ---- 公共 seam ----

    async def get_session(self, scope: WizardScope) -> WizardSessionView | None:
        session = self._sessions.get(scope)
        if session is None:
            return None
        if not session.completed and session.expires_at <= self._now():
            await self._drop(session)
            return None
        return session.view()

    async def cancel(self, session_code: str) -> bool:
        session = self._by_code.get(str(session_code))
        if session is None:
            return False
        if not session.completed:
            await self._drop(session)
        return True

    async def authorize_send(
        self,
        token: QQAdmissionToken,
        reply: WizardReply,
    ) -> bool:
        """发送前最后一道复核：重审必须是最后一步。

        任何 canonical 读取（await）都发生在重审之前，重审之后的 active/
        generation 为同步检查，确保读取或重审等待期间的封禁/撤销/close 不漏检。
        """
        generation = self._generation
        completed = self._completed_reply_for(reply)
        if completed is not None and not await self._canonical_matches(completed):
            return False
        if not await self._recheck(token):
            return False
        return self._active and generation == self._generation

    def _completed_reply_for(self, reply: WizardReply) -> _CompletedReply | None:
        for candidate, completed in self._completed_replies:
            if candidate is reply:
                return completed
        return None

    async def _canonical_matches(self, completed: _CompletedReply) -> bool:
        canonical = await self._read_canonical(completed.scope)
        if completed.operation == "unbind":
            return canonical is None
        if completed.character_name is None:
            return False
        return canonical == completed.character_name

    async def finish_send(self, reply: WizardReply) -> None:
        """发送尝试结束后清理已完成会话；普通回复为 no-op。

        handler 在真正发送的 ``finally`` 中调用：无论发送成功、失败还是发送前
        重审拒绝，都在发送尝试结束后才撤销临时会话，避免提前清理使成功回复
        的重审失效，也不把撤销权限扩大到未发送的回复。
        """
        for index, (candidate, completed) in enumerate(self._completed_replies):
            if candidate is not reply:
                continue
            del self._completed_replies[index]
            with suppress(Exception):
                await self._coordinator.cancel(completed.session_code)
            return

    def reset(self) -> None:
        """撤销当前向导代次：在途旧调用不得继续写库。"""
        self._generation += 1
        self._active = False
        self._sessions.clear()
        self._by_code.clear()
        self._completed_replies.clear()
        self._seen_messages.clear()

    async def close(self) -> None:
        """撤销待清理的已完成会话，再关闭并废弃当前代次。"""
        for _candidate, completed in list(self._completed_replies):
            with suppress(Exception):
                await self._coordinator.cancel(completed.session_code)
        self.reset()

    async def _prune(self) -> None:
        """有界清理过期草稿，避免临时状态无限保留。"""
        now = self._now()
        for session in list(self._sessions.values()):
            if session.expires_at <= now:
                await self._drop(session)
        while len(self._sessions) > _MAX_SESSIONS:
            oldest = min(
                self._sessions.values(),
                key=lambda item: item.created_at,
            )
            await self._drop(oldest)

    def _session_still_current(self, snapshot: _ConfirmSnapshot) -> bool:
        session = self._sessions.get(snapshot.scope)
        if session is None or session.session_code != snapshot.session_code:
            return False
        if session.completed:
            return False
        return session.expires_at > self._now()

    async def handle_event(  # noqa: PLR0911
        self,
        event: GroupAtMessageCreateEvent,
        token: QQAdmissionToken,
    ) -> WizardReply | None:
        if not self._active:
            return None
        if type(event) is not GroupAtMessageCreateEvent:
            return None
        content = getattr(event, "content", None)
        message_id = getattr(event, "id", None)
        if (
            not isinstance(content, str)
            or not isinstance(message_id, str)
            or not message_id
        ):
            return None
        command_text = bind_command_text(content)
        if command_text is None:
            return None
        await self._prune()
        seen_key = (
            token.app_id,
            token.group_openid,
            token.member_openid,
            message_id,
        )
        if seen_key in self._seen_messages:
            return None
        self._mark_seen(seen_key)
        scope = WizardScope(
            app_id=token.app_id,
            group_openid=token.group_openid,
            member_openid=token.member_openid,
        )
        verb, remainder = self._split_command(command_text)
        if verb == "":
            return await self._handle_bind(token, event, scope)
        if verb == "name":
            session_code, name_text = self._split_name_argument(remainder)
            if not session_code or name_text is None:
                return await self._reply(token, event, BAD_COMMAND)
            return await self._handle_name(token, event, scope, session_code, name_text)
        if verb == "reuse":
            session_code = self._first_token(remainder)
            if not session_code:
                return await self._reply(token, event, BAD_COMMAND)
            return await self._handle_reuse(token, event, scope, session_code)
        if verb == "confirm":
            session_code = self._first_token(remainder)
            if not session_code:
                return await self._reply(token, event, BAD_COMMAND)
            return await self._handle_confirm(token, event, scope, session_code)
        if verb == "cancel":
            session_code = self._first_token(remainder)
            if not session_code:
                return await self._reply(token, event, BAD_COMMAND)
            return await self._handle_cancel(token, event, scope, session_code)
        if verb == "rename":
            return await self._handle_rename(token, event, scope)
        if verb == "unbind":
            return await self._handle_unbind(token, event, scope)
        return await self._reply(token, event, BAD_COMMAND)

    # ---- 命令解析 ----

    @staticmethod
    def _split_command(content: str) -> tuple[str, str]:
        remainder = content[len("/bind") :]
        stripped = remainder.lstrip()
        if not stripped:
            return ("", "")
        pieces = stripped.split(None, 1)
        return (pieces[0], pieces[1] if len(pieces) > 1 else "")

    @staticmethod
    def _first_token(remainder: str) -> str:
        pieces = remainder.split(None, 1)
        return pieces[0] if pieces else ""

    @staticmethod
    def _split_name_argument(remainder: str) -> tuple[str, str | None]:
        """切出会话码并保留其后完整剩余文本（不按空格截断）。"""
        length = len(remainder)
        index = 0
        while index < length and remainder[index].isspace():
            index += 1
        start = index
        while index < length and not remainder[index].isspace():
            index += 1
        if start >= length:
            return ("", None)
        if index >= length:
            return (remainder[start:], None)
        return (remainder[start:index], remainder[index + 1 :])

    # ---- 会话定位 ----

    async def _locate(
        self,
        session_code: str,
        scope: WizardScope,
    ) -> _Session | str:
        """按会话码定位并核验作用域；返回会话或定稿错误文案。"""
        session = self._by_code.get(session_code)
        if session is None:
            return EXPIRED
        if not session.completed and session.expires_at <= self._now():
            await self._drop(session)
            return EXPIRED
        if (
            session.scope.app_id != scope.app_id
            or session.scope.member_openid != scope.member_openid
        ):
            return NOT_YOUR_FLOW
        if session.scope.group_openid != scope.group_openid:
            return WRONG_GROUP
        return session

    # ---- 主 /bind ----

    async def _handle_bind(
        self,
        token: QQAdmissionToken,
        event: GroupAtMessageCreateEvent,
        scope: WizardScope,
    ) -> WizardReply | None:
        existing = self._sessions.get(scope)
        if (
            existing is not None
            and not existing.completed
            and existing.expires_at <= self._now()
        ):
            await self._drop(existing)
            existing = None
        if existing is not None and existing.completed:
            canonical = await self._read_canonical(scope)
            if canonical is not None:
                return await self._reply(
                    token,
                    event,
                    EXISTING_BINDING.format(name=_escape_name(canonical)),
                    (
                        (
                            WizardButton("改名", "/bind rename"),
                            WizardButton("解绑", "/bind unbind"),
                        ),
                    ),
                )
            existing = None
        if existing is not None:
            return await self._advance_or_repeat(existing, token, event)
        identity = self._verified_identity(token)
        if identity is None:
            return await self._start_challenge(token, event, scope)
        return await self._start_verified(token, event, scope, identity)

    async def _advance_or_repeat(
        self,
        session: _Session,
        token: QQAdmissionToken,
        event: GroupAtMessageCreateEvent,
    ) -> WizardReply | None:
        if session.step == "challenge_pending":
            verified = await self._resolve_evidence(session.scope)
            if verified is not None and verified.session_code == session.session_code:
                if not await self._recheck(token):
                    return None
                return await self._advance_after_evidence(session, event, verified)
            if session.challenge_mode == "business":
                return await self._reply(token, event, NOT_READY)
            return None
        return await self._reply(
            token,
            event,
            self._step_body(session),
            self._step_keyboard(session),
        )

    async def _advance_after_evidence(
        self,
        session: _Session,
        event: GroupAtMessageCreateEvent,
        verified: QQVerifiedBindingSession,
    ) -> WizardReply:
        session.expected_group_id = verified.group_id
        session.expected_member_qq = verified.member_qq
        if session.operation == "bind":
            canonical = await self._read_canonical(session.scope)
            if canonical is not None:
                return self._build(
                    event,
                    EXISTING_BINDING.format(name=_escape_name(canonical)),
                    (
                        (
                            WizardButton("改名", "/bind rename"),
                            WizardButton("解绑", "/bind unbind"),
                        ),
                    ),
                )
            legacy = await self._legacy_candidate(verified.member_qq)
            if legacy is not None:
                session.previous_name = legacy
                session.step = "legacy_choice"
                return self._build(
                    event,
                    LEGACY_CHOICE.format(name=_escape_name(legacy)),
                    self._step_keyboard(session),
                )
        session.step = "name_input"
        return self._build(event, NAME_INPUT, self._step_keyboard(session))

    async def _start_challenge(  # noqa: PLR0911
        self,
        token: QQAdmissionToken,
        event: GroupAtMessageCreateEvent,
        scope: WizardScope,
    ) -> WizardReply | None:
        claim = token.claim
        if claim is not None:
            if claim.expires_at <= self._now():
                return None
            if not await self._recheck(token):
                return None
            session = _Session(
                session_code=claim.session_code,
                scope=scope,
                operation="bind",
                step="challenge_pending",
                created_at=self._now(),
                expires_at=claim.expires_at,
                challenge_sent=True,
                challenge_mode="unmapped",
            )
            self._store(session)
            return self._build(
                event,
                CHALLENGE_BODY.format(session=session.session_code),
                self._step_keyboard(session),
            )
        if not await self._recheck(token):
            return None
        request = QQInitialBindRequest(
            app_id=scope.app_id,
            group_openid=scope.group_openid,
            member_openid=scope.member_openid,
            qq_message_id=event.id,
            command=str(getattr(event, "content", "/bind")),
        )
        try:
            claimed = await self._coordinator.claim_initial_bind(request)
        except Exception:
            return None
        if claimed is None:
            return None
        session = _Session(
            session_code=claimed.session_code,
            scope=scope,
            operation="bind",
            step="challenge_pending",
            created_at=self._now(),
            expires_at=claimed.expires_at,
            challenge_sent=True,
            challenge_mode="business",
        )
        self._store(session)
        return self._build(
            event,
            CHALLENGE_BODY.format(session=session.session_code),
            self._step_keyboard(session),
        )

    async def _start_verified(
        self,
        token: QQAdmissionToken,
        event: GroupAtMessageCreateEvent,
        scope: WizardScope,
        identity: tuple[int, int],
    ) -> WizardReply | None:
        if not await self._recheck(token):
            return None
        canonical = await self._read_canonical(scope)
        if canonical is not None:
            return self._build(
                event,
                EXISTING_BINDING.format(name=_escape_name(canonical)),
                (
                    (
                        WizardButton("改名", "/bind rename"),
                        WizardButton("解绑", "/bind unbind"),
                    ),
                ),
            )
        verified = token.verified_session
        if verified is not None:
            session_code = verified.session_code
            expires_at = verified.expires_at
        else:
            session_code = self._new_code()
            expires_at = self._now() + SESSION_TTL
        legacy = await self._legacy_candidate(identity[1])
        session = _Session(
            session_code=session_code,
            scope=scope,
            operation="bind",
            step="legacy_choice" if legacy is not None else "name_input",
            created_at=self._now(),
            expires_at=expires_at,
            previous_name=legacy,
            expected_group_id=identity[0],
            expected_member_qq=identity[1],
        )
        self._store(session)
        return self._build(
            event,
            self._step_body(session),
            self._step_keyboard(session),
        )

    # ---- /bind name ----

    async def _handle_name(  # noqa: PLR0911
        self,
        token: QQAdmissionToken,
        event: GroupAtMessageCreateEvent,
        scope: WizardScope,
        session_code: str,
        name_text: str,
    ) -> WizardReply | None:
        session = await self._locate(session_code, scope)
        if isinstance(session, str):
            return await self._reply(token, event, session)
        if session.completed:
            return await self._reply(token, event, EXPIRED)
        if session.step not in _NAME_STEPS or session.operation == "unbind":
            return await self._reply(token, event, WRONG_STEP)
        try:
            normalized = validate_character_name(name_text)
        except CharacterNameValidationError as validation_error:
            text = (
                NAME_CONTROL_ERROR
                if "控制字符" in str(validation_error)
                else NAME_FORMAT_ERROR
            )
            return await self._reply(token, event, text)
        if session.operation == "rename":
            current = await self._read_canonical(scope)
            if current is None:
                return await self._reply(token, event, NO_CHARACTER_NAME)
            session.previous_name = current
        if not await self._recheck(token):
            return None
        session.character_name = normalized
        session.step = (
            "rename_confirm" if session.operation == "rename" else "binding_confirm"
        )
        return self._build(
            event,
            self._step_body(session),
            self._step_keyboard(session),
        )

    # ---- /bind reuse ----

    async def _handle_reuse(  # noqa: PLR0911
        self,
        token: QQAdmissionToken,
        event: GroupAtMessageCreateEvent,
        scope: WizardScope,
        session_code: str,
    ) -> WizardReply | None:
        session = await self._locate(session_code, scope)
        if isinstance(session, str):
            return await self._reply(token, event, session)
        if session.completed:
            return await self._reply(token, event, EXPIRED)
        if session.operation != "bind" or session.step not in {
            "legacy_choice",
            "name_input",
        }:
            return await self._reply(token, event, WRONG_STEP)
        candidate = session.previous_name
        if candidate is None:
            member_qq = session.expected_member_qq or token.member_qq
            candidate = await self._legacy_candidate(member_qq)
        if candidate is None:
            return await self._reply(token, event, NO_LEGACY)
        try:
            normalized = validate_character_name(candidate)
        except CharacterNameValidationError:
            return await self._reply(token, event, NO_LEGACY)
        if not await self._recheck(token):
            return None
        session.previous_name = candidate
        session.character_name = normalized
        session.step = "binding_confirm"
        return self._build(
            event,
            self._step_body(session),
            self._step_keyboard(session),
        )

    # ---- /bind confirm ----

    async def _handle_confirm(  # noqa: PLR0911
        self,
        token: QQAdmissionToken,
        event: GroupAtMessageCreateEvent,
        scope: WizardScope,
        session_code: str,
    ) -> WizardReply | None:
        session = await self._locate(session_code, scope)
        if isinstance(session, str):
            return await self._reply(token, event, session)
        if session.completed:
            return await self._reply(token, event, self._success_body(session))
        if session.step not in _CONFIRM_STEPS:
            return await self._reply(token, event, WRONG_STEP)
        if not await self._recheck(token):
            return None
        snapshot = _ConfirmSnapshot(
            scope=session.scope,
            session_code=session.session_code,
            operation=session.operation,
            character_name=session.character_name,
            previous_name=session.previous_name,
            expected_group_id=session.expected_group_id,
            expected_member_qq=session.expected_member_qq,
            expires_at=session.expires_at,
        )
        generation = self._generation
        committed, text = await self._commit(snapshot, token, generation)
        if not committed:
            if text is None:
                return None
            return self._build(event, text, self._step_keyboard(session))
        if (
            self._active
            and generation == self._generation
            and self._sessions.get(snapshot.scope) is session
        ):
            session.character_name = snapshot.character_name
            session.previous_name = snapshot.previous_name
            session.completed = True
            session.step = "completed"
        await self._publish()
        reply = self._build(
            event,
            self._success_body_for(snapshot.operation, snapshot.character_name),
        )
        self._completed_replies.append(
            (
                reply,
                _CompletedReply(
                    session_code=snapshot.session_code,
                    scope=snapshot.scope,
                    operation=snapshot.operation,
                    character_name=snapshot.character_name,
                ),
            )
        )
        return reply

    async def _commit(  # noqa: PLR0911
        self,
        snapshot: _ConfirmSnapshot,
        token: QQAdmissionToken,
        generation: int,
    ) -> tuple[bool, str | None]:
        session_db = self._session_factory()
        try:
            try:
                await lock_group_scope(
                    session_db,
                    app_id=snapshot.scope.app_id,
                    group_openid=snapshot.scope.group_openid,
                )
            except BindingPersistenceError:
                return (False, IDENTITY_UNCONFIRMED)
            except Exception:
                return (False, IDENTITY_UNCONFIRMED)
            if not self._active or generation != self._generation:
                return (False, None)
            if not await self._recheck(token):
                return (False, None)
            if not self._session_still_current(snapshot):
                return (False, None)
            try:
                outcome = await self._apply(session_db, snapshot, token)
            except BindingConflictError as conflict:
                return (False, _conflict_text(conflict))
            except BindingPersistenceError:
                return (False, IDENTITY_UNCONFIRMED)
            except Exception:
                return (False, IDENTITY_UNCONFIRMED)
            if not outcome[0]:
                return outcome
            if not await self._final_commit_gate(snapshot, token, generation):
                with suppress(Exception):
                    await session_db.rollback()
                return (False, None)
            try:
                await session_db.commit()
            except Exception as commit_error:
                raise BindingCommitOutcomeUnknownError(
                    "绑定提交结果不确定"
                ) from commit_error
            return outcome
        finally:
            with suppress(Exception):
                await session_db.close()

    async def _final_commit_gate(
        self,
        snapshot: _ConfirmSnapshot,
        token: QQAdmissionToken,
        generation: int,
    ) -> bool:
        """写入后、commit 前的最终闸门。

        先 await 最新准入重审（policy/user_ban 与正式身份），再同步复核
        active/generation/会话当前与 TTL，确保重审等待期间发生的
        cancel/close 不会被漏掉；任一步失败都必须回滚暂存事务。
        """
        if not await self._recheck(token):
            return False
        return (
            self._active
            and generation == self._generation
            and self._session_still_current(snapshot)
            and snapshot.expires_at > self._now()
        )

    async def _apply(  # noqa: PLR0911
        self,
        session_db: AsyncSession,
        snapshot: _ConfirmSnapshot,
        token: QQAdmissionToken,
    ) -> tuple[bool, str | None]:
        scope = snapshot.scope
        transaction = BindingTransaction(session_db)
        expected_group = (
            snapshot.expected_group_id
            if snapshot.expected_group_id is not None
            else token.group_id
        )
        expected_member = (
            snapshot.expected_member_qq
            if snapshot.expected_member_qq is not None
            else token.member_qq
        )
        group = await transaction.resolve_group(
            app_id=scope.app_id,
            group_openid=scope.group_openid,
        )
        if (
            group is not None
            and expected_group is not None
            and str(group.group_id) != str(expected_group)
        ):
            return (False, GROUP_CONFLICT)
        row = await transaction.resolve_member(
            app_id=scope.app_id,
            group_openid=scope.group_openid,
            member_openid=scope.member_openid,
        )
        if (
            row is not None
            and expected_member is not None
            and str(row.member_qq) != str(expected_member)
        ):
            return (False, MEMBER_CONFLICT)
        if expected_member is not None:
            by_qq = await transaction.resolve_member_by_qq(
                app_id=scope.app_id,
                group_openid=scope.group_openid,
                member_qq=str(expected_member),
            )
            if by_qq is not None and by_qq.member_openid != scope.member_openid:
                return (False, MEMBER_CONFLICT)
        canonical = row.character_name if row is not None else None
        target = snapshot.character_name
        if snapshot.operation == "bind":
            if target is None:
                return (False, EXPIRED)
            if canonical is not None:
                if canonical == target:
                    return (True, "")
                return (False, EXPIRED)
            if expected_group is None or expected_member is None:
                return (False, IDENTITY_UNCONFIRMED)
            await transaction.bind(
                app_id=scope.app_id,
                group_id=str(expected_group),
                group_openid=scope.group_openid,
                member_qq=str(expected_member),
                member_openid=scope.member_openid,
                character_name=target,
            )
            return (True, "")
        if snapshot.operation == "rename":
            if target is None or snapshot.previous_name is None or canonical is None:
                return (False, EXPIRED)
            if canonical == target:
                return (True, "")
            if canonical == snapshot.previous_name:
                await transaction.rename(
                    app_id=scope.app_id,
                    group_openid=scope.group_openid,
                    member_openid=scope.member_openid,
                    character_name=target,
                )
                return (True, "")
            return (False, EXPIRED)
        if canonical is None:
            return (True, "")
        if snapshot.previous_name is not None and canonical == snapshot.previous_name:
            await transaction.clear(
                app_id=scope.app_id,
                group_openid=scope.group_openid,
                member_openid=scope.member_openid,
            )
            return (True, "")
        return (False, EXPIRED)

    async def _publish(self) -> None:
        if self._manager is not None:
            await self._manager.refresh_snapshot()

    # ---- /bind cancel ----

    async def _handle_cancel(
        self,
        token: QQAdmissionToken,
        event: GroupAtMessageCreateEvent,
        scope: WizardScope,
        session_code: str,
    ) -> WizardReply | None:
        session = await self._locate(session_code, scope)
        if isinstance(session, str):
            return await self._reply(token, event, session)
        await self._drop(session)
        return await self._reply(token, event, CANCELLED)

    # ---- /bind rename 与 /bind unbind ----

    async def _handle_rename(
        self,
        token: QQAdmissionToken,
        event: GroupAtMessageCreateEvent,
        scope: WizardScope,
    ) -> WizardReply | None:
        existing = self._sessions.get(scope)
        if existing is not None and not existing.completed:
            if existing.expires_at <= self._now():
                await self._drop(existing)
            else:
                return await self._reply(token, event, WRONG_STEP)
        canonical = await self._read_canonical(scope)
        if canonical is None:
            return await self._reply(token, event, NO_CHARACTER_NAME)
        if not await self._recheck(token):
            return None
        session = _Session(
            session_code=self._new_code(),
            scope=scope,
            operation="rename",
            step="name_input",
            created_at=self._now(),
            expires_at=self._now() + SESSION_TTL,
            previous_name=canonical,
        )
        self._store(session)
        return self._build(
            event,
            self._step_body(session),
            self._step_keyboard(session),
        )

    async def _handle_unbind(
        self,
        token: QQAdmissionToken,
        event: GroupAtMessageCreateEvent,
        scope: WizardScope,
    ) -> WizardReply | None:
        existing = self._sessions.get(scope)
        if existing is not None and not existing.completed:
            if existing.expires_at <= self._now():
                await self._drop(existing)
            else:
                return await self._reply(token, event, WRONG_STEP)
        canonical = await self._read_canonical(scope)
        if canonical is None:
            return await self._reply(token, event, NO_CHARACTER_NAME)
        if not await self._recheck(token):
            return None
        session = _Session(
            session_code=self._new_code(),
            scope=scope,
            operation="unbind",
            step="unbind_confirm",
            created_at=self._now(),
            expires_at=self._now() + SESSION_TTL,
            previous_name=canonical,
        )
        self._store(session)
        return self._build(
            event,
            self._step_body(session),
            self._step_keyboard(session),
        )

    # ---- 视图渲染 ----

    @staticmethod
    def _success_body_for(
        operation: WizardOperation,
        character_name: str | None,
    ) -> str:
        if operation == "bind":
            return BIND_SUCCESS.format(name=_escape_name(character_name or ""))
        if operation == "rename":
            return RENAME_SUCCESS.format(name=_escape_name(character_name or ""))
        return UNBIND_SUCCESS

    @classmethod
    def _success_body(cls, session: _Session) -> str:
        return cls._success_body_for(session.operation, session.character_name)

    def _step_body(self, session: _Session) -> str:  # noqa: PLR0911
        if session.step == "challenge_pending":
            return CHALLENGE_BODY.format(session=session.session_code)
        if session.step == "legacy_choice":
            return LEGACY_CHOICE.format(name=_escape_name(session.previous_name or ""))
        if session.step == "name_input":
            return NAME_INPUT
        if session.step == "binding_confirm":
            return BINDING_CONFIRM.format(
                name=_escape_name(session.character_name or "")
            )
        if session.step == "rename_confirm":
            return RENAME_CONFIRM.format(
                old=_escape_name(session.previous_name or ""),
                new=_escape_name(session.character_name or ""),
            )
        if session.step == "unbind_confirm":
            return UNBIND_CONFIRM.format(name=_escape_name(session.previous_name or ""))
        return self._success_body(session)

    @staticmethod
    def _step_keyboard(session: _Session) -> tuple[tuple[WizardButton, ...], ...]:  # noqa: PLR0911
        code = session.session_code
        cancel = WizardButton("取消", f"/bind cancel {code}")
        if session.step == "challenge_pending":
            return ((WizardButton("继续绑定", "/bind"), cancel),)
        if session.step == "legacy_choice":
            return (
                (
                    WizardButton("沿用旧名", f"/bind reuse {code}"),
                    WizardButton("重新填写", f"/bind name {code} "),
                    cancel,
                ),
            )
        if session.step == "name_input":
            return ((WizardButton("填写名字", f"/bind name {code} "), cancel),)
        if session.step == "binding_confirm":
            return (
                (
                    WizardButton("确认绑定", f"/bind confirm {code}"),
                    WizardButton("修改名字", f"/bind name {code} "),
                    cancel,
                ),
            )
        if session.step == "rename_confirm":
            return (
                (
                    WizardButton("确认修改", f"/bind confirm {code}"),
                    WizardButton("修改名字", f"/bind name {code} "),
                    cancel,
                ),
            )
        if session.step == "unbind_confirm":
            return ((WizardButton("确认解绑", f"/bind confirm {code}"), cancel),)
        return ()


_wizard: BindingWizard | None = None


def set_binding_wizard(wizard: BindingWizard | None) -> None:
    """安装或撤销当前进程的绑定向导实例。"""
    global _wizard  # noqa: PLW0603
    _wizard = wizard


def get_binding_wizard() -> BindingWizard | None:
    """读取当前进程的绑定向导实例。"""
    return _wizard


__all__ = [
    "BAD_COMMAND",
    "BINDING_CONFIRM",
    "BIND_SUCCESS",
    "CANCELLED",
    "CHALLENGE_BODY",
    "EXISTING_BINDING",
    "EXPIRED",
    "LEGACY_CHOICE",
    "NAME_INPUT",
    "NOT_READY",
    "RENAME_CONFIRM",
    "RENAME_SUCCESS",
    "SESSION_TTL",
    "UNBIND_CONFIRM",
    "UNBIND_SUCCESS",
    "BindingCommitOutcomeUnknownError",
    "BindingCoordinatorPort",
    "BindingWizard",
    "WizardButton",
    "WizardOperation",
    "WizardReply",
    "WizardScope",
    "WizardSessionView",
    "WizardStep",
    "bind_command_text",
    "get_binding_wizard",
    "set_binding_wizard",
]
