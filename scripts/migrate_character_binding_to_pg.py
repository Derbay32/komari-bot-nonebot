"""将旧版角色名绑定 JSON 显式导入 PostgreSQL。

v2.0.0 起连接串统一读取 nonebot-plugin-orm 权威的 ``SQLALCHEMY_DATABASE_URL``
（``postgresql://`` 与 ``postgresql+asyncpg://`` 均可）。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import asyncpg

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from komari_bot.plugins.character_binding.manager import (
    CharacterNameValidationError,
    validate_character_name,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

DEFAULT_BINDINGS_PATH = PROJECT_ROOT / "data" / "character_binding" / "bindings.json"
_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS komari_character_bindings (
    user_id TEXT PRIMARY KEY,
    character_name TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
)
"""
_UPSERT_SQL = """
INSERT INTO komari_character_bindings (user_id, character_name)
VALUES ($1, $2)
ON CONFLICT (user_id) DO UPDATE
SET character_name = EXCLUDED.character_name,
    updated_at = now()
"""

logger = logging.getLogger("migrate_character_binding_to_pg")
if not logging.getLogger().handlers:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s | %(message)s",
    )


def _load_bindings(path: Path) -> Mapping[object, object]:
    """读取旧版绑定 JSON 对象。"""
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        msg = f"角色绑定 JSON 顶层必须是对象: {path}"
        raise TypeError(msg)
    return raw


def _validate_binding(
    raw_user_id: object,
    raw_character_name: object,
) -> tuple[str, str]:
    """校验一条旧绑定记录。"""
    if not isinstance(raw_user_id, str) or not raw_user_id:
        msg = "用户 ID 必须是非空字符串"
        raise ValueError(msg)
    if not isinstance(raw_character_name, str):
        msg = "角色名必须是字符串"
        raise TypeError(msg)
    return raw_user_id, validate_character_name(raw_character_name)


def _resolve_dsn() -> str:
    """读取 nonebot-plugin-orm 权威数据库连接串（SQLALCHEMY_DATABASE_URL）。"""
    dsn = os.environ.get("SQLALCHEMY_DATABASE_URL", "")
    if not dsn:
        msg = (
            "未配置 SQLALCHEMY_DATABASE_URL，"
            "请通过环境变量设置 nonebot-plugin-orm 的连接串"
        )
        raise RuntimeError(msg)
    return dsn


async def migrate_bindings(
    *,
    bindings_path: Path = DEFAULT_BINDINGS_PATH,
) -> dict[str, int]:
    """把文件中的有效绑定 upsert 到 PostgreSQL。"""
    raw_bindings = _load_bindings(bindings_path)
    connection = await asyncpg.connect(dsn=_resolve_dsn(), command_timeout=30)
    written = 0
    skipped = 0
    try:
        await connection.execute(_TABLE_DDL)
        for raw_user_id, raw_character_name in raw_bindings.items():
            try:
                user_id, character_name = _validate_binding(
                    raw_user_id,
                    raw_character_name,
                )
            except (CharacterNameValidationError, TypeError, ValueError) as error:
                skipped += 1
                logger.warning(
                    "跳过非法角色绑定: user_id=%r reason=%s",
                    raw_user_id,
                    error,
                )
                continue

            await connection.execute(
                _UPSERT_SQL,
                user_id,
                character_name,
            )
            written += 1
    finally:
        await connection.close()

    stats = {"written": written, "skipped": skipped}
    logger.info(
        "角色绑定迁移完成: written=%s skipped=%s source=%s",
        written,
        skipped,
        bindings_path,
    )
    return stats


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="将 data/character_binding/bindings.json 导入 PostgreSQL",
    )
    parser.add_argument(
        "--bindings-path",
        type=Path,
        default=DEFAULT_BINDINGS_PATH,
        help="旧版 bindings.json 路径",
    )
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    asyncio.run(
        migrate_bindings(
            bindings_path=args.bindings_path,
        )
    )


if __name__ == "__main__":
    main()
