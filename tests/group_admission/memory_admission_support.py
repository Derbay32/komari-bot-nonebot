"""TSK-230 komari_memory 群工作休眠与归属传播验收测试共享基础设施。

本模块只承载测试专用装置，不承载任何生产语义；全部被测对象是真实
komari_memory 的 worker / repository / manager 入口，Fake 只负责依赖注入
与记录，绝不替代生产对象。

TSK-230 语义（承接 ADR-0012 后文，未落地于生产的验收见各用例）：

- 候选发现应当只读身份/状态/归属（不读正文），受限时正文 reader 必须
  fail-if-called（AC1）；
- 受限群不领业务租约（claim）不读缓冲正文、保存有效 revision / 安全进度并
  休眠，随眠不耗 failure/retry/dead-letter（AC2）；
- 同 revision 不重复重裁决；新 revision 或 failed→ready 才重裁决（AC3）；
- 缺失/非法/冲突归属进入 ``ADMISSION_ATTRIBUTION_FAILED`` 持有态，不自动
  重试、不删除（AC8）。

当前生产 komari_memory 尚未接入群准入（``grep group_admission`` 无引用），
因此「受限时效果不得发生」「归属失败必须持有」等验收用例全部以红态失败，
证明接入缺失——这正是本票的红基线。

``install_scripted_adjudicate`` 复用 ``tests.group_admission.chat_admission_support``
的替身装置；本模块只新增记忆侧 fake 与 revision sidecar。
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from komari_bot.plugins.komari_memory.services.conversation_processing import (
    ConversationSnapshotClaim,
)
from tests.group_admission.chat_admission_support import (
    ScriptedAdjudicate,
    install_scripted_adjudicate,
)


class RevisionSidecar:
    """测试专用 revision 侧车：记录每次休眠时保存的 effective revision。

    表达「保存当前 effective revision 并休眠，仅在 revision 变化时重裁决」
    的受限群侧车契约（AC3）。生产必须把 deferred revision 写进自有
    recording / snapshot / ledger，不建中央 jobs 表；本 fake 承接该写入面。
    """

    def __init__(self) -> None:
        #: group_id -> 上次休眠时固化的 effective revision（None 表示未记录）。
        self.deferred: dict[str, int | None] = {}
        self.writes: list[tuple[str, int | None]] = []

    def save(self, group_id: str, revision: int | None) -> None:
        self.deferred[group_id] = revision
        self.writes.append((group_id, revision))

    def revision_changed(self, group_id: str, current: int | None) -> bool:
        return self.deferred.get(group_id) != current


class DormantProcessingStorage:
    """``_ProcessingStorage`` 形状的存根注入对象（最小动词面）。

    只注入生产 ``ConversationProcessingLifecycle`` 需要的动词并记录调用：
    claim / get / dead-letter / restore / ack / 孤儿扫描 / 活跃群。所有动词
    返回安全默认值，绝不替生产实现准入逻辑。revision/sidecar 归属由测试经
    ``sidecar`` 注入断言。
    """

    def __init__(self) -> None:
        self.config = SimpleNamespace(conversation_processing_lease_seconds=120)
        self.claim_calls: list[dict[str, str]] = []
        self.get_calls: list[dict[str, str]] = []
        self.ack_calls: list[dict[str, str]] = []
        self.restore_calls: list[dict[str, str]] = []
        self.dead_letter_calls: list[dict[str, object]] = []
        self.orphan_scan_calls = 0
        self.active_groups: list[str] = []
        self.should_trigger: dict[str, bool] = {}
        self.processor_calls = 0

    async def claim_conversation_buffer(
        self, group_id: str, owner_token: str, token: str
    ) -> ConversationSnapshotClaim:
        del owner_token, token
        self.claim_calls.append({"group_id": group_id})
        return ConversationSnapshotClaim(status="claimed", processing_key=f"pk:{group_id}")

    async def get_processing_conversation_buffer(
        self, group_id: str, processing_key: str, owner_token: str
    ) -> list[Any]:
        del processing_key, owner_token
        self.get_calls.append({"group_id": group_id})
        return []

    async def renew_processing_conversation_lease(
        self, group_id: str, processing_key: str, owner_token: str
    ) -> bool:
        del group_id, processing_key, owner_token
        return True

    async def ack_processing_conversation_buffer(
        self, group_id: str, processing_key: str, owner_token: str
    ) -> bool:
        del processing_key, owner_token
        self.ack_calls.append({"group_id": group_id})
        return True

    async def restore_processing_conversation_buffer(
        self, group_id: str, processing_key: str, owner_token: str
    ) -> bool:
        del processing_key, owner_token
        self.restore_calls.append({"group_id": group_id})
        return True

    async def update_last_summary(self, group_id: str) -> None:
        del group_id

    async def dead_letter_processing_conversation_buffer(
        self,
        group_id: str,
        processing_key: str,
        owner_token: str,
        *,
        failure_code: str,
        attempt_count: int,
    ) -> bool:
        del processing_key, owner_token, failure_code, attempt_count
        self.dead_letter_calls.append({"group_id": group_id})
        return True

    async def get_orphaned_conversation_processing_keys(self) -> list[tuple[str, str]]:
        self.orphan_scan_calls += 1
        return []

    async def get_active_groups(self) -> list[str]:
        return list(self.active_groups)

    async def claim_existing_conversation_processing(
        self, group_id: str, processing_key: str, owner_token: str
    ) -> ConversationSnapshotClaim:
        del group_id, processing_key, owner_token
        return ConversationSnapshotClaim(status="claimed", processing_key="pk-existing")

    async def initialize_conversation_chunk_manifest(self, **kwargs: object) -> str:
        del kwargs
        return "{}"

    async def get_conversation_chunk_state(
        self, group_id: str, processing_key: str, owner_token: str, field: str
    ) -> str | None:
        del group_id, processing_key, owner_token, field
        return None

    async def set_conversation_chunk_state(self, **kwargs: object) -> None:
        del kwargs

    async def should_trigger_summary(self, group_id: str) -> bool:
        return self.should_trigger.get(group_id, False)


class FailIfCalledReader:
    """AC1 正文 reader 的 fail-if-called 替身。

    若生产在受限时仍读取缓冲正文，本读体断言将抛错而失败（红）。"""

    def __init__(self) -> None:
        self.called = False

    async def __call__(self, *args: Any, **kwargs: Any) -> list[Any]:
        del args, kwargs
        self.called = True
        raise AssertionError("fail-if-called: 受限群仍读取了对话正文 buffer")  # noqa: TRY003


__all__ = [
    "ADMISSION_ATTRIBUTION_FAILED",
    "DormantProcessingStorage",
    "FailIfCalledReader",
    "RevisionSidecar",
    "ScriptedAdjudicate",
    "install_scripted_adjudicate",
]

ADMISSION_ATTRIBUTION_FAILED = "ADMISSION_ATTRIBUTION_FAILED"
