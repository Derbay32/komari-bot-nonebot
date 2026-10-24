"""Exercise receive ordering across a real Prompt storage await."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, cast
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from tests.komari_decision.jev_test_support import (
    DecisionHarness,
    create_scratch_database,
    drop_scratch_database,
    make_identity,
    make_message,
    make_request,
    skip_without_services,
    write_prompt,
)
from tests.pg_support import reset_shared_orm_engine

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterator


pytestmark = pytest.mark.asyncio


@pytest.fixture(scope="module")
def decision_database() -> Iterator[str]:
    skip_without_services()
    url = asyncio.run(create_scratch_database(f"_jevreceive{uuid4().hex[:8]}"))
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


@pytest.mark.parametrize("later_count", [1, 12])
async def test_later_summon_cannot_enter_earlier_input_during_prompt_read(
    decision_database: str, monkeypatch: pytest.MonkeyPatch, later_count: int
) -> None:
    import nonebot_plugin_orm

    prompt_read = asyncio.Event()
    release_prompt = asyncio.Event()
    original_factory = nonebot_plugin_orm.get_session

    class SessionProbe:
        def __init__(self, inner: AsyncSession) -> None:
            self.inner = inner

        async def get(self, entity: Any, *args: Any, **kwargs: Any) -> Any:
            value = await self.inner.get(entity, *args, **kwargs)
            if (
                getattr(entity, "__tablename__", "") == "komari_prompt_komari_decision"
                and not prompt_read.is_set()
            ):
                prompt_read.set()
                await release_prompt.wait()
            return value

        def __getattr__(self, name: str) -> Any:
            return getattr(self.inner, name)

    async with DecisionHarness(decision_database) as harness:
        assert harness.decision is not None
        # Invalidate the real loader through the real Prompt storage API.
        await write_prompt()
        monkeypatch.setattr(
            nonebot_plugin_orm,
            "get_session",
            lambda **kwargs: cast(
                "AsyncSession", SessionProbe(original_factory(**kwargs))
            ),
        )
        first = make_identity()
        platform_time = datetime.now(UTC).replace(microsecond=0)
        first_request = make_request(
            identity=first,
            target=make_message(
                identity=first, timestamp=platform_time, text="Earlier target"
            ),
            received_at=platform_time + timedelta(milliseconds=100),
        )
        predecessors = (
            [f"Previous {index}" for index in range(10)] if later_count == 12 else []
        )
        for index, text in enumerate(predecessors):
            received = platform_time - timedelta(seconds=10 - index)
            await harness.decision.observe_delivered(
                make_message(group_id=first.group_id, text=text, timestamp=received),
                protocol=first.protocol,
                account_id=first.account_id,
                received_at=received,
            )
        pending = asyncio.create_task(harness.decision.evaluate(first_request))
        try:
            await asyncio.wait_for(prompt_read.wait(), timeout=3)
            for index in range(later_count):
                second = make_identity(
                    account_id=first.account_id, group_id=first.group_id
                )
                second_result = await harness.decision.evaluate(
                    make_request(
                        identity=second,
                        target=make_message(
                            identity=second,
                            timestamp=platform_time,
                            text=f"Later summon {index}",
                        ),
                        received_at=platform_time + timedelta(milliseconds=200 + index),
                        real_platform_at=True,
                    )
                )
                assert second_result.status == "bypass"
            release_prompt.set()
            first_result = await pending
            assert first_result.status == "success"
            conversation = harness.transport.state()["untrusted_group_conversation"]
            texts = [
                conversation["messages"][key]["text"]
                for key in conversation["timeline"]
            ]
            assert texts == [*predecessors, "Earlier target"], (
                "Post-trigger reception must not become preceding context."
            )
        finally:
            release_prompt.set()
            await asyncio.gather(pending, return_exceptions=True)
            monkeypatch.setattr(nonebot_plugin_orm, "get_session", original_factory)


async def test_receive_time_controls_cutoff_and_order_despite_delayed_ingress(
    decision_database: str,
) -> None:
    async with DecisionHarness(decision_database) as harness:
        assert harness.decision is not None
        first = make_identity()
        clock = datetime.now(UTC).replace(microsecond=0)
        await harness.decision.observe_delivered(
            make_message(
                group_id=first.group_id, timestamp=clock, text="Second reception"
            ),
            protocol=first.protocol,
            account_id=first.account_id,
            received_at=clock + timedelta(milliseconds=200),
        )
        result = await harness.decision.evaluate(
            make_request(
                identity=first,
                target=make_message(
                    identity=first, timestamp=clock, text="First reception"
                ),
                received_at=clock + timedelta(milliseconds=100),
            )
        )
        assert result.status == "success"
        conversation = harness.transport.state(0)["untrusted_group_conversation"]
        assert [
            conversation["messages"][key]["text"] for key in conversation["timeline"]
        ] == ["First reception"]
        third = make_identity(account_id=first.account_id, group_id=first.group_id)
        result = await harness.decision.evaluate(
            make_request(
                identity=third,
                target=make_message(
                    identity=third, timestamp=clock, text="Third reception"
                ),
                received_at=clock + timedelta(milliseconds=300),
            )
        )
        assert result.status == "success"
        conversation = harness.transport.state(1)["untrusted_group_conversation"]
        assert [
            conversation["messages"][key]["text"] for key in conversation["timeline"]
        ] == ["First reception", "Second reception", "Third reception"]


@pytest.mark.parametrize("equal_receipts", [False, True])
async def test_window_prunes_by_reception_with_stable_equal_time_ties(
    decision_database: str, *, equal_receipts: bool
) -> None:
    async with DecisionHarness(decision_database) as harness:
        assert harness.decision is not None
        identity = make_identity()
        clock = datetime.now(UTC).replace(microsecond=0)
        indexes = range(12) if equal_receipts else reversed(range(12))
        for index in indexes:
            await harness.decision.observe_delivered(
                make_message(
                    group_id=identity.group_id, timestamp=clock, text=f"Prior {index}"
                ),
                protocol=identity.protocol,
                account_id=identity.account_id,
                received_at=clock
                + timedelta(milliseconds=0 if equal_receipts else index),
            )
        result = await harness.decision.evaluate(
            make_request(
                identity=identity,
                target=make_message(
                    identity=identity, timestamp=clock, text="Current target"
                ),
                received_at=clock + timedelta(seconds=1),
            )
        )
        assert result.status == "success"
        conversation = harness.transport.state()["untrusted_group_conversation"]
        assert [
            conversation["messages"][key]["text"] for key in conversation["timeline"]
        ] == [*(f"Prior {index}" for index in range(2, 12)), "Current target"]
