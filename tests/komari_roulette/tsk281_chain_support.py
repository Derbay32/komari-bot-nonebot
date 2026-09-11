"""TSK-281 stage-B1b chain support: real ``/bind`` wizard → real roulette game.

This support wires the **two real production assemblies** into one isolated
registry window:

* ``character_binding`` — the real package import (evidence matcher + ``/bind``
  matcher) plus the real ``init_plugin`` (``QQBindingCoordinator`` +
  ``BindingWizard`` + ``CharacterBindingManager``), driven by
  :func:`tests.character_binding.tsk281_native_chain_support.binding_chain_window`
  through the real global admission gate and real OneBot ``get_msg`` evidence;
* ``komari_roulette`` — the real composition root (``lifecycle`` reload + the
  real driver startup hook) with the real QQ runtime installed over the real
  binding/admission authority.

Everything that the ticket forbids inheriting from the earlier draft is gone:
no hand-built collector / coordinator / wizard, no fixed numeric group, no
permissive blacklist, and the reloaded roulette modules are restored afterwards.

The replacement seams are written out in full (not just the first two):

* **platform transport** — the OneBot ``get_msg`` payload map and the QQ
  recording ``Bot``; the real matchers, delivery and adapter message build still
  run, so every published keyboard/Markdown payload is real;
* **restricted admission policy store** — ``AdmissionStorageFake`` serves a
  whitelist containing only this case's numeric group, so a routine recovery
  scan can never touch a foreign scope;
* **``user_ban``** — ``is_configured_superuser_id`` / ``is_user_banned`` are
  replaced with a never-banned double, so the real ``komari_user_bans`` table
  and its revision cache are **not** exercised by this chain;
* **scheduler** — the ``nonebot_plugin_apscheduler`` singleton is swapped for
  :class:`FakeScheduler`, so the real maintenance jobs are recorded and
  callable without sleeping;
* **config-manager acquisition** — the top-level ``get_config_manager`` getter
  is wrapped to record calls and reuse one **real** ``ConfigManager`` per plugin
  name (a registry-shaped acquisition fake, not a replacement config store).

The admission runtime, the canonical binding manager / transaction, the command
service, the maintenance worker and the roulette runtime stay real.  A Redis URL
is present in the environment only for configuration parity; this chain does not
claim to exercise any Redis-backed call.
"""

from __future__ import annotations

import asyncio
import importlib
import sys
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast
from uuid import uuid4

from nonebot import get_driver

from tests.character_binding.test_reply_evidence import (
    _challenge_event,
    _get_msg_payload,
    _original_event,
)
from tests.character_binding.tsk277_support import (
    BIND_SUCCESS,
    BINDING_CONFIRM,
    NAME_INPUT,
    RENAME_CONFIRM,
    RENAME_SUCCESS,
    UNBIND_CONFIRM,
    UNBIND_SUCCESS,
    markdown_content,
    payload_buttons,
    reference_message_id,
)
from tests.character_binding.tsk281_native_chain_support import (
    NativeMember,
    NativeScope,
    OneBotGetMsgTransport,
    binding_chain_window,
    numeric_id,
)
from tests.group_admission.entry_gate_support import dispatch
from tests.group_admission.qq_admission_support import dispatch_qq, make_group_at
from tests.komari_roulette.command_support import Scope, member_id
from tests.komari_roulette.tsk279_lifecycle_support import (
    PACKAGE,
    QQ_MODULE,
    FakeScheduler,
    RecordingQQBot,
    _install_config_manager_getter_fake,
    _pop_reloadable_roulette_modules,
    application_api,
    invoke_hook,
)
from tests.komari_roulette.tsk279_support import Tsk279Harness, harness_fixture_body

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable, Mapping

    import pytest
    from sqlalchemy.ext.asyncio import AsyncEngine

    from komari_bot.plugins.komari_roulette.domain import RandomSource

__all__ = [
    "CMD_BIND",
    "CMD_BIND_RENAME",
    "CMD_BIND_UNBIND",
    "CMD_CANCEL",
    "CMD_CREATE",
    "CMD_FORFEIT",
    "CMD_JOIN",
    "CMD_LEADERBOARD",
    "CMD_START",
    "COMMAND_SERVICE_MODULE",
    "Chain",
    "ChainQQBot",
    "chain_context",
    "install_repair_lifecycle_require_seam",
]

CMD_BIND = "/bind"
CMD_BIND_RENAME = "/bind rename"
CMD_BIND_UNBIND = "/bind unbind"
CMD_CANCEL = "/轮盘 取消"
CMD_CREATE = "/轮盘 开局"
CMD_FORFEIT = "/轮盘 弃权"
CMD_JOIN = "/轮盘 加入"
CMD_LEADERBOARD = "/轮盘 排行榜"
CMD_START = "/轮盘 开始"

#: Module that owns the composition-root default domain ``RandomSource``.
COMMAND_SERVICE_MODULE = f"{PACKAGE}.command_service"


class _PlatformId:
    """Minimal platform response object exposing the ``post_group_messages`` id."""

    __slots__ = ("id",)

    def __init__(self, value: str | None) -> None:
        self.id = value


class ChainQQBot(RecordingQQBot):
    """Real QQ ``Bot`` whose every ``post_group_messages`` returns a unique id.

    The platform idempotency key is the inbound message id (per command), so
    each command's platform receipt must be distinct as well; reusing one fixed
    id would collide the fulfilment rows the real delivery writes.
    """

    def __init__(self, self_id: str) -> None:
        super().__init__(self_id, platform_message_id=None)
        self._platform_seq = 0
        #: Terminal-only fault injection for the UNKNOWN delivery path: the
        #: transport records the network attempt and then answers with a
        #: failure (or no usable id) exactly once for the targeted message id.
        self.fail_once_msg_ids: set[str] = set()
        self.no_id_msg_ids: set[str] = set()

    async def call_api(self, api: str, **data: Any) -> Any:
        self.calls.append((api, data))
        if api != "post_group_messages":
            return None
        self._platform_seq += 1
        msg_id = data.get("msg_id")
        if isinstance(msg_id, str) and msg_id in self.fail_once_msg_ids:
            self.fail_once_msg_ids.discard(msg_id)
            raise TimeoutError("tsk281 injected terminal transport failure")  # noqa: TRY003
        if isinstance(msg_id, str) and msg_id in self.no_id_msg_ids:
            self.no_id_msg_ids.discard(msg_id)
            return _PlatformId(None)
        return _PlatformId(f"tsk281-platform-{self._platform_seq}")


def _scope_from(current: Scope, *, member_count: int) -> NativeScope:
    """Derive the native binding scope from one harness scope (own rows only)."""

    return NativeScope(
        app_id=current.app_id,
        group_openid=current.group_openid,
        group_id=numeric_id(uuid4().hex[:16]),
        members=tuple(
            NativeMember(
                seat=seat,
                member_openid=member_id(current, seat),
                member_qq=numeric_id(uuid4().hex[:16]),
            )
            for seat in range(1, member_count + 1)
        ),
    )


def _published_button_command(
    payload: dict[str, Any],
    label: str,
    command_prefix: str,
) -> str:
    """Extract one full command string from a published public keyboard button.

    The wizard deliberately keeps some session codes out of the Markdown body,
    so the real public keyboard button data is the only legitimate source.
    Guessing the code or reading/renaming through SQL is never allowed.
    """

    commands = [
        data
        for button_label, data, _action_type in payload_buttons(payload)
        if button_label == label
    ]
    assert len(commands) == 1, f"公开键盘应恰有一个 {label!r} 按钮，实际 {commands!r}"
    prefix = f"{command_prefix} "
    command = commands[0]
    assert command.startswith(prefix), command
    return command


def _published_button_code(
    payload: dict[str, Any],
    label: str,
    command_prefix: str,
) -> str:
    """Extract one wizard session code from a published keyboard button.

    ``/bind rename`` and ``/bind unbind`` deliberately keep the session code
    out of the Markdown body, so the real public keyboard button data is the
    only legitimate source.  Guessing the code or reading/renaming through SQL
    is never allowed.
    """

    command = _published_button_command(payload, label, command_prefix)
    session_code = command[len(f"{command_prefix} ") :].strip()
    assert session_code, command
    assert not session_code.isdigit(), (
        f"会话码必须来自真实向导按钮，不得是数字 id: {session_code!r}"
    )
    return session_code


@dataclass(slots=True)
class Chain:
    """One live real-binding + real-roulette chain on a real-PG scope."""

    harness: Tsk279Harness
    current: Scope
    scope: NativeScope
    qq: ChainQQBot
    onebot: OneBotGetMsgTransport
    _msg_seq: int = 0
    _onebot_seq: int = 281_000_000

    # -- identity / message-id helpers -----------------------------------
    def next_msg_id(self, tag: str) -> str:
        self._msg_seq += 1
        return f"tsk281-{tag}-{self._msg_seq}"

    def next_onebot_id(self) -> int:
        self._onebot_seq += 1
        return self._onebot_seq

    def member(self, seat: int) -> NativeMember:
        return self.scope.member(seat)

    # -- real QQ dispatch (real 274 gate + real matchers) ----------------
    async def send_qq(
        self,
        content: str,
        *,
        member_openid: str,
        message_id: str,
        timeout_seconds: float | None = None,
    ) -> list[tuple[str, dict[str, Any]]]:
        before = len(self.qq.calls)
        event = make_group_at(
            content=content,
            group_openid=self.current.group_openid,
            member_openid=member_openid,
            message_id=message_id,
        )
        if timeout_seconds is None:
            await dispatch_qq(cast("Any", self.qq), event)
        else:
            await asyncio.wait_for(
                dispatch_qq(cast("Any", self.qq), event), timeout=timeout_seconds
            )
        return list(self.qq.calls[before:])

    async def send_command(
        self,
        seat: int,
        command: str,
        *,
        tag: str,
    ) -> list[tuple[str, dict[str, Any]]]:
        return await self.send_qq(
            command,
            member_openid=self.member(seat).member_openid,
            message_id=self.next_msg_id(tag),
        )

    def fail_once_terminal_send(self, message_id: str) -> None:
        """Make the recording transport raise once for one inbound message id."""

        self.qq.fail_once_msg_ids.add(message_id)

    def no_id_terminal_send(self, message_id: str) -> None:
        """Make the recording transport answer with no usable id exactly once."""

        self.qq.no_id_msg_ids.add(message_id)

    # -- real OneBot evidence --------------------------------------------
    async def _deliver_evidence(
        self,
        member: NativeMember,
        session_code: str,
    ) -> int:
        original_id = self.next_onebot_id()
        self.onebot.serve(
            original_id,
            _get_msg_payload(
                message_id=original_id,
                original_text=CMD_BIND,
                group_id=self.scope.group_id,
                sender_id=member.member_qq,
            ),
        )
        await dispatch(
            self.onebot,
            _original_event(
                message_id=original_id,
                group_id=self.scope.group_id,
                member_qq=member.member_qq,
                text=CMD_BIND,
            ),
        )
        challenge_id = self.next_onebot_id()
        await dispatch(
            self.onebot,
            _challenge_event(
                message_id=challenge_id,
                session_code=session_code,
                quoted_message_id=original_id,
                quoted_text=CMD_BIND,
                group_id=self.scope.group_id,
                quoted_sender=member.member_qq,
            ),
        )
        # The OneBot side stays strictly read-only and proves the evidence read
        # targeted *this* independently minted integer reference id.
        assert self.onebot.calls[-1] == ("get_msg", {"message_id": original_id}), (
            "OneBot 证据链只允许 get_msg 只读读取本次独立整数原消息"
        )
        return original_id

    # -- real two-robot /bind wizard -------------------------------------
    async def bind(self, seat: int, name: str) -> str:
        """Run the real ``/bind`` wizard for one seat to a committed binding."""

        member = self.member(seat)
        tag = f"bind-seat{seat}"
        challenge_message_id = self.next_msg_id(f"{tag}-challenge")
        challenge_calls = await self.send_qq(
            CMD_BIND,
            member_openid=member.member_openid,
            message_id=challenge_message_id,
        )
        assert len(challenge_calls) == 1, challenge_calls
        challenge_payload = challenge_calls[-1][1]
        challenge_body = markdown_content(challenge_payload)
        assert "会话码：" in challenge_body, challenge_body
        session_code = challenge_body.split("会话码：", 1)[1].split("\n", 1)[0]
        assert session_code, challenge_body
        # The native QQ challenge must reference *this* QQ inbound message id
        # (a QQ string id), never a OneBot integer.  ``bind`` runs for both
        # seats, so the reference target is verified for every user.
        assert not challenge_message_id.isdigit(), challenge_message_id
        assert reference_message_id(challenge_payload) == challenge_message_id, (
            "QQ 首次挑战必须原生引用本次原始入站消息 "
            f"{challenge_message_id!r}，实际 {reference_message_id(challenge_payload)!r}"
        )

        onebot_original_id = await self._deliver_evidence(member, session_code)
        assert isinstance(onebot_original_id, int)
        assert str(onebot_original_id) != challenge_message_id, (
            "OneBot 引用必须使用自己的独立整数消息 id，不得复用 QQ 字符串 id"
        )

        name_calls = await self.send_qq(
            CMD_BIND,
            member_openid=member.member_openid,
            message_id=self.next_msg_id(f"{tag}-continue"),
        )
        assert len(name_calls) == 1, name_calls
        assert markdown_content(name_calls[-1][1]) == NAME_INPUT

        confirm_calls = await self.send_qq(
            f"{CMD_BIND} name {session_code} {name}",
            member_openid=member.member_openid,
            message_id=self.next_msg_id(f"{tag}-name"),
        )
        assert len(confirm_calls) == 1, confirm_calls
        assert markdown_content(confirm_calls[-1][1]) == BINDING_CONFIRM.format(
            name=name
        )

        done_calls = await self.send_qq(
            f"{CMD_BIND} confirm {session_code}",
            member_openid=member.member_openid,
            message_id=self.next_msg_id(f"{tag}-confirm"),
            timeout_seconds=30,
        )
        assert len(done_calls) == 1, done_calls
        assert markdown_content(done_calls[-1][1]) == BIND_SUCCESS.format(name=name)
        return session_code

    # -- real /bind rename + /bind unbind (public keyboard codes) --------
    async def rename_start(self, seat: int) -> str:
        """Real ``/bind rename`` → session code from the public keyboard button.

        The rename prompt body never carries the session code, so the only
        public source is the real ``填写名字`` button data; no SQL read or guess
        can produce this value.
        """

        member = self.member(seat)
        calls = await self.send_qq(
            CMD_BIND_RENAME,
            member_openid=member.member_openid,
            message_id=self.next_msg_id(f"bind-rename-seat{seat}"),
        )
        assert len(calls) == 1, calls
        payload = calls[-1][1]
        body = markdown_content(payload)
        assert body == NAME_INPUT, body
        session_code = _published_button_code(payload, "填写名字", f"{CMD_BIND} name")
        assert session_code not in body, (
            "改名会话码必须只来自公开键盘按钮，不得出现在正文中"
        )
        return session_code

    async def rename_cancel_draft(self, seat: int) -> tuple[str, str]:
        """真实 ``/bind rename`` 草稿 → (会话码, 公开“取消”按钮的完整命令)。

        改名提示正文不含会话码；会话码（``填写名字`` 按钮）与完整取消命令
        （``取消`` 按钮）都只来自真实公开键盘按钮 data；调用方发送的是按钮原始
        命令，既不猜码也不读库。
        """

        member = self.member(seat)
        calls = await self.send_qq(
            CMD_BIND_RENAME,
            member_openid=member.member_openid,
            message_id=self.next_msg_id(f"bind-rename-cancel-seat{seat}"),
        )
        assert len(calls) == 1, calls
        payload = calls[-1][1]
        assert markdown_content(payload) == NAME_INPUT, markdown_content(payload)
        session_code = _published_button_code(payload, "填写名字", f"{CMD_BIND} name")
        cancel_command = _published_button_command(
            payload, "取消", f"{CMD_BIND} cancel"
        )
        assert cancel_command == f"{CMD_BIND} cancel {session_code}", cancel_command
        return session_code, cancel_command

    async def rename_name(
        self,
        seat: int,
        session_code: str,
        *,
        old_name: str,
        new_name: str,
    ) -> None:
        """Real ``/bind name {code} {new}``; assert the confirmation step."""

        member = self.member(seat)
        calls = await self.send_qq(
            f"{CMD_BIND} name {session_code} {new_name}",
            member_openid=member.member_openid,
            message_id=self.next_msg_id(f"bind-rename-name-seat{seat}"),
        )
        assert len(calls) == 1, calls
        payload = calls[-1][1]
        body = markdown_content(payload)
        assert body == RENAME_CONFIRM.format(old=old_name, new=new_name), body
        confirm_code = _published_button_code(
            payload, "确认修改", f"{CMD_BIND} confirm"
        )
        assert confirm_code == session_code, (
            f"改名确认按钮必须复用同一会话码：{confirm_code!r} != {session_code!r}"
        )

    async def rename_confirm(
        self,
        seat: int,
        session_code: str,
        *,
        new_name: str,
    ) -> None:
        """Real ``/bind confirm {code}`` committing the rename."""

        member = self.member(seat)
        calls = await self.send_qq(
            f"{CMD_BIND} confirm {session_code}",
            member_openid=member.member_openid,
            message_id=self.next_msg_id(f"bind-rename-confirm-seat{seat}"),
            timeout_seconds=30,
        )
        assert len(calls) == 1, calls
        assert markdown_content(calls[-1][1]) == RENAME_SUCCESS.format(name=new_name)

    async def unbind_start(self, seat: int, *, expected_name: str) -> str:
        """Real ``/bind unbind`` → session code from the public keyboard button."""

        member = self.member(seat)
        calls = await self.send_qq(
            CMD_BIND_UNBIND,
            member_openid=member.member_openid,
            message_id=self.next_msg_id(f"bind-unbind-seat{seat}"),
        )
        assert len(calls) == 1, calls
        payload = calls[-1][1]
        body = markdown_content(payload)
        assert body == UNBIND_CONFIRM.format(name=expected_name), body
        session_code = _published_button_code(
            payload, "确认解绑", f"{CMD_BIND} confirm"
        )
        assert session_code not in body, (
            "解绑会话码必须只来自公开键盘按钮，不得出现在正文中"
        )
        return session_code

    async def unbind_confirm(self, seat: int, session_code: str) -> None:
        """Real ``/bind confirm {code}`` committing the ordinary unbind."""

        member = self.member(seat)
        calls = await self.send_qq(
            f"{CMD_BIND} confirm {session_code}",
            member_openid=member.member_openid,
            message_id=self.next_msg_id(f"bind-unbind-confirm-seat{seat}"),
            timeout_seconds=30,
        )
        assert len(calls) == 1, calls
        assert markdown_content(calls[-1][1]) == UNBIND_SUCCESS

    # -- PG observations -------------------------------------------------
    async def receipt_rows(self) -> list[Mapping[str, Any]]:
        from sqlalchemy import text

        async with self.harness.session_factory() as session:
            rows = (
                (
                    await session.execute(
                        text(
                            "SELECT receipt_id, inbound_msg_id, result_code "
                            "FROM komari_roulette_command_receipts "
                            "WHERE app_id = :app_id AND group_openid = :group "
                            "ORDER BY created_at ASC, receipt_id ASC"
                        ),
                        self._params(),
                    )
                )
                .mappings()
                .all()
            )
        return [dict(row) for row in rows]

    async def game_rows(self) -> list[Mapping[str, Any]]:
        from sqlalchemy import text

        async with self.harness.session_factory() as session:
            rows = (
                (
                    await session.execute(
                        text(
                            "SELECT game_id, lifecycle, phase, host_seq, "
                            "next_join_seq FROM komari_roulette_games "
                            "WHERE app_id = :app_id AND group_openid = :group "
                            "ORDER BY created_at ASC"
                        ),
                        self._params(),
                    )
                )
                .mappings()
                .all()
            )
        return [dict(row) for row in rows]

    async def player_rows(self) -> list[Mapping[str, Any]]:
        from sqlalchemy import text

        async with self.harness.session_factory() as session:
            rows = (
                (
                    await session.execute(
                        text(
                            "SELECT p.join_seq, p.member_openid, p.display_name "
                            "FROM komari_roulette_players AS p "
                            "JOIN komari_roulette_games AS g "
                            "ON g.game_id = p.game_id "
                            "WHERE g.app_id = :app_id AND g.group_openid = :group "
                            "ORDER BY p.join_seq ASC"
                        ),
                        self._params(),
                    )
                )
                .mappings()
                .all()
            )
        return [dict(row) for row in rows]

    async def player_count_for_game(self, game_id: str) -> int:
        """Count the frozen players of one exact game (no cross-game ambiguity)."""

        from sqlalchemy import text

        async with self.harness.session_factory() as session:
            return int(
                await session.scalar(
                    text(
                        "SELECT count(*) FROM komari_roulette_players "
                        "WHERE game_id = :game_id"
                    ),
                    {"game_id": game_id},
                )
                or 0
            )

    async def result_rows(self) -> list[Mapping[str, Any]]:
        from sqlalchemy import text

        async with self.harness.session_factory() as session:
            rows = (
                (
                    await session.execute(
                        text(
                            "SELECT game_id, lifecycle, reason, winner_seq, "
                            "winner_member_openid, winner_display_name, ended_at "
                            "FROM komari_roulette_results "
                            "WHERE app_id = :app_id AND group_openid = :group "
                            "ORDER BY game_id"
                        ),
                        self._params(),
                    )
                )
                .mappings()
                .all()
            )
        return [dict(row) for row in rows]

    async def result_player_rows(self) -> list[Mapping[str, Any]]:
        from sqlalchemy import text

        async with self.harness.session_factory() as session:
            rows = (
                (
                    await session.execute(
                        text(
                            "SELECT rp.game_id, rp.join_seq, rp.member_openid, "
                            "rp.display_name, rp.alive, rp.eliminated_reason "
                            "FROM komari_roulette_result_players AS rp "
                            "JOIN komari_roulette_results AS r "
                            "ON r.game_id = rp.game_id "
                            "WHERE r.app_id = :app_id AND r.group_openid = :group "
                            "ORDER BY rp.join_seq"
                        ),
                        self._params(),
                    )
                )
                .mappings()
                .all()
            )
        return [dict(row) for row in rows]

    async def leaderboard_rows(self) -> list[Mapping[str, Any]]:
        from sqlalchemy import text

        async with self.harness.session_factory() as session:
            rows = (
                (
                    await session.execute(
                        text(
                            "SELECT member_openid, display_name, wins "
                            "FROM komari_roulette_leaderboard "
                            "WHERE app_id = :app_id AND group_openid = :group "
                            "ORDER BY member_openid"
                        ),
                        self._params(),
                    )
                )
                .mappings()
                .all()
            )
        return [dict(row) for row in rows]

    async def fulfillment_row(
        self,
        inbound_msg_id: str,
    ) -> Mapping[str, Any] | None:
        from sqlalchemy import text

        async with self.harness.session_factory() as session:
            row = (
                (
                    await session.execute(
                        text(
                            "SELECT f.state, f.platform_message_id "
                            "FROM komari_roulette_fulfillments AS f "
                            "JOIN komari_roulette_command_receipts AS r "
                            "ON r.receipt_id = f.receipt_id "
                            "WHERE r.app_id = :app_id AND r.group_openid = :group "
                            "AND r.inbound_msg_id = :msg"
                        ),
                        {**self._params(), "msg": inbound_msg_id},
                    )
                )
                .mappings()
                .one_or_none()
            )
        return dict(row) if row is not None else None

    async def receipt_body(self, inbound_msg_id: str) -> str | None:
        from sqlalchemy import text

        async with self.harness.session_factory() as session:
            value = await session.scalar(
                text(
                    "SELECT reply_projection->>'body' "
                    "FROM komari_roulette_command_receipts "
                    "WHERE app_id = :app_id AND group_openid = :group "
                    "AND inbound_msg_id = :msg"
                ),
                {**self._params(), "msg": inbound_msg_id},
            )
        return str(value) if value is not None else None

    async def current_snapshot(self) -> Any:
        """Read the current game through the real public ``load_current`` seam.

        The real storage adapter loads and validates the aggregate from
        PostgreSQL; the test never forges a snapshot and never writes state
        through SQL to reach a phase or an inventory.
        """

        from komari_bot.plugins.komari_roulette.domain import GroupRef
        from komari_bot.plugins.komari_roulette.storage import (
            PostgresRouletteStorage,
        )

        group = GroupRef(
            app_id=self.current.app_id,
            group_openid=self.current.group_openid,
        )
        async with self.harness.session_factory() as session:
            return await PostgresRouletteStorage(session).load_current(group)

    def qq_runtime(self) -> Any:
        return importlib.import_module(QQ_MODULE).get_roulette_qq_runtime()

    def _params(self) -> dict[str, str]:
        return {
            "app_id": self.current.app_id,
            "group": self.current.group_openid,
        }

    # -- pre-chain strong assertion --------------------------------------
    async def assert_unbound_group_cannot_start(self) -> None:
        """A not-yet-canonical group must be rejected before any effect.

        The real QQ runtime must already be installed, otherwise "no send" would
        be vacuous; the identical ``/轮盘 开局`` command succeeds right after the
        real wizard binds the host, so the rejection is the missing canonical
        group, not a missing handler.
        """

        assert self.qq_runtime() is not None, (
            "真实组合根必须已安装 QQ runtime，否则『未开局』断言失去意义"
        )
        assert await self.receipt_rows() == []
        assert await self.game_rows() == []
        before = len(self.qq.calls)
        await self.send_command(1, CMD_CREATE, tag="prechain-create")
        assert len(self.qq.calls) == before, "未绑定群不得产生任何 QQ 发送"
        assert await self.receipt_rows() == []
        assert await self.game_rows() == []


# ---------------------------------------------------------------------------
# Assembly: real binding window + real roulette composition root
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _RouletteModuleSnapshot:
    """Exact pre-reload roulette module tree plus the package ``__dict__``.

    ``importlib.reload`` re-executes the package ``__init__`` inside the very
    same package object, so the module objects alone are not enough: the
    rewritten re-exports (``lifecycle`` / ``qq`` / ``get_roulette_observation``)
    live in the package ``__dict__``.  Both are captured and restored.
    """

    modules: dict[str, Any]
    package_dict: dict[str, Any]


def _snapshot_roulette_modules() -> _RouletteModuleSnapshot:
    package = sys.modules[PACKAGE]
    return _RouletteModuleSnapshot(
        modules={
            name: module
            for name, module in sys.modules.items()
            if name == PACKAGE or name.startswith(PACKAGE + ".")
        },
        package_dict=dict(package.__dict__),
    )


def _restore_roulette_modules(snapshot: _RouletteModuleSnapshot) -> None:
    """Restore the exact pre-reload module tree and package re-exports.

    The run being torn down owns the *new* QQ module, which the caller clears
    before this is called.  The saved (pre-entry) module keeps whatever runtime
    it already had, so an outer window is never corrupted.
    """

    for name in [
        candidate
        for candidate in sys.modules
        if candidate == PACKAGE or candidate.startswith(PACKAGE + ".")
    ]:
        sys.modules.pop(name, None)
    sys.modules.update(snapshot.modules)
    package = sys.modules.get(PACKAGE)
    if package is None:
        return
    package.__dict__.clear()
    package.__dict__.update(snapshot.package_dict)


#: 修复服务管理装配经真实 ``nonebot.require`` 声明的依赖插件 id → 真实模块。
_REPAIR_DEPENDENCY_MODULES: dict[str, str] = {
    "komari_roulette": PACKAGE,
}


def install_repair_lifecycle_require_seam(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """让修复服务管理装配能解析 ``require("komari_roulette")``。

    ``komari_management`` 的 ``start_binding_repair_service`` 用**真实**
    ``nonebot.require``（测试 conftest 只替换 ``nonebot.plugin.require``）声明
    ``require("komari_roulette")``。本链的 roulette 组合根由
    ``importlib.reload`` 驱动，从未注册进 NoneBot 插件管理器，真实加载器无法
    解析该插件 id。

    本接缝只把已导入的**真实**模块作为依赖答案返回（不伪造模块、不替换业务
    对象）；其余插件名一律委派给进入本接缝前的 ``nonebot.require``。
    ``nonebot.plugin.require``（conftest 注册表桩）保持不变，避免影响包重载
    期各插件自身的 ``require`` 声明。
    """

    import nonebot

    original_require = nonebot.require

    def _seam_require(name: str) -> Any:
        module_path = _REPAIR_DEPENDENCY_MODULES.get(name)
        if module_path is not None:
            return importlib.import_module(module_path)
        return original_require(name)

    monkeypatch.setattr(nonebot, "require", _seam_require)


async def _assemble_roulette(
    monkeypatch: pytest.MonkeyPatch,
    holder: dict[str, Any],
    *,
    random_source: RandomSource | None = None,
) -> None:
    driver = get_driver()
    importlib.import_module(PACKAGE)
    holder["modules"] = _snapshot_roulette_modules()

    apscheduler_mod: Any = sys.modules.get("nonebot_plugin_apscheduler")
    holder["apscheduler_mod"] = apscheduler_mod
    holder["previous_scheduler"] = getattr(apscheduler_mod, "scheduler", None)
    if apscheduler_mod is not None:
        apscheduler_mod.scheduler = FakeScheduler()

    _calls, cache, _state = _install_config_manager_getter_fake(monkeypatch)

    _pop_reloadable_roulette_modules()
    before_startup = set(driver._lifespan._startup_funcs)
    before_shutdown = set(driver._lifespan._shutdown_funcs)
    importlib.reload(sys.modules[PACKAGE])
    holder["reloaded"] = True
    new_startup = [
        hook for hook in driver._lifespan._startup_funcs if hook not in before_startup
    ]
    new_shutdown = [
        hook for hook in driver._lifespan._shutdown_funcs if hook not in before_shutdown
    ]
    assert len(new_startup) == 1, (
        "reloading komari_roulette 必须注册恰一个 driver startup hook，"
        f"实际 {len(new_startup)}"
    )
    assert len(new_shutdown) == 1, (
        "reloading komari_roulette 必须注册恰一个 driver shutdown hook，"
        f"实际 {len(new_shutdown)}"
    )

    # The domain randomness port is the single allowed substitute seam: the real
    # composition root keeps building the real ``RouletteCommandService`` and is
    # merely handed a scripted, contract-conforming ``RandomSource``.  The real
    # domain still validates every chamber order (exact live/blank counts) and
    # the source only returns legally drawable items, so no illegal magazine or
    # draw bypass can be smuggled in.
    if random_source is not None:
        command_service_module = importlib.import_module(COMMAND_SERVICE_MODULE)
        monkeypatch.setattr(
            command_service_module,
            "_DefaultRandomSource",
            lambda source=random_source: source,
        )

    # Real driver startup hook -> real composition root.
    await invoke_hook(new_startup[0])
    app = application_api()["get_roulette_application"]()
    assert app is not None, "真实 startup hook 必须安装 roulette 应用"
    assert importlib.import_module(QQ_MODULE).get_roulette_qq_runtime() is not None, (
        "真实组合根必须安装 QQ runtime（依赖就绪路径）"
    )
    config_manager = cache["komari_roulette"]
    holder["config_manager"] = config_manager
    holder["original_plugin_enable"] = bool(config_manager.get().plugin_enable)
    await config_manager.update_field_async("plugin_enable", value=True)
    await app.runtime.run_recovery_tick()
    assert app.runtime.accepting, (
        "启用 plugin_enable 并跑一次真实 recovery tick 后 runtime 必须接受业务"
    )


async def _teardown_roulette(holder: dict[str, Any]) -> None:
    """Unwind the roulette side, then surface every teardown failure.

    Each recovery step still runs after an earlier one raises, so a broken
    teardown can never be mistaken for a green test.  Only the QQ runtime this
    run installed is cleared; a pre-existing runtime is left untouched.
    """

    errors: list[BaseException] = []

    async def _run_async(awaitable: Awaitable[Any]) -> None:
        try:
            await awaitable
        except BaseException as error:  # collected and re-raised, never swallowed
            errors.append(error)

    def _run_sync(action: Callable[[], Any]) -> None:
        try:
            action()
        except BaseException as error:  # collected and re-raised, never swallowed
            errors.append(error)

    config_manager = holder.get("config_manager")
    original = holder.get("original_plugin_enable")
    if config_manager is not None and original is not None:
        await _run_async(
            config_manager.update_field_async("plugin_enable", value=original)
        )

    if holder.get("reloaded"):
        api: dict[str, Any] | None = None
        try:
            api = application_api()
        except BaseException as error:  # collected and re-raised, never swallowed
            errors.append(error)
        if api is not None:
            await _run_async(api["stop_roulette_application"]())

    snapshot: _RouletteModuleSnapshot | None = holder.get("modules")
    saved_qq = snapshot.modules.get(QQ_MODULE) if snapshot is not None else None
    current_qq = sys.modules.get(QQ_MODULE)
    if current_qq is not None and current_qq is not saved_qq:
        _run_sync(current_qq.clear_roulette_qq_runtime)

    if snapshot is not None:
        _run_sync(lambda: _restore_roulette_modules(snapshot))

    apscheduler_mod = holder.get("apscheduler_mod")
    if apscheduler_mod is not None:
        _run_sync(
            lambda: setattr(
                apscheduler_mod,
                "scheduler",
                holder.get("previous_scheduler"),
            )
        )

    if not errors:
        return
    if len(errors) == 1:
        raise errors[0]
    raise BaseExceptionGroup("roulette teardown failed", errors)  # noqa: TRY003


@dataclass(frozen=True, slots=True)
class _RouletteConfigState:
    existed: bool


async def _snapshot_roulette_config(engine: AsyncEngine) -> _RouletteConfigState:
    from sqlalchemy import text

    async with engine.connect() as connection:
        exists = await connection.scalar(
            text("SELECT 1 FROM komari_roulette_config WHERE id = 1")
        )
    return _RouletteConfigState(existed=exists is not None)


async def _restore_roulette_config(
    engine: AsyncEngine,
    state: _RouletteConfigState,
) -> None:
    """Return the singleton config row to its exact pre-case presence.

    A cleanup failure is surfaced instead of swallowed: the caller must see
    that the DB is not back to its pre-case state.
    """

    if state.existed:
        return
    from sqlalchemy import text

    async with engine.begin() as connection:
        await connection.execute(
            text("DELETE FROM komari_roulette_config WHERE id = 1")
        )


@asynccontextmanager
async def chain_context(
    monkeypatch: pytest.MonkeyPatch,
    *,
    seat_names: Mapping[int, str],
    random_source: RandomSource | None = None,
) -> AsyncIterator[Chain]:
    """Assemble one real binding + roulette chain on a fresh real-PG scope."""

    async for harness in harness_fixture_body():
        async with harness.scope("tsk281-chain") as current:
            scope = _scope_from(current, member_count=len(seat_names))
            config_state = await _snapshot_roulette_config(harness.engine)
            holder: dict[str, Any] = {}
            # 本窗口的随机源 / driver config / 受限准入 registry / 调度器等补丁
            # 一律经**窗口局部** ``monkeypatch.context()`` 安装：窗口退出（正常
            # 或异常）即逐条撤销，同一用例内开第二个窗口不会继承上一窗口写入
            # ``command_service._DefaultRandomSource`` 的脚本替身。调用方在外层
            # ``monkeypatch`` 上设置的故障注入 / 旧 runtime 哨兵在进入本窗口前已
            # 生效，不属于本窗口上下文，也不会被本窗口撤销。
            with monkeypatch.context() as window_patch:
                try:
                    async with binding_chain_window(window_patch, scope=scope):
                        try:
                            await _assemble_roulette(
                                window_patch,
                                holder,
                                random_source=random_source,
                            )
                            chain = Chain(
                                harness=harness,
                                current=current,
                                scope=scope,
                                qq=ChainQQBot(scope.app_id),
                                onebot=OneBotGetMsgTransport(),
                            )
                            yield chain
                        finally:
                            # A partial assembly (or an early failure) still
                            # unwinds the roulette side before the binding
                            # window closes.
                            await _teardown_roulette(holder)
                finally:
                    await _restore_roulette_config(harness.engine, config_state)
