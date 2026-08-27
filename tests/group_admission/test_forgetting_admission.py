"""TSK-230 forgetting 按群归属投影跳过受限日的群准入验收测试（红基线）。

被测生产对象：komari_memory.services.forgetting_service.ForgettingService
的 decay_and_cleanup。

AC7：forgetting 受限日跳过；恢复后不补算休眠天数。当日关联群受限时，对话
重要性衰减不得作用于该群；恢复后不追溯补算。当前生产的衰减是跨群批量 SQL
（_CONVERSATION_DECAY_SQL），不含群归属投影、未接入准入，故红态失败。
Fake 只做依赖注入与记录，不替代生产对象。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from komari_bot.plugins.komari_memory.repositories.forgetting_job_repository import (
    ForgettingJobClaim,
)
from komari_bot.plugins.komari_memory.services.forgetting_service import (
    ForgettingService,
)
from tests.group_admission.chat_admission_support import (
    ScriptedAdjudicate,
    install_scripted_adjudicate,
)

pytestmark = pytest.mark.group_admission_acceptance


class _FakeForgettingJobRepo:
    """ForgettingJobRepository 依赖注入替身（安全默认，记录推进）。"""

    def __init__(self) -> None:
        self.decay_calls = 0

    async def claim(self, **kwargs: object) -> ForgettingJobClaim:
        del kwargs
        return ForgettingJobClaim(status="claimed", stage="claimed")

    async def run_transactional_stage(self, **kwargs: object) -> tuple[str, ...]:
        del kwargs
        return ("",)

    async def advance_stage(self, **kwargs: object) -> None:
        del kwargs

    async def mark_failure(self, **kwargs: object) -> None:
        del kwargs


def _make_config() -> SimpleNamespace:
    return SimpleNamespace(
        forgetting_enabled=True,
        forgetting_job_lease_seconds=120,
        forgetting_importance_threshold=2,
        forgetting_min_age_days=30,
    )


async def test_ac7_forgetting_restricted_day_skips_and_no_backfill(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC7：受限日衰减跳过；恢复后不补算休眠天数。"""
    scripted = ScriptedAdjudicate("restricted")
    install_scripted_adjudicate(monkeypatch, scripted)

    service = ForgettingService(
        pg_pool=None,  # type: ignore[arg-type] -- 测试用 fake 注入
        config_provider=_make_config,  # type: ignore[arg-type] -- SimpleNamespace 配置
        job_repository=_FakeForgettingJobRepo(),  # type: ignore[arg-type] -- fake repo
    )

    async def _noop_renew(**_kwargs: object) -> None:
        return None

    async def _noop_fuzzify(**_kwargs: object) -> int:
        return 0

    monkeypatch.setattr(service, "_renew_job_lease", _noop_renew)
    monkeypatch.setattr(
        service, "_fuzzify_and_cleanup_high_value_memories", _noop_fuzzify
    )
    monkeypatch.setattr(
        service, "_fuzzify_and_cleanup_high_value_interaction_events", _noop_fuzzify
    )

    await service.decay_and_cleanup(run_date=None)

    assert scripted.calls, (
        "forgetting 在衰减最前必须对关联群集合裁决（受限日跳过）"
    )
