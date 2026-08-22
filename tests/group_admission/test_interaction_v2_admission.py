"""TSK-230 interaction v2 按 (group_id, user_id) 分区群准入验收测试（红基线）。

被测生产对象：``komari_memory.handlers.interaction_event_worker`` 的
``_process_claimed_user``（跨群互动后台总结入口）与
``repositories.interaction_event_repository``。

AC6：interaction v2 按 (group_id,user_id) 分区；混合 admitted/restricted 群
互不阻塞；全局 commit（insert_interaction_event）前携带完整关联群裁决，成功
后才清理贡献。当前生产仍是跨群单用户语义，未按群分区、未接入准入，红态失败。
Fake 只做依赖注入与记录，不替代生产对象。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import pytest

from komari_bot.plugins.komari_memory.handlers import (
    interaction_event_worker as worker_module,
)
from tests.group_admission.chat_admission_support import (
    ScriptedAdjudicate,
    install_scripted_adjudicate,
)


def _record(group_id: str) -> dict[str, object]:
    return {
        "event": "用户分享了轻小说",
        "result": "小鞠认真回应",
        "emotion": "开心",
        "display_name": "阿明",
        "timestamp": 1.0,
        "group_id": group_id,
    }


class _FakeRedis:
    """interaction worker 的 Redis 依赖注入替身。"""

    def __init__(self, records: list[dict[str, object]]) -> None:
        self.records = records
        self.renew_calls = 0
        self.ack_calls: list[object] = []
        self.requeue_calls: list[object] = []

    async def renew_interaction_summary_lease(self, **_kwargs: object) -> bool:
        self.renew_calls += 1
        return True

    async def snapshot_global_interactions(self, user_id: str, token: str) -> str:
        return f"processing:{user_id}:{token}"

    async def get_processing_global_interactions(self, processing_key: str) -> list[dict[str, object]]:
        del processing_key
        return list(self.records)

    async def ack_processing_global_interactions(self, **kwargs: str) -> bool:
        self.ack_calls.append(kwargs)
        return True

    async def requeue_processing_global_interactions(self, **kwargs: str) -> bool:
        self.requeue_calls.append(kwargs)
        return True


class _FakeMemory:
    """MemoryService 依赖注入替身：dedup 查询 + 全局 commit 记录。"""

    def __init__(self) -> None:
        self.insert_calls: list[object] = []

    async def get_interaction_event_id_by_dedup_key(self, dedup_key: str) -> int | None:
        del dedup_key
        return None

    async def insert_interaction_event(self, **kwargs: object) -> int:
        self.insert_calls.append(kwargs)
        return 1


def _make_config() -> SimpleNamespace:
    return SimpleNamespace(
        global_interaction_enabled=True,
        global_interaction_processing_lease_seconds=30,
    )


async def test_ac6_global_commit_adjudicates_full_associated_group_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC6：全局 commit 前生产必须携带完整关联群集合裁决。

    当前生产在 ``insert_interaction_event`` 前不裁决，也不携带群集合 → 红。
    """
    scripted = ScriptedAdjudicate("admitted")
    install_scripted_adjudicate(monkeypatch, scripted)
    redis = _FakeRedis([_record("10001"), _record("10002")])
    memory = _FakeMemory()
    monkeypatch.setattr(worker_module, "get_config", _make_config)
    monkeypatch.setattr(
        worker_module.agent_run_logger_plugin, "create_collector", lambda *a, **k: None  # noqa: ARG005 -- 收集器注入哨兵
    )

    async def _noop_finalize(*args: object, **kwargs: object) -> None:
        del args, kwargs

    monkeypatch.setattr(
        worker_module.agent_run_logger_plugin, "finalize_collector", _noop_finalize
    )

    async def _noop(_delay: float) -> None:
        """不等待。"""

    monkeypatch.setattr(asyncio, "sleep", _noop)

    async def _fake_summarize(**_kwargs: object) -> object:
        from types import SimpleNamespace

        return SimpleNamespace(event_summary="s", importance=4)

    monkeypatch.setattr(worker_module, "summarize_interaction_events", _fake_summarize)

    await worker_module._process_claimed_user(
        redis=redis,  # type: ignore[arg-type] -- 记录型 fake 注入
        memory=memory,  # type: ignore[arg-type] -- 记录型 fake 注入
        user_id="u1",
        owner_token="owner",
        lease_seconds=30,
    )

    assert scripted.calls, "全局 commit 前未对关联群集合裁决"
    associated = {
        tuple(call[0]) for call in scripted.calls if isinstance(call[0], list)
    }
    assert (10001,) in associated and (10002,) in associated, (
        "全局 commit 前必须以归一化正整数集合覆盖完整关联群"
    )
    assert memory.insert_calls, "获准群贡献应完成全局 commit"


class _MixedPartitionAdjudicate:
    """补1 混批替身：10002 受限，其余获准（按身份而非顺序裁决）。"""

    def __init__(self) -> None:
        self.calls: list[object] = []
        self._restricted = {(10002,)}

    def __call__(self, associated_group_ids: object, **kwargs: object) -> object:
        del kwargs
        from komari_bot.plugins.group_admission import (
            AdmissionQualification,
            AdmissionResult,
        )

        self.calls.append(associated_group_ids)
        normalized = (
            tuple(associated_group_ids)
            if isinstance(associated_group_ids, (list, tuple))
            else None
        )
        if normalized in self._restricted:
            return AdmissionResult(
                qualification=AdmissionQualification.REJECTED,
                effective_revision=1,
                reason_code="policy_restricted",
            )
        return AdmissionResult(
            qualification=AdmissionQualification.BUSINESS,
            effective_revision=1,
            reason_code="policy_admitted",
        )


async def test_ac6_mixed_restricted_partition_does_not_block_admitted_partition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """补项1（AC6 混批）：admitted 群贡献不被同批次 restricted 群拖累。

    分区处理/独立提交：10001 获准入、10002 受限，worker 仍应完成 10001 分区
    的全局 commit（insert 被调用），受限的 10002 贡献休眠、不阻断获准入分区。
    """
    adjudicate = _MixedPartitionAdjudicate()
    import komari_bot.plugins.group_admission as admission_package

    monkeypatch.setattr(admission_package, "adjudicate", adjudicate)
    redis = _FakeRedis([_record("10001"), _record("10002")])
    memory = _FakeMemory()
    monkeypatch.setattr(worker_module, "get_config", _make_config)
    monkeypatch.setattr(
        worker_module.agent_run_logger_plugin, "create_collector", lambda *a, **k: None  # noqa: ARG005 -- 收集器注入哨兵
    )

    async def _noop_finalize(*args: object, **kwargs: object) -> None:
        del args, kwargs

    monkeypatch.setattr(
        worker_module.agent_run_logger_plugin, "finalize_collector", _noop_finalize
    )

    async def _noop(_delay: float) -> None:
        """不等待。"""

    monkeypatch.setattr(asyncio, "sleep", _noop)

    async def _fake_summarize(**_kwargs: object) -> object:
        from types import SimpleNamespace

        return SimpleNamespace(event_summary="s", importance=4)

    monkeypatch.setattr(worker_module, "summarize_interaction_events", _fake_summarize)

    await worker_module._process_claimed_user(
        redis=redis,  # type: ignore[arg-type] -- 记录型 fake 注入
        memory=memory,  # type: ignore[arg-type] -- 记录型 fake 注入
        user_id="u1",
        owner_token="owner",
        lease_seconds=30,
    )

    consulted = {
        tuple(c) if isinstance(c, (list, tuple)) else None for c in adjudicate.calls
    }
    assert (10001,) in consulted and (10002,) in consulted, (
        "混批必须逐个关联群裁决（10001/10002 均被 consult）"
    )
    assert memory.insert_calls, "获准入分区（10001）的全局 commit 不被受限分区（10002）拖累"
    assert redis.requeue_calls == [], "获准入分区完成后不应因受限分区重新入队"
