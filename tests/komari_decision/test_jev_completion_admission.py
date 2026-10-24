"""Exercise admission changes at real completion SQL and Redis boundaries."""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING, Any, cast
from uuid import uuid4

import httpx2
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from tests.komari_decision.jev_test_support import (
    DecisionHarness,
    HarnessOptions,
    build_answers,
    create_scratch_database,
    drop_scratch_database,
    make_request,
    read_dispatch,
    skip_without_services,
)
from tests.pg_support import reset_shared_orm_engine

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable, Iterator


pytestmark = pytest.mark.asyncio


@pytest.fixture(scope="module")
def decision_database() -> Iterator[str]:
    skip_without_services()
    url = asyncio.run(create_scratch_database(f"_jevcompletion{uuid4().hex[:8]}"))
    try:
        yield url
    finally:
        asyncio.run(drop_scratch_database(url))


@pytest.fixture(autouse=True)
async def scratch_prompt_storage(
    decision_database: str, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[None]:
    from nonebot import require

    require("nonebot_plugin_orm")
    import nonebot_plugin_orm

    await reset_shared_orm_engine()
    engine = create_async_engine(decision_database)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(nonebot_plugin_orm, "get_session", lambda **_kwargs: sessions())
    try:
        yield
    finally:
        await engine.dispose()
        await reset_shared_orm_engine()


@pytest.mark.parametrize("provider_failure", [False, True])
async def test_revocation_after_final_row_read_never_submits_success(
    decision_database: str, *, provider_failure: bool
) -> None:
    admitted = True
    response_returned = False
    revoked_at_sql = False
    submitted_success: list[dict[str, Any]] = []

    async def responder(
        request: httpx2.Request, _index: int
    ) -> dict[str, Any] | httpx2.Response:
        nonlocal response_returned
        response_returned = True
        if provider_failure:
            return httpx2.Response(503, json={"error": "synthetic outage"}, request=request)
        return build_answers(json.loads(request.content)["questions"])

    class SessionProbe:
        def __init__(self, inner: AsyncSession) -> None:
            self.inner = inner

        async def scalars(self, statement: Any, *args: Any, **kwargs: Any) -> Any:
            nonlocal admitted, revoked_at_sql
            result = await self.inner.scalars(statement, *args, **kwargs)
            if (
                response_returned
                and not revoked_at_sql
                and "komari_decision_dispatches.decision_id =" in str(statement)
            ):
                admitted = False
                revoked_at_sql = True
            return result

        async def execute(self, statement: Any, *args: Any, **kwargs: Any) -> Any:
            if str(statement).startswith("UPDATE komari_decision_dispatches"):
                params = statement.compile().params
                if not admitted and (
                    params.get("state") == "success"
                    or params.get("notification_eligible") is True
                ):
                    submitted_success.append(params)
            return await self.inner.execute(statement, *args, **kwargs)

        def __getattr__(self, name: str) -> Any:
            return getattr(self.inner, name)

    def wrap(factory: Callable[[], AsyncSession]) -> Callable[[], AsyncSession]:
        return lambda: cast("AsyncSession", SessionProbe(factory()))

    options = HarnessOptions(
        admission=lambda _event: admitted,
        responder=responder,
        session_factory_wrapper=wrap,
    )
    async with DecisionHarness(decision_database, options) as harness:
        assert harness.decision is not None and harness.engine is not None
        request = make_request(summary_candidate=True)
        outcomes = await asyncio.gather(
            harness.decision.evaluate(request), return_exceptions=True
        )
        assert revoked_at_sql, "The test must reach the real final-row read."
        assert submitted_success == [], "Revoked results must not reach the SQL write."
        row = await read_dispatch(harness.engine, request.event)
        assert row is not None
        assert row["state"] == "cancelled"
        assert row["reason_code"] == "admission_revoked"
        assert row["business_route"] == "none"
        assert row["recent_retained"] is not True
        assert row["notification_eligible"] is False
        assert row["slot_held"] is False
        result = outcomes[0]
        if isinstance(result, BaseException):
            assert isinstance(result, asyncio.CancelledError)
        else:
            assert result.status == "cancelled"
            assert result.reason_code == "admission_revoked"


async def test_revocation_after_settlement_read_never_confirms_provisional(
    decision_database: str,
) -> None:
    admitted = True
    revoked_at_sql = False
    denied_confirms: list[object] = []

    class SessionProbe:
        def __init__(self, inner: AsyncSession) -> None:
            self.inner = inner

        async def scalars(self, statement: Any, *args: Any, **kwargs: Any) -> Any:
            nonlocal admitted, revoked_at_sql
            result = await self.inner.scalars(statement, *args, **kwargs)
            if not revoked_at_sql and "komari_decision_dispatches.message_id IN" in str(
                statement
            ):
                admitted = False
                revoked_at_sql = True
            return result

        def __getattr__(self, name: str) -> Any:
            return getattr(self.inner, name)

    class RedisProbe:
        def __init__(self, inner: Any) -> None:
            self.inner = inner

        async def eval(self, *args: Any, **kwargs: Any) -> Any:
            if not admitted and "confirm" in args[2:]:
                denied_confirms.append(args[2:])
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
        request = make_request()
        outcomes = await asyncio.gather(
            harness.decision.evaluate(request), return_exceptions=True
        )
        assert revoked_at_sql, "The test must reach the real settlement-facts read."
        assert denied_confirms == [], "A fresh denial must block Redis confirmation."
        from komari_bot.plugins.komari_decision.services.jev_recent import scope_keys

        keys = scope_keys(
            request.event.protocol, request.event.account_id, request.event.group_id
        )
        assert harness.redis is not None
        state = await cast(
            "Awaitable[object]",
            harness.redis.hget(keys.states, request.event.message_id),
        )
        assert state != b"c"

        row = await read_dispatch(harness.engine, request.event)
        assert row is not None
        assert row["state"] == "cancelled"
        assert row["reason_code"] == "admission_revoked"
        assert row["business_route"] == "none"
        assert row["notification_eligible"] is False
        admitted = True
        result = outcomes[0]
        if isinstance(result, BaseException):
            assert isinstance(result, asyncio.CancelledError)
        else:
            assert result.status == "cancelled"
            assert result.reason_code == "admission_revoked"
            assert (
                await harness.decision.claim_handoff(request, result, "chat") is False
            )
        replay = await harness.decision.evaluate(
            make_request(identity=request.event, target=request.target)
        )
        assert replay.status == "duplicate"
        assert len(harness.transport.requests) == 1
