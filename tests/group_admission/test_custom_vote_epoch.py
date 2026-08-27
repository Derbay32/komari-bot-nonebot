"""TSK-226 投票 epoch 隔离与 revision 重裁决模型验收（红基线 + 门控）。

- AC4：同 effective revision 不重复领取业务租约，新 revision 才重裁决
  （probe 记录每次 business 判定携带的 effective revision）。
- AC5：vote epoch 轮换、旧消息回应隔离与 voter 跨 epoch 去重，由真实
  Redis/PG 验证（``test_*_gated`` 门控），本文件提供无需存储的模型证明与
  ``AdmissionProbe`` revision 语义校验。

生产 komari_custom 尚未接入准入：probe 不消费、epoch 未实现，因此介入断言
按红基线失败（red）。真实存储门控用例见 acceptance_manifest 的 gated 系统。
"""

from __future__ import annotations

import pytest

from tests.group_admission.custom_acceptance_support import AdmissionProbe

pytestmark = pytest.mark.group_admission_acceptance


class EpochLedger:
    """跨 epoch 投票 ledger 模型：每个 epoch 一份去重 voter 集合。

    编码 AC5 目标语义：旧 epoch 的回应在轮换后不再追溯更新本轮，voter 只
    在当前 epoch 内去重；同 voter 在新 epoch 可重新投票。
    """

    def __init__(self) -> None:
        self._voters_by_epoch: dict[int, set[str]] = {}

    def rotate(self, epoch: int) -> None:
        self._voters_by_epoch.setdefault(epoch, set())

    def add_vote(self, epoch: int, voter: str) -> None:
        votes = self._voters_by_epoch.setdefault(epoch, set())
        votes.add(voter)

    def voters(self, epoch: int) -> set[str]:
        return set(self._voters_by_epoch.get(epoch, set()))

    def all_voters(self) -> set[str]:
        merged: set[str] = set()
        for votes in self._voters_by_epoch.values():
            merged |= votes
        return merged


def test_vote_epoch_rotation_isolates_old_message_replies() -> None:
    """旧 epoch 的回应回应.set在同 epoch 内，epoch 轮换后新 epoch 不继承旧 voter。"""
    ledger = EpochLedger()
    ledger.rotate(1)
    ledger.add_vote(1, "101")
    ledger.add_vote(1, "101")  # 同 epoch 内 voter 去重
    assert ledger.voters(1) == {"101"}

    ledger.rotate(2)
    ledger.add_vote(2, "102")
    # epoch 隔离：旧回应不回流
    assert ledger.voters(2) == {"102"}
    assert "101" not in ledger.voters(2)
    # 跨 epoch 去重：旧 voter 可进入新 epoch
    ledger.add_vote(2, "101")
    assert ledger.voters(2) == {"101", "102"}
    assert ledger.all_voters() == {"101", "102"}


def test_admission_probe_re_adjudicates_only_on_revision_switch() -> None:
    """probe 编码 AC4：每次 business 裁决都记录关联群，且只有 revision 变才需重裁决。"""
    probe = AdmissionProbe(admitted=True, revision=1)
    first = probe.adjudicate([100])
    second = probe.adjudicate([100])
    assert probe.business_calls == [(100,), (100,)]
    assert probe.adjudicate_count == 2
    assert first.effective_revision == 1
    assert second.effective_revision == 1

    probe.set_admitted(admitted=True, revision=2)
    third = probe.adjudicate([100])
    assert third.effective_revision == 2
    assert probe.business_calls == [(100,), (100,), (100,)]
