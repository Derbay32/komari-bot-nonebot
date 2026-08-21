"""TSK-222：group_admission 裁决真值表穷举验收。

覆盖已约定裁决矩阵的全部维度（经顶层同步 ``adjudicate`` 观测）：

1. blacklist 空集全部获准；非空集合内受限、其余获准；
2. whitelist 空集全部获准（明确领域规则，非「空白名单拒绝全部」）；
   非空仅集合内获准；
3. 多关联群必须全部获准，mixed 整体 restricted，重复群号不改变结果；
4. BUSINESS 空/非法归属 → REJECTED/``group_attribution_unavailable``；
   failed 无有效策略且归属合法 → REJECTED/``effective_policy_unavailable``；
5. FACT_FINALIZATION 合法非空关联群始终授自身资格，不查黑/白真值表，
   ready/failed 均可；空或非法归属拒绝；
6. TECHNICAL_CLEANUP 空或合法关联群始终授自身资格；非空含非法值拒绝；
7. ``result.effective_revision``：有 effective snapshot 时为其 revision，
   冷启动 failed 无 snapshot 时为 None（即使授予 fact/cleanup）。

实际观察到的 reason code 逐一精确断言，全部落在冻结闭集内，且本票
``adjudicate`` 从不产生 ``private_input_rejected``。
"""

from __future__ import annotations

import pytest

from tests.group_admission.runtime_support import (
    AdmissionStorageFake,
    install_singleton,
    start_runtime,
    stored_policy,
)

pytestmark = pytest.mark.group_admission_acceptance

BLACKLIST_EMPTY: dict[str, object] = {"mode": "blacklist", "group_ids": []}
WHITELIST_EMPTY: dict[str, object] = {"mode": "whitelist", "group_ids": []}


async def _ready_runtime(
    monkeypatch: pytest.MonkeyPatch,
    policy: dict[str, object],
    *,
    revision: int = 1,
) -> None:
    storage = AdmissionStorageFake(stored_policy(revision, policy))
    runtime, _manager = await start_runtime(monkeypatch, storage)
    install_singleton(monkeypatch, runtime)


async def _failed_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    storage = AdmissionStorageFake(
        fetch_error=RuntimeError("storage offline during cold start")
    )
    runtime, _manager = await start_runtime(monkeypatch, storage)
    install_singleton(monkeypatch, runtime)


# ---------------------------------------------------------------------------
# 规则 1/2：黑白名单空/非空真值表
# ---------------------------------------------------------------------------


async def test_blacklist_empty_set_admits_every_legal_group(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _ready_runtime(monkeypatch, BLACKLIST_EMPTY)
    from komari_bot.plugins.group_admission import AdmissionQualification, adjudicate

    result = adjudicate([100])

    assert result.qualification is AdmissionQualification.BUSINESS
    assert result.reason_code == "policy_admitted"
    assert result.effective_revision == 1

    other = adjudicate([999999])
    assert other.qualification is AdmissionQualification.BUSINESS
    assert other.reason_code == "policy_admitted"


async def test_blacklist_non_empty_restricts_listed_group(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    policy: dict[str, object] = {"mode": "blacklist", "group_ids": [200]}
    await _ready_runtime(monkeypatch, policy)
    from komari_bot.plugins.group_admission import AdmissionQualification, adjudicate

    result = adjudicate([200])

    assert result.qualification is AdmissionQualification.REJECTED
    assert result.reason_code == "policy_restricted"
    assert result.effective_revision == 1


async def test_blacklist_non_empty_admits_unlisted_group(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    policy: dict[str, object] = {"mode": "blacklist", "group_ids": [200]}
    await _ready_runtime(monkeypatch, policy)
    from komari_bot.plugins.group_admission import AdmissionQualification, adjudicate

    result = adjudicate([300])

    assert result.qualification is AdmissionQualification.BUSINESS
    assert result.reason_code == "policy_admitted"


async def test_whitelist_empty_set_admits_every_legal_group(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _ready_runtime(monkeypatch, WHITELIST_EMPTY)
    from komari_bot.plugins.group_admission import AdmissionQualification, adjudicate

    result = adjudicate([100])

    assert result.qualification is AdmissionQualification.BUSINESS
    assert result.reason_code == "policy_admitted"


async def test_whitelist_non_empty_admits_listed_group(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    policy: dict[str, object] = {"mode": "whitelist", "group_ids": [100]}
    await _ready_runtime(monkeypatch, policy)
    from komari_bot.plugins.group_admission import AdmissionQualification, adjudicate

    result = adjudicate([100])

    assert result.qualification is AdmissionQualification.BUSINESS
    assert result.reason_code == "policy_admitted"


async def test_whitelist_non_empty_restricts_unlisted_group(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    policy: dict[str, object] = {"mode": "whitelist", "group_ids": [100]}
    await _ready_runtime(monkeypatch, policy)
    from komari_bot.plugins.group_admission import AdmissionQualification, adjudicate

    result = adjudicate([200])

    assert result.qualification is AdmissionQualification.REJECTED
    assert result.reason_code == "policy_restricted"


# ---------------------------------------------------------------------------
# 规则 3：多关联群 / mixed / 重复
# ---------------------------------------------------------------------------


async def test_multiple_associated_groups_all_admitted_pass(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    policy: dict[str, object] = {
        "mode": "whitelist",
        "group_ids": [100, 200, 200],  # 策略侧重复群号确定性去重
    }
    await _ready_runtime(monkeypatch, policy)
    from komari_bot.plugins.group_admission import AdmissionQualification, adjudicate

    result = adjudicate([100, 200])

    assert result.qualification is AdmissionQualification.BUSINESS
    assert result.reason_code == "policy_admitted"


async def test_mixed_associated_groups_restrict_the_whole_action(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    policy: dict[str, object] = {"mode": "whitelist", "group_ids": [100]}
    await _ready_runtime(monkeypatch, policy)
    from komari_bot.plugins.group_admission import AdmissionQualification, adjudicate

    result = adjudicate([100, 200])

    assert result.qualification is AdmissionQualification.REJECTED
    assert result.reason_code == "policy_restricted"


@pytest.mark.parametrize(
    ("policy", "group_ids", "expected_qualification", "expected_reason"),
    [
        (
            {"mode": "whitelist", "group_ids": [100]},
            [100, 100],
            "business",
            "policy_admitted",
        ),
        (
            {"mode": "blacklist", "group_ids": [200]},
            [200, 200],
            "rejected",
            "policy_restricted",
        ),
        (
            {"mode": "blacklist", "group_ids": []},
            [100, 100, 100],
            "business",
            "policy_admitted",
        ),
    ],
    ids=[
        "whitelist-duplicated-admitted",
        "blacklist-duplicated-restricted",
        "empty-blacklist-duplicated-admitted",
    ],
)
async def test_duplicate_associated_group_ids_do_not_change_outcome(
    monkeypatch: pytest.MonkeyPatch,
    policy: dict[str, object],
    group_ids: list[int],
    expected_qualification: str,
    expected_reason: str,
) -> None:
    await _ready_runtime(monkeypatch, policy)
    from komari_bot.plugins.group_admission import adjudicate

    result = adjudicate(group_ids)

    assert result.qualification.value == expected_qualification
    assert result.reason_code == expected_reason


# ---------------------------------------------------------------------------
# 规则 4：BUSINESS 归属失败与 failed 冷启动
# ---------------------------------------------------------------------------


async def test_business_missing_attribution_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _ready_runtime(monkeypatch, BLACKLIST_EMPTY)
    from komari_bot.plugins.group_admission import AdmissionQualification, adjudicate

    result = adjudicate(None)  # type: ignore[arg-type] 故意非法归属

    assert result.qualification is AdmissionQualification.REJECTED
    assert result.reason_code == "group_attribution_unavailable"


@pytest.mark.parametrize(
    "attribution",
    [
        [],
        "123",
        True,
        [0],
        [-5],
        ["123"],
        [True],
        [1.5],
        [None],
        [100, "x"],
    ],
    ids=[
        "empty-list",
        "string-as-collection",
        "bool-as-collection",
        "zero-group-id",
        "negative-group-id",
        "string-element",
        "bool-element",
        "float-element",
        "none-element",
        "mixed-legal-and-illegal",
    ],
)
async def test_business_illegal_attribution_elements_are_rejected(
    monkeypatch: pytest.MonkeyPatch,
    attribution: object,
) -> None:
    await _ready_runtime(monkeypatch, BLACKLIST_EMPTY)
    from komari_bot.plugins.group_admission import AdmissionQualification, adjudicate

    result = adjudicate(attribution)  # type: ignore[arg-type]

    assert result.qualification is AdmissionQualification.REJECTED
    assert result.reason_code == "group_attribution_unavailable"


async def test_business_failed_runtime_rejects_legal_attribution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _failed_runtime(monkeypatch)
    from komari_bot.plugins.group_admission import AdmissionQualification, adjudicate

    result = adjudicate([100])

    assert result.qualification is AdmissionQualification.REJECTED
    assert result.reason_code == "effective_policy_unavailable"


# ---------------------------------------------------------------------------
# 规则 5：FACT_FINALIZATION
# ---------------------------------------------------------------------------


async def test_fact_finalization_granted_for_restricted_group(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    policy: dict[str, object] = {"mode": "blacklist", "group_ids": [200]}
    await _ready_runtime(monkeypatch, policy)
    from komari_bot.plugins.group_admission import (
        AdmissionIntent,
        AdmissionQualification,
        adjudicate,
    )

    result = adjudicate([200], intent=AdmissionIntent.FACT_FINALIZATION)

    assert result.qualification is AdmissionQualification.FACT_FINALIZATION
    assert result.reason_code == "fact_finalization_granted"
    assert result.effective_revision == 1


async def test_fact_finalization_granted_on_failed_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _failed_runtime(monkeypatch)
    from komari_bot.plugins.group_admission import (
        AdmissionIntent,
        AdmissionQualification,
        adjudicate,
    )

    result = adjudicate([100], intent=AdmissionIntent.FACT_FINALIZATION)

    assert result.qualification is AdmissionQualification.FACT_FINALIZATION
    assert result.reason_code == "fact_finalization_granted"
    assert result.effective_revision is None


@pytest.mark.parametrize(
    "attribution",
    [[], None, [0], ["x"], [-1]],
    ids=["empty", "none", "zero", "string-element", "negative"],
)
async def test_fact_finalization_requires_legal_non_empty_attribution(
    monkeypatch: pytest.MonkeyPatch,
    attribution: object,
) -> None:
    await _ready_runtime(monkeypatch, BLACKLIST_EMPTY)
    from komari_bot.plugins.group_admission import (
        AdmissionIntent,
        AdmissionQualification,
        adjudicate,
    )

    result = adjudicate(  # type: ignore[arg-type] 故意非法归属
        attribution,
        intent=AdmissionIntent.FACT_FINALIZATION,
    )

    assert result.qualification is AdmissionQualification.REJECTED
    assert result.reason_code == "group_attribution_unavailable"


# ---------------------------------------------------------------------------
# 规则 6：TECHNICAL_CLEANUP
# ---------------------------------------------------------------------------


async def test_technical_cleanup_granted_with_empty_attribution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _ready_runtime(monkeypatch, BLACKLIST_EMPTY)
    from komari_bot.plugins.group_admission import (
        AdmissionIntent,
        AdmissionQualification,
        adjudicate,
    )

    result = adjudicate([], intent=AdmissionIntent.TECHNICAL_CLEANUP)

    assert result.qualification is AdmissionQualification.TECHNICAL_CLEANUP
    assert result.reason_code == "technical_cleanup_granted"
    assert result.effective_revision == 1


async def test_technical_cleanup_granted_for_restricted_group(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    policy: dict[str, object] = {"mode": "whitelist", "group_ids": [100]}
    await _ready_runtime(monkeypatch, policy)
    from komari_bot.plugins.group_admission import (
        AdmissionIntent,
        AdmissionQualification,
        adjudicate,
    )

    result = adjudicate([200], intent=AdmissionIntent.TECHNICAL_CLEANUP)

    assert result.qualification is AdmissionQualification.TECHNICAL_CLEANUP
    assert result.reason_code == "technical_cleanup_granted"


@pytest.mark.parametrize(
    "attribution",
    [[0], ["x"], [-3], [True]],
    ids=["zero", "string-element", "negative", "bool-element"],
)
async def test_technical_cleanup_rejects_illegal_attribution_elements(
    monkeypatch: pytest.MonkeyPatch,
    attribution: object,
) -> None:
    await _ready_runtime(monkeypatch, BLACKLIST_EMPTY)
    from komari_bot.plugins.group_admission import (
        AdmissionIntent,
        AdmissionQualification,
        adjudicate,
    )

    result = adjudicate(  # type: ignore[arg-type] 故意非法归属
        attribution,
        intent=AdmissionIntent.TECHNICAL_CLEANUP,
    )

    assert result.qualification is AdmissionQualification.REJECTED
    assert result.reason_code == "group_attribution_unavailable"


# ---------------------------------------------------------------------------
# 规则 7：result.effective_revision
# ---------------------------------------------------------------------------


async def test_result_effective_revision_tracks_effective_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    policy: dict[str, object] = {"mode": "blacklist", "group_ids": [200]}
    await _ready_runtime(monkeypatch, policy, revision=7)
    from komari_bot.plugins.group_admission import AdmissionIntent, adjudicate

    admitted = adjudicate([300])
    restricted = adjudicate([200])
    fact = adjudicate([200], intent=AdmissionIntent.FACT_FINALIZATION)
    cleanup = adjudicate([], intent=AdmissionIntent.TECHNICAL_CLEANUP)

    assert admitted.effective_revision == 7
    assert restricted.effective_revision == 7
    assert fact.effective_revision == 7
    assert cleanup.effective_revision == 7


async def test_failed_cold_start_grants_carry_none_effective_revision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _failed_runtime(monkeypatch)
    from komari_bot.plugins.group_admission import AdmissionIntent, adjudicate

    fact = adjudicate([100], intent=AdmissionIntent.FACT_FINALIZATION)
    cleanup = adjudicate([], intent=AdmissionIntent.TECHNICAL_CLEANUP)
    rejected_business = adjudicate([100])

    assert fact.effective_revision is None
    assert cleanup.effective_revision is None
    assert rejected_business.effective_revision is None
