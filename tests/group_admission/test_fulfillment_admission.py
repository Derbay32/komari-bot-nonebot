"""TSK-228 ``06 — 接入回复履约与冻结承诺准入`` 红基线验收。

驱动真实生产对象（ReplyFulfillmentWorkflow / ReplyCommitmentWorkflow）注入
内存 Fake 依赖，按 ADR-0012「回复履约按阶段裁决」逐 AC 断言。当前生产履约
工作流尚未接入准入（任一阶段都未调用顶层 adjudicate），受限态与 intent
断言以红态失败终止，证明接入缺失。
"""

from __future__ import annotations

import importlib
from typing import Any

import pytest

from tests.group_admission import fulfillment_admission_support as fas
from tests.group_admission.chat_admission_support import (
    ScriptedAdjudicate,
    install_scripted_adjudicate,
)

pytestmark = pytest.mark.group_admission_acceptance

_WF = "komari_bot.plugins.komari_chat.services.reply_fulfillment_workflow"


def _wf() -> Any:
    return importlib.import_module(_WF)


def _pending(fid: str) -> tuple[Any, fas.ReservationHandoff]:
    mh = importlib.import_module(
        "komari_bot.plugins.komari_chat.handlers.message_handler"
    )
    from komari_bot.plugins.komari_memory.services.redis_manager import MessageSchema

    off = fas.ReservationHandoff()
    p = mh.PendingReply(
        reply="回复正文",
        reply_to_message_id="m1",
        message=MessageSchema(
            user_id="u1", user_nickname="测试", group_id="g1",
            content="你好", timestamp=1.0, message_id="msg1"),
        reply_result=mh.ReplyResult(
            content="回复正文",
            interaction_history={"event": "发言", "result": "回复", "emotion": "平静"},
            favorability_delta=1, favorability_reason="互动"),
        force_reply=False, bot_nickname="小鞠", bot_self_id="bot1",
        adapter_name="OneBot V11", reason="s", reply_score=0.9,
        fulfillment_id=fid, request_trace_id="t", reply_timestamp=2.0,
        proactive_reservation_id="r1", proactive_handoff=off,
    )
    return p, off


def _make_fulfill(
    repo: fas.ParentChildRepository,
    *,
    senders: dict[tuple[str, str], Any] | None = None,
) -> tuple[Any, list[bool], fas.ActiveReservationSpy]:
    called: list[bool] = []
    spy = fas.ActiveReservationSpy()

    class _Commit:
        async def recover_fulfillment(self, _fid: str) -> bool:
            return True

        async def recover_pending(self) -> int:
            return 0

        async def cleanup_terminal_fulfillments(self) -> int:
            return 0

    w = _wf().ReplyFulfillmentWorkflow(
        repository=repo, proactive_reservation=spy, config_getter=fas.config,
        recovery_senders_getter=lambda: senders or {}, commitment_workflow=_Commit(),
        alert_service=fas.AlertSpy(called))
    return w, called, spy


async def test_restricted_prepare_keeps_minimal_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    """AC1: 准备前受限 -> 只保留最小未送达身份并释放预占。"""
    s = ScriptedAdjudicate("restricted")
    install_scripted_adjudicate(monkeypatch, s)
    repo = fas.ParentChildRepository()
    pending, off = _pending("op1")
    w, _called, _spy = _make_fulfill(repo)
    sent: list[bool] = []

    async def _send(_p: object) -> object:
        sent.append(True)
        return _wf().ReplyDeliveryResult.pending_confirmation()

    result = await w.fulfill(pending, send_reply=_send)
    assert s.calls, "准备前未裁决"
    assert sent == [], "受限下仍调用发送能力"
    assert result is False
    rec = repo.records["op1"]
    assert rec["reply_content"] in (None, ""), "仍持久化完整正文"
    assert repo.commitment_payloads.get("op1") in (None, {}), "仍冻结承诺"
    assert off.released > 0, "预占未释放"


async def test_fulfill_unknown_delivery_reconciles_never_resends(monkeypatch: pytest.MonkeyPatch) -> None:
    """AC3: 开始发送可完成；送达未知进入 reconciliation 且永不自动重发。"""
    s = ScriptedAdjudicate("admitted")
    install_scripted_adjudicate(monkeypatch, s)
    repo = fas.ParentChildRepository()
    pending, _off = _pending("acu3")
    w, _called, _spy = _make_fulfill(repo)
    sent: list[bool] = []

    async def _send(_p: object) -> object:
        sent.append(True)
        return _wf().ReplyDeliveryResult.pending_confirmation()

    assert await w.fulfill(pending, send_reply=_send) is False
    assert len(sent) == 1, "开始发送应允许完成"
    assert "fact_finalization" in fas.admitted_intents(s), "对账未按 FACT_FINALIZATION 裁决"


async def test_restricted_not_started_terminates_not_resurrect(monkeypatch: pytest.MonkeyPatch) -> None:
    """AC2: NOT_STARTED 受限后终止而非休眠，恢复准入后不复活。"""
    s = ScriptedAdjudicate("restricted")
    install_scripted_adjudicate(monkeypatch, s)
    repo = fas.ParentChildRepository()
    repo.seed("acn2")
    sent: list[bool] = []

    async def _sender(_p: object) -> object:
        sent.append(True)
        return _wf().ReplyDeliveryResult.delivered("p")

    w, _called, _spy = _make_fulfill(repo, senders={("bot1", "OneBot V11"): _sender})
    await w.recover_pending()
    assert s.calls, "恢复发送前未裁决"
    assert sent == [], "受限 NOT_STARTED 仍被补发"
    assert repo.not_started_ids == set()
    s.set_state("admitted")
    await w.recover_pending()
    assert sent == [], "恢复准入后不应复活补发"


async def test_commitment_reconcile_consults_fact_finalization(monkeypatch: pytest.MonkeyPatch) -> None:
    """AC4: DELIVERED 对账以 FACT_FINALIZATION，每项承诺分别重裁决。"""
    s = ScriptedAdjudicate("admitted")
    install_scripted_adjudicate(monkeypatch, s)
    repo = fas.CommitmentRepo()
    repo.seed("ac4")
    down = fas.CommitmentDownstreams()
    wf, down2 = fas.build_commitment_workflow(repo, down)
    assert await wf.recover_fulfillment("ac4") is True
    assert "fact_finalization" in fas.admitted_intents(s), "承诺对账未按 FACT_FINALIZATION 裁决"
    assert len(down2.favor) == 1


async def test_commitment_parent_attribution_missing_safe_hold(monkeypatch: pytest.MonkeyPatch) -> None:
    """AC6: 父级归属缺失时安全持有，不执行冻结承诺。"""
    s = ScriptedAdjudicate("admitted")
    install_scripted_adjudicate(monkeypatch, s)
    repo = fas.CommitmentRepo()
    repo.seed("ac6", group_id="")
    down = fas.CommitmentDownstreams()
    wf, down2 = fas.build_commitment_workflow(repo, down)
    await wf.recover_fulfillment("ac6")
    assert s.calls, "父归属缺失时未裁决"
    assert down2.favor == [], "父归属缺失仍执行好感度承诺"


async def test_stage_barrier_between_recon_and_commitment(monkeypatch: pytest.MonkeyPatch) -> None:
    """AC7: 对账获准但承诺受限时，承诺不得执行。"""
    s = ScriptedAdjudicate("admitted")
    s.set_sequence("admitted", "restricted")
    install_scripted_adjudicate(monkeypatch, s)
    repo = fas.CommitmentRepo()
    repo.seed("ac7")
    down = fas.CommitmentDownstreams()
    wf, down2 = fas.build_commitment_workflow(repo, down)
    await wf.recover_fulfillment("ac7")
    assert len(s.calls) >= 2, "对账与承诺各需一次裁决"
    assert down2.favor == [], "承诺受限仍执行承诺"


async def test_cleanup_terminal_consults_technical_cleanup(monkeypatch: pytest.MonkeyPatch) -> None:
    """AC5: 终态证据清理只按 TECHNICAL_CLEANUP。"""
    s = ScriptedAdjudicate("admitted")
    install_scripted_adjudicate(monkeypatch, s)
    repo = fas.CommitmentRepo()
    repo.seed("ac5")
    down = fas.CommitmentDownstreams()
    wf, _d2 = fas.build_commitment_workflow(repo, down)
    await wf.cleanup_terminal_fulfillments()
    assert "technical_cleanup" in fas.admitted_intents(s)
    assert down.alerted
