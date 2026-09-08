"""TSK-274 real character_binding coordinator contract tests."""

from __future__ import annotations

import asyncio
import dataclasses
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import pytest

from tests.character_binding.test_reply_evidence import (
    _real_character_binding_package,
)
from tests.group_admission.management_support import prepare_control_plane
from tests.group_admission.qq_admission_support import (
    APP_ID,
    GROUP_ID,
    GROUP_OPENID,
    MEMBER_OPENID,
    MEMBER_QQ,
    QQ_MESSAGE_ID,
    SECOND_APP_ID,
    SECOND_GROUP_OPENID,
    SECOND_MEMBER_OPENID,
    require_qq_contract,
)
from tests.group_admission.registry_isolation_support import (
    registry_isolation_context,
)
from tests.group_admission.runtime_support import AdmissionStorageFake, stored_policy

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Iterator, Mapping


pytestmark = pytest.mark.group_admission_acceptance

_BASE_TIME = datetime(2026, 9, 8, 12, 0, tzinfo=UTC)


class FrozenClock:
    def __init__(self) -> None:
        self.current = _BASE_TIME

    def __call__(self) -> datetime:
        return self.current

    def advance(self, delta: timedelta) -> None:
        self.current += delta


async def _empty_fetcher(_message_id: int) -> dict[str, object]:
    return {}


@contextmanager
def _real_binding_package() -> Iterator[Any]:
    with registry_isolation_context(), _real_character_binding_package() as package:
        yield package


async def _prepare_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    storage = AdmissionStorageFake(
        stored_policy(1, {"mode": "blacklist", "group_ids": []})
    )
    await prepare_control_plane(monkeypatch, storage)


def _request(
    package: Any,
    *,
    app_id: str = APP_ID,
    group_openid: str = GROUP_OPENID,
    member_openid: str = MEMBER_OPENID,
    message_id: str = QQ_MESSAGE_ID,
) -> Any:
    return package.QQInitialBindRequest(
        app_id=app_id,
        group_openid=group_openid,
        member_openid=member_openid,
        qq_message_id=message_id,
        command="/bind",
    )


def _build_coordinator(
    package: Any,
    *,
    clock: FrozenClock,
    resolve_group: Callable[[str, str], Awaitable[int | None]],
    apps: tuple[str, ...] = (APP_ID,),
    official_by_app: Mapping[str, str] | None = None,
) -> Any:
    from komari_bot.plugins.character_binding.reply_evidence import (
        ReplyEvidenceCollector,
    )

    official_by_app = official_by_app or dict.fromkeys(apps, "9274001")
    collectors = tuple(
        ReplyEvidenceCollector(
            app_id=app_id,
            official_bot_qq=official_by_app[app_id],
            message_fetcher=_empty_fetcher,
            clock=clock,
        )
        for app_id in apps
    )
    return package.QQBindingCoordinator(
        collectors=collectors,
        group_resolver=resolve_group,
        clock=clock,
    )


@pytest.mark.asyncio
async def test_real_coordinator_claim_is_one_shot_and_generation_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _prepare_runtime(monkeypatch)

    async def resolve_group(_app_id: str, _group_openid: str) -> int | None:
        return None

    with _real_binding_package() as binding:
        admission = require_qq_contract(
            "QQInitialBindRequest",
            "register_qq_group_resolver",
            "register_qq_initial_bind_claimer",
            "register_qq_binding_session_resolver",
        )
        assert hasattr(binding, "QQBindingCoordinator")
        clock = FrozenClock()
        coordinator = _build_coordinator(
            binding,
            clock=clock,
            resolve_group=resolve_group,
        )
        await coordinator.start()
        try:
            request = _request(admission)
            first = await coordinator.claim_initial_bind(request)
            duplicate = await coordinator.claim_initial_bind(request)

            assert first is not None
            assert first.is_new is True
            assert duplicate is not None
            assert duplicate.session_code == first.session_code
            assert duplicate.is_new is False
            assert duplicate.expires_at == first.expires_at

            active = await coordinator.resolve_verified_binding_session(
                APP_ID,
                GROUP_OPENID,
                MEMBER_OPENID,
            )
            assert active is None, "未收到 evidence 不能冒充已核验 session"

            coordinator.reset_generation()
            assert (
                await coordinator.resolve_verified_binding_session(
                    APP_ID,
                    GROUP_OPENID,
                    MEMBER_OPENID,
                )
                is None
            )
        finally:
            await coordinator.close()

        reply_evidence = __import__(
            "komari_bot.plugins.character_binding.reply_evidence",
            fromlist=["get_runtime_collectors"],
        )
        assert reply_evidence.get_runtime_collectors() == ()


@pytest.mark.asyncio
async def test_concurrent_claims_are_single_new_and_identity_isolated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """同一身份并发只产生一个新 claim，不同身份不能互相去重。"""
    await _prepare_runtime(monkeypatch)

    async def resolve_group(_app_id: str, _group_openid: str) -> int | None:
        return None

    with _real_binding_package() as binding:
        admission = require_qq_contract("QQInitialBindRequest")
        coordinator = _build_coordinator(
            binding,
            clock=FrozenClock(),
            resolve_group=resolve_group,
            apps=(APP_ID, SECOND_APP_ID),
        )
        await coordinator.start()
        try:
            same_request = _request(admission, message_id="same-inbound-message")
            same_results = await asyncio.gather(
                *(coordinator.claim_initial_bind(same_request) for _ in range(8))
            )
            same_claims = [claim for claim in same_results if claim is not None]
            assert sum(claim.is_new for claim in same_claims) == 1
            assert len({claim.session_code for claim in same_claims}) == 1
            assert len({claim.expires_at for claim in same_claims}) == 1

            independent_requests = [
                _request(
                    admission,
                    app_id=SECOND_APP_ID,
                    group_openid=GROUP_OPENID,
                    member_openid=MEMBER_OPENID,
                    message_id="same-inbound-message",
                ),
                _request(
                    admission,
                    app_id=APP_ID,
                    group_openid=SECOND_GROUP_OPENID,
                    member_openid=MEMBER_OPENID,
                    message_id="same-inbound-message",
                ),
                _request(
                    admission,
                    app_id=APP_ID,
                    group_openid=GROUP_OPENID,
                    member_openid=SECOND_MEMBER_OPENID,
                    message_id="same-inbound-message",
                ),
            ]
            independent = await asyncio.gather(
                *(coordinator.claim_initial_bind(request) for request in independent_requests)
            )
            assert all(claim is not None and claim.is_new for claim in independent)
            assert len({claim.session_code for claim in independent if claim is not None}) == 3
        finally:
            await coordinator.close()


@pytest.mark.asyncio
async def test_unconfigured_app_has_no_initial_claim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """缺失该 app 的可信官 Bot QQ 时，首次取证资格保持关闭。"""
    await _prepare_runtime(monkeypatch)

    async def resolve_group(_app_id: str, _group_openid: str) -> int | None:
        return None

    with _real_binding_package() as binding:
        admission = require_qq_contract("QQInitialBindRequest")
        coordinator = _build_coordinator(
            binding,
            clock=FrozenClock(),
            resolve_group=resolve_group,
            apps=(APP_ID,),
        )
        await coordinator.start()
        try:
            claim = await coordinator.claim_initial_bind(
                _request(
                    admission,
                    app_id=SECOND_APP_ID,
                    group_openid=SECOND_GROUP_OPENID,
                    member_openid=SECOND_MEMBER_OPENID,
                )
            )
            assert claim is None
        finally:
            await coordinator.close()


@pytest.mark.asyncio
async def test_invalid_official_qq_cannot_create_a_claiming_collector(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """非数字官 Bot 身份在装配时拒绝，不能降级成可取证会话。"""
    await _prepare_runtime(monkeypatch)

    async def resolve_group(_app_id: str, _group_openid: str) -> int | None:
        return None

    with _real_binding_package() as binding, pytest.raises(
        ValueError,
        match="official_bot_qq",
    ):
        _build_coordinator(
            binding,
            clock=FrozenClock(),
            resolve_group=resolve_group,
            official_by_app={APP_ID: "qq-openid-not-numeric"},
        )


@pytest.mark.asyncio
async def test_cancel_close_and_forged_claims_cannot_reopen_binding_effect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _prepare_runtime(monkeypatch)

    async def resolve_group(_app_id: str, _group_openid: str) -> int | None:
        return None

    with _real_binding_package() as binding:
        admission = require_qq_contract(
            "QQInitialBindRequest",
            "QQAdmissionToken",
            "QQEffectDecision",
        )
        coordinator = _build_coordinator(
            binding,
            clock=FrozenClock(),
            resolve_group=resolve_group,
        )
        await coordinator.start()
        claim = await coordinator.claim_initial_bind(_request(admission))
        assert claim is not None
        token = admission.QQAdmissionToken(
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
        try:
            await coordinator.cancel(claim.session_code)
            cancelled = await coordinator.recheck(
                token,
                effect="binding_challenge",
            )
            assert cancelled.allowed is False

            forged_claim = dataclasses.replace(claim, session_code="forged-session")
            forged_token = dataclasses.replace(token, claim=forged_claim)
            forged = await coordinator.recheck(
                forged_token,
                effect="binding_challenge",
            )
            assert forged.allowed is False
        finally:
            await coordinator.close()

        assert await coordinator.claim_initial_bind(_request(admission)) is None


@pytest.mark.asyncio
async def test_coordinator_rejects_expired_and_stale_binding_effects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _prepare_runtime(monkeypatch)

    async def resolve_group(_app_id: str, _group_openid: str) -> int | None:
        return None

    with _real_binding_package() as binding:
        admission = require_qq_contract(
            "QQInitialBindRequest",
            "QQEffectDecision",
            "recheck_qq_effect",
        )
        assert hasattr(binding, "QQBindingCoordinator")
        clock = FrozenClock()
        coordinator = _build_coordinator(
            binding,
            clock=clock,
            resolve_group=resolve_group,
        )
        await coordinator.start()
        try:
            claim = await coordinator.claim_initial_bind(_request(admission))
            assert claim is not None

            challenge_token = admission.QQAdmissionToken(
                scope="binding_challenge",
                app_id=APP_ID,
                group_openid=GROUP_OPENID,
                member_openid=MEMBER_OPENID,
                qq_message_id=QQ_MESSAGE_ID,
                group_id=None,
                member_qq=None,
                effective_policy_revision=1,
                connection_generation=0,
                claim=claim,
                verified_session=None,
            )
            before = await coordinator.recheck(
                challenge_token,
                effect="binding_challenge",
            )
            assert isinstance(before, admission.QQEffectDecision)
            assert before.allowed is True
            assert before.effect == "binding_challenge"

            clock.advance(timedelta(minutes=10))
            after = await coordinator.recheck(
                challenge_token,
                effect="binding_challenge",
            )
            assert after.allowed is False, "精确十分钟 TTL 到点必须失效"

            coordinator.reset_generation()
            stale = await coordinator.recheck(
                challenge_token,
                effect="binding_challenge",
            )
            assert stale.allowed is False
        finally:
            await coordinator.close()


@pytest.mark.asyncio
async def test_accepted_evidence_promotes_session_to_binding_without_qq_send(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _prepare_runtime(monkeypatch)

    async def resolve_group(_app_id: str, _group_openid: str) -> int | None:
        return None

    with _real_binding_package() as binding:
        admission = require_qq_contract("QQInitialBindRequest")
        assert hasattr(binding, "QQBindingCoordinator")
        from komari_bot.plugins.character_binding.reply_evidence import ReplyEvidence

        clock = FrozenClock()
        coordinator = _build_coordinator(
            binding,
            clock=clock,
            resolve_group=resolve_group,
        )
        await coordinator.start()
        try:
            claim = await coordinator.claim_initial_bind(_request(admission))
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
                onebot_original_message_id=81001,
                challenge_message_id=81002,
                connection_generation=claim.connection_generation,
            )

            token = await coordinator.accept_reply_evidence(evidence)
            assert token is not None
            assert token.scope == "binding"
            assert token.group_id == GROUP_ID
            assert token.member_qq == MEMBER_QQ
            assert token.verified_session is not None
            assert token.verified_session.session_code == claim.session_code

            active = await coordinator.resolve_verified_binding_session(
                APP_ID,
                GROUP_OPENID,
                MEMBER_OPENID,
            )
            assert active is not None
            assert active.group_id == GROUP_ID
            assert active.member_qq == MEMBER_QQ
        finally:
            await coordinator.close()
