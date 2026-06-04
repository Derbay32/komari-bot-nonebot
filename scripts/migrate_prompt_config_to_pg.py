"""将旧版 YAML prompt 配置显式导入 PostgreSQL。"""

from __future__ import annotations

import argparse
import logging
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from komari_bot.common.prompt_storage import validate_prompt_values
from komari_bot.plugins.group_history_summary.prompt_template import (
    DEFAULTS as GROUP_HISTORY_PROMPT_DEFAULTS,
)
from komari_bot.plugins.komari_chat.services.prompt_template import (
    _DEFAULTS as KOMARI_CHAT_PROMPT_DEFAULTS,
)
from komari_bot.plugins.komari_memory.services.summary_prompt_template import (
    DEFAULTS as KOMARI_MEMORY_SUMMARY_PROMPT_DEFAULTS,
)

logger = logging.getLogger("migrate_prompt_config_to_pg")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)


@dataclass(frozen=True, slots=True)
class PromptMigrationResource:
    """待迁移的 prompt 资源。"""

    resource_id: str
    display_name: str
    legacy_file_path: Path
    defaults: dict[str, str]


_RESOURCES = (
    PromptMigrationResource(
        resource_id="komari_chat",
        display_name="Komari Chat Prompt",
        legacy_file_path=Path("config") / "prompts" / "komari_memory.yaml",
        defaults=KOMARI_CHAT_PROMPT_DEFAULTS,
    ),
    PromptMigrationResource(
        resource_id="komari_memory_summary",
        display_name="Komari Memory Summary Prompt",
        legacy_file_path=Path("config") / "prompts" / "komari_memory_summary.yaml",
        defaults=KOMARI_MEMORY_SUMMARY_PROMPT_DEFAULTS,
    ),
    PromptMigrationResource(
        resource_id="group_history_summary",
        display_name="Group History Summary Prompt",
        legacy_file_path=Path("config") / "prompts" / "group_history_summary.yaml",
        defaults=GROUP_HISTORY_PROMPT_DEFAULTS,
    ),
)


def _load_yaml_mapping(path: Path) -> dict[str, Any]:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        msg = f"提示词 YAML 顶层必须是对象: {path}"
        raise TypeError(msg)
    return data


def _collect_prompt_values(resource: PromptMigrationResource) -> dict[str, str] | None:
    if not resource.legacy_file_path.exists():
        logger.info("跳过不存在的提示词文件: %s", resource.legacy_file_path)
        return None

    data = _load_yaml_mapping(resource.legacy_file_path)
    allowed_data = {
        key: value
        for key, value in data.items()
        if key in resource.defaults and isinstance(value, str)
    }
    return validate_prompt_values(resource.defaults, allowed_data)


def migrate_prompts(*, dry_run: bool) -> dict[str, int]:
    """迁移本轮支持的字符串 prompt。"""
    stats = {"success": 0, "skipped": 0, "failed": 0}
    storage = None
    if not dry_run:
        from komari_bot.common.prompt_storage import get_prompt_storage

        storage = get_prompt_storage()

    for resource in _RESOURCES:
        try:
            values = _collect_prompt_values(resource)
            if values is None:
                stats["skipped"] += 1
                continue
            if dry_run:
                logger.info(
                    "[dry-run] 将导入 %s，字段数: %s",
                    resource.resource_id,
                    len(values),
                )
            else:
                assert storage is not None
                storage.upsert(
                    resource_id=resource.resource_id,
                    display_name=resource.display_name,
                    prompt_data=values,
                )
                logger.info(
                    "已导入提示词: %s -> %s",
                    resource.legacy_file_path,
                    resource.resource_id,
                )
        except Exception:
            logger.exception("迁移提示词失败: %s", resource.legacy_file_path)
            stats["failed"] += 1
            continue
        stats["success"] += 1

    return stats


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="将旧版 YAML prompt 导入 PostgreSQL")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="仅输出将导入的资源，不写入 PostgreSQL",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    stats = migrate_prompts(dry_run=args.dry_run)
    logger.info(
        "迁移统计: success=%s skipped=%s failed=%s",
        stats["success"],
        stats["skipped"],
        stats["failed"],
    )


if __name__ == "__main__":
    main()
