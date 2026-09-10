"""TSK-278: roulette needs the live QQ business recheck, not token presence.

The only authorized business entry is the real ``group_admission`` seam.  This
test wires the handler's mandatory ``business_gate`` to the real
``recheck_qq_effect(..., effect="business")`` so that a group whose admission
policy is revoked *after* ingress but *before* the domain write yields zero
domain writes and zero network sends.

It deliberately lives next to the TSK-274 recheck suite: the roulette handler
must not re-implement admission, and the shared seam must stay the single
source of truth.
"""

from __future__ import annotations

from typing import Any

import pytest

from komari_bot.plugins.komari_roulette.qq.handler import RouletteQQHandler
from tests.group_admission.management_support import prepare_control_plane
from tests.group_admission.qq_admission_support import (
    APP_ID,
    GROUP_ID,
    GROUP_OPENID,
    MEMBER_OPENID,
    QQ_MESSAGE_ID,
    QQProbeBot,
    make_group_at,
    require_qq_contract,
)
from tests.group_admission.registry_isolation_support import (
    registry_isolation_context,
)
from tests.group_admission.runtime_support import AdmissionStorageFake, stored_policy

pytestmark = pytest.mark.group_admission_acceptance


class _RecordingService:
    """Minimal TSK-276 command surface; records calls, never writes."""

    def __init__(self) -> None:
        self.observe_calls: list[Any] = []
        self.execute_calls: list[Any] = []

    async def observe_current(self, group: Any) -> None:
        self.observe_calls.append(group)

    async def execute_group_command(
        self,
        request: Any,
        *,
        observation: Any = None,
    ) -> Any:
        self.execute_calls.append((request, observation))
        return object()


class _RecordingDelivery:
    def __init__(self) -> None:
        self.calls: list[Any] = []

    async def deliver(self, receipt: Any, sender: Any) -> None:
        self.calls.append((receipt, sender))


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


@pytest.mark.asyncio
async def test_roulette_business_gate_revocation_blocks_write_with_real_seam(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = AdmissionStorageFake(
        stored_policy(1, {"mode": "blacklist", "group_ids": []})
    )
    await prepare_control_plane(monkeypatch, storage)
    package = require_qq_contract(
        "QQAdmissionToken",
        "QQ_ADMISSION_STATE_KEY",
        "register_qq_group_resolver",
        "recheck_qq_effect",
    )
    token = _business_token(package)
    state = {package.QQ_ADMISSION_STATE_KEY: token}

    async def resolve_group(_app_id: str, _group_openid: str) -> int:
        return GROUP_ID

    decisions: list[bool] = []

    async def business_gate(_bot: Any, _event: Any, gate_token: Any) -> bool:
        decision = await package.recheck_qq_effect(gate_token, effect="business")
        decisions.append(bool(decision.allowed))
        return bool(decision.allowed)

    service = _RecordingService()
    delivery = _RecordingDelivery()
    with registry_isolation_context():
        package.register_qq_group_resolver(resolve_group)
        try:
            handler = RouletteQQHandler(
                service=service,
                delivery=delivery,
                business_gate=business_gate,  # type: ignore[call-arg]  # RED: 生产尚未接受该必填参数
            )
            bot = QQProbeBot(APP_ID)
            event = make_group_at(content="/轮盘 开枪")

            # 准入仍然允许：领域写入一次、发送一次。
            await handler.handle(bot, event, state=state)
            assert decisions == [True]
            assert len(service.execute_calls) == 1
            assert len(delivery.calls) == 1

            # 群准入在 ingress 之后、执行前被撤回：gate 重新裁决必须拦住写入。
            storage.deliver(
                stored_policy(2, {"mode": "blacklist", "group_ids": [GROUP_ID]})
            )
            await handler.handle(bot, event, state=state)
            assert decisions == [True, False]
            assert len(service.execute_calls) == 1
            assert len(delivery.calls) == 1
        finally:
            package.register_qq_group_resolver(None)
