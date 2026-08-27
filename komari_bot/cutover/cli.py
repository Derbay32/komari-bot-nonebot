"""cutover CLI 公共入口：argparse 编排、JSON 输出面与命令实现。

契约要点（验收锁定）：

- ``main(argv) -> int``；stdout 恒为单行 ``sort_keys`` JSON；成功
  ``status=ok`` 退出 0，失败 ``status=error`` + closed ``error_code``
  退出非零；argparse 参数缺失由其自身以非零退出码报错；
- 所有输出只含 closed code 与聚合 count，绝不出现动态身份（群号/用户/
  名单成员/正文/URL）；
- 写命令持会话级 advisory lock（与 0012/0013 迁移同键），被占时
  ``LOCK_BUSY`` 快速失败，成功结束后锁即时释放；
- phase 只准前进：CAS 乱序/跳步一律 ``PHASE_OUT_OF_ORDER``。
"""

from __future__ import annotations

import asyncio
import json
import sys
from typing import Any

import komari_bot.cutover.gate as gate_store
from komari_bot.admission_policy import (
    PolicyCanonicalizationError,
    canonicalize_policy,
    policy_fingerprint,
)
from komari_bot.cutover import redis_face
from komari_bot.cutover.gate import CutoverCliError

_COMMANDS_WITH_REDIS = ("capture-evidence", "finalize-redis")
_WRITE_COMMANDS = ("prepare-policy", "capture-evidence", "finalize-redis", "abort-pre-backfill")


def _emit(payload: dict[str, Any]) -> None:
    """单行 sort_keys JSON 输出（stdout 唯一出口）。"""
    sys.stdout.write(json.dumps(payload, sort_keys=True) + "\n")


def _ok(command: str, **fields: Any) -> int:
    _emit({"command": command, "status": "ok", **fields})
    return 0


def _fail(command: str, error_code: str, **details: Any) -> int:
    _emit({"command": command, "status": "error", "error_code": error_code, **details})
    return 1


# ---------------------------------------------------------------------------
# policy 文件装载
# ---------------------------------------------------------------------------


def _load_policy_payload(policy_file: str) -> dict[str, Any]:
    """读取并 canonical 化策略文件；任何非法形态统一 POLICY_FILE_INVALID。

    异常消息只含共享包固定文案与 closed code，绝不回显文件内容。
    """
    try:
        import pathlib

        raw = pathlib.Path(policy_file).read_text(encoding="utf-8")
        payload = json.loads(raw)
        return canonicalize_policy(payload)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise CutoverCliError("POLICY_FILE_INVALID", reason=type(error).__name__) from error
    except PolicyCanonicalizationError as error:
        raise CutoverCliError("POLICY_FILE_INVALID") from error


# ---------------------------------------------------------------------------
# 连接编排
# ---------------------------------------------------------------------------


class _Session:
    """一次命令执行的连接编排：可选会话级 advisory lock。"""

    def __init__(self, database_url: str) -> None:
        self._database_url = database_url
        self._connection: Any = None
        self._lock_held = False

    async def connect(self, *, with_lock: bool) -> Any:
        connection = await gate_store.connect(self._database_url)
        self._connection = connection
        if with_lock:
            acquired = await connection.fetchval(
                "SELECT pg_try_advisory_lock($1)", gate_store.CUTOVER_LOCK_KEY
            )
            if not acquired:
                await self.close()
                raise CutoverCliError("LOCK_BUSY")
            self._lock_held = True
        return connection

    async def close(self) -> None:
        if self._connection is not None:
            if self._lock_held:
                await self._connection.execute(
                    "SELECT pg_advisory_unlock($1)", gate_store.CUTOVER_LOCK_KEY
                )
                self._lock_held = False
            await self._connection.close()
            self._connection = None


async def _require_gate(connection: Any) -> dict[str, Any]:
    row = await gate_store.fetch_gate_row(connection)
    if row is None:
        raise CutoverCliError("GATE_MISSING")
    return row


def _require_cas_updated(status: str | None, *, error_code: str) -> None:
    """CAS 必须命中单行更新；未命中以 closed code 拒绝（事务随之回滚）。"""
    if status != "UPDATE 1":
        raise CutoverCliError(error_code)


def _ensure(condition: object, error_code: str, **details: Any) -> None:
    """条件不成立即以 closed code 拒绝；details 只允许聚合 count。"""
    if not condition:
        raise CutoverCliError(error_code, **details)


def _require_gate_phase(gate: dict[str, Any], allowed: tuple[str, ...]) -> str:
    """gate 相位必须在允许闭集内，返回当前相位（否则 PHASE_OUT_OF_ORDER）。"""
    phase = str(gate["phase"])
    if phase not in allowed:
        raise CutoverCliError("PHASE_OUT_OF_ORDER")
    return phase


async def _require_matching_policy(
    connection: Any,
    gate: dict[str, Any],
    expected_fingerprint: str,
) -> tuple[int, str]:
    """校验 operator 期望指纹与库内配置内容一致，返回 (revision, fingerprint)。

    - 期望指纹 ≠ gate 记录 → ``POLICY_FINGERPRINT_MISMATCH``；
    - 配置行缺失或内容指纹漂移 → ``POLICY_MISSING`` / ``POLICY_DRIFT``。
    """
    if gate["policy_fingerprint"] != expected_fingerprint:
        raise CutoverCliError("POLICY_FINGERPRINT_MISMATCH")
    config = await gate_store.fetch_admission_config(connection)
    if config is None:
        raise CutoverCliError("POLICY_MISSING")
    stored_text = config["policy"]
    if isinstance(stored_text, (str, bytes, bytearray)):
        stored_text = str(stored_text)
    else:  # asyncpg 对 JSONB 返回字符串；防御其他驱动形态
        stored_text = json.dumps(stored_text)
    try:
        stored_fingerprint = gate_store.stored_policy_fingerprint(stored_text)
    except (json.JSONDecodeError, PolicyCanonicalizationError, ValueError) as error:
        raise CutoverCliError("POLICY_DRIFT") from error
    if stored_fingerprint != gate["policy_fingerprint"]:
        raise CutoverCliError("POLICY_DRIFT")
    revision = config["revision"]
    return int(revision), str(gate["policy_fingerprint"])


# ---------------------------------------------------------------------------
# audit / status（只读）
# ---------------------------------------------------------------------------


#: audit 聚合的八个 legacy 资源闭集（与验收支持层同一闭集）。
LEGACY_RESOURCES = (
    "komari_knowledge",
    "komari_search",
    "komari_help",
    "group_history_summary",
    "komari_decision",
    "sr",
    "komari_memory",
    "komari_custom",
)


async def _run_audit(args: Any) -> int:
    command = "audit"
    try:
        canonical = _load_policy_payload(args.policy_file)
    except CutoverCliError as error:
        return _fail(command, error.error_code)

    connection = await gate_store.connect(args.database_url)
    try:
        projection = await gate_store.aggregate_projection(connection)
        legacy_rows = await gate_store.fetch_legacy_config_rows(connection)
    finally:
        await connection.close()

    return _ok(
        command,
        policy={"valid": True, "fingerprint": policy_fingerprint(canonical)},
        legacy_resources=_legacy_resource_entries(legacy_rows),
        **projection,
    )


def _legacy_resource_entries(rows: dict[str, Any]) -> list[dict[str, Any]]:
    """八资源闭集逐项投影：present/entry_count/valid_entry_count/fingerprint。

    只输出聚合计数与 canonical 指纹，名单本身绝不出现；
    user_entry_count 仅在存在有效用户条目时附加（同样只含 count）。
    空名单资源输出空名单 canonical 指纹；有名单但无任何合法子集时无可
    canonical 化对象，不输出指纹。
    """
    entries: list[dict[str, Any]] = []
    for resource in LEGACY_RESOURCES:
        data = rows.get(resource)
        if not isinstance(data, dict):
            entries.append(
                {
                    "resource": resource,
                    "present": False,
                    "entry_count": 0,
                    "valid_entry_count": 0,
                    "fingerprint": None,
                }
            )
            continue
        group_raw = data.get("group_whitelist")
        group_list = group_raw if isinstance(group_raw, list) else []
        valid_ids = [
            element for element in group_list if type(element) is int and element > 0
        ]
        users_raw = data.get("user_whitelist")
        user_list = users_raw if isinstance(users_raw, list) else []
        valid_users = [
            element
            for element in user_list
            if (isinstance(element, str) and bool(element))
            or (type(element) is int and element > 0)
        ]
        if valid_ids:
            fingerprint = gate_store.canonical_list_fingerprint(valid_ids)
        elif not group_list:
            fingerprint = gate_store.canonical_list_fingerprint([])
        else:
            fingerprint = None
        entry: dict[str, Any] = {
            "resource": resource,
            "present": True,
            "entry_count": len(group_list),
            "valid_entry_count": len(valid_ids),
            "fingerprint": fingerprint,
        }
        if valid_users:
            entry["user_entry_count"] = len(valid_users)
        entries.append(entry)
    return entries


async def _run_status(args: Any) -> int:
    command = "status"
    connection = await gate_store.connect(args.database_url)
    try:
        try:
            gate = await _require_gate(connection)
        except CutoverCliError as error:
            return _fail(command, error.error_code)
        version_row = await connection.fetchval(
            "SELECT version_num FROM alembic_version"
        )
        projection = await gate_store.aggregate_projection(connection)
    finally:
        await connection.close()

    snapshot = {column: gate[column] for column in gate_store.GATE_COLUMNS}
    snapshot.pop("id")
    updated_at = snapshot.get("phase_updated_at")
    if updated_at is not None:
        snapshot["phase_updated_at"] = updated_at.isoformat()
    return _ok(
        command,
        alembic_version=str(version_row),
        **snapshot,
        **projection,
    )


# ---------------------------------------------------------------------------
# prepare-policy
# ---------------------------------------------------------------------------


async def _run_prepare_policy(args: Any) -> int:
    command = "prepare-policy"
    try:
        canonical = _load_policy_payload(args.policy_file)
    except CutoverCliError as error:
        return _fail(command, error.error_code)

    fingerprint = policy_fingerprint(canonical)
    if fingerprint != args.expected_fingerprint:
        return _fail(command, "POLICY_FINGERPRINT_MISMATCH")

    session = _Session(args.database_url)
    apply_mode = bool(args.apply)
    try:
        connection = await session.connect(with_lock=apply_mode)
        gate = await _require_gate(connection)
        phase = _require_gate_phase(gate, ("EXPANDED", "POLICY_PREPARED"))
        if phase == "POLICY_PREPARED" and gate["policy_fingerprint"] == fingerprint:
            revision = gate["policy_revision"]
            return _ok(
                command,
                phase=phase,
                policy_revision=int(revision) if revision is not None else None,
                fingerprint=fingerprint,
                applied=False,
                idempotent=True,
            )
        if not apply_mode:
            return _ok(
                command,
                phase="POLICY_PREPARED",
                fingerprint=fingerprint,
                applied=False,
                idempotent=False,
            )

        async with connection.transaction():
            config_revision = await connection.fetchval(
                f"INSERT INTO {gate_store.CONFIG_TABLE} (id, revision, updated_at, policy)"
                " VALUES (1, 1, NOW(), CAST($1 AS JSONB))"
                " ON CONFLICT (id) DO UPDATE"
                " SET revision = {t}.revision + 1, updated_at = NOW(),"
                "     policy = EXCLUDED.policy"
                " RETURNING revision".format(t=gate_store.CONFIG_TABLE),
                json.dumps(canonical, sort_keys=True, separators=(",", ":")),
            )
            cas = await connection.execute(
                f"UPDATE {gate_store.GATE_TABLE}"
                " SET phase = 'POLICY_PREPARED', policy_revision = $1,"
                "     policy_fingerprint = $2, phase_updated_at = NOW()"
                " WHERE id = 1 AND phase IN ('EXPANDED', 'POLICY_PREPARED')",
                int(config_revision),
                fingerprint,
            )
            _require_cas_updated(cas, error_code="PHASE_OUT_OF_ORDER")
        return _ok(
            command,
            phase="POLICY_PREPARED",
            policy_revision=int(config_revision),
            fingerprint=fingerprint,
            applied=True,
            idempotent=False,
        )
    except CutoverCliError as error:
        return _fail(command, error.error_code)
    finally:
        await session.close()


# ---------------------------------------------------------------------------
# capture-evidence
# ---------------------------------------------------------------------------


async def _run_capture_evidence(args: Any) -> int:
    command = "capture-evidence"
    session = _Session(args.database_url)
    client: Any = None
    try:
        connection = await session.connect(with_lock=bool(args.apply))
        gate = await _require_gate(connection)
        phase = _require_gate_phase(
            gate, ("POLICY_PREPARED", "REDIS_EVIDENCE_CAPTURED")
        )
        await _require_matching_policy(connection, gate, args.expected_fingerprint)
        _ensure(args.backup_checkpoint, "BACKUP_CHECKPOINT_MISSING")

        try:
            client = await redis_face.connect_redis(args.redis_url)
            await client.ping()
            entries = await redis_face.scan_evidence_entries(client)
        except Exception as error:
            raise CutoverCliError("REDIS_UNAVAILABLE") from error
        finally:
            if client is not None:
                await client.aclose()
                client = None

        evidence_digest = redis_face.evidence_aggregate_digest(entries)

        if phase == "REDIS_EVIDENCE_CAPTURED":
            # 重入校验：活体证据必须与已暂存摘要逐字节一致。不一致说明
            # 暂存证据已失效或被绕过写入——作废 staged evidence 并回退
            # 到 POLICY_PREPARED，再以 PHASE_OUT_OF_ORDER 拒绝本次捕获。
            evidence_consistent = evidence_digest == gate["redis_evidence_digest"]
            if not evidence_consistent:
                async with connection.transaction():
                    await connection.execute(
                        f"UPDATE {gate_store.GATE_TABLE}"
                        " SET phase = 'POLICY_PREPARED',"
                        "     redis_evidence_digest = NULL,"
                        "     backup_checkpoint = NULL, phase_updated_at = NOW()"
                        " WHERE id = 1 AND phase = 'REDIS_EVIDENCE_CAPTURED'"
                    )
            _ensure(evidence_consistent, "PHASE_OUT_OF_ORDER", invalidated=True)
            async with connection.transaction():
                cas = await connection.execute(
                    f"UPDATE {gate_store.GATE_TABLE}"
                    " SET phase = 'REDIS_EVIDENCE_CAPTURED', backup_checkpoint = $1,"
                    "     redis_evidence_digest = $2, phase_updated_at = NOW()"
                    " WHERE id = 1 AND phase = 'REDIS_EVIDENCE_CAPTURED'",
                    args.backup_checkpoint,
                    evidence_digest,
                )
                _require_cas_updated(cas, error_code="PHASE_OUT_OF_ORDER")
            return _ok(
                command,
                phase="REDIS_EVIDENCE_CAPTURED",
                backup_checkpoint=args.backup_checkpoint,
                redis_evidence_digest=evidence_digest,
                scanned_entry_count=len(entries),
                idempotent=True,
            )

        async with connection.transaction():
            cas = await connection.execute(
                f"UPDATE {gate_store.GATE_TABLE}"
                " SET phase = 'REDIS_EVIDENCE_CAPTURED', backup_checkpoint = $1,"
                "     redis_evidence_digest = $2, phase_updated_at = NOW()"
                " WHERE id = 1 AND phase = 'POLICY_PREPARED'",
                args.backup_checkpoint,
                evidence_digest,
            )
            _require_cas_updated(cas, error_code="PHASE_OUT_OF_ORDER")
        return _ok(
            command,
            phase="REDIS_EVIDENCE_CAPTURED",
            backup_checkpoint=args.backup_checkpoint,
            redis_evidence_digest=evidence_digest,
            scanned_entry_count=len(entries),
        )
    except CutoverCliError as error:
        return _fail(command, error.error_code, **error.details)
    finally:
        if client is not None:
            await client.aclose()
        await session.close()


# ---------------------------------------------------------------------------
# finalize-redis
# ---------------------------------------------------------------------------


async def _run_finalize_redis(args: Any) -> int:
    command = "finalize-redis"
    session = _Session(args.database_url)
    client: Any = None
    released_count = 0
    quarantined_count = 0
    dormant_count = 0
    try:
        connection = await session.connect(with_lock=bool(args.apply))
        gate = await _require_gate(connection)
        phase = str(gate["phase"])

        if phase == "REDIS_FINALIZED":
            return _ok(command, phase=phase, idempotent=True)
        _require_gate_phase(gate, ("POSTGRES_BACKFILLED",))
        await _require_matching_policy(connection, gate, args.expected_fingerprint)

        certified_digest = gate["redis_finalizer_digest"]
        try:
            client = await redis_face.connect_redis(args.redis_url)
            await client.ping()
        except Exception as error:
            raise CutoverCliError("REDIS_UNAVAILABLE") from error

        if certified_digest is not None:
            # 重入校验模式：认证后再次进入说明此前认证被回退——active 面
            # 出现任何残留都属 cutover 后新写入，必须拒绝而非静默处置。
            active_residual = await redis_face.count_active_legacy_keys(client)
            _ensure(active_residual == 0, "LEGACY_ACTIVE_NONZERO",
                    residual_count=active_residual)
        else:
            released_count = await _release_cleanup_ledger(connection, client)
            quarantined_count, dormant_count = await _dispose_legacy_families(
                connection, client, gate
            )
            active_residual = await redis_face.count_active_legacy_keys(client)
            _ensure(active_residual == 0, "LEGACY_ACTIVE_NONZERO",
                    residual_count=active_residual)

        attestation_digest = await _attest_quarantine_ledger(connection)
        async with connection.transaction():
            cas = await connection.execute(
                f"UPDATE {gate_store.GATE_TABLE}"
                " SET phase = 'REDIS_FINALIZED', redis_finalizer_digest = $1,"
                "     phase_updated_at = NOW()"
                " WHERE id = 1 AND phase = 'POSTGRES_BACKFILLED'",
                attestation_digest,
            )
            _require_cas_updated(cas, error_code="PHASE_OUT_OF_ORDER")
        return _ok(
            command,
            phase="REDIS_FINALIZED",
            redis_finalizer_digest=attestation_digest,
            released_count=released_count,
            quarantined_count=quarantined_count,
            dormant_count=dormant_count,
        )
    except CutoverCliError as error:
        return _fail(command, error.error_code, **error.details)
    finally:
        if client is not None:
            await client.aclose()
        await session.close()


async def _release_cleanup_ledger(connection: Any, client: Any) -> int:
    """逐条释放未完成的 cleanup ledger 条目并落 release_finished_at。"""
    rows = await connection.fetch(
        "SELECT reservation_id, group_id FROM"
        " komari_reply_reservation_cleanup_ledger"
        " WHERE release_finished_at IS NULL ORDER BY reservation_id"
    )
    released = 0
    for row in rows:
        await redis_face.release_reservation(
            client,
            group_id=int(row["group_id"]),
            reservation_id=str(row["reservation_id"]),
        )
        marked = await connection.fetchval(
            "UPDATE komari_reply_reservation_cleanup_ledger"
            " SET release_finished_at = NOW()"
            " WHERE reservation_id = $1 AND release_finished_at IS NULL"
            " RETURNING 1",
            str(row["reservation_id"]),
        )
        if marked is not None:
            released += 1
    return released


async def _dispose_legacy_families(
    connection: Any,
    client: Any,
    gate: dict[str, Any],
) -> tuple[int, int]:
    """按处置计划执行隔离/休眠并写 quarantine 底册，返回 (隔离数, 休眠数)。"""
    plan = await redis_face.build_finalize_plan(client)
    for key in plan.quarantine_keys:
        await redis_face.execute_quarantine(client, key)
        digest = redis_face.payload_digest(key, plan.serialized_values[key])
        await connection.execute(
            "INSERT INTO komari_admission_quarantine_ledger (key_family, payload_digest)"
            " VALUES ($1, $2)",
            key,
            digest,
        )
    for key in plan.dormancy_keys:
        await redis_face.execute_dormancy(
            client, key, policy_revision=gate["policy_revision"]
        )
    return plan.quarantined_count, plan.dormant_count


async def _attest_quarantine_ledger(connection: Any) -> str:
    rows = await connection.fetch(
        "SELECT key_family, payload_digest FROM komari_admission_quarantine_ledger"
        " ORDER BY entry_id"
    )
    return redis_face.aggregate_attestation_digest(
        [(str(row["key_family"]), str(row["payload_digest"])) for row in rows]
    )


# ---------------------------------------------------------------------------
# abort-pre-backfill
# ---------------------------------------------------------------------------


async def _run_abort_pre_backfill(args: Any) -> int:
    command = "abort-pre-backfill"
    session = _Session(args.database_url)
    try:
        connection = await session.connect(with_lock=bool(args.apply))
        gate = await _require_gate(connection)
        # 策略尚未暂存（gate 指纹为 NULL）时无从比对期望指纹——直接落到
        # 相位判定，让 EXPANDED 等早期相位命中 ABORT_FORBIDDEN 闭码。
        if (
            gate["policy_fingerprint"] is not None
            and gate["policy_fingerprint"] != args.expected_fingerprint
        ):
            return _fail(command, "POLICY_FINGERPRINT_MISMATCH")
        if str(gate["phase"]) != "REDIS_EVIDENCE_CAPTURED":
            return _fail(command, "ABORT_FORBIDDEN")
        async with connection.transaction():
            cas = await connection.execute(
                f"UPDATE {gate_store.GATE_TABLE}"
                " SET phase = 'POLICY_PREPARED', redis_evidence_digest = NULL,"
                "     backup_checkpoint = NULL, phase_updated_at = NOW()"
                " WHERE id = 1 AND phase = 'REDIS_EVIDENCE_CAPTURED'"
            )
            _require_cas_updated(cas, error_code="ABORT_FORBIDDEN")
        return _ok(command, phase="POLICY_PREPARED", applied=True)
    except CutoverCliError as error:
        return _fail(command, error.error_code)
    finally:
        await session.close()


# ---------------------------------------------------------------------------
# argparse 编排与入口
# ---------------------------------------------------------------------------


def _build_parser() -> Any:
    import argparse
    import os

    parser = argparse.ArgumentParser(prog="komari-bot-cutover")
    subparsers = parser.add_subparsers(dest="command", required=True)

    #: 显式 URL 优先；缺省回落环境变量（与运行时共享同一配置面）。
    database_url_default = os.environ.get("SQLALCHEMY_DATABASE_URL")
    redis_url_default = os.environ.get("KOMARI_REDIS_URL")

    audit = subparsers.add_parser("audit")
    audit.add_argument("--database-url", default=database_url_default)
    audit.add_argument("--policy-file", required=True)

    status = subparsers.add_parser("status")
    status.add_argument("--database-url", default=database_url_default)
    status.add_argument("--policy-file", default=None)

    prepare = subparsers.add_parser("prepare-policy")
    prepare.add_argument("--database-url", default=database_url_default)
    prepare.add_argument("--policy-file", required=True)
    prepare.add_argument("--expected-fingerprint", required=True)
    prepare.add_argument("--apply", action="store_true")

    capture = subparsers.add_parser("capture-evidence")
    capture.add_argument("--database-url", default=database_url_default)
    capture.add_argument("--redis-url", default=redis_url_default)
    capture.add_argument("--expected-fingerprint", required=True)
    capture.add_argument("--backup-checkpoint", default=None)
    capture.add_argument("--apply", action="store_true")

    finalize = subparsers.add_parser("finalize-redis")
    finalize.add_argument("--database-url", default=database_url_default)
    finalize.add_argument("--redis-url", default=redis_url_default)
    finalize.add_argument("--expected-fingerprint", required=True)
    finalize.add_argument("--apply", action="store_true")

    abort = subparsers.add_parser("abort-pre-backfill")
    abort.add_argument("--database-url", default=database_url_default)
    abort.add_argument("--expected-fingerprint", required=True)
    abort.add_argument("--apply", action="store_true")

    return parser


_HANDLERS = {
    "audit": _run_audit,
    "status": _run_status,
    "prepare-policy": _run_prepare_policy,
    "capture-evidence": _run_capture_evidence,
    "finalize-redis": _run_finalize_redis,
    "abort-pre-backfill": _run_abort_pre_backfill,
}

assert set(_HANDLERS) >= set(_WRITE_COMMANDS) | set(_COMMANDS_WITH_REDIS)


def main(argv: list[str]) -> int:
    """CLI 公共入口：返回进程退出码（argparse 错误透传非零码）。"""
    parser = _build_parser()
    try:
        args = parser.parse_args(list(argv))
    except SystemExit as exc:  # argparse 参数错误：非零退出且无需 JSON
        code = exc.code
        return 2 if code is None else int(code)
    command = str(args.command)
    if getattr(args, "database_url", None) is None:
        # 显式与环境变量均未提供 DSN：门控库无从定位，统一 closed code 失败。
        return _fail(command, "GATE_MISSING")
    handler = _HANDLERS[command]
    return int(asyncio.run(handler(args)))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main(sys.argv[1:]))
