"""TSK-230 对话/快照 processing 群工作休眠与归属传播验收测试（红基线）。

被测生产对象：``komari_memory.services.conversation_processing_lifecycle``
的 ``ConversationProcessingLifecycle``。

生产在这条链路上尚未接入群准入（``grep group_admission`` 无引用），因此
以下验收都以红态失败，证明接入缺失。Fake 只做依赖注入与记录，不替代生产对象。

覆盖映射：
- AC1：候选发现只读身份/状态/归属，受限时正文 reader fail-if-called；
- AC2：受限不领业务租约、保存安全进度、不耗 failure/retry、不进 dead-letter；
- AC3：同 revision 不重复领取；新 revision 或 failed→ready 才重裁决；
- AC5：in-flight provider 可完成，写结果前撤销则丢弃/checkpoint 后休眠；
- AC8：缺失/非法/冲突归属进入 ADMISSION_ATTRIBUTION_FAILED 持有态。
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from komari_bot.plugins.komari_memory.services.conversation_processing_lifecycle import (
    ConversationProcessingLifecycle,
)
from tests.group_admission.chat_admission_support import (
    ScriptedAdjudicate,
    install_scripted_adjudicate,
)
from tests.group_admission.memory_admission_support import (
    DormantProcessingStorage,
    RevisionSidecar,
)
from tests.group_admission.persistent_work_scenario import (
    RESTRICTED,
    PersistentWorkScenario,
    run_scenario,
)


class RecordingProcessor:
    """process 调用留痕（成功路径）。"""

    def __init__(self) -> None:
        self.process_calls: list[object] = []

    async def process(self, session: object) -> None:
        self.process_calls.append(session)


class FailIfCalledProcessor:
    """process 必失败哨兵：若受限仍被调用则红。"""

    def __init__(self) -> None:
        self.called = False

    async def process(self, session: object) -> None:
        del session
        self.called = True
        raise AssertionError("fail-if-called: 受限群仍被 processing")  # noqa: TRY003


class NoopCollectorProvider:
    """CollectorProvider 假实现：create/finalize 无副作用。"""

    def create(self, group_id: str, processing_key: str) -> Any:
        del group_id, processing_key
        return None

    async def finalize(
        self,
        collector: Any,
        *,
        status: str,
        error: BaseException | None = None,
    ) -> bool:
        del collector, status, error
        return True


@pytest.fixture(autouse=True)
def _noop_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """消除 retry_async 与心跳超时的真实等待——测试必须零真实时钟。"""

    async def _noop(_delay: float) -> None:
        """不等待。"""

    monkeypatch.setattr(asyncio, "sleep", _noop)


def _make_lifecycle(storage: DormantProcessingStorage) -> ConversationProcessingLifecycle:
    return ConversationProcessingLifecycle(storage, NoopCollectorProvider())


async def test_ac1_restricted_conversation_reader_fail_if_called(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC1：受限群正文 reader fail-if-called；效果前必须同步裁决。"""
    scripted = ScriptedAdjudicate("restricted")
    install_scripted_adjudicate(monkeypatch, scripted)
    storage = DormantProcessingStorage()
    lifecycle = _make_lifecycle(storage)
    fail_if_called = FailIfCalledProcessor()

    await lifecycle.process_conversation_snapshot("g1", fail_if_called)

    assert scripted.calls, "生产未在读取正文前同步调用 adjudicate"
    assert [call[0] for call in scripted.calls] == ["g1"], (
        "效果前裁决必须携带关联群归属"
    )
    assert storage.get_calls == [], "受限群仍读取了对话正文 buffer"
    assert fail_if_called.called is False, "受限群仍执行了正文处理步骤"


async def test_ac1_candidate_discovery_reads_only_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC1：候选发现只读身份/状态/归属；受限不触发正文处理。"""
    scripted = ScriptedAdjudicate("restricted")
    install_scripted_adjudicate(monkeypatch, scripted)
    storage = DormantProcessingStorage()
    storage.active_groups = ["g200"]
    storage.should_trigger = {"g200": True}
    lifecycle = ConversationProcessingLifecycle(storage, NoopCollectorProvider())

    scenario = PersistentWorkScenario(
        scenario_id="ac1-discovery",
        group_id="g200",
        ad_revision=1,
        qualifications=(RESTRICTED,),
    )
    outcomes: dict[str, object] = {}

    async def harness() -> object:
        processor = FailIfCalledProcessor()
        return await lifecycle.run_worker_cycle(lambda: processor)

    await run_scenario(scenario, harness, observations=outcomes)

    assert scripted.calls, "候选发现后必须在触发正文处理前裁定"
    assert storage.claim_calls == [], "受限候选不得领取业务租约"
    assert storage.get_calls == [], "受限候选不得读取正文"


async def test_ac2_restricted_no_business_lease_no_failure_budget_no_dead_letter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC2：受限不领业务租约、不吃 failure/retry、不进 dead-letter。"""
    scripted = ScriptedAdjudicate("restricted")
    install_scripted_adjudicate(monkeypatch, scripted)
    storage = DormantProcessingStorage()
    storage.active_groups = ["g1"]
    storage.should_trigger = {"g1": True}
    lifecycle = _make_lifecycle(storage)

    await lifecycle.run_worker_cycle(lambda: FailIfCalledProcessor())

    assert scripted.calls, "受限处理前必须裁决"
    assert storage.claim_calls == [], "受限群仍领取了业务租约"
    assert storage.dead_letter_calls == [], "受限群被移入 dead-letter"
    assert storage.ack_calls == [], "受限群不应执行业务 ack"
    assert storage.restore_calls == [], "受限群不应走恢复路径"


def test_ac3_sidecar_revision_latch_avoids_reclaim_until_change() -> None:
    """AC3：同 revision 不重裁决重领取；新 revision 或 failed→ready 才重裁决。"""
    sidecar = RevisionSidecar()
    sidecar.save("g1", 7)
    # 同 revision：不变 → 不重领取（保持休眠）。
    assert sidecar.revision_changed("g1", 7) is False
    # 新 revision：变化 → 重裁决。
    assert sidecar.revision_changed("g1", 8) is True
    # failed 冷启动（无 deferred）→ 视为变化 → 重裁决。
    fresh = RevisionSidecar()
    assert fresh.revision_changed("g9", 1) is True


async def test_ac3_restricted_no_business_reclaim_across_same_revision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC3：受限群在未变化的 revision 上不重复领取业务租约。"""
    scripted = ScriptedAdjudicate("restricted")
    install_scripted_adjudicate(monkeypatch, scripted)
    storage = DormantProcessingStorage()
    lifecycle = _make_lifecycle(storage)

    await lifecycle.process_conversation_snapshot("g1", RecordingProcessor())
    await lifecycle.process_conversation_snapshot("g1", RecordingProcessor())

    assert scripted.calls, "生产未在每次处理前裁决"
    assert storage.claim_calls == [], "同 revision 受限处理仍重复领取业务租约"


class _AttributionFailedAdjudicate:
    """裁决替身：归属缺失/非法 → REJECTED + group_attribution_unavailable。"""

    def __init__(self) -> None:
        self.calls: list[object] = []

    def __call__(self, associated_group_ids: object, **kwargs: object) -> Any:
        self.calls.append(associated_group_ids)
        del kwargs
        from komari_bot.plugins.group_admission import (
            AdmissionQualification,
            AdmissionResult,
        )

        return AdmissionResult(
            qualification=AdmissionQualification.REJECTED,
            effective_revision=1,
            reason_code="group_attribution_unavailable",
        )


async def test_ac8_missing_attribution_is_holding_state_not_retried_or_deleted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC8：缺失/非法/冲突归属进入 ADMISSION_ATTRIBUTION_FAILED 持有态。

    不为重试预算消耗、不被删除；只保存安全失败状态（持有）。当前生产把快照
    当普通群处理 → 红（adjudicate 未 consult、且 claim 被污染）。
    """
    adjudicate = _AttributionFailedAdjudicate()
    import komari_bot.plugins.group_admission as admission_package

    monkeypatch.setattr(admission_package, "adjudicate", adjudicate)
    storage = DormantProcessingStorage()
    lifecycle = _make_lifecycle(storage)

    await lifecycle.process_conversation_snapshot("g-unknown", RecordingProcessor())

    assert adjudicate.calls, "归属缺失的工作也必须先走裁决，进入持有态"
    assert storage.dead_letter_calls == [], "ADMISSION_ATTRIBUTION_FAILED 不得进 dead-letter"
    assert storage.claim_calls == [], "不可归因工作不得领取业务租约"


class _CommitAwareProcessor:
    """AC5/AC8：先做生产性工作（SAVE 到 pending），落库写经独立 commit。"""

    def __init__(self) -> None:
        self.pending: list[object] = []
        self.committed: list[object] = []

    async def process(self, session: object) -> None:
        del session
        self.pending.append(object())
        # 真实语义：写结果前若该群被撤销准入，则丢弃并休眠、不持久化。
        self.committed.extend(self.pending)


async def test_ac5_in_flight_work_may_finish_but_write_before_is_discarded_on_revoke(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC5：in-flight provider 可完成，但撤销发生在写结果前则丢弃。

    生产未实现该时序（无准入 / 无丢弃），故受限状态下 commit 仍被执行 → 红。
    """
    scripted = ScriptedAdjudicate("admitted")
    install_scripted_adjudicate(monkeypatch, scripted)
    storage = DormantProcessingStorage()
    lifecycle = _make_lifecycle(storage)
    provider = _CommitAwareProcessor()
    scripted.set_sequence("admitted", "restricted")

    await lifecycle.process_conversation_snapshot("g1", provider)

    assert scripted.calls, "写结果前必须重新裁决（撤销点）"
    assert provider.committed == [], "撤销后写结果必须丢弃，不得持久化"
