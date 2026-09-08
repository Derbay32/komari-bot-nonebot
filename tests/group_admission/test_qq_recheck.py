"""TSK-274 effect-before-use recheck and revocation contracts."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import pytest

from tests.character_binding.test_reply_evidence import _real_character_binding_package
from tests.group_admission.management_support import prepare_control_plane
from tests.group_admission.qq_admission_support import (
    APP_ID,
    GROUP_ID,
    GROUP_OPENID,
    MEMBER_OPENID,
    MEMBER_QQ,
    QQ_MESSAGE_ID,
    require_qq_contract,
)
from tests.group_admission.registry_isolation_support import (
    registry_isolation_context,
)
from tests.group_admission.runtime_support import AdmissionStorageFake, stored_policy

pytestmark = pytest.mark.group_admission_acceptance


def _business_token(package: Any, *, member_qq: int | None = None) -> Any:
    return package.QQAdmissionToken(
        scope="business",
        app_id=APP_ID,
        group_openid=GROUP_OPENID,
        member_openid=MEMBER_OPENID,
        qq_message_id=QQ_MESSAGE_ID,
        group_id=GROUP_ID,
        member_qq=member_qq,
        effective_policy_revision=1,
        connection_generation=0,
        claim=None,
        verified_session=None,
    )


def _binding_token(package: Any) -> Any:
    session = package.QQVerifiedBindingSession(
        session_code="verified-recheck-tsk274",
        app_id=APP_ID,
        group_openid=GROUP_OPENID,
        member_openid=MEMBER_OPENID,
        qq_message_id="qq-original-bind-tsk274",
        group_id=GROUP_ID,
        member_qq=MEMBER_QQ,
        connection_generation=0,
        expires_at=datetime.now(UTC) + timedelta(minutes=10),
    )
    return package.QQAdmissionToken(
        scope="binding",
        app_id=APP_ID,
        group_openid=GROUP_OPENID,
        member_openid=MEMBER_OPENID,
        qq_message_id="qq-bind-continue-tsk274",
        group_id=GROUP_ID,
        member_qq=MEMBER_QQ,
        effective_policy_revision=1,
        connection_generation=0,
        claim=None,
        verified_session=session,
    )


@pytest.mark.asyncio
async def test_business_recheck_rejects_policy_revocation_during_await(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Final business effect must use policy after an awaited authority read."""
    storage = AdmissionStorageFake(
        stored_policy(1, {"mode": "blacklist", "group_ids": []})
    )
    await prepare_control_plane(monkeypatch, storage)
    package = require_qq_contract(
        "QQAdmissionToken",
        "QQEffectDecision",
        "register_qq_group_resolver",
        "recheck_qq_effect",
    )
    resolver_started = asyncio.Event()
    release_resolver = asyncio.Event()

    async def resolve_group(_app_id: str, _group_openid: str) -> int:
        resolver_started.set()
        await release_resolver.wait()
        return GROUP_ID

    with registry_isolation_context():
        package.register_qq_group_resolver(resolve_group)
        task: asyncio.Task[Any] | None = None
        try:
            task = asyncio.create_task(
                package.recheck_qq_effect(_business_token(package), effect="business")
            )
            await asyncio.wait_for(resolver_started.wait(), timeout=1)
            storage.deliver(
                stored_policy(2, {"mode": "blacklist", "group_ids": [GROUP_ID]})
            )
            release_resolver.set()
            decision = await asyncio.wait_for(task, timeout=1)
        finally:
            release_resolver.set()
            if task is not None and not task.done():
                task.cancel()
            if task is not None:
                with suppress(asyncio.CancelledError):
                    await asyncio.wait_for(task, timeout=1)
            package.register_qq_group_resolver(None)

    assert isinstance(decision, package.QQEffectDecision)
    assert decision.allowed is False
    assert decision.effect == "business"


@pytest.mark.asyncio
async def test_binding_recheck_rejects_policy_revocation_during_await(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Evidence-backed binding effects are rechecked after session await."""
    storage = AdmissionStorageFake(
        stored_policy(1, {"mode": "blacklist", "group_ids": []})
    )
    await prepare_control_plane(monkeypatch, storage)
    package = require_qq_contract(
        "QQAdmissionToken",
        "QQVerifiedBindingSession",
        "QQEffectDecision",
        "register_qq_binding_session_resolver",
        "recheck_qq_effect",
    )
    resolver_started = asyncio.Event()
    release_resolver = asyncio.Event()
    token = _binding_token(package)

    async def resolve_session(
        _app_id: str,
        _group_openid: str,
        _member_openid: str,
    ) -> Any:
        resolver_started.set()
        await release_resolver.wait()
        return token.verified_session

    with registry_isolation_context():
        package.register_qq_binding_session_resolver(resolve_session)
        task: asyncio.Task[Any] | None = None
        try:
            task = asyncio.create_task(
                package.recheck_qq_effect(token, effect="binding")
            )
            await asyncio.wait_for(resolver_started.wait(), timeout=1)
            storage.deliver(
                stored_policy(2, {"mode": "blacklist", "group_ids": [GROUP_ID]})
            )
            release_resolver.set()
            decision = await asyncio.wait_for(task, timeout=1)
        finally:
            release_resolver.set()
            if task is not None and not task.done():
                task.cancel()
            if task is not None:
                with suppress(asyncio.CancelledError):
                    await asyncio.wait_for(task, timeout=1)
            package.register_qq_binding_session_resolver(None)

    assert isinstance(decision, package.QQEffectDecision)
    assert decision.allowed is False
    assert decision.effect == "binding"


@pytest.mark.asyncio
async def test_business_recheck_rechecks_authoritative_member_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stale token cannot keep using a member QQ after its authority changes."""
    storage = AdmissionStorageFake(
        stored_policy(1, {"mode": "blacklist", "group_ids": []})
    )
    await prepare_control_plane(monkeypatch, storage)
    package = require_qq_contract(
        "QQEffectDecision",
        "register_qq_group_resolver",
        "register_qq_ban_checker",
        "recheck_qq_effect",
    )
    token = _business_token(package, member_qq=MEMBER_QQ)
    observed_members: list[str] = []

    async def resolve_group(_app_id: str, _group_openid: str) -> int:
        return GROUP_ID

    async def resolve_member(
        _app_id: str,
        _group_openid: str,
        _member_openid: str,
    ) -> int:
        replacement = MEMBER_QQ + 1
        observed_members.append(str(replacement))
        return replacement

    async def ban_checker(member_qq: int, _scope: str) -> bool:
        return member_qq == MEMBER_QQ + 1

    with registry_isolation_context():
        package.register_qq_group_resolver(
            resolve_group,
            member_resolver=resolve_member,
        )
        package.register_qq_ban_checker(ban_checker)
        try:
            decision = await package.recheck_qq_effect(token, effect="business")
        finally:
            package.register_qq_group_resolver(None)
            package.register_qq_ban_checker(None)

    assert isinstance(decision, package.QQEffectDecision)
    assert decision.allowed is False
    assert observed_members == [str(MEMBER_QQ + 1)]


@pytest.mark.asyncio
async def test_business_recheck_rejects_policy_revocation_during_ban_await(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The final business policy check runs after the ban authority await."""
    storage = AdmissionStorageFake(
        stored_policy(1, {"mode": "blacklist", "group_ids": []})
    )
    await prepare_control_plane(monkeypatch, storage)
    package = require_qq_contract(
        "QQEffectDecision",
        "register_qq_group_resolver",
        "register_qq_ban_checker",
        "recheck_qq_effect",
    )
    ban_started = asyncio.Event()
    release_ban = asyncio.Event()

    async def resolve_group(_app_id: str, _group_openid: str) -> int:
        return GROUP_ID

    async def ban_checker(_member_qq: int, _scope: str) -> bool:
        ban_started.set()
        await release_ban.wait()
        return False

    task: asyncio.Task[Any] | None = None
    with registry_isolation_context():
        package.register_qq_group_resolver(resolve_group)
        package.register_qq_ban_checker(ban_checker)
        try:
            task = asyncio.create_task(
                package.recheck_qq_effect(
                    _business_token(package, member_qq=MEMBER_QQ),
                    effect="business",
                )
            )
            await asyncio.wait_for(ban_started.wait(), timeout=1)
            storage.deliver(
                stored_policy(2, {"mode": "blacklist", "group_ids": [GROUP_ID]})
            )
            release_ban.set()
            decision = await asyncio.wait_for(task, timeout=1)
        finally:
            release_ban.set()
            if task is not None and not task.done():
                task.cancel()
            if task is not None:
                with suppress(asyncio.CancelledError):
                    await asyncio.wait_for(task, timeout=1)
            package.register_qq_group_resolver(None)
            package.register_qq_ban_checker(None)

    assert isinstance(decision, package.QQEffectDecision)
    assert decision.allowed is False
    assert decision.effect == "business"


@pytest.mark.asyncio
async def test_binding_recheck_rejects_policy_revocation_during_ban_await(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The final binding policy check runs after the ban authority await."""
    storage = AdmissionStorageFake(
        stored_policy(1, {"mode": "blacklist", "group_ids": []})
    )
    await prepare_control_plane(monkeypatch, storage)
    package = require_qq_contract(
        "QQEffectDecision",
        "QQVerifiedBindingSession",
        "register_qq_binding_session_resolver",
        "register_qq_ban_checker",
        "recheck_qq_effect",
    )
    token = _binding_token(package)
    ban_started = asyncio.Event()
    release_ban = asyncio.Event()

    async def resolve_session(
        _app_id: str,
        _group_openid: str,
        _member_openid: str,
    ) -> Any:
        return token.verified_session

    async def ban_checker(_member_qq: int, _scope: str) -> bool:
        ban_started.set()
        await release_ban.wait()
        return False

    task: asyncio.Task[Any] | None = None
    with registry_isolation_context():
        package.register_qq_binding_session_resolver(resolve_session)
        package.register_qq_ban_checker(ban_checker)
        try:
            task = asyncio.create_task(
                package.recheck_qq_effect(token, effect="binding")
            )
            await asyncio.wait_for(ban_started.wait(), timeout=1)
            storage.deliver(
                stored_policy(2, {"mode": "blacklist", "group_ids": [GROUP_ID]})
            )
            release_ban.set()
            decision = await asyncio.wait_for(task, timeout=1)
        finally:
            release_ban.set()
            if task is not None and not task.done():
                task.cancel()
            if task is not None:
                with suppress(asyncio.CancelledError):
                    await asyncio.wait_for(task, timeout=1)
            package.register_qq_binding_session_resolver(None)
            package.register_qq_ban_checker(None)

    assert isinstance(decision, package.QQEffectDecision)
    assert decision.allowed is False
    assert decision.effect == "binding"


@pytest.mark.asyncio
async def test_binding_challenge_recheck_rejects_when_group_becomes_restricted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A real unknown-group claim cannot send after its group becomes restricted."""
    storage = AdmissionStorageFake(
        stored_policy(1, {"mode": "blacklist", "group_ids": []})
    )
    await prepare_control_plane(monkeypatch, storage)
    group_mapped = False

    async def resolve_group(_app_id: str, _group_openid: str) -> int | None:
        return GROUP_ID if group_mapped else None

    async def fetch_message(_message_id: int) -> dict[str, object]:
        return {}

    with registry_isolation_context(), _real_character_binding_package() as binding:
        package = require_qq_contract(
            "QQAdmissionToken",
            "QQEffectDecision",
            "QQInitialBindRequest",
            "recheck_qq_effect",
        )
        from komari_bot.plugins.character_binding.reply_evidence import (
            ReplyEvidenceCollector,
        )

        def clock() -> datetime:
            return datetime.now(UTC)

        binding_public = cast("Any", binding)
        coordinator = binding_public.QQBindingCoordinator(
            collectors=(
                ReplyEvidenceCollector(
                    app_id=APP_ID,
                    official_bot_qq="9274001",
                    message_fetcher=fetch_message,
                    clock=clock,
                ),
            ),
            group_resolver=resolve_group,
            clock=clock,
        )
        await coordinator.start()
        try:
            claim = await coordinator.claim_initial_bind(
                package.QQInitialBindRequest(
                    app_id=APP_ID,
                    group_openid=GROUP_OPENID,
                    member_openid=MEMBER_OPENID,
                    qq_message_id=QQ_MESSAGE_ID,
                    command="/bind",
                )
            )
            assert claim is not None
            token = package.QQAdmissionToken(
                scope="binding_challenge",
                app_id=APP_ID,
                group_openid=GROUP_OPENID,
                member_openid=MEMBER_OPENID,
                qq_message_id=QQ_MESSAGE_ID,
                group_id=None,
                member_qq=None,
                effective_policy_revision=1,
                connection_generation=claim.connection_generation,
                claim=claim,
                verified_session=None,
            )
            allowed = await coordinator.recheck(
                token,
                effect="binding_challenge",
            )
            assert allowed.allowed is True

            group_mapped = True
            storage.deliver(
                stored_policy(2, {"mode": "blacklist", "group_ids": [GROUP_ID]})
            )
            decision = await coordinator.recheck(
                token,
                effect="binding_challenge",
            )
        finally:
            await coordinator.close()

    assert isinstance(decision, package.QQEffectDecision)
    assert decision.allowed is False
    assert decision.effect == "binding_challenge"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("entrypoint", "invalidation"),
    (
        ("coordinator", "cancel"),
        ("coordinator", "reset"),
        ("coordinator", "close"),
        ("public", "cancel"),
        ("public", "reset"),
        ("public", "close"),
    ),
    ids=(
        "coordinator-cancel",
        "coordinator-reset",
        "coordinator-close",
        "public-cancel",
        "public-reset",
        "public-close",
    ),
)
async def test_binding_recheck_rejects_after_lifecycle_invalidation_during_ban_await(
    monkeypatch: pytest.MonkeyPatch,
    entrypoint: str,
    invalidation: str,
) -> None:
    """A token cannot survive coordinator invalidation during ban authority."""
    storage = AdmissionStorageFake(
        stored_policy(1, {"mode": "blacklist", "group_ids": []})
    )
    await prepare_control_plane(monkeypatch, storage)

    async def resolve_group(_app_id: str, _group_openid: str) -> int | None:
        return None

    async def fetch_message(_message_id: int) -> dict[str, object]:
        return {}

    ban_calls = 0
    ban_started = asyncio.Event()
    release_ban = asyncio.Event()

    async def ban_checker(_member_qq: int, _scope: str) -> bool:
        nonlocal ban_calls
        ban_calls += 1
        if ban_calls == 1:
            return False
        ban_started.set()
        await release_ban.wait()
        return False

    with registry_isolation_context(), _real_character_binding_package() as binding:
        package = require_qq_contract(
            "QQAdmissionToken",
            "QQEffectDecision",
            "QQInitialBindRequest",
        )
        from komari_bot.plugins.character_binding.reply_evidence import (
            ReplyEvidence,
            ReplyEvidenceCollector,
        )

        def clock() -> datetime:
            return datetime.now(UTC)

        binding_public = cast("Any", binding)
        coordinator = binding_public.QQBindingCoordinator(
            collectors=(
                ReplyEvidenceCollector(
                    app_id=APP_ID,
                    official_bot_qq="9274001",
                    message_fetcher=fetch_message,
                    clock=clock,
                ),
            ),
            group_resolver=resolve_group,
            clock=clock,
            ban_checker=ban_checker,
        )
        await coordinator.start()
        task: asyncio.Task[Any] | None = None
        decision: Any = None
        try:
            claim = await coordinator.claim_initial_bind(
                package.QQInitialBindRequest(
                    app_id=APP_ID,
                    group_openid=GROUP_OPENID,
                    member_openid=MEMBER_OPENID,
                    qq_message_id=QQ_MESSAGE_ID,
                    command="/bind",
                )
            )
            assert claim is not None
            evidence = ReplyEvidence(
                app_id=APP_ID,
                session_code=claim.session_code,
                group_openid=GROUP_OPENID,
                member_openid=MEMBER_OPENID,
                group_id=str(GROUP_ID),
                member_qq=str(MEMBER_QQ),
                original_command="/bind",
                qq_message_id=QQ_MESSAGE_ID,
                onebot_original_message_id=91001,
                challenge_message_id=91002,
                connection_generation=claim.connection_generation,
            )
            token = await coordinator.accept_reply_evidence(evidence)
            assert token is not None
            assert token.verified_session is not None

            if entrypoint == "coordinator":
                task = asyncio.create_task(
                    coordinator.recheck(token, effect="binding")
                )
            else:
                task = asyncio.create_task(
                    package.recheck_qq_effect(token, effect="binding")
                )
            await asyncio.wait_for(ban_started.wait(), timeout=1)

            if invalidation == "cancel":
                await coordinator.cancel(token.verified_session.session_code)
            elif invalidation == "reset":
                coordinator.reset_generation()
            else:
                await coordinator.close()

            release_ban.set()
            decision = await asyncio.wait_for(task, timeout=1)
        finally:
            release_ban.set()
            if task is not None and not task.done():
                task.cancel()
            if task is not None:
                with suppress(asyncio.CancelledError):
                    await asyncio.wait_for(task, timeout=1)
            await coordinator.close()

    assert isinstance(decision, package.QQEffectDecision)
    assert decision.allowed is False
    assert decision.effect == "binding"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mapping",
    (
        "none",
        "same",
        "different",
        "error",
    ),
    ids=("unmapped", "same-mapping", "different-mapping", "mapping-error"),
)
async def test_binding_recheck_applies_current_formal_group_mapping(
    monkeypatch: pytest.MonkeyPatch,
    mapping: str,
) -> None:
    """Binding continuation accepts unknown/same mapping and rejects conflicts."""
    storage = AdmissionStorageFake(
        stored_policy(1, {"mode": "blacklist", "group_ids": []})
    )
    await prepare_control_plane(monkeypatch, storage)

    package = require_qq_contract(
        "QQEffectDecision",
        "register_qq_ban_checker",
        "register_qq_binding_session_resolver",
        "register_qq_group_resolver",
        "recheck_qq_effect",
    )
    token = _binding_token(package)

    async def resolve_group(_app_id: str, _group_openid: str) -> int | None:
        if mapping == "none":
            return None
        if mapping == "same":
            return GROUP_ID
        if mapping == "different":
            return GROUP_ID + 1
        raise RuntimeError("formal mapping unavailable")  # noqa: TRY003

    async def resolve_session(
        _app_id: str,
        _group_openid: str,
        _member_openid: str,
    ) -> Any:
        return token.verified_session

    async def ban_checker(_member_qq: int, _scope: str) -> bool:
        return False

    with registry_isolation_context():
        package.register_qq_group_resolver(resolve_group)
        package.register_qq_binding_session_resolver(resolve_session)
        package.register_qq_ban_checker(ban_checker)
        try:
            decision = await package.recheck_qq_effect(token, effect="binding")
        finally:
            package.register_qq_group_resolver(None)
            package.register_qq_binding_session_resolver(None)
            package.register_qq_ban_checker(None)

    assert isinstance(decision, package.QQEffectDecision)
    assert decision.allowed is (mapping in {"none", "same"})
    assert decision.effect == "binding"
