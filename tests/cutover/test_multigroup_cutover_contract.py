"""TSK-247 —— 多群号 operator cutover 指纹契约验收（门控 PG + Redis）。

锁定统一策略 canonical/指纹契约在真实 cutover 链上的行为：

- AC1：至少两个不同群号的乱序/重复输入经 ``prepare-policy`` 规范化存储
  后可通过 0013 coordinated contract 终检（单事务内对存储 canonical 字节
  重算 digest 与 gate 指纹一致）。修复前红基线：旧实现 CLI 共享指纹取去重
  降序形态，与存储升序 canonical 形态分叉 → 0013 以
  ``POLICY_FINGERPRINT_DRIFT`` 阻断合法多群号升级（红）；当前测试用于防止
  该缺陷回归；
- AC4：真实持久策略或 gate 指纹篡改仍以 ``POLICY_FINGERPRINT_DRIFT``
  closed code 阻断，错误面无群号/正文（回归锁定，修复后仍必须阻断）。

全部用例从 ``komari_bot.cutover.cli.main(argv)`` 公共入口驱动真实生产
代码；policy 落库与 gate 指纹由真实 ``prepare-policy`` 产生（不再用非
canonical 顺序直写掩盖 operator 路径，AC5）。隔离库 = 门控库名 +
``_tsk232b1mgc<tag>``，用例结束 DROP；Redis 使用测试专用逻辑库（db 15）
并在前后清理前缀族。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit, urlunsplit

from komari_bot.admission_policy import policy_fingerprint
from tests.cutover.support import (
    CANARY_GROUP_A,
    CANARY_GROUP_B,
    CANARY_GROUP_C,
    MULTIGROUP_CANONICAL_POLICY,
    MULTIGROUP_RAW_POLICY,
    REDIS_URL,
    SKIP_NO_POSTGRES,
    SKIP_NO_REDIS,
    cutover_scratch_database,
    fetch_admission_config,
    fetch_gate_row,
    run_bootstrap,
    run_cli,
    stage_gate_phase,
    write_policy_file,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

    import pytest

pytestmark = [SKIP_NO_POSTGRES]

_REDIS_TEST_DB = 15
_GATE_TABLE = "komari_group_admission_gate"
_CLEANUP_KEY_PREFIXES = ("komari_chat:proactive:", "komari_memory:")


def _redis_test_url() -> str:
    """把门控 Redis DSN 指到测试专用逻辑库（与生产库物理隔离）。"""
    if not REDIS_URL:
        return ""
    parts = urlsplit(REDIS_URL)
    return urlunsplit(
        (parts.scheme, parts.netloc, f"/{_REDIS_TEST_DB}", parts.query, "")
    )


def _redis_run(handler: Any) -> None:
    """在测试逻辑库连接上执行异步 handler（teardown 安全）。"""
    import redis.asyncio as aioredis

    async def _run() -> None:
        client = aioredis.from_url(_redis_test_url(), decode_responses=True)
        try:
            await handler(client)
        finally:
            await client.aclose()

    asyncio.run(_run())


@contextlib.contextmanager
def _redis_guard() -> Iterator[None]:
    """进入前清空测试逻辑库前缀族，退出时再清理一次。"""

    async def _cleanup(client: Any) -> None:
        keys: list[str] = []
        for prefix in _CLEANUP_KEY_PREFIXES:
            keys.extend([key async for key in client.scan_iter(f"{prefix}*")])
        if keys:
            await client.delete(*keys)

    _redis_run(_cleanup)
    try:
        yield
    finally:
        _redis_run(_cleanup)


async def _connect(params: dict[str, Any]) -> Any:
    import asyncpg

    return await asyncpg.connect(**params)


async def _fetch_version(params: dict[str, Any]) -> str:
    connection = await _connect(params)
    try:
        value = await connection.fetchval("SELECT version_num FROM alembic_version")
    finally:
        await connection.close()
    return str(value)


async def _clear_fresh_flag(params: dict[str, Any]) -> None:
    """存量生产库语义：cutover 库不是 fresh 安装。"""
    connection = await _connect(params)
    try:
        await connection.execute(f"UPDATE {_GATE_TABLE} SET is_fresh = FALSE")
    finally:
        await connection.close()


def _prepare_argv(database_url: str, policy_file: str) -> list[str]:
    """真实 prepare-policy：expected 指纹取 CLI 共享指纹（operator 视角）。"""
    return [
        "prepare-policy",
        "--database-url",
        database_url,
        "--policy-file",
        policy_file,
        "--expected-fingerprint",
        policy_fingerprint(MULTIGROUP_RAW_POLICY),
        "--apply",
    ]


def _finalize_argv(database_url: str) -> list[str]:
    return [
        "finalize-redis",
        "--database-url",
        database_url,
        "--redis-url",
        _redis_test_url(),
        "--expected-fingerprint",
        policy_fingerprint(MULTIGROUP_RAW_POLICY),
        "--apply",
    ]


def _upgrade_0012(_params: dict[str, Any], database_url: str) -> None:
    result = run_bootstrap(database_url, "upgrade", "0012")
    assert result.returncode == 0, result.stderr


def _authenticate_via_finalize(
    capsys: pytest.CaptureFixture[str],
    params: dict[str, Any],
    database_url: str,
) -> None:
    """真实 finalize-redis CLI 产出合法 attestation（空 Redis 测试逻辑库）。"""
    exit_code, payload = run_cli(capsys, *_finalize_argv(database_url))
    assert exit_code == 0, payload
    gate = fetch_gate_row(params)
    assert gate is not None
    assert gate["phase"] == "REDIS_FINALIZED"


def _expect_0013_blocked(database_url: str, closed_code: str) -> None:
    """驱动 upgrade 0013 并断言：失败、closed code、错误面无群号/正文。"""
    result = run_bootstrap(database_url, "upgrade", "0013")
    assert result.returncode != 0, f"{closed_code} 未被阻断"
    output = f"{result.stdout}\n{result.stderr}"
    assert closed_code in output, f"缺少 closed code {closed_code}: {output}"
    for canary in (CANARY_GROUP_A, CANARY_GROUP_B, CANARY_GROUP_C):
        assert str(canary) not in output, f"错误面不得出现群号 {canary}: {output}"


@contextlib.contextmanager
def _prepared_multigroup_cutover(
    capsys: pytest.CaptureFixture[str],
    tmp_path: Any,
    tag: str,
) -> Iterator[tuple[dict[str, Any], str]]:
    """真实 prepare-policy 多群号策略并推进到 REDIS_FINALIZED 终态。

    共享 AC1/AC4 编排：0011 停链 → 真实 prepare-policy（canonical 落库 +
    真实 gate 指纹）→ 编排 evidence 相位（保留真实 revision/指纹）→
    0012 → 真实 finalize-redis → REDIS_FINALIZED。
    """
    with cutover_scratch_database(f"mgc{tag}", revision="0011") as (params, database_url):
        asyncio.run(_clear_fresh_flag(params))
        policy_file = write_policy_file(tmp_path, MULTIGROUP_RAW_POLICY)
        exit_code, payload = run_cli(capsys, *_prepare_argv(database_url, policy_file))
        assert exit_code == 0, payload
        gate = fetch_gate_row(params)
        assert gate is not None
        assert gate["phase"] == "POLICY_PREPARED"
        assert gate["policy_fingerprint"] == policy_fingerprint(MULTIGROUP_RAW_POLICY)

        # AC5：落库形态必须是真实 canonical（升序去重），不得是非 canonical。
        config = fetch_admission_config(params)
        assert config is not None
        assert json.loads(str(config["policy"])) == MULTIGROUP_CANONICAL_POLICY

        # evidence 相位编排只改相位与证据摘要，保留真实 policy revision/指纹。
        stage_gate_phase(
            params,
            phase="REDIS_EVIDENCE_CAPTURED",
            policy_revision=gate["policy_revision"],
            policy_fingerprint=gate["policy_fingerprint"],
            redis_evidence_digest="e" * 64,
        )
        _upgrade_0012(params, database_url)
        with _redis_guard():
            _authenticate_via_finalize(capsys, params, database_url)
        yield params, database_url


# ---------------------------------------------------------------------------
# AC1：合法多群号乱序/重复输入必须通过 0013 终检
# ---------------------------------------------------------------------------


@SKIP_NO_REDIS
def test_0013_succeeds_with_multigroup_policy_via_real_prepare_policy(
    capsys: pytest.CaptureFixture[str],
    tmp_path: Any,
) -> None:
    """真实 prepare-policy 规范化存储多群号策略后，0013 终检必须放行。

    修复前红基线：旧实现 CLI 共享指纹对去重降序形态取摘要，而 0013 对存储
    升序 canonical 字节重算 digest → 合法升级被 ``POLICY_FINGERPRINT_DRIFT``
    阻断（红）；当前测试用于防止该缺陷回归。
    """
    with _prepared_multigroup_cutover(capsys, tmp_path, "a1") as (params, database_url):
        result = run_bootstrap(database_url, "upgrade", "head")
        assert result.returncode == 0, (
            f"多群号合法升级必须通过 0013 终检:\n{result.stdout}\n{result.stderr}"
        )
        assert asyncio.run(_fetch_version(params)) == "0020"


# ---------------------------------------------------------------------------
# AC4：真实篡改仍以 POLICY_FINGERPRINT_DRIFT 阻断，错误面无群号/正文
# ---------------------------------------------------------------------------


@SKIP_NO_REDIS
def test_0013_blocks_with_policy_tamper_for_multigroup(
    capsys: pytest.CaptureFixture[str],
    tmp_path: Any,
) -> None:
    """持久策略内容被真实篡改后，0013 仍以 POLICY_FINGERPRINT_DRIFT 阻断。"""
    with _prepared_multigroup_cutover(capsys, tmp_path, "tp") as (params, database_url):
        asyncio.run(_overwrite_policy(params, {"mode": "whitelist", "group_ids": [42]}))
        _expect_0013_blocked(database_url, "POLICY_FINGERPRINT_DRIFT")
        assert asyncio.run(_fetch_version(params)) == "0012"


@SKIP_NO_REDIS
def test_0013_blocks_with_gate_fingerprint_tamper_for_multigroup(
    capsys: pytest.CaptureFixture[str],
    tmp_path: Any,
) -> None:
    """gate 指纹被真实篡改后，0013 仍以 POLICY_FINGERPRINT_DRIFT 阻断。"""
    with _prepared_multigroup_cutover(capsys, tmp_path, "tf") as (params, database_url):
        gate = fetch_gate_row(params)
        assert gate is not None
        stage_gate_phase(
            params,
            phase="REDIS_FINALIZED",
            policy_revision=gate["policy_revision"],
            policy_fingerprint="f" * 64,
        )
        _expect_0013_blocked(database_url, "POLICY_FINGERPRINT_DRIFT")
        assert asyncio.run(_fetch_version(params)) == "0012"


async def _overwrite_policy(params: dict[str, Any], policy: dict[str, Any]) -> None:
    connection = await _connect(params)
    try:
        await connection.execute(
            "UPDATE komari_group_admission_config SET policy = $1 WHERE id = 1",
            json.dumps(policy),
        )
    finally:
        await connection.close()
