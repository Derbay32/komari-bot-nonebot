"""日志存储在创建和关闭会话失败时保持同一不可用契约。"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from komari_bot.decision.jev import (
    DecisionLogMetadata,
    DecisionResult,
    EventIdentity,
)
from komari_bot.plugins.komari_decision.repositories.decision_log_repository import (
    STORAGE_UNAVAILABLE_MESSAGE,
    DecisionLogRepository,
    DecisionLogStorageUnavailable,
)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "operation", ["record", "list_metadata", "get_metadata", "get_body", "prune"]
)
@pytest.mark.parametrize("failure_phase", ["factory", "close"])
async def test_session_lifecycle_failures_have_safe_storage_error(
    operation: str, failure_phase: str
) -> None:
    session = AsyncMock(spec=AsyncSession)
    session.scalar.return_value = 0
    result = MagicMock()
    result.mappings.return_value.first.return_value = None
    result.mappings.return_value.all.return_value = []
    result.rowcount = 0
    session.execute.return_value = result
    secret_error = RuntimeError("synthetic-sensitive-database-detail")
    if failure_phase == "close":
        session.close.side_effect = secret_error

    def factory() -> AsyncSession:
        if failure_phase == "factory":
            raise secret_error
        return cast("AsyncSession", session)

    repository = DecisionLogRepository(factory, lambda: 7)
    decision_id = str(uuid4())
    now = datetime.now(UTC)
    metadata = DecisionLogMetadata(
        decision_id=decision_id,
        event=EventIdentity(
            protocol="onebot_v11", account_id="1", group_id="2", message_id="3"
        ),
        created_at=now,
        finished_at=now,
        result=DecisionResult(
            decision_id=decision_id, status="filtered", reason_code="filtered"
        ),
    )
    kwargs: dict[str, Any] = {}
    match operation:
        case "record":
            kwargs = {"metadata": metadata, "body": None}
        case "get_metadata" | "get_body":
            kwargs = {"decision_id": decision_id}
    with pytest.raises(DecisionLogStorageUnavailable) as captured:
        await getattr(repository, operation)(**kwargs)
    assert str(captured.value) == STORAGE_UNAVAILABLE_MESSAGE
    if failure_phase == "close":
        session.close.assert_awaited_once()
