"""压缩 komari_memory_user_profile 中的用户画像。

默认是 dry-run，仅打印压缩前后统计，不写库。

用法：
1. dry-run:
   poetry run python scripts/compact_komari_memory_profiles.py
2. 执行回写:
   poetry run python scripts/compact_komari_memory_profiles.py --apply
3. 仅处理指定群或用户:
   poetry run python scripts/compact_komari_memory_profiles.py --group-id 123456 --user-id 10001
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from openai import AsyncOpenAI
from pydantic import BaseModel, Field

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from komari_bot.common.database_config import (
    load_database_config_from_file,
    merge_database_config,
)
from komari_bot.common.postgres import create_postgres_pool
from komari_bot.common.profile_compaction import (
    LoggerLike,
    compact_profile_with_llm,
    count_profile_traits,
    normalize_profile_for_storage,
    summarize_profile_compaction_diff,
)

_PROFILE_TABLE = "komari_memory_user_profile"
logger = logging.getLogger("compact_komari_memory_profiles")
if not logging.getLogger().handlers:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s | %(message)s",
    )


class StandaloneMemoryConfig(BaseModel):
    """脚本使用的最小 komari_memory 配置。"""

    pg_host: str | None = Field(default=None)
    pg_port: int | None = Field(default=None)
    pg_database: str | None = Field(default=None)
    pg_user: str | None = Field(default=None)
    pg_password: str | None = Field(default=None)
    pg_pool_min_size: int | None = Field(default=None)
    pg_pool_max_size: int | None = Field(default=None)
    llm_model_summary: str = Field(default="gemini-2.5-flash-lite")
    llm_temperature_summary: float = Field(default=0.3)
    llm_max_tokens_summary: int = Field(default=2048)
    summary_chunk_token_limit: int = Field(default=3000)
    profile_trait_limit: int = Field(default=20)


class StandaloneLLMConfig(BaseModel):
    """脚本使用的最小 llm_provider 配置。"""

    deepseek_api_token: str = Field(default="")
    deepseek_api_base: str = Field(default="https://api.deepseek.com/v1")
    deepseek_temperature: float = Field(default=1.0)
    deepseek_max_tokens: int = Field(default=8192)
    deepseek_timeout_seconds: float = Field(default=300.0)
    deepseek_reasoning_effort: str = Field(default="")
    deepseek_frequency_penalty: float = Field(default=0.0)


@dataclass(frozen=True)
class ProfileRow:
    user_id: str
    group_id: str
    version: int
    display_name: str
    traits: Any
    updated_at: Any
    importance: int


class DirectLLMClient:
    """脚本模式下的轻量 OpenAI 兼容客户端。"""

    def __init__(self, config: StandaloneLLMConfig) -> None:
        self.config = config
        self._client = AsyncOpenAI(
            api_key=self.config.deepseek_api_token,
            base_url=str(self.config.deepseek_api_base),
            timeout=float(self.config.deepseek_timeout_seconds),
        )

    async def generate_text(
        self,
        *,
        prompt: str,
        model: str,
        system_instruction: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        response_format: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> str:
        del response_format
        messages: list[dict[str, Any]] = []
        if system_instruction:
            messages.append({"role": "system", "content": system_instruction})
        messages.append({"role": "user", "content": prompt})

        reasoning_effort = str(
            kwargs.get(
                "reasoning_effort",
                self.config.deepseek_reasoning_effort,
            )
            or ""
        ).strip()
        request_data: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": (
                temperature
                if temperature is not None
                else self.config.deepseek_temperature
            ),
            "max_tokens": (
                max_tokens
                if max_tokens is not None
                else self.config.deepseek_max_tokens
            ),
            "frequency_penalty": self.config.deepseek_frequency_penalty,
            "stream": False,
        }
        if reasoning_effort:
            request_data["reasoning_effort"] = reasoning_effort

        response = await self._client.chat.completions.create(**request_data)

        if not response.choices:
            msg = f"画像压缩脚本 LLM 响应格式异常: {response}"
            raise RuntimeError(msg)

        content = response.choices[0].message.content or ""
        return str(content).strip()

    async def close(self) -> None:
        await self._client.close()


def _load_schema_config(
    config_path: Path,
    schema_cls: type[Any],
    *,
    allow_missing: bool = False,
) -> Any:
    if not config_path.exists():
        if allow_missing:
            return schema_cls()
        raise FileNotFoundError(f"配置文件不存在: {config_path}")  # noqa: TRY003

    payload = json.loads(config_path.read_text(encoding="utf-8"))
    return schema_cls(**payload)


async def _fetch_profile_rows(
    pool: Any,
    *,
    group_id: str | None = None,
    user_id: str | None = None,
) -> list[ProfileRow]:
    conditions: list[str] = []
    args: list[Any] = []

    if group_id:
        conditions.append(f"group_id = ${len(args) + 1}")
        args.append(group_id)
    if user_id:
        conditions.append(f"user_id = ${len(args) + 1}")
        args.append(user_id)

    where_clause = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    query = f"""
        SELECT
            user_id,
            group_id,
            version,
            display_name,
            traits,
            updated_at,
            importance
        FROM {_PROFILE_TABLE}
        {where_clause}
        ORDER BY group_id, user_id
    """
    async with pool.acquire() as conn:
        rows = await conn.fetch(query, *args)

    return [
        ProfileRow(
            user_id=str(row["user_id"]),
            group_id=str(row["group_id"]),
            version=int(row["version"] or 1),
            display_name=str(row["display_name"] or row["user_id"]),
            traits=row["traits"],
            updated_at=row["updated_at"],
            importance=int(row["importance"] or 4),
        )
        for row in rows
    ]


def _normalize_profile_updated_at(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=UTC)

    text = str(value or "").strip()
    if text:
        normalized_text = f"{text[:-1]}+00:00" if text.endswith("Z") else text
        try:
            parsed = datetime.fromisoformat(normalized_text)
        except ValueError:
            logger.warning(
                "[KomariMemory] 画像 updated_at 解析失败，回退当前时间: raw=%s",
                text,
            )
        else:
            return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)
    return datetime.now(UTC)


async def _update_profile_row(
    pool: Any,
    *,
    row: ProfileRow,
    compacted_profile: dict[str, Any],
) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            f"""
            UPDATE {_PROFILE_TABLE}
            SET version = $3,
                display_name = $4,
                traits = $5::jsonb,
                updated_at = $6::timestamptz,
                importance = $7
            WHERE user_id = $1 AND group_id = $2
            """,
            row.user_id,
            row.group_id,
            int(compacted_profile.get("version", 1) or 1),
            str(compacted_profile.get("display_name", "")).strip() or row.user_id,
            json.dumps(compacted_profile.get("traits", {}), ensure_ascii=False),
            _normalize_profile_updated_at(compacted_profile.get("updated_at")),
            row.importance,
        )


def _build_profile_payload(row: ProfileRow) -> dict[str, Any] | None:
    traits_raw = row.traits
    if isinstance(traits_raw, str):
        try:
            traits_raw = json.loads(traits_raw)
        except (TypeError, ValueError):
            traits_raw = None
    updated_at = row.updated_at
    if hasattr(updated_at, "isoformat"):
        updated_at = updated_at.isoformat()
    return {
        "version": max(1, int(row.version or 1)),
        "user_id": str(row.user_id),
        "display_name": str(row.display_name).strip() or str(row.user_id),
        "traits": dict(traits_raw) if isinstance(traits_raw, dict) else {},
        "updated_at": str(updated_at or "").strip(),
    }


async def _process_profile_rows(
    rows: list[ProfileRow],
    *,
    memory_config: Any,
    llm_generate_text: Any,
    apply: bool,
    update_profile: Any,
) -> dict[str, int]:
    stats = {
        "scanned": 0,
        "updated": 0,
        "would_update": 0,
        "skipped": 0,
        "failed": 0,
    }

    for index, row in enumerate(rows, start=1):
        stats["scanned"] += 1
        parsed = _build_profile_payload(row)
        if parsed is None:
            logger.warning(
                f"[KomariMemory] 画像瘦身跳过非法画像结构: row={index}/{len(rows)} "
                f"group={row.group_id} user={row.user_id}"
            )
            stats["failed"] += 1
            continue

        before_profile = normalize_profile_for_storage(
            parsed,
            fallback_user_id=row.user_id,
            fallback_display_name=str(parsed.get("display_name", "")).strip(),
        )
        before_traits = count_profile_traits(before_profile)
        if before_traits <= memory_config.profile_trait_limit:
            logger.info(
                f"[KomariMemory] 画像瘦身跳过: row={index}/{len(rows)} group={row.group_id} "
                f"user={row.user_id} traits={before_traits} "
                f"limit={memory_config.profile_trait_limit} reason=within_limit"
            )
            stats["skipped"] += 1
            continue

        trace_id = f"profile-migrate-{row.group_id}-{row.user_id}"
        try:
            compacted_profile = await compact_profile_with_llm(
                profile=before_profile,
                config=memory_config,
                llm_generate_text=llm_generate_text,
                trace_id=trace_id,
                source="migration_script",
                log=cast("LoggerLike", logger),
            )
        except Exception:
            logger.exception(
                f"[KomariMemory] 画像瘦身失败: row={index}/{len(rows)} "
                f"group={row.group_id} user={row.user_id}"
            )
            stats["failed"] += 1
            continue

        diff = summarize_profile_compaction_diff(before_profile, compacted_profile)
        logger.info(
            f"[KomariMemory] 画像瘦身结果: row={index}/{len(rows)} group={row.group_id} "
            f"user={row.user_id} apply={apply} before_traits={diff['before_traits']} "
            f"after_traits={diff['after_traits']} before_chars={diff['before_chars']} "
            f"after_chars={diff['after_chars']} removed_keys={diff['removed_keys']} "
            f"added_keys={diff['added_keys']} kept_keys={diff['kept_keys']}"
        )

        if apply:
            await update_profile(row=row, compacted_profile=compacted_profile)
            stats["updated"] += 1
        else:
            stats["would_update"] += 1

    return stats


async def run(
    *,
    apply: bool,
    database_config_path: Path,
    memory_config_path: Path,
    llm_config_path: Path,
    group_id: str | None = None,
    user_id: str | None = None,
) -> dict[str, int]:
    shared_database_config = load_database_config_from_file(database_config_path)
    memory_config = _load_schema_config(
        memory_config_path,
        StandaloneMemoryConfig,
        allow_missing=True,
    )
    llm_config = _load_schema_config(llm_config_path, StandaloneLLMConfig)
    if not str(llm_config.deepseek_api_token).strip():
        raise ValueError("llm_provider 缺少 deepseek_api_token，无法执行画像压缩")  # noqa: TRY003

    effective_database_config = merge_database_config(
        shared_database_config,
        memory_config,
    )
    pool = await create_postgres_pool(effective_database_config, command_timeout=60)
    client = DirectLLMClient(llm_config)

    try:
        rows = await _fetch_profile_rows(pool, group_id=group_id, user_id=user_id)
        logger.info(
            f"[KomariMemory] 画像瘦身脚本启动: rows={len(rows)} apply={apply} "
            f"trait_limit={memory_config.profile_trait_limit} group={group_id or '-'} user={user_id or '-'}"
        )
        stats = await _process_profile_rows(
            rows,
            memory_config=memory_config,
            llm_generate_text=client.generate_text,
            apply=apply,
            update_profile=lambda **kwargs: _update_profile_row(pool, **kwargs),
        )
        logger.info(
            f"[KomariMemory] 画像瘦身脚本完成: scanned={stats['scanned']} "
            f"updated={stats['updated']} would_update={stats['would_update']} "
            f"skipped={stats['skipped']} failed={stats['failed']}"
        )
        return stats
    finally:
        await client.close()
        await pool.close()


def _build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="压缩 komari_memory 用户画像")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="执行真实回写；默认仅 dry-run",
    )
    parser.add_argument(
        "--database-config-path",
        type=Path,
        default=Path("config/config_manager/database_config.json"),
        help="共享数据库配置文件路径",
    )
    parser.add_argument(
        "--memory-config-path",
        type=Path,
        default=Path("config/config_manager/komari_memory_config.json"),
        help="komari_memory 配置文件路径",
    )
    parser.add_argument(
        "--llm-config-path",
        type=Path,
        default=Path("config/config_manager/llm_provider_config.json"),
        help="llm_provider 配置文件路径",
    )
    parser.add_argument("--group-id", type=str, default=None, help="仅处理指定群号")
    parser.add_argument("--user-id", type=str, default=None, help="仅处理指定用户")
    return parser


def main() -> None:
    args = _build_argument_parser().parse_args()
    asyncio.run(
        run(
            apply=bool(args.apply),
            database_config_path=args.database_config_path,
            memory_config_path=args.memory_config_path,
            llm_config_path=args.llm_config_path,
            group_id=args.group_id,
            user_id=args.user_id,
        )
    )


if __name__ == "__main__":
    main()
