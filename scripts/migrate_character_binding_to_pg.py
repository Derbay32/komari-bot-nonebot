"""将旧版角色名绑定 JSON 显式导入 PostgreSQL。"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import asyncpg

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from komari_bot.common.database_config import (
    DatabaseConfigSchema,
    load_database_config_from_env,
    load_database_config_from_file,
)
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


async def migrate_bindings(
    *,
    bindings_path: Path = DEFAULT_BINDINGS_PATH,
    database_config_path: Path | None = None,
) -> dict[str, int]:
    """把文件中的有效绑定 upsert 到 PostgreSQL。"""
    raw_bindings = _load_bindings(bindings_path)
    config = (
        load_database_config_from_file(database_config_path)
        if database_config_path is not None
        else load_database_config_from_env()
    )
    connection = await _connect(config)
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


async def _connect(config: DatabaseConfigSchema) -> asyncpg.Connection:
    """建立脚本专用 asyncpg 直连。"""
    return await asyncpg.connect(
        host=config.pg_host,
        port=config.pg_port,
        database=config.pg_database,
        user=config.pg_user,
        password=config.pg_password,
        command_timeout=30,
    )


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
    parser.add_argument(
        "--database-config",
        type=Path,
        default=None,
        help="可选的旧版数据库 JSON 配置；默认读取 PG_* 环境变量",
    )
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    asyncio.run(
        migrate_bindings(
            bindings_path=args.bindings_path,
            database_config_path=args.database_config,
        )
    )


if __name__ == "__main__":
    main()
