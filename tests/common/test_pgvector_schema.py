"""pgvector schema helper tests."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from komari_bot.common.pgvector_schema import (
    ensure_vector_column_dimension,
    get_vector_column_dimension,
    parse_vector_type_dimension,
)


class _FakeConnection:
    def __init__(self, row: dict[str, Any] | None) -> None:
        self._row = row

    async def fetchrow(self, query: str, table_name: str, column_name: str) -> dict[str, Any] | None:
        del query, table_name, column_name
        return self._row


class _FakeAcquire:
    def __init__(self, row: dict[str, Any] | None) -> None:
        self._conn = _FakeConnection(row)

    async def __aenter__(self) -> _FakeConnection:
        return self._conn

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        del exc_type, exc, tb


class _FakePool:
    def __init__(self, row: dict[str, Any] | None) -> None:
        self._row = row

    def acquire(self) -> _FakeAcquire:
        return _FakeAcquire(self._row)


def test_parse_vector_type_dimension_with_fixed_size() -> None:
    assert parse_vector_type_dimension("vector(4096)") == 4096


def test_parse_vector_type_dimension_without_size() -> None:
    assert parse_vector_type_dimension("vector") is None


def test_parse_vector_type_dimension_rejects_non_vector() -> None:
    with pytest.raises(ValueError):
        parse_vector_type_dimension("real[]")


def test_get_vector_column_dimension_reads_catalog_type() -> None:
    dimension = asyncio.run(
        get_vector_column_dimension(
            _FakePool({"data_type": "vector(512)"}),
            table_name="komari_knowledge",
            column_name="embedding",
        )
    )
    assert dimension == 512


def test_ensure_vector_column_dimension_accepts_matching_dimension() -> None:
    asyncio.run(
        ensure_vector_column_dimension(
            _FakePool({"data_type": "vector(4096)"}),
            table_name="komari_memory_conversations",
            column_name="embedding",
            expected_dimension=4096,
            label="KomariMemory",
        )
    )


def test_ensure_vector_column_dimension_rejects_dimension_mismatch() -> None:
    with pytest.raises(RuntimeError, match="KomariKnowledge 向量维度不匹配"):
        asyncio.run(
            ensure_vector_column_dimension(
                _FakePool({"data_type": "vector(512)"}),
                table_name="komari_knowledge",
                column_name="embedding",
                expected_dimension=1024,
                label="KomariKnowledge",
            )
        )
