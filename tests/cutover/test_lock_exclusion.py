"""TSK-232 轮 B —— cutover 写命令 advisory lock 排他与快速失败红基线。

锁定锁契约：所有写命令与 0012/0013 迁移共用同一稳定 advisory lock 键
（``int.from_bytes(blake2b(b"komari_group_admission:cutover-gate",
digest_size=8), signed=True)``）；CLI 写命令以会话级 ``pg_advisory_lock``
持有（命令生命周期），锁被占用时必须以 closed code ``LOCK_BUSY`` 快速失败，
绝不永久阻塞；命令成功结束后锁立即释放。

测试方法：后台线程持独立 asyncpg 连接占用同一把会话级锁，驱动
``komari_bot.cutover.cli.main(argv)`` 真实入口并施加硬超时护栏（超时即判
失败，防止实现阻塞导致测试挂死）。红基线失败原因 = CLI 模块不存在。
隔离库 = 门控库名 + ``_tsk232b1lock``，用例结束 DROP。

注：迁移侧 ``pg_advisory_xact_lock`` 共享键的验证属迁移链数据面验收范围，
本文件不覆盖（xact 语义为等待而非快速失败，无法以 LOCK_BUSY 断言）。
"""

from __future__ import annotations

import asyncio
import importlib
import json
import threading
import time
from typing import TYPE_CHECKING, Any

from tests.cutover.support import (
    CUTOVER_LOCK_KEY,
    SKIP_NO_POSTGRES,
    VALID_POLICY,
    VALID_POLICY_FINGERPRINT,
    cutover_scratch_database,
    fetch_gate_row,
)
from tests.db.tsk197_gate_support import parse_dsn

if TYPE_CHECKING:
    import pytest

pytestmark = [SKIP_NO_POSTGRES]

#: LOCK_BUSY 必须在此时间内返回（含 PG 连接建立开销的宽裕上界）。
FAIL_FAST_TIMEOUT_SECONDS = 30.0


class _LockHolder:
    """在独立线程/连接上持有会话级 advisory lock 的测试夹具。"""

    def __init__(self, params: dict[str, Any]) -> None:
        self._params = params
        self.ready = threading.Event()
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        import asyncpg

        async def _hold() -> None:
            connection = await asyncpg.connect(**self._params)
            try:
                await connection.execute(
                    "SELECT pg_advisory_lock($1)", CUTOVER_LOCK_KEY
                )
                self.ready.set()
                # 阻塞本线程直到主线程放行（Event.wait 为同步调用）。
                self.stop.wait()
                await connection.execute(
                    "SELECT pg_advisory_unlock($1)", CUTOVER_LOCK_KEY
                )
            finally:
                await connection.close()

        asyncio.run(_hold())

    def __enter__(self) -> "_LockHolder":
        self.thread.start()
        assert self.ready.wait(15), "锁持有线程未能就绪"
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.stop.set()
        self.thread.join(timeout=15)


def _run_cli_with_timeout(
    capsys: pytest.CaptureFixture[str],
    timeout_seconds: float,
    *args: str,
) -> tuple[int, dict[str, Any] | None]:
    """带硬超时护栏地驱动 main(argv)；命令挂死即测试失败。"""
    cli_main = importlib.import_module("komari_bot.cutover.cli").main

    outcome: dict[str, Any] = {}

    def _target() -> None:
        try:
            outcome["exit_code"] = cli_main(list(args))
        except BaseException as error:
            outcome["error"] = error

    worker = threading.Thread(target=_target, daemon=True)
    started = time.monotonic()
    worker.start()
    worker.join(timeout=timeout_seconds)
    elapsed = time.monotonic() - started
    assert not worker.is_alive(), (
        f"写命令未在 {timeout_seconds}s 内返回（耗时 {elapsed:.1f}s）——"
        "LOCK_BUSY 快速失败缺失"
    )
    if "error" in outcome:
        raise outcome["error"]
    assert "exit_code" in outcome, "CLI 线程未返回退出码"
    exit_code = int(outcome["exit_code"])
    captured = capsys.readouterr()
    stdout = captured.out.strip()
    payload = json.loads(stdout) if stdout else None
    return exit_code, payload


async def _try_advisory_lock(params: dict[str, Any]) -> bool:
    """外部连接 try-acquire 共享键；成功即证明 CLI 已释放会话锁。"""
    import asyncpg

    connection = await asyncpg.connect(**params)
    try:
        acquired: bool = await connection.fetchval(
            "SELECT pg_try_advisory_lock($1)", CUTOVER_LOCK_KEY
        )
        if acquired:
            await connection.execute("SELECT pg_advisory_unlock($1)", CUTOVER_LOCK_KEY)
        return acquired
    finally:
        await connection.close()


def test_write_command_fails_fast_with_lock_busy_then_succeeds(
    capsys: pytest.CaptureFixture[str],
    tmp_path: Any,
) -> None:
    """锁被占 → LOCK_BUSY 快速失败且状态不变；释放后成功且锁即时归还。"""
    policy_file = tmp_path / "policy.json"
    policy_file.write_text(json.dumps(VALID_POLICY), encoding="utf-8")

    with cutover_scratch_database("lock") as (params, database_url):
        argv = [
            "prepare-policy",
            "--database-url",
            database_url,
            "--policy-file",
            str(policy_file),
            "--expected-fingerprint",
            VALID_POLICY_FINGERPRINT,
            "--apply",
        ]
        holder_params = {
            **parse_dsn(_scratch_asyncpg_dsn(database_url)),
        }

        with _LockHolder(holder_params):
            exit_code, payload = _run_cli_with_timeout(
                capsys,
                FAIL_FAST_TIMEOUT_SECONDS,
                *argv,
            )
            assert exit_code != 0
            assert payload is not None
            assert payload["status"] == "error"
            assert payload["error_code"] == "LOCK_BUSY"

            gate = fetch_gate_row(params)
            assert gate is not None
            assert gate["phase"] == "EXPANDED"
            assert gate["policy_revision"] is None

        # 锁释放后同一命令必须成功（排除 LOCK_BUSY 是永久性拒绝的假阳性）。
        cli_main = importlib.import_module("komari_bot.cutover.cli").main
        retry_exit = int(cli_main(argv))
        assert retry_exit == 0

        # 成功路径结束时会话级锁已释放：外部连接可立即 try-acquire 同一键。
        acquired = asyncio.run(_try_advisory_lock(params))
        assert acquired is True


def _scratch_asyncpg_dsn(database_url: str) -> str:
    """把 SQLAlchemy 风格 DSN 剥成 asyncpg 直连 DSN。"""
    return database_url.replace("postgresql+asyncpg://", "postgresql://")
