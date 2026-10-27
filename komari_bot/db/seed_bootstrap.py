"""Optional Jev prompt bootstrap for the container prestart path.

DSH owns the chat and group-summary prompts.  Python PostgreSQL prompt storage
is retained only for the Jev decision prompt, which still uses the existing
typed single-row table and authenticated management API.  This module is the
deployment entry point for an optional initial Jev prompt asset:

    python -m komari_bot.db.seed_bootstrap [--decision-seed-file PATH]

The default asset is ``komari_bot/db/initial_data/decision_prompt.yaml``.  It
has a strict, closed shape (``version`` plus one ``decision_prompt`` mapping),
rejects duplicate YAML keys and validates every text field against the shared
content budget.

The command only inserts the missing Jev singleton or fills empty fields.  It
never overwrites non-empty administrator text.  Missing, malformed or failed
optional initialization emits one fixed safe diagnostic and exits successfully
so it cannot block independent services.  Cancellation and process signals
continue to propagate.  Alembic failure remains fatal in ``prestart.sh``.

No chat/summary prompt asset is read, seeded or required for cold start, and
the runtime never reads a seed file as a prompt fallback.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Hashable
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml
from yaml.constructor import ConstructorError

from komari_bot.config.typed_config import ensure_typed_prompt_model
from komari_bot.llm.content_budget import (
    CONTENT_TEXT_BUDGET,
    validate_text_budget,
)

from .orm_bootstrap import _bootstrap_nonebot

if TYPE_CHECKING:
    from collections.abc import Sequence

#: Optional Jev decision prompt asset (the only remaining Python prompt seed).
DEFAULT_DECISION_SEED_FILE = (
    Path(__file__).resolve().parent / "initial_data" / "decision_prompt.yaml"
)

#: Storage-only fields inherited from ``TypedPromptModel``.
_PROMPT_STORAGE_FIELDS: frozenset[str] = frozenset({"id", "revision", "updated_at"})

_DECISION_PROMPT_RESOURCE_ID = "komari_decision"
_DECISION_PROMPT_SECTION_KEY = "decision_prompt"
_DECISION_SEED_ROOT_KEYS: frozenset[str] = frozenset(
    {"version", _DECISION_PROMPT_SECTION_KEY}
)

#: Fixed safe diagnostic. It never contains prompt text, paths, exception
#: messages or tracebacks, and never claims that a write was rolled back.
_DECISION_SEED_UNAVAILABLE_MESSAGE = "判定 Prompt 初始化未能确认完成。"

_MAPPING_INTERNAL_TAGS: frozenset[str] = frozenset(
    {
        "tag:yaml.org,2002:merge",
        "tag:yaml.org,2002:value",
    }
)


class _UniqueKeySafeLoader(yaml.SafeLoader):
    """Local SafeLoader that rejects duplicate mapping keys."""

    def construct_mapping(
        self,
        node: yaml.MappingNode,
        deep: bool = False,  # noqa: FBT001, FBT002
    ) -> dict[Any, Any]:
        seen: set[Any] = set()
        for key_node, _value_node in node.value:
            if key_node.tag in _MAPPING_INTERNAL_TAGS:
                continue
            key = self.construct_object(key_node, deep=deep)
            if not isinstance(key, Hashable):
                continue
            if key in seen:
                raise ConstructorError(  # noqa: TRY003
                    "while constructing a mapping",
                    node.start_mark,
                    f"found duplicate key {key!r}",
                    key_node.start_mark,
                )
            seen.add(key)
        return super().construct_mapping(node, deep=deep)


class SeedValidationError(RuntimeError):
    """The optional Jev prompt asset failed validation."""


def _load_yaml_document(path: Path) -> dict[str, Any]:
    """Read a strict YAML mapping and reject duplicate keys."""
    try:
        raw = yaml.load(
            path.read_text(encoding="utf-8"),
            Loader=_UniqueKeySafeLoader,
        ) or {}
    except (OSError, UnicodeDecodeError) as exc:
        msg = f"读取初始数据文件失败: {path}（{exc}）"
        raise SeedValidationError(msg) from exc
    except yaml.YAMLError as exc:
        msg = f"初始数据 YAML 解析失败: {path}（{exc}）"
        raise SeedValidationError(msg) from exc

    if not isinstance(raw, dict):
        msg = f"初始数据根节点必须是对象: {path}"
        raise SeedValidationError(msg)
    return raw


def _prompt_model(resource_id: str) -> Any:
    model_cls = ensure_typed_prompt_model(resource_id)
    if model_cls is None:
        msg = f"无法加载 Prompt 强类型 Schema（{resource_id}.prompt_schema）"
        raise SeedValidationError(msg)
    return model_cls


def _prompt_schema_fields(resource_id: str) -> set[str]:
    return set(_prompt_model(resource_id).model_fields) - _PROMPT_STORAGE_FIELDS


def _prompt_table_name(resource_id: str) -> str:
    return str(_prompt_model(resource_id).__tablename__)


def _validate_prompt_values(
    mapping: object,
    resource_id: str,
    path: Path,
) -> dict[str, str]:
    """Validate one closed prompt block against its typed schema and budget."""
    if not isinstance(mapping, dict):
        msg = f"{resource_id} Prompt 初始数据必须是对象: {path}"
        raise SeedValidationError(msg)

    fields = _prompt_schema_fields(resource_id)
    provided = {key for key in mapping if isinstance(key, str)}
    missing = sorted(fields - provided)
    if missing:
        msg = (
            f"{resource_id} Prompt 初始数据缺少字段: "
            f"{', '.join(missing)}（{path}）"
        )
        raise SeedValidationError(msg)
    extra = sorted(str(key) for key in set(mapping) - fields)
    if extra:
        msg = (
            f"{resource_id} Prompt 初始数据包含未知字段: "
            f"{', '.join(extra)}（{path}）"
        )
        raise SeedValidationError(msg)

    normalized: dict[str, str] = {}
    for field in sorted(fields):
        value = mapping[field]
        if not isinstance(value, str) or not value.strip():
            msg = (
                f"{resource_id} Prompt 字段 {field} 的初始值必须是非空字符串"
                f"（{path}）"
            )
            raise SeedValidationError(msg)
        try:
            validate_text_budget(
                value,
                label=f"{resource_id} Prompt 初始数据字段 {field}",
                budget=CONTENT_TEXT_BUDGET,
            )
        except ValueError as exc:
            msg = f"{exc}（{path}）"
            raise SeedValidationError(msg) from exc
        normalized[field] = value
    return normalized


def load_decision_prompt_seed(path: Path) -> dict[str, str]:
    """Read and validate the optional Jev prompt asset.

    The asset shape is exactly ``{version: non-empty string,
    decision_prompt: {typed fields}}``.  The parser error text may contain
    asset fragments; the CLI boundary reduces all failures to one fixed
    diagnostic.
    """
    path = path if path.is_absolute() else path.resolve()
    raw = _load_yaml_document(path)

    version = raw.get("version")
    if not isinstance(version, str) or not version.strip():
        msg = f"判定 Prompt 初始数据的 version 必须是非空字符串: {path}"
        raise SeedValidationError(msg)

    section = raw.get(_DECISION_PROMPT_SECTION_KEY)
    if section is None:
        msg = (
            "判定 Prompt 初始数据缺少 "
            f"{_DECISION_PROMPT_SECTION_KEY} 块: {path}"
        )
        raise SeedValidationError(msg)
    if not isinstance(section, dict):
        msg = (
            "判定 Prompt 初始数据的 "
            f"{_DECISION_PROMPT_SECTION_KEY} 必须是对象: {path}"
        )
        raise SeedValidationError(msg)

    unexpected_root = sorted(
        str(key) for key in set(raw) - _DECISION_SEED_ROOT_KEYS
    )
    if unexpected_root:
        msg = (
            "判定 Prompt 初始数据包含未知顶层键: "
            f"{', '.join(unexpected_root)}（{path}）"
        )
        raise SeedValidationError(msg)

    return _validate_prompt_values(section, _DECISION_PROMPT_RESOURCE_ID, path)


async def _seed_prompt_row(
    conn: Any,
    resource_id: str,
    prompt_values: dict[str, str],
) -> tuple[int, int]:
    """Insert a missing singleton or fill only its empty fields."""
    table = _prompt_table_name(resource_id)
    fields = sorted(prompt_values)
    row = await conn.fetchrow(f"SELECT * FROM {table} WHERE id = 1")
    if row is None:
        columns = ", ".join(fields)
        placeholders = ", ".join(f"${index}" for index in range(2, 2 + len(fields)))
        await conn.execute(
            f"INSERT INTO {table}"
            " (id, revision, updated_at, "
            + columns
            + ") VALUES (1, 1, $1, "
            + placeholders
            + ")",
            datetime.now(UTC),
            *(prompt_values[field] for field in fields),
        )
        return 1, 0

    updates = {
        field: prompt_values[field]
        for field in fields
        if row[field] is None or not str(row[field]).strip()
    }
    if not updates:
        return 0, 0
    ordered = sorted(updates)
    set_clause = ", ".join(
        f'"{field}" = ${index}' for index, field in enumerate(ordered, start=2)
    )
    await conn.execute(
        f"UPDATE {table} SET "
        + set_clause
        + ", revision = revision + 1, updated_at = $1 WHERE id = 1",
        datetime.now(UTC),
        *(updates[field] for field in ordered),
    )
    return 0, len(updates)


async def _verify_prompt_cold_start(conn: Any, resource_id: str) -> None:
    """Verify the optional Jev singleton has no empty text field."""
    fields = _prompt_schema_fields(resource_id)
    table = _prompt_table_name(resource_id)
    row = await conn.fetchrow(f"SELECT * FROM {table} WHERE id = 1")
    if row is None:
        msg = f"{resource_id} Prompt 初始数据未写入数据库（{table} 缺行）"
        raise RuntimeError(msg)
    empty = [
        field
        for field in sorted(fields)
        if row[field] is None or not str(row[field]).strip()
    ]
    if empty:
        msg = f"{resource_id} Prompt 初始数据字段为空: {', '.join(empty)}"
        raise RuntimeError(msg)


async def _seed_optional_decision_prompt(
    pool: Any,
    prompt_values: dict[str, str],
) -> tuple[int, int] | None:
    """Seed Jev in its own lease/transaction, isolating every optional failure."""
    counts: tuple[int, int] | None = None
    try:
        async with pool.acquire() as conn, conn.transaction():
            counts = await _seed_prompt_row(
                conn,
                _DECISION_PROMPT_RESOURCE_ID,
                prompt_values,
            )
            await _verify_prompt_cold_start(conn, _DECISION_PROMPT_RESOURCE_ID)
    except Exception:
        # Do not catch BaseException: cancellation and signals must propagate.
        print(_DECISION_SEED_UNAVAILABLE_MESSAGE, file=sys.stderr)  # noqa: T201
        return None
    return counts


async def _seed_database(prompt_values: dict[str, str]) -> tuple[int, int] | None:
    from komari_bot.db.orm_connection import get_shared_orm_connection_pool

    return await _seed_optional_decision_prompt(
        get_shared_orm_connection_pool(),
        prompt_values,
    )


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m komari_bot.db.seed_bootstrap",
        description=(
            "可选的 Jev 判定 Prompt 初始数据播种：只补插缺失单行或补齐空字段，"
            "不覆盖已有管理员文案。"
        ),
    )
    parser.add_argument(
        "--decision-seed-file",
        type=Path,
        default=DEFAULT_DECISION_SEED_FILE,
        help=(
            "判定 Prompt 初始数据文件路径（可选资源；"
            f"默认: {DEFAULT_DECISION_SEED_FILE}）"
        ),
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> None:
    """Validate and optionally seed Jev without blocking independent services."""
    args = _parse_args(argv)

    try:
        prompt_values = load_decision_prompt_seed(args.decision_seed_file)
    except SeedValidationError:
        print(_DECISION_SEED_UNAVAILABLE_MESSAGE, file=sys.stderr)  # noqa: T201
        return

    try:
        _bootstrap_nonebot()
    except Exception:
        print(_DECISION_SEED_UNAVAILABLE_MESSAGE, file=sys.stderr)  # noqa: T201
        return

    try:
        counts = asyncio.run(_seed_database(prompt_values))
    except Exception:
        print(_DECISION_SEED_UNAVAILABLE_MESSAGE, file=sys.stderr)  # noqa: T201
        return

    if counts is None:
        return

    print(  # noqa: T201
        "[seed_bootstrap] 可选判定 Prompt 初始化完成: "
        f"新建 {counts[0]} 行，补齐 {counts[1]} 个字段"
    )


if __name__ == "__main__":
    main()
