"""验证身份登记之后的准入变化与仅请求 Noul 的缓存处置。"""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING, Any, cast
from uuid import uuid4

import httpx2
import pytest

from tests.komari_decision.jev_test_support import (
    DecisionHarness,
    FailingRedis,
    HarnessOptions,
    build_answers,
    create_scratch_database,
    drop_scratch_database,
    make_identity,
    make_message,
    make_request,
    read_dispatch,
    skip_without_services,
    update_config_row,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.asyncio


@pytest.fixture(scope="module")
def decision_database() -> Iterator[str]:
    skip_without_services()
    url = asyncio.run(create_scratch_database(f"_jeveffect{uuid4().hex[:8]}"))
    try:
        yield url
    finally:
        asyncio.run(drop_scratch_database(url))


@pytest.mark.parametrize("summoned", [False, True])
async def test_revocation_during_identity_write_prevents_target_body_io(
    decision_database: str, *, summoned: bool
) -> None:
    admitted = True
    denied_text = "身份登记期间撤权后不得写入的目标正文"
    body_writes: list[object] = []

    class SessionProbe:
        def __init__(self, inner: AsyncSession) -> None:
            self.inner = inner

        async def scalar(self, statement: Any, *args: Any, **kwargs: Any) -> Any:
            nonlocal admitted
            value = await self.inner.scalar(statement, *args, **kwargs)
            if str(statement).startswith("INSERT INTO komari_decision_dispatches"):
                admitted = False
            return value

        def __getattr__(self, name: str) -> Any:
            return getattr(self.inner, name)

    class RedisProbe:
        def __init__(self, inner: Any) -> None:
            self.inner = inner

        async def eval(self, *args: Any, **kwargs: Any) -> Any:
            if denied_text in str(args):
                body_writes.append(args)
            return await self.inner.eval(*args, **kwargs)

        def __getattr__(self, name: str) -> Any:
            return getattr(self.inner, name)

    def wrap(factory: Callable[[], AsyncSession]) -> Callable[[], AsyncSession]:
        return lambda: cast("AsyncSession", SessionProbe(factory()))

    options = HarnessOptions(
        admission=lambda _event: admitted,
        session_factory_wrapper=wrap,
        redis_wrapper=RedisProbe,
    )
    async with DecisionHarness(decision_database, options) as harness:
        assert harness.decision is not None and harness.engine is not None
        identity = make_identity()
        result = await harness.decision.evaluate(
            make_request(
                identity=identity,
                target=make_message(identity=identity, text=denied_text),
                real_platform_at=summoned,
            )
        )
        assert body_writes == []
        assert result.status == "cancelled"
        assert result.reason_code == "admission_revoked"
        assert harness.transport.requests == []
        row = await read_dispatch(harness.engine, identity)
        assert row is not None
        assert row["state"] == "cancelled"
        assert row["slot_held"] is False
        assert row["notification_eligible"] is False


async def test_noul_only_success_persists_cache_disposition_without_invented_choices(
    decision_database: str,
) -> None:
    holders: list[FailingRedis] = []

    def wrap(client: Any) -> FailingRedis:
        wrapper = FailingRedis(client)
        holders.append(wrapper)
        return wrapper

    async def responder(request: httpx2.Request, index: int) -> dict[str, Any]:
        if index == 0:
            holders[0].arms()
        return build_answers(json.loads(request.content)["questions"], summary=0.9)

    options = HarnessOptions(redis_wrapper=wrap, responder=responder)
    async with DecisionHarness(decision_database, options) as harness:
        assert harness.decision is not None and harness.engine is not None
        identity = make_identity()
        result = await harness.decision.evaluate(
            make_request(
                identity=identity, real_platform_at=True, summary_candidate=True
            )
        )
        assert result.status == "success"
        assert result.route == "summary"
        assert result.reply_choice is None
        assert result.retention_choice is None
        assert holders[0].failed_evals == 1
        row = await read_dispatch(harness.engine, identity)
        assert row is not None
        assert row["recent_retained"] is True


async def test_repeated_cancellation_waits_for_owned_finalization(
    decision_database: str,
) -> None:
    admitted = True
    request_started = asyncio.Event()
    finalize_started = asyncio.Event()
    release_finalize = asyncio.Event()
    release_request = asyncio.Event()

    class SessionProbe:
        def __init__(self, inner: AsyncSession) -> None:
            self.inner = inner

        async def execute(self, statement: Any, *args: Any, **kwargs: Any) -> Any:
            result = await self.inner.execute(statement, *args, **kwargs)
            if (
                str(statement).startswith("UPDATE komari_decision_dispatches")
                and statement.compile().params.get("reason_code") == "admission_revoked"
                and statement.compile().params.get("slot_held") is False
                and not finalize_started.is_set()
            ):
                finalize_started.set()
                await release_finalize.wait()
            return result

        def __getattr__(self, name: str) -> Any:
            return getattr(self.inner, name)

    def wrap(factory: Callable[[], AsyncSession]) -> Callable[[], AsyncSession]:
        return lambda: cast("AsyncSession", SessionProbe(factory()))

    async def responder(request: httpx2.Request, _index: int) -> dict[str, Any]:
        request_started.set()
        await release_request.wait()
        return build_answers(json.loads(request.content)["questions"])

    options = HarnessOptions(
        admission=lambda _event: admitted,
        session_factory_wrapper=wrap,
        responder=responder,
    )
    async with DecisionHarness(decision_database, options) as harness:
        assert harness.decision is not None
        pending = asyncio.create_task(harness.decision.evaluate(make_request()))
        closing: asyncio.Task[None] | None = None
        try:
            await asyncio.wait_for(request_started.wait(), timeout=5)
            admitted = False
            await asyncio.wait_for(finalize_started.wait(), timeout=5)
            # 观察器已取消一次；公开 close 再次取消，清理仍停在外部数据库边界。
            closing = asyncio.create_task(harness.decision.close())
            await asyncio.sleep(0.1)
            assert not pending.done()
        finally:
            release_finalize.set()
            release_request.set()
            tasks = [pending] if closing is None else [pending, closing]
            await asyncio.wait_for(
                asyncio.gather(*tasks, return_exceptions=True), timeout=10
            )


@pytest.mark.parametrize("provider_failure", [False, True])
async def test_disable_before_finalization_does_not_confirm_stale_result(
    decision_database: str, *, provider_failure: bool
) -> None:
    sdk_finished = False
    disabled = False
    holders: list[DecisionHarness] = []
    old_text = "已失效判定的正文不能在成功定案后重新加入输入"

    class SessionProbe:
        def __init__(self, inner: AsyncSession) -> None:
            self.inner = inner

        async def execute(self, statement: Any, *args: Any, **kwargs: Any) -> Any:
            nonlocal disabled
            if (
                sdk_finished
                and not disabled
                and "SELECT pg_advisory_xact_lock" in str(statement)
            ):
                disabled = True
                engine = holders[0].engine
                assert engine is not None
                await update_config_row(engine, {"plugin_enable": False})
            return await self.inner.execute(statement, *args, **kwargs)

        def __getattr__(self, name: str) -> Any:
            return getattr(self.inner, name)

    def wrap(factory: Callable[[], AsyncSession]) -> Callable[[], AsyncSession]:
        return lambda: cast("AsyncSession", SessionProbe(factory()))

    async def responder(
        request: httpx2.Request, index: int
    ) -> dict[str, Any] | httpx2.Response:
        nonlocal sdk_finished
        sdk_finished = True
        if provider_failure and index == 0:
            return httpx2.Response(
                503, json={"error": "synthetic unavailable"}, request=request
            )
        return build_answers(json.loads(request.content)["questions"])

    options = HarnessOptions(session_factory_wrapper=wrap, responder=responder)
    async with DecisionHarness(decision_database, options) as harness:
        holders.append(harness)
        assert harness.decision is not None and harness.engine is not None
        identity = make_identity()
        try:
            result = await harness.decision.evaluate(
                make_request(identity=identity, text=old_text)
            )
        except asyncio.CancelledError:
            pass
        else:
            assert result.status in (
                {"failed", "cancelled"} if provider_failure else {"cancelled"}
            )
        assert disabled
        await update_config_row(harness.engine, {"plugin_enable": True})
        follower = make_identity(
            account_id=identity.account_id, group_id=identity.group_id
        )
        result = await harness.decision.evaluate(make_request(identity=follower))
        assert result.status == "success"
        assert old_text not in str(harness.transport.state(1))
