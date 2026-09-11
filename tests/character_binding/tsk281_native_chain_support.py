"""TSK-281 stage-B1 native binding-chain fixture (test-only, no production semantics).

This module extracts the *real* first-binding assembly that the B1a chain proved
green, so a second production assembly (the roulette composition root) can be
registered inside the **same** isolated registry window instead of fabricating a
second window or hand-building a collector / coordinator / wizard.

What stays real:

* ``character_binding`` is imported for real (the ``tests/conftest`` package
  shim is removed): importing the package registers the real evidence matcher
  and the real ``/bind`` matcher, and ``init_plugin()`` installs the real
  ``QQBindingCoordinator`` + ``BindingWizard``;
* the global ``group_admission`` event gate is registered inside the isolated
  window by ``event_gate_context``;
* OneBot evidence travels through a real ``OneBotBot.call_api("get_msg")`` call
  (only the transport payload is local), and the real coordinator verifies it;
* canonical group/member/role writes go through the real manager /
  ``BindingTransaction`` on real PostgreSQL.

Only the platform transports (OneBot ``get_msg`` payload and the QQ recording
bot) and the admission **policy store** are replaced, as the ticket allows.  The
policy is a *restricted whitelist* containing only this case's numeric group, so
a routine recovery scan can never touch a foreign scope instead of relying on a
broad "allow everything" blacklist.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast
from uuid import uuid4

from nonebot.adapters.onebot.v11 import Adapter as OneBotAdapter
from nonebot.adapters.onebot.v11 import Bot as OneBotBot
from sqlalchemy import text

from tests.character_binding.test_reply_evidence import (
    _real_character_binding_package,
)
from tests.group_admission.entry_gate_support import event_gate_context

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable

    import pytest
    from sqlalchemy.ext.asyncio import AsyncEngine

__all__ = [
    "NativeMember",
    "NativeScope",
    "OneBotGetMsgTransport",
    "binding_chain_window",
    "cleanup_scope_rows",
    "committed_rows",
    "native_scope",
    "numeric_id",
    "scope_counts",
]

#: OneBot/QQ numeric identities are drawn from a wide int32 range: the shared
#: gated database can contain foreign canonical rows this case may not own or
#: clean, and a narrow space would make an accidental collision (which changes
#: the wizard's legacy-name path) measurably likely.
_MIN_NUMERIC_ID = 100_000
_MAX_NUMERIC_ID = 2_147_483_646  # +1 stays a positive int32 reference message id


def numeric_id(nonce: str) -> int:
    """Map a hex nonce into the wide positive int32 identity space."""

    return _MIN_NUMERIC_ID + int(nonce, 16) % (_MAX_NUMERIC_ID - _MIN_NUMERIC_ID + 1)


@dataclass(frozen=True, slots=True)
class NativeMember:
    """One QQ member inside the case scope plus its numeric OneBot identity."""

    seat: int
    member_openid: str
    member_qq: int


@dataclass(frozen=True, slots=True)
class NativeScope:
    """One unique app / group scope and its QQ members.

    ``group_id`` and every ``member_qq`` are numeric OneBot identities that are
    deliberately independent of the QQ ``group_openid`` / ``member_openid``
    strings (different protocols never share an id).
    """

    app_id: str
    group_openid: str
    group_id: int
    members: tuple[NativeMember, ...]

    def member(self, seat: int) -> NativeMember:
        for candidate in self.members:
            if candidate.seat == seat:
                return candidate
        message = f"unknown seat {seat}"
        raise KeyError(message)

    @property
    def anchor(self) -> NativeMember:
        return self.members[0]

    # Backwards-compatible flat accessors for the single-member B1a chain.
    @property
    def member_openid(self) -> str:
        return self.anchor.member_openid

    @property
    def member_qq(self) -> int:
        return self.anchor.member_qq


def native_scope(tag: str, *, member_count: int = 1) -> NativeScope:
    """Build a fresh unique scope for one case (seat 1 is the anchor)."""

    if member_count < 1:
        message = "member_count must be >= 1"
        raise ValueError(message)
    suffix = uuid4().hex[:10]
    members = tuple(
        NativeMember(
            seat=seat,
            member_openid=(
                f"tsk281-member-{tag}-{suffix}"
                if seat == 1
                else f"tsk281-member-{tag}-{suffix}-{seat}"
            ),
            member_qq=numeric_id(uuid4().hex[:16]),
        )
        for seat in range(1, member_count + 1)
    )
    return NativeScope(
        app_id=f"tsk281-app-{tag}-{suffix}",
        group_openid=f"tsk281-group-{tag}-{suffix}",
        group_id=numeric_id(uuid4().hex[:16]),
        members=members,
    )


class OneBotGetMsgTransport(OneBotBot):
    """Real OneBot V11 identity whose transport only serves ``get_msg`` reads.

    The whole production evidence path stays intact; only the platform read is a
    local payload map so the case is deterministic and offline.
    """

    def __init__(self, self_id: str = "onebot-tsk281") -> None:
        adapter = cast("OneBotAdapter", OneBotAdapter.__new__(OneBotAdapter))
        super().__init__(adapter, self_id)
        self.payloads: dict[int, dict[str, object]] = {}
        self.calls: list[tuple[str, dict[str, object]]] = []

    def serve(self, message_id: int, payload: dict[str, object]) -> None:
        self.payloads[message_id] = payload

    async def call_api(self, api: str, **data: object) -> object:
        self.calls.append((api, dict(data)))
        assert api == "get_msg", f"OneBot 侧只允许 get_msg，实际 {api}"
        message_id = data.get("message_id")
        if not isinstance(message_id, int) or message_id not in self.payloads:
            message = f"no get_msg payload for {message_id!r}"
            raise RuntimeError(message)
        return self.payloads[message_id]


async def scope_counts(engine: AsyncEngine, scope: NativeScope) -> tuple[int, int]:
    """Count canonical groups/members for this exact scope."""

    params = {"app_id": scope.app_id, "group_openid": scope.group_openid}
    async with engine.connect() as connection:
        groups = int(
            await connection.scalar(
                text(
                    "SELECT count(*) FROM komari_character_binding_groups "
                    "WHERE app_id = :app_id AND group_openid = :group_openid"
                ),
                params,
            )
            or 0
        )
        members = int(
            await connection.scalar(
                text(
                    "SELECT count(*) FROM komari_character_binding_members "
                    "WHERE app_id = :app_id AND group_openid = :group_openid"
                ),
                params,
            )
            or 0
        )
    return groups, members


async def committed_rows(
    engine: AsyncEngine,
    scope: NativeScope,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Read the canonical committed group + member rows for this scope."""

    params = {"app_id": scope.app_id, "group_openid": scope.group_openid}
    async with engine.connect() as connection:
        groups = (
            (
                await connection.execute(
                    text(
                        "SELECT group_id FROM komari_character_binding_groups "
                        "WHERE app_id = :app_id AND group_openid = :group_openid"
                    ),
                    params,
                )
            )
            .mappings()
            .all()
        )
        members = (
            (
                await connection.execute(
                    text(
                        "SELECT member_openid, member_qq, character_name "
                        "FROM komari_character_binding_members "
                        "WHERE app_id = :app_id AND group_openid = :group_openid "
                        "ORDER BY member_openid"
                    ),
                    params,
                )
            )
            .mappings()
            .all()
        )
    return [dict(row) for row in groups], [dict(row) for row in members]


async def cleanup_scope_rows(engine: AsyncEngine, scope: NativeScope) -> None:
    """Delete only this case's canonical group/member rows (own scope only)."""

    params = {"app_id": scope.app_id, "group_openid": scope.group_openid}
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "DELETE FROM komari_character_binding_members "
                "WHERE app_id = :app_id AND group_openid = :group_openid"
            ),
            params,
        )
        await connection.execute(
            text(
                "DELETE FROM komari_character_binding_groups "
                "WHERE app_id = :app_id AND group_openid = :group_openid"
            ),
            params,
        )


def _configure_driver_qq(monkeypatch: pytest.MonkeyPatch, scope: NativeScope) -> None:
    """Point the driver QQ config at this case's single app + trusted QQ id."""

    from nonebot import get_driver
    from nonebot.adapters.qq.config import BotInfo, Intents
    from nonebot.config import Config as NoneBotConfig

    from tests.character_binding.test_reply_evidence import OFFICIAL_BOT_QQ

    driver = get_driver()
    config_values = driver.config.model_dump()
    config_values.update(
        {
            "qq_is_sandbox": True,
            "qq_bots": [
                BotInfo(
                    id=scope.app_id,
                    token="tsk281-native-token",
                    secret="tsk281-native-secret",
                    intent=Intents(c2c_group_at_messages=True),
                    use_websocket=False,
                ).model_dump()
            ],
            "qq_official_bot_qq_by_app": {scope.app_id: OFFICIAL_BOT_QQ},
        }
    )
    monkeypatch.setattr(driver, "config", NoneBotConfig.model_validate(config_values))


def _install_never_ban(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the real binding coordinator's ban seam answer "not banned".

    The real ``komari_user_bans`` table and its revision cache are not exercised
    by this chain; the production coordinator and its callables still run.
    """

    from komari_bot.plugins import user_ban as user_ban_module

    async def _never_banned(_user_id: str, _scope: object) -> bool:
        return False

    monkeypatch.setattr(
        user_ban_module,
        "is_configured_superuser_id",
        lambda _user_id: False,
        raising=False,
    )
    monkeypatch.setattr(
        user_ban_module,
        "is_user_banned",
        _never_banned,
        raising=False,
    )


async def _prepare_restricted_admission(
    monkeypatch: pytest.MonkeyPatch,
    scope: NativeScope,
) -> None:
    """Start the real admission runtime on a restricted whitelist for this group.

    The already-started runtime keeps the fake store's watcher after the real
    config storage factory is restored, so every later real ``ConfigManager``
    (the roulette composition root) reads PostgreSQL.
    """

    from komari_bot.plugins.config_manager import manager as manager_module
    from tests.group_admission.management_support import prepare_control_plane
    from tests.group_admission.runtime_support import (
        AdmissionStorageFake,
        stored_policy,
    )

    storage = AdmissionStorageFake(
        stored_policy(1, {"mode": "whitelist", "group_ids": [scope.group_id]})
    )
    original_get_config_storage = manager_module.get_config_storage
    await prepare_control_plane(monkeypatch, storage)
    manager_module.get_config_storage = original_get_config_storage


@asynccontextmanager
async def binding_chain_window(
    monkeypatch: pytest.MonkeyPatch,
    *,
    scope: NativeScope,
    before_init: Callable[[Any], None] | None = None,
) -> AsyncIterator[Any]:
    """Assemble the real binding chain in one isolated registry window.

    The caller owns the engine and the scope cleanup.  A second real production
    assembly (the roulette composition root) must be driven by the caller from
    *inside* this window (after ``init_plugin`` and before the window closes), so
    that a partial registration unwinds the roulette side before the binding
    plugin is closed.

    ``init_plugin`` itself already publishes a global coordinator *before* its
    own final steps can fail, so it must run inside the guaranteed-close scope:
    a partial initialization still unwinds this plugin's own coordinator and
    manager (cleanup errors are surfaced, never swallowed).  ``before_init``
    runs on the freshly imported real package object just before
    ``init_plugin``; a fault-injection test uses it to fail *after* the real
    coordinator start.
    """

    _configure_driver_qq(monkeypatch, scope)
    _install_never_ban(monkeypatch)
    await _prepare_restricted_admission(monkeypatch, scope)
    async with event_gate_context():
        with _real_character_binding_package() as binding:
            package = cast("Any", binding)
            if before_init is not None:
                before_init(package)
            try:
                await package.init_plugin()
                yield package
            finally:
                await package.close_plugin()
