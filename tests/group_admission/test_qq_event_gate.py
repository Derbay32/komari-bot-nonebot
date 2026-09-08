"""TSK-274 QQ event qualification and NoneBot state handoff tests."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import pytest

from tests.group_admission.management_support import prepare_control_plane
from tests.group_admission.qq_admission_support import (
    APP_ID,
    GROUP_ID,
    GROUP_OPENID,
    MEMBER_OPENID,
    MEMBER_QQ,
    OFFICIAL_BOT_QQ,
    ForgedGroupAtMessageCreateEvent,
    QQProbeBot,
    dispatch_qq,
    event_gate_context,
    make_c2c,
    make_direct_message,
    make_group_at,
    make_guild_message,
    make_interaction,
    make_plain_group,
    public_state_token,
    register_state_probe,
    require_qq_contract,
)
from tests.group_admission.runtime_support import (
    AdmissionStorageFake,
    stored_policy,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Iterator

    from nonebot.adapters.qq.event import Event

pytestmark = pytest.mark.group_admission_acceptance

@contextmanager
def _registered_qq_contract(
    package: Any,
    *,
    group_resolver: Callable[[str, str], Awaitable[int | None]],
    claimer: Callable[[Any], Awaitable[Any]] | None = None,
    session_resolver: Callable[[str, str, str], Awaitable[Any]] | None = None,
    ban_checker: Callable[[int, str], Awaitable[bool]] | None = None,
) -> Iterator[None]:
    package.register_qq_group_resolver(group_resolver)
    package.register_qq_initial_bind_claimer(claimer)
    package.register_qq_binding_session_resolver(session_resolver)
    package.register_qq_ban_checker(ban_checker)
    try:
        yield
    finally:
        package.register_qq_group_resolver(None)
        package.register_qq_initial_bind_claimer(None)
        package.register_qq_binding_session_resolver(None)
        package.register_qq_ban_checker(None)


async def _prepare_runtime(
    monkeypatch: pytest.MonkeyPatch,
    *,
    policy: dict[str, object],
    fetch_error: Exception | None = None,
) -> AdmissionStorageFake:
    storage = AdmissionStorageFake(
        stored_policy(1, policy),
        fetch_error=fetch_error,
    )
    await prepare_control_plane(monkeypatch, storage)
    return storage


def _claim(package: Any, *, is_new: bool, session_code: str = "claim-tsk274") -> Any:
    return package.QQBindClaim(
        session_code=session_code,
        app_id=APP_ID,
        group_openid=GROUP_OPENID,
        member_openid=MEMBER_OPENID,
        qq_message_id="qq-bind-tsk274",
        connection_generation=0,
        # Gate smoke cases must not depend on the wall clock matching the
        # frozen timestamp used by the event model.  Exact expiry is covered
        # by the coordinator's injected FrozenClock tests.
        expires_at=datetime.now(UTC) + timedelta(minutes=10),
        is_new=is_new,
    )


@pytest.mark.asyncio
async def test_mapped_admitted_group_is_business_without_member_binding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Group admission must not add a global member-binding gate."""
    await _prepare_runtime(
        monkeypatch,
        policy={"mode": "blacklist", "group_ids": []},
    )
    package = require_qq_contract(
        "qualify_qq_event",
        "register_qq_group_resolver",
        "register_qq_initial_bind_claimer",
        "register_qq_binding_session_resolver",
    )
    claimer_calls: list[Any] = []

    async def resolve_group(app_id: str, group_openid: str) -> int:
        assert (app_id, group_openid) == (APP_ID, GROUP_OPENID)
        return GROUP_ID

    async def claim(request: Any) -> Any:
        claimer_calls.append(request)
        return _claim(package, is_new=True)

    async with event_gate_context():
        with _registered_qq_contract(
            package,
            group_resolver=resolve_group,
            claimer=claim,
        ):
            token = await package.qualify_qq_event(
                QQProbeBot(APP_ID),
                make_group_at(content="/轮盘 开枪"),
            )

    assert token is not None
    assert token.scope == "business"
    assert token.group_id == GROUP_ID
    assert token.member_qq is None
    assert token.claim is None
    assert claimer_calls == []


@pytest.mark.asyncio
async def test_mapped_restricted_group_is_silent_and_does_not_claim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _prepare_runtime(
        monkeypatch,
        policy={"mode": "blacklist", "group_ids": [GROUP_ID]},
    )
    package = require_qq_contract("qualify_qq_event")

    async def resolve_group(_app_id: str, _group_openid: str) -> int:
        return GROUP_ID

    claim_calls: list[Any] = []

    async def claim(request: Any) -> Any:
        claim_calls.append(request)
        return _claim(package, is_new=True)

    async with event_gate_context():
        with _registered_qq_contract(
            package,
            group_resolver=resolve_group,
            claimer=claim,
        ):
            token = await package.qualify_qq_event(
                QQProbeBot(),
                make_group_at(),
            )

    assert token is None
    assert claim_calls == []


@pytest.mark.asyncio
async def test_superuser_ban_bypass_does_not_bypass_restricted_group_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """封禁绕过只影响 user-ban，不能把受限群变成 BUSINESS。"""
    await _prepare_runtime(
        monkeypatch,
        policy={"mode": "blacklist", "group_ids": [GROUP_ID]},
    )
    package = require_qq_contract("qualify_qq_event")
    ban_calls: list[tuple[int, str]] = []

    async def resolve_group(_app_id: str, _group_openid: str) -> int:
        return GROUP_ID

    async def ban_checker(member_qq: int, scope: str) -> bool:
        # The injected facade already applied the SUPERUSER ban bypass.
        ban_calls.append((member_qq, scope))
        return False

    async with event_gate_context():
        with _registered_qq_contract(
            package,
            group_resolver=resolve_group,
            ban_checker=ban_checker,
        ):
            token = await package.qualify_qq_event(
                QQProbeBot(),
                make_group_at(content="/轮盘 开枪"),
            )

    assert token is None
    assert ban_calls == []


@pytest.mark.asyncio
async def test_group_resolver_failure_cannot_fall_into_unknown_challenge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _prepare_runtime(
        monkeypatch,
        policy={"mode": "blacklist", "group_ids": []},
    )
    package = require_qq_contract("qualify_qq_event")

    async def resolve_group(_app_id: str, _group_openid: str) -> int:
        raise RuntimeError("binding storage unavailable")  # noqa: TRY003

    claim_calls: list[Any] = []

    async def claim(request: Any) -> Any:
        claim_calls.append(request)
        return _claim(package, is_new=True)

    async with event_gate_context():
        with _registered_qq_contract(
            package,
            group_resolver=resolve_group,
            claimer=claim,
        ):
            token = await package.qualify_qq_event(
                QQProbeBot(),
                make_group_at(),
            )

    assert token is None
    assert claim_calls == []


@pytest.mark.asyncio
async def test_unmapped_group_accepts_only_trimmed_initial_bind(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _prepare_runtime(
        monkeypatch,
        policy={"mode": "blacklist", "group_ids": []},
    )
    package = require_qq_contract("qualify_qq_event")
    calls: list[Any] = []

    async def resolve_group(_app_id: str, _group_openid: str) -> int | None:
        return None

    async def claim(request: Any) -> Any:
        calls.append(request)
        return _claim(package, is_new=len(calls) == 1)

    async with event_gate_context():
        with _registered_qq_contract(
            package,
            group_resolver=resolve_group,
            claimer=claim,
        ):
            allowed = await package.qualify_qq_event(
                QQProbeBot(),
                make_group_at(content="  /bind  "),
            )
            rejected = await package.qualify_qq_event(
                QQProbeBot(),
                make_group_at(content="/bind continue"),
            )
            game = await package.qualify_qq_event(
                QQProbeBot(),
                make_group_at(content="/轮盘 开枪"),
            )

    assert allowed is not None
    assert allowed.scope == "binding_challenge"
    assert allowed.group_id is None
    assert allowed.member_qq is None
    assert allowed.claim is not None
    assert allowed.claim.is_new is True
    assert rejected is None
    assert game is None
    assert [request.command for request in calls] == ["/bind"]


@pytest.mark.asyncio
async def test_verified_unmapped_session_allows_binding_continuation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Evidence-backed binding can continue before formal group mapping exists."""
    await _prepare_runtime(
        monkeypatch,
        policy={"mode": "blacklist", "group_ids": []},
    )
    package = require_qq_contract("qualify_qq_event")
    verified = package.QQVerifiedBindingSession(
        session_code="verified-tsk274",
        app_id=APP_ID,
        group_openid=GROUP_OPENID,
        member_openid=MEMBER_OPENID,
        qq_message_id="qq-bind-tsk274",
        group_id=GROUP_ID,
        member_qq=OFFICIAL_BOT_QQ + 100,
        connection_generation=0,
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
    )

    async def resolve_group(_app_id: str, _group_openid: str) -> int | None:
        return None

    async def resolve_session(
        app_id: str,
        group_openid: str,
        member_openid: str,
    ) -> Any:
        assert (app_id, group_openid, member_openid) == (
            APP_ID,
            GROUP_OPENID,
            MEMBER_OPENID,
        )
        return verified

    async with event_gate_context():
        with _registered_qq_contract(
            package,
            group_resolver=resolve_group,
            session_resolver=resolve_session,
        ):
            token = await package.qualify_qq_event(
                QQProbeBot(),
                make_group_at(
                    content="/bind continue",
                    message_id="qq-bind-continue-tsk274",
                ),
            )

    assert token is not None
    assert token.scope == "binding"
    assert token.group_id == GROUP_ID
    assert token.member_qq == verified.member_qq
    assert token.claim is None
    assert token.verified_session == verified
    assert token.qq_message_id == "qq-bind-continue-tsk274"


@pytest.mark.asyncio
async def test_verified_numeric_member_qq_is_checked_by_ban_facade(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only evidence-proven QQ reaches the injected user-ban facade."""
    await _prepare_runtime(
        monkeypatch,
        policy={"mode": "blacklist", "group_ids": []},
    )
    package = require_qq_contract("qualify_qq_event")
    verified = package.QQVerifiedBindingSession(
        session_code="verified-banned-tsk274",
        app_id=APP_ID,
        group_openid=GROUP_OPENID,
        member_openid=MEMBER_OPENID,
        qq_message_id="qq-bind-tsk274",
        group_id=GROUP_ID,
        member_qq=MEMBER_QQ,
        connection_generation=0,
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
    )
    ban_calls: list[tuple[int, str]] = []

    async def resolve_group(_app_id: str, _group_openid: str) -> int | None:
        return None

    async def resolve_session(
        _app_id: str,
        _group_openid: str,
        _member_openid: str,
    ) -> Any:
        return verified

    async def ban_checker(member_qq: int, scope: str) -> bool:
        ban_calls.append((member_qq, scope))
        return True

    async with event_gate_context():
        with _registered_qq_contract(
            package,
            group_resolver=resolve_group,
            session_resolver=resolve_session,
            ban_checker=ban_checker,
        ):
            token = await package.qualify_qq_event(
                QQProbeBot(),
                make_group_at(content="/bind continue"),
            )

    assert token is None
    assert ban_calls == [(MEMBER_QQ, "command")]


@pytest.mark.asyncio
async def test_ban_facade_failure_fails_closed_for_verified_binding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _prepare_runtime(
        monkeypatch,
        policy={"mode": "blacklist", "group_ids": []},
    )
    package = require_qq_contract("qualify_qq_event")
    verified = package.QQVerifiedBindingSession(
        session_code="verified-ban-error-tsk274",
        app_id=APP_ID,
        group_openid=GROUP_OPENID,
        member_openid=MEMBER_OPENID,
        qq_message_id="qq-bind-tsk274",
        group_id=GROUP_ID,
        member_qq=MEMBER_QQ,
        connection_generation=0,
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
    )

    async def resolve_group(_app_id: str, _group_openid: str) -> int | None:
        return None

    async def resolve_session(
        _app_id: str,
        _group_openid: str,
        _member_openid: str,
    ) -> Any:
        return verified

    async def ban_checker(_member_qq: int, _scope: str) -> bool:
        raise RuntimeError("ban service unavailable")  # noqa: TRY003

    async with event_gate_context():
        with _registered_qq_contract(
            package,
            group_resolver=resolve_group,
            session_resolver=resolve_session,
            ban_checker=ban_checker,
        ):
            token = await package.qualify_qq_event(
                QQProbeBot(),
                make_group_at(content="/bind continue"),
            )

    assert token is None


@pytest.mark.asyncio
async def test_failed_policy_does_not_open_unknown_challenge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _prepare_runtime(
        monkeypatch,
        policy={"mode": "blacklist", "group_ids": []},
        fetch_error=RuntimeError("policy unavailable"),
    )
    package = require_qq_contract("qualify_qq_event")
    claim_calls: list[Any] = []

    async def resolve_group(_app_id: str, _group_openid: str) -> int | None:
        return None

    async def claim(request: Any) -> Any:
        claim_calls.append(request)
        return _claim(package, is_new=True)

    async with event_gate_context():
        with _registered_qq_contract(
            package,
            group_resolver=resolve_group,
            claimer=claim,
        ):
            token = await package.qualify_qq_event(
                QQProbeBot(),
                make_group_at(),
            )

    assert token is None
    assert claim_calls == []


@pytest.mark.asyncio
async def test_degraded_lkg_policy_still_allows_one_unknown_bind_challenge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DEGRADED + last-known-good remains an effective policy for the gate."""
    storage = await _prepare_runtime(
        monkeypatch,
        policy={"mode": "blacklist", "group_ids": []},
    )
    storage.deliver(
        stored_policy(2, {"mode": "invalid", "group_ids": []})
    )
    package = require_qq_contract("qualify_qq_event")

    async def resolve_group(_app_id: str, _group_openid: str) -> int | None:
        return None

    claim_calls: list[Any] = []

    async def claim(request: Any) -> Any:
        claim_calls.append(request)
        return _claim(package, is_new=True)

    async with event_gate_context():
        with _registered_qq_contract(
            package,
            group_resolver=resolve_group,
            claimer=claim,
        ):
            token = await package.qualify_qq_event(
                QQProbeBot(),
                make_group_at(content="/bind"),
            )

    assert token is not None
    assert token.scope == "binding_challenge"
    assert token.effective_policy_revision == 1
    assert len(claim_calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("event_factory", "allowed"),
    [
        (make_group_at, True),
        (make_plain_group, False),
        (make_c2c, False),
        (make_guild_message, False),
        (make_direct_message, False),
        (make_interaction, False),
        (
            lambda: make_group_at(event_cls=ForgedGroupAtMessageCreateEvent),
            False,
        ),
    ],
    ids=[
        "native-group-at",
        "plain-group",
        "c2c",
        "guild",
        "direct",
        "interaction",
        "forged-subclass",
    ],
)
async def test_qq_event_closed_set(
    monkeypatch: pytest.MonkeyPatch,
    event_factory: Callable[[], Event],
    allowed: bool,  # noqa: FBT001
) -> None:
    await _prepare_runtime(
        monkeypatch,
        policy={"mode": "blacklist", "group_ids": []},
    )
    package = require_qq_contract("qualify_qq_event")

    async def resolve_group(_app_id: str, _group_openid: str) -> int:
        return GROUP_ID

    async with event_gate_context():
        with _registered_qq_contract(package, group_resolver=resolve_group):
            token = await package.qualify_qq_event(QQProbeBot(), event_factory())

    assert (token is not None) is allowed


@pytest.mark.asyncio
async def test_event_gate_places_one_claim_in_shared_state_for_handler(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The handler reads the preprocessor token instead of qualifying again."""
    await _prepare_runtime(
        monkeypatch,
        policy={"mode": "blacklist", "group_ids": []},
    )
    package = require_qq_contract("QQ_ADMISSION_STATE_KEY")
    claim_calls: list[Any] = []

    async def resolve_group(_app_id: str, _group_openid: str) -> None:
        return None

    async def claim(request: Any) -> Any:
        claim_calls.append(request)
        return _claim(package, is_new=len(claim_calls) == 1)

    captured: list[dict[object, object]] = []
    async with event_gate_context():
        register_state_probe(captured)
        with _registered_qq_contract(
            package,
            group_resolver=resolve_group,
            claimer=claim,
        ):
            bot = QQProbeBot()
            await dispatch_qq(bot, make_group_at())
            await dispatch_qq(
                bot,
                make_group_at(message_id="qq-message-tsk274-duplicate"),
            )

    assert len(captured) == 2
    tokens = [public_state_token(state) for state in captured]
    assert all(token is not None for token in tokens)
    assert all(token.scope == "binding_challenge" for token in tokens)
    assert [token.claim.is_new for token in tokens] == [True, False]
    assert len(claim_calls) == 2
    assert bot.calls == []
