"""将旧版 JSON 插件配置一次性导入 PostgreSQL 配置表。

脚本只执行 upsert，不删除本地 JSON 文件。``database_config.json`` 会被跳过，
因为 PG / Redis 引导配置必须来自 dotenv / 环境变量。
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from komari_bot.plugins.config_manager.storage import get_config_storage

logger = logging.getLogger("migrate_json_config_to_pg")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)


def _resolve_plugin_name(config_path: Path) -> str:
    stem = config_path.stem
    return stem.removesuffix("_config")


def _load_json_config(config_path: Path) -> dict[str, Any]:
    data = json.loads(config_path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        msg = f"配置文件顶层必须是对象: {config_path}"
        raise TypeError(msg)
    return data


def migrate_config_dir(config_dir: Path) -> dict[str, int]:
    """迁移指定目录下的旧版插件 JSON 配置。"""
    stats = {"success": 0, "skipped": 0, "failed": 0}
    storage = get_config_storage()

    for config_path in sorted(config_dir.glob("*_config.json")):
        if config_path.name == "database_config.json":
            logger.info("跳过数据库引导配置: %s", config_path)
            stats["skipped"] += 1
            continue

        plugin_name = _resolve_plugin_name(config_path)
        try:
            data = _load_json_config(config_path)
            storage.upsert(
                plugin_name=plugin_name,
                schema_name="LegacyJsonConfig",
                config_data=data,
                version=str(data.get("version", "1.0")),
            )
        except Exception:
            logger.exception("迁移失败: %s", config_path)
            stats["failed"] += 1
            continue

        logger.info("已导入配置: %s -> %s", config_path, plugin_name)
        stats["success"] += 1

    return stats


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="将旧版 JSON 插件配置导入 PostgreSQL")
    parser.add_argument(
        "--config-dir",
        type=Path,
        default=Path("config/config_manager"),
        help="旧版插件 JSON 配置目录",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    stats = migrate_config_dir(args.config_dir)
    logger.info(
        "迁移统计: success=%s skipped=%s failed=%s",
        stats["success"],
        stats["skipped"],
        stats["failed"],
    )


if __name__ == "__main__":
    main()
