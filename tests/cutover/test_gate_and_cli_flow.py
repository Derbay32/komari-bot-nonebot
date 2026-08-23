"""TSK-232 轮 B —— cutover gate/phase CAS 与 CLI 命令面集成红基线（门控 DB）。

锁定骨架面验收契约（数据面转换矩阵由另一批测试覆盖，不在本文件）：

- ``prepare-policy``：成功路径写 config 单行 + gate CAS 到 POLICY_PREPARED
  （同一 PG 事务）、fingerprint 不符拒绝（POLICY_FINGERPRINT_MISMATCH）、
  相位已前进拒绝（PHASE_OUT_OF_ORDER）、dry-run 不落库、幂等 no-op；
- ``capture-evidence``：前置相位校验、缺 backup_checkpoint 拒绝
  （BACKUP_CHECKPOINT_MISSING）、policy 漂移拒绝、Redis 不可用快速失败
  （REDIS_UNAVAILABLE）、幂等重跑；
- ``finalize-redis``：仅锁命令存在、参数校验生效与 POSTGRES_BACKFILLED
  相位前置（PHASE_OUT_OF_ORDER）；其行为矩阵属数据面子代理；
- ``abort-pre-backfill``：evidence 相位回退到 POLICY_PREPARED 并清 staged
  evidence、更早/更晚相位一律 ABORT_FORBIDDEN、重复 abort 状态不劣化；
- ``status``：gate 全量快照 + alembic_version + 聚合计数，gate 行缺失
  报 GATE_MISSING；
- phase 只准前进：operator 路径跳步必须被 CAS 拒绝。

红基线纪律：全部用例从 ``komari_bot.cutover.cli.main(argv)`` 公共入口驱动
真实生产代码；目标模块未实现时以 ModuleNotFoundError 红、gate 表未建时以
表不存在红。隔离库 = 门控库名 + ``_tsk232b1flow``，用例结束 DROP。
"""

from __future__ import annotations

import importlib.util
import json
from datetime import datetime
from typing import TYPE_CHECKING, Any

from tests.cutover.support import (
    DEFAULT_SEEDED_POLICY,
    DEFAULT_SEEDED_POLICY_FINGERPRINT,
    OUTBOX_COUNT_KEYS,
    REDIS_URL,
    SKIP_NO_POSTGRES,
    SKIP_NO_REDIS,
    VALID_POLICY,
    VALID_POLICY_FINGERPRINT,
    cutover_scratch_database,
    delete_gate_rows,
    fetch_admission_config,
    fetch_gate_row,
    find_key,
    oracle_fingerprint,
    overwrite_admission_policy,
    run_cli,
    stage_gate_phase,
    write_policy_file,
)

if TYPE_CHECKING:
    import pytest

pytestmark = [SKIP_NO_POSTGRES]

CHECKPOINT_CANARY = "ckpt-canary-20260823-opaque"
EVIDENCE_DIGEST_CANARY = "d" * 64


def _policy_args(policy_file: str) -> list[str]:
    return [
        "--policy-file",
        policy_file,
        "--expected-fingerprint",
        VALID_POLICY_FINGERPRINT,
    ]


# ---------------------------------------------------------------------------
# 骨架存在性（无 DB）
# ---------------------------------------------------------------------------


def test_cutover_package_public_surface_exists() -> None:
    """``komari_bot.cutover.cli.main`` 与 ``python -m`` 入口模块必须存在。"""
    assert importlib.util.find_spec("komari_bot.cutover") is not None
    assert importlib.util.find_spec("komari_bot.cutover.cli") is not None
    assert importlib.util.find_spec("komari_bot.cutover.__main__") is not None


# ---------------------------------------------------------------------------
# prepare-policy
# ---------------------------------------------------------------------------


def test_prepare_policy_apply_writes_config_and_advances_gate(
    capsys: pytest.CaptureFixture[str],
    tmp_path: Any,
) -> None:
    """成功路径：config 写入 canonical policy、gate CAS 到 POLICY_PREPARED。"""
    with cutover_scratch_database("flow") as (params, database_url):
        policy_file = write_policy_file(tmp_path, VALID_POLICY)
        exit_code, payload = run_cli(
            capsys,
            "prepare-policy",
            "--database-url",
            database_url,
            *_policy_args(policy_file),
            "--apply",
        )
        assert exit_code == 0
        assert payload is not None
        assert payload["command"] == "prepare-policy"
        assert payload["status"] == "ok"

        gate = fetch_gate_row(params)
        assert gate is not None
        assert gate["phase"] == "POLICY_PREPARED"
        assert isinstance(gate["policy_revision"], int)
        assert gate["policy_revision"] >= 2
        assert gate["policy_fingerprint"] == VALID_POLICY_FINGERPRINT

        config = fetch_admission_config(params)
        assert config is not None
        assert json.loads(str(config["policy"])) == {
            "mode": VALID_POLICY["mode"],
            "group_ids": sorted(VALID_POLICY["group_ids"]),
        }
        # 写入与 gate CAS 同一事务的一致性投影：两侧 revision 一致。
        assert config["revision"] == gate["policy_revision"]


def test_prepare_policy_idempotent_rerun_is_noop(
    capsys: pytest.CaptureFixture[str],
    tmp_path: Any,
) -> None:
    """同 canonical policy 幂等重跑：成功、phase 不变、revision 不再增长。"""
    with cutover_scratch_database("flow") as (params, database_url):
        policy_file = write_policy_file(tmp_path, VALID_POLICY)
        argv = [
            "prepare-policy",
            "--database-url",
            database_url,
            *_policy_args(policy_file),
            "--apply",
        ]
        first_code, _ = run_cli(capsys, *argv)
        assert first_code == 0
        gate_after_first = fetch_gate_row(params)
        assert gate_after_first is not None
        revision_after_first = gate_after_first["policy_revision"]

        second_code, second_payload = run_cli(capsys, *argv)
        assert second_code == 0
        assert second_payload is not None
        assert second_payload["status"] == "ok"
        assert find_key(second_payload, "idempotent")

        gate_after_second = fetch_gate_row(params)
        assert gate_after_second is not None
        assert gate_after_second["phase"] == "POLICY_PREPARED"
        assert gate_after_second["policy_revision"] == revision_after_first


def test_prepare_policy_dry_run_does_not_write(
    capsys: pytest.CaptureFixture[str],
    tmp_path: Any,
) -> None:
    """无 --apply 时只做校验并输出变更摘要，绝不写库。"""
    with cutover_scratch_database("flow") as (params, database_url):
        policy_file = write_policy_file(tmp_path, VALID_POLICY)
        exit_code, payload = run_cli(
            capsys,
            "prepare-policy",
            "--database-url",
            database_url,
            *_policy_args(policy_file),
        )
        assert exit_code == 0
        assert payload is not None
        assert payload["status"] == "ok"

        gate = fetch_gate_row(params)
        assert gate is not None
        assert gate["phase"] == "EXPANDED"
        assert gate["policy_revision"] is None
        assert gate["policy_fingerprint"] is None

        config = fetch_admission_config(params)
        assert config is not None
        assert config["revision"] == 1
        assert json.loads(str(config["policy"])) == DEFAULT_SEEDED_POLICY


def test_prepare_policy_rejects_fingerprint_mismatch(
    capsys: pytest.CaptureFixture[str],
    tmp_path: Any,
) -> None:
    """expected-fingerprint 与文件实际指纹不符 → POLICY_FINGERPRINT_MISMATCH。"""
    with cutover_scratch_database("flow") as (params, database_url):
        other_fingerprint = oracle_fingerprint({"mode": "whitelist", "group_ids": [1]})
        policy_file = write_policy_file(tmp_path, VALID_POLICY)
        exit_code, payload = run_cli(
            capsys,
            "prepare-policy",
            "--database-url",
            database_url,
            "--policy-file",
            policy_file,
            "--expected-fingerprint",
            other_fingerprint,
            "--apply",
        )
        assert exit_code != 0
        assert payload is not None
        assert payload["status"] == "error"
        assert payload["error_code"] == "POLICY_FINGERPRINT_MISMATCH"

        gate = fetch_gate_row(params)
        assert gate is not None
        assert gate["phase"] == "EXPANDED"
        assert gate["policy_revision"] is None


def test_prepare_policy_rejected_once_phase_advanced(
    capsys: pytest.CaptureFixture[str],
    tmp_path: Any,
) -> None:
    """gate 已越过 EXPANDED 后再 prepare → PHASE_OUT_OF_ORDER（CAS 拒绝回退）。"""
    with cutover_scratch_database("flow") as (params, database_url):
        stage_gate_phase(
            params,
            phase="REDIS_EVIDENCE_CAPTURED",
            policy_revision=7,
            policy_fingerprint=DEFAULT_SEEDED_POLICY_FINGERPRINT,
        )
        policy_file = write_policy_file(tmp_path, VALID_POLICY)
        exit_code, payload = run_cli(
            capsys,
            "prepare-policy",
            "--database-url",
            database_url,
            *_policy_args(policy_file),
            "--apply",
        )
        assert exit_code != 0
        assert payload is not None
        assert payload["status"] == "error"
        assert payload["error_code"] == "PHASE_OUT_OF_ORDER"

        gate = fetch_gate_row(params)
        assert gate is not None
        assert gate["phase"] == "REDIS_EVIDENCE_CAPTURED"
        assert gate["policy_revision"] == 7


# ---------------------------------------------------------------------------
# capture-evidence
# ---------------------------------------------------------------------------


def _prepared_argv(database_url: str, policy_file: str) -> list[str]:
    return [
        "prepare-policy",
        "--database-url",
        database_url,
        *_policy_args(policy_file),
        "--apply",
    ]


@SKIP_NO_REDIS
def test_capture_evidence_success_records_gate_fields(
    capsys: pytest.CaptureFixture[str],
    tmp_path: Any,
) -> None:
    """POLICY_PREPARED → REDIS_EVIDENCE_CAPTURED：digest 与 checkpoint 落 gate。"""
    with cutover_scratch_database("flow") as (params, database_url):
        policy_file = write_policy_file(tmp_path, VALID_POLICY)
        code, _ = run_cli(capsys, *_prepared_argv(database_url, policy_file))
        assert code == 0

        exit_code, payload = run_cli(
            capsys,
            "capture-evidence",
            "--database-url",
            database_url,
            "--redis-url",
            REDIS_URL,
            "--expected-fingerprint",
            VALID_POLICY_FINGERPRINT,
            "--backup-checkpoint",
            CHECKPOINT_CANARY,
            "--apply",
        )
        assert exit_code == 0
        assert payload is not None
        assert payload["command"] == "capture-evidence"
        assert payload["status"] == "ok"

        gate = fetch_gate_row(params)
        assert gate is not None
        assert gate["phase"] == "REDIS_EVIDENCE_CAPTURED"
        assert gate["backup_checkpoint"] == CHECKPOINT_CANARY
        digest = gate["redis_evidence_digest"]
        assert isinstance(digest, str) and len(digest) == 64
        int(digest, 16)  # 聚合身份摘要必须是 SHA-256 hex


def test_capture_evidence_requires_backup_checkpoint(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """缺 --backup-checkpoint → BACKUP_CHECKPOINT_MISSING（JSON 错误面）。"""
    with cutover_scratch_database("flow") as (params, database_url):
        stage_gate_phase(
            params,
            phase="POLICY_PREPARED",
            policy_revision=2,
            policy_fingerprint=DEFAULT_SEEDED_POLICY_FINGERPRINT,
        )
        exit_code, payload = run_cli(
            capsys,
            "capture-evidence",
            "--database-url",
            database_url,
            "--redis-url",
            REDIS_URL or "unused://no-redis",
            "--expected-fingerprint",
            DEFAULT_SEEDED_POLICY_FINGERPRINT,
            "--apply",
        )
        assert exit_code != 0
        assert payload is not None
        assert payload["status"] == "error"
        assert payload["error_code"] == "BACKUP_CHECKPOINT_MISSING"


def test_capture_evidence_rejects_expanded_phase(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """operator 路径不允许跳步：EXPANDED 直接 capture → PHASE_OUT_OF_ORDER。"""
    with cutover_scratch_database("flow") as (_params, database_url):
        exit_code, payload = run_cli(
            capsys,
            "capture-evidence",
            "--database-url",
            database_url,
            "--redis-url",
            REDIS_URL or "unused://no-redis",
            "--expected-fingerprint",
            VALID_POLICY_FINGERPRINT,
            "--backup-checkpoint",
            CHECKPOINT_CANARY,
            "--apply",
        )
        assert exit_code != 0
        assert payload is not None
        assert payload["error_code"] == "PHASE_OUT_OF_ORDER"


def test_capture_evidence_rejects_policy_drift(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """evidence 捕获前 config policy 与 gate 指纹漂移 → 拒绝。

    具体闭码由实现裁定（POLICY_DRIFT 或 POLICY_FROZEN 二选一），此处按
    契约允许集合断言；实现确定后应收敛为字面量。
    """
    with cutover_scratch_database("flow") as (params, database_url):
        stage_gate_phase(
            params,
            phase="POLICY_PREPARED",
            policy_revision=2,
            policy_fingerprint=DEFAULT_SEEDED_POLICY_FINGERPRINT,
        )
        drift_policy = {"mode": "whitelist", "group_ids": [42]}
        overwrite_admission_policy(params, drift_policy)

        exit_code, payload = run_cli(
            capsys,
            "capture-evidence",
            "--database-url",
            database_url,
            "--redis-url",
            REDIS_URL or "unused://no-redis",
            "--expected-fingerprint",
            DEFAULT_SEEDED_POLICY_FINGERPRINT,
            "--backup-checkpoint",
            CHECKPOINT_CANARY,
            "--apply",
        )
        assert exit_code != 0
        assert payload is not None
        assert payload["status"] == "error"
        assert payload["error_code"] in ("POLICY_DRIFT", "POLICY_FROZEN")


@SKIP_NO_REDIS
def test_capture_evidence_idempotent_rerun(
    capsys: pytest.CaptureFixture[str],
    tmp_path: Any,
) -> None:
    """同状态重复 capture 为 no-op：快照字段不变（phase_updated_at 除外）。"""
    with cutover_scratch_database("flow") as (params, database_url):
        policy_file = write_policy_file(tmp_path, VALID_POLICY)
        code, _ = run_cli(capsys, *_prepared_argv(database_url, policy_file))
        assert code == 0
        argv = [
            "capture-evidence",
            "--database-url",
            database_url,
            "--redis-url",
            REDIS_URL,
            "--expected-fingerprint",
            VALID_POLICY_FINGERPRINT,
            "--backup-checkpoint",
            CHECKPOINT_CANARY,
            "--apply",
        ]
        first_code, _ = run_cli(capsys, *argv)
        assert first_code == 0
        snapshot_first = fetch_gate_row(params)

        second_code, second_payload = run_cli(capsys, *argv)
        assert second_code == 0
        assert second_payload is not None
        assert second_payload["status"] == "ok"

        snapshot_second = fetch_gate_row(params)
        assert snapshot_second is not None
        assert snapshot_first is not None
        for column in (
            "phase",
            "policy_revision",
            "policy_fingerprint",
            "backup_checkpoint",
            "redis_evidence_digest",
        ):
            assert snapshot_second[column] == snapshot_first[column]


def test_capture_evidence_reports_redis_unavailable(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Redis 不可达时快速失败 REDIS_UNAVAILABLE，而非挂死或裸 traceback。"""
    with cutover_scratch_database("flow") as (params, database_url):
        stage_gate_phase(
            params,
            phase="POLICY_PREPARED",
            policy_revision=2,
            policy_fingerprint=DEFAULT_SEEDED_POLICY_FINGERPRINT,
        )
        exit_code, payload = run_cli(
            capsys,
            "capture-evidence",
            "--database-url",
            database_url,
            "--redis-url",
            "redis://127.0.0.1:1/0",
            "--expected-fingerprint",
            DEFAULT_SEEDED_POLICY_FINGERPRINT,
            "--backup-checkpoint",
            CHECKPOINT_CANARY,
            "--apply",
        )
        assert exit_code != 0
        assert payload is not None
        assert payload["status"] == "error"
        assert payload["error_code"] == "REDIS_UNAVAILABLE"
        gate = fetch_gate_row(params)
        assert gate is not None
        assert gate["phase"] == "POLICY_PREPARED"


# ---------------------------------------------------------------------------
# finalize-redis（仅骨架面：相位前置与参数校验）
# ---------------------------------------------------------------------------


def test_finalize_redis_requires_postgres_backfilled_phase(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """phase 未到 POSTGRES_BACKFILLED → finalize-redis 拒绝 PHASE_OUT_OF_ORDER。"""
    with cutover_scratch_database("flow") as (params, database_url):
        stage_gate_phase(
            params,
            phase="REDIS_EVIDENCE_CAPTURED",
            policy_revision=3,
            policy_fingerprint=DEFAULT_SEEDED_POLICY_FINGERPRINT,
            backup_checkpoint=CHECKPOINT_CANARY,
            redis_evidence_digest=EVIDENCE_DIGEST_CANARY,
        )
        exit_code, payload = run_cli(
            capsys,
            "finalize-redis",
            "--database-url",
            database_url,
            "--redis-url",
            REDIS_URL or "unused://no-redis",
            "--expected-fingerprint",
            DEFAULT_SEEDED_POLICY_FINGERPRINT,
            "--apply",
        )
        assert exit_code != 0
        assert payload is not None
        assert payload["status"] == "error"
        assert payload["error_code"] == "PHASE_OUT_OF_ORDER"

        gate = fetch_gate_row(params)
        assert gate is not None
        assert gate["phase"] == "REDIS_EVIDENCE_CAPTURED"


def test_finalize_redis_validates_required_arguments(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """缺 --expected-fingerprint 时参数校验生效（非零退出）。"""
    with cutover_scratch_database("flow") as (_params, database_url):
        exit_code, _payload = run_cli(
            capsys,
            "finalize-redis",
            "--database-url",
            database_url,
            "--apply",
        )
        assert exit_code != 0


# ---------------------------------------------------------------------------
# abort-pre-backfill
# ---------------------------------------------------------------------------

_ABORT_BASE_ARGS = ("abort-pre-backfill", "--apply")


def test_abort_pre_backfill_rolls_back_to_policy_prepared(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """evidence 相位 abort：清 staged evidence、CAS 回 POLICY_PREPARED 且保留策略字段。"""
    with cutover_scratch_database("flow") as (params, database_url):
        stage_gate_phase(
            params,
            phase="REDIS_EVIDENCE_CAPTURED",
            policy_revision=5,
            policy_fingerprint=DEFAULT_SEEDED_POLICY_FINGERPRINT,
            backup_checkpoint=CHECKPOINT_CANARY,
            redis_evidence_digest=EVIDENCE_DIGEST_CANARY,
        )
        exit_code, payload = run_cli(
            capsys,
            *_ABORT_BASE_ARGS,
            "--database-url",
            database_url,
            "--expected-fingerprint",
            DEFAULT_SEEDED_POLICY_FINGERPRINT,
        )
        assert exit_code == 0
        assert payload is not None
        assert payload["command"] == "abort-pre-backfill"
        assert payload["status"] == "ok"

        gate = fetch_gate_row(params)
        assert gate is not None
        assert gate["phase"] == "POLICY_PREPARED"
        assert gate["redis_evidence_digest"] is None
        assert gate["backup_checkpoint"] is None
        assert gate["policy_revision"] == 5
        assert gate["policy_fingerprint"] == DEFAULT_SEEDED_POLICY_FINGERPRINT


def test_abort_pre_backfill_forbidden_on_early_phases(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """更早 phase 无 evidence 可清：EXPANDED 直接 abort → ABORT_FORBIDDEN。"""
    with cutover_scratch_database("flow") as (_params, database_url):
        exit_code, payload = run_cli(
            capsys,
            *_ABORT_BASE_ARGS,
            "--database-url",
            database_url,
            "--expected-fingerprint",
            DEFAULT_SEEDED_POLICY_FINGERPRINT,
        )
        assert exit_code != 0
        assert payload is not None
        assert payload["status"] == "error"
        assert payload["error_code"] == "ABORT_FORBIDDEN"


def test_abort_pre_backfill_forbidden_after_postgres_backfill(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """POSTGRES_BACKFILLED 之后 forward-only：abort → ABORT_FORBIDDEN。"""
    with cutover_scratch_database("flow") as (params, database_url):
        stage_gate_phase(
            params,
            phase="POSTGRES_BACKFILLED",
            policy_revision=6,
            policy_fingerprint=DEFAULT_SEEDED_POLICY_FINGERPRINT,
            redis_finalizer_digest=("a" * 64),
        )
        exit_code, payload = run_cli(
            capsys,
            *_ABORT_BASE_ARGS,
            "--database-url",
            database_url,
            "--expected-fingerprint",
            DEFAULT_SEEDED_POLICY_FINGERPRINT,
        )
        assert exit_code != 0
        assert payload is not None
        assert payload["error_code"] == "ABORT_FORBIDDEN"

        gate = fetch_gate_row(params)
        assert gate is not None
        assert gate["phase"] == "POSTGRES_BACKFILLED"


def test_abort_pre_backfill_repeat_stays_safe(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """abort 幂等语义：重复调用状态不劣化（仍 POLICY_PREPARED、evidence 保持为空）。"""
    with cutover_scratch_database("flow") as (params, database_url):
        stage_gate_phase(
            params,
            phase="REDIS_EVIDENCE_CAPTURED",
            policy_revision=8,
            policy_fingerprint=DEFAULT_SEEDED_POLICY_FINGERPRINT,
            backup_checkpoint=CHECKPOINT_CANARY,
            redis_evidence_digest=EVIDENCE_DIGEST_CANARY,
        )
        argv = [
            *_ABORT_BASE_ARGS,
            "--database-url",
            database_url,
            "--expected-fingerprint",
            DEFAULT_SEEDED_POLICY_FINGERPRINT,
        ]
        first_code, _ = run_cli(capsys, *argv)
        assert first_code == 0

        second_code, second_payload = run_cli(capsys, *argv)
        # 字面量契约：非 evidence 相位 abort 属 ABORT_FORBIDDEN；无论实现选择
        # 幂等 ok 还是 ABORT_FORBIDDEN，终态都必须保持已回退安全态。
        if second_code == 0:
            assert second_payload is not None
            assert second_payload["status"] == "ok"
        else:
            assert second_payload is not None
            assert second_payload["error_code"] == "ABORT_FORBIDDEN"

        gate = fetch_gate_row(params)
        assert gate is not None
        assert gate["phase"] == "POLICY_PREPARED"
        assert gate["redis_evidence_digest"] is None
        assert gate["backup_checkpoint"] is None
        assert gate["policy_revision"] == 8


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------


def test_status_snapshot_on_fresh_database(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """fresh 库 status：gate 全量快照 + alembic_version + 全部聚合计数。"""
    from tests.db.tsk197_gate_support import HEAD_REVISION

    with cutover_scratch_database("flow") as (_params, database_url):
        exit_code, payload = run_cli(
            capsys,
            "status",
            "--database-url",
            database_url,
        )
        assert exit_code == 0
        assert payload is not None
        assert payload["command"] == "status"
        assert payload["status"] == "ok"
        assert find_key(payload, "alembic_version") == HEAD_REVISION
        assert find_key(payload, "phase") == "EXPANDED"
        assert find_key(payload, "is_fresh") is True
        # gate 全量快照字段名完备性：可空字段值可为 null，但键必须存在。
        serialized = json.dumps(payload)
        for field in (
            "policy_revision",
            "policy_fingerprint",
            "backup_checkpoint",
            "redis_evidence_digest",
            "redis_finalizer_digest",
            "phase_updated_at",
        ):
            assert f'"{field}"' in serialized, f"status 快照缺少 {field}"
        updated_at = find_key(payload, "phase_updated_at")
        assert isinstance(updated_at, str)
        datetime.fromisoformat(updated_at)
        for key in OUTBOX_COUNT_KEYS:
            assert find_key(payload, key) == 0
        assert find_key(payload, "processing_count") == 0
        assert find_key(payload, "incomplete_count") == 0


def test_status_reflects_prepare_result(
    capsys: pytest.CaptureFixture[str],
    tmp_path: Any,
) -> None:
    """prepare 之后 status 快照携带 policy_revision/fingerprint。"""
    with cutover_scratch_database("flow") as (params, database_url):
        policy_file = write_policy_file(tmp_path, VALID_POLICY)
        code, _ = run_cli(capsys, *_prepared_argv(database_url, policy_file))
        assert code == 0

        exit_code, payload = run_cli(
            capsys,
            "status",
            "--database-url",
            database_url,
        )
        assert exit_code == 0
        assert payload is not None
        assert find_key(payload, "phase") == "POLICY_PREPARED"
        assert find_key(payload, "policy_fingerprint") == VALID_POLICY_FINGERPRINT
        revision = find_key(payload, "policy_revision")
        assert isinstance(revision, int) and revision >= 2
        gate = fetch_gate_row(params)
        assert gate is not None
        assert gate["policy_revision"] == revision


def test_status_reports_gate_missing(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """gate 行缺失 → status 失败 GATE_MISSING。"""
    with cutover_scratch_database("flow") as (params, database_url):
        delete_gate_rows(params)
        exit_code, payload = run_cli(
            capsys,
            "status",
            "--database-url",
            database_url,
        )
        assert exit_code != 0
        assert payload is not None
        assert payload["status"] == "error"
        assert payload["error_code"] == "GATE_MISSING"
