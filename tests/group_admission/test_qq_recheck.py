"""TSK-274 effect-before-use recheck and revocation contracts."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

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


def _business_token(package: Any) -> Any:
    return package.QQAdmissionToken(
        scope="business",
        app_id=APP_ID,
        group_openid=GROUP_OPENID,
        member_openid=MEMBER_OPENID,
        qq_message_id=QQ_MESSAGE_ID,
        group_id=GROUP_ID,
        member_qq=None,
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
