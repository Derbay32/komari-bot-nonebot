"""将旧版 YAML prompt 配置显式导入 PostgreSQL。"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import asyncpg
import yaml

if TYPE_CHECKING:
    from collections.abc import Mapping

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DOTENV_PATH = PROJECT_ROOT / ".env"

_DOTENV_LINE_PATTERN = re.compile(
    r"^(?:export\s+)?(?P<key>[A-Za-z_][A-Za-z0-9_]*)\s*=\s*(?P<value>.*)$"
)

_PROMPT_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS komari_prompt_configs (
    resource_id VARCHAR(128) PRIMARY KEY,
    display_name VARCHAR(128) NOT NULL,
    prompt_data JSONB NOT NULL DEFAULT '{}'::jsonb,
    version VARCHAR(32) NOT NULL DEFAULT '1.0',
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);
"""

_PROMPT_UPDATED_AT_INDEX_DDL = """
CREATE INDEX IF NOT EXISTS idx_komari_prompt_configs_updated_at
    ON komari_prompt_configs (updated_at DESC);
"""

KOMARI_CHAT_PROMPT_DEFAULTS: dict[str, str] = {
    "system_prompt": "你是一个友善的助手。",
    "memory_ack": "好的，我了解了。",
    "memory_ack_role": "assistant",
    "output_instruction": "请将最终回复放在 <content></content> 标签中。",
    "cot_prefix": "<think>\n开始思考。\n",
    "cot_prefix_role": "assistant",
}

GROUP_HISTORY_PROMPT_DEFAULTS: dict[str, str] = {
    "system_prompt": "你是一个专业的群聊总结助手，只基于聊天记录归纳事实。",
    "planning_system_prompt": (
        "你是一个群聊消息检索助手。"
        "你的任务是根据用户的总结请求，决定需要获取哪些聊天记录。"
        "你可以调用工具来获取群聊消息，请根据用户需求选择合适的工具和参数。"
        "获取到足够消息后，简短说明规划完成即可。"
    ),
    "memory_ack": "已收到聊天记录，我先梳理重点。",
    "memory_ack_role": "assistant",
    "output_instruction": (
        "请仅输出总结正文，使用 <content></content> 包裹。"
        "正文控制在 120-220 字，尽量清晰、紧凑、客观。"
    ),
    "cot_prefix": "<think>\n我先按时间梳理讨论脉络，再输出总结。\n",
    "cot_prefix_role": "assistant",
}

KOMARI_MEMORY_SUMMARY_PROMPT_DEFAULTS: dict[str, str] = {
    "memory_summary_common_system": (
        "你是《败犬女主太多了！》中的小鞠知花。\n"
        "你正在阅读一段群聊记录，需要基于对话内容总结并维护长期记忆。\n"
        "请只依据聊天记录中的可靠信息行动，不要编造用户事实。\n"
        "输出必须使用简体中文。"
    ),
    "profile_agent_workflow_system": (
        "当前任务：维护用户画像。\n\n"
        "你拥有以下工具：\n"
        "- read_profile：读取某个用户的已有画像。需要改谁就读谁，不要一次性读取所有人。\n"
        "- write_profile：暂存画像修改操作，不会直接写库，返回 diff 和冲突信息。\n"
        "- preview_profile：查看当前暂存区汇总 diff。\n"
        "- count_profile_traits：查询某用户的有效 trait 数量（可选择纳入暂存区修改）。\n"
        "- commit_profile：提交当前暂存区。提交前会校验 trait 上限，超限会返回错误并保留暂存区，需继续压缩后重试。\n\n"
        "工作流程：\n"
        "1. 阅读群聊记录，识别需要更新画像的用户。\n"
        "2. 对每个需要修改的用户，先用 read_profile 读取其已有画像。\n"
        "3. 基于对话内容和已有画像，决定 add / set / delete 操作。\n"
        "4. 调用 write_profile 暂存修改（同一个用户的操作应整合到一次调用中）。\n"
        "5. 检查返回的 diff 和冲突信息，如有冲突则调整后重新暂存。\n"
        "6. 【重要】对已暂存修改的用户，用 count_profile_traits(user_id, include_staged=true) 检查提交后的有效 trait 数量。\n"
        "7. 如果 needs_compaction 为 true，说明该用户 trait 数将超 {{profile_trait_limit}} 上限，必须进行压缩：\n"
        "   - 用 write_profile 对同一用户发起 delete 操作删除不重要、过时的短期特征\n"
        "   - 用 write_profile 对同一用户发起 set 操作合并语义相近的 trait key\n"
        "   - 压缩后再次用 count_profile_traits 确认不超过 {{profile_trait_limit}}\n"
        "8. 全部暂存 + 压缩完成后，调用 preview_profile 检查汇总 diff。\n"
        "9. 确认 diff 完全正确后，调用 commit_profile 提交。\n"
        "10. 如果 commit_profile 返回 limit_exceeded，必须直接根据 violations 中的超限用户和 traits 对对应用户继续 delete / set 压缩，然后再次调用 commit_profile，直到提交成功或无可安全压缩内容；不要为了确认哪个用户超限而重复查询。\n"
        "11. 如果 commit_profile 返回 conflict，说明画像在本次 Agent 会话期间被外部修改。必须重新 read_profile(include_staged=true) 读取冲突用户，整合外部新值与当前暂存修改，再用 write_profile 调整暂存区，最后再次 commit_profile；不要直接结束任务。\n"
        "12. commit_profile 成功后，输出简短总结。\n\n"
        "硬性约束：\n"
        "- 只提取长期稳定的画像信息（身份、长期偏好、稳定习惯、关系认知、长期事实）。\n"
        "- 不记录短期状态、一次性事件、瞬时情绪、当天安排。\n"
        "- 严禁为 bot 自身（{{bot_user_ids}}）生成任何画像操作。\n"
        "- 只对在对话中展现了新信息的用户操作，旧画像无变化则不要动。\n"
        "- 禁止把完整画像全量重写，只输出增量操作。\n"
        "- 单个用户最终 trait 数不能超过 {{profile_trait_limit}} 条。\n"
        "- commit_profile 返回 conflict 时，必须重新读取冲突用户并整合后重试提交。\n"
        "- 未成功调用 commit_profile 前不要结束任务。"
    ),
    "summary_workflow_system": (
        "当前任务：总结群聊对话，生成记忆条目。\n\n"
        "请阅读群聊记录，将对话内容总结为一段或多段独立记忆：\n"
        "- 按话题或时间段自然拆分，不要强行把所有内容合并成一条\n"
        "- 每条记忆的 content 是对应片段的简短总结\n"
        "- 每条记忆标注 importance（1-5），反映该段对话的重要程度\n\n"
        "重要性评估标准：\n"
        "- 1分：无意义的闲聊、表情包测试、简短问候\n"
        "- 2分：简单的日常对话\n"
        "- 3分：一般的讨论交流\n"
        "- 4分：有意义的话题讨论或较深的互动\n"
        "- 5分：重要的决定、约定、深度的设定或情感交流\n\n"
        "硬性约束：\n"
        "- 只基于聊天记录中的可靠信息总结，不要编造内容\n"
        "- 输出必须使用简体中文\n\n"
        "请严格返回以下 JSON 格式：\n"
        "{{json_response_example}}"
    ),
    "json_response_example": (
        '{"memories": [{"content": "大家讨论了周末聚餐的安排，初步定在周六晚上吃火锅。", "importance": 3}, '
        '{"content": "小明分享了他最近去北海道的旅行经历，展示了照片。", "importance": 4}, '
        '{"content": "群里闲聊天气和日常。", "importance": 2}]}'
    ),
}

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
    default_file_name: str
    defaults: dict[str, str]


_RESOURCES = (
    PromptMigrationResource(
        resource_id="komari_chat",
        display_name="Komari Chat Prompt",
        legacy_file_path=Path("config") / "prompts" / "komari_memory.yaml",
        default_file_name="komari_memory.yaml",
        defaults=KOMARI_CHAT_PROMPT_DEFAULTS,
    ),
    PromptMigrationResource(
        resource_id="komari_memory_summary",
        display_name="Komari Memory Summary Prompt",
        legacy_file_path=Path("config") / "prompts" / "komari_memory_summary.yaml",
        default_file_name="komari_memory_summary.yaml",
        defaults=KOMARI_MEMORY_SUMMARY_PROMPT_DEFAULTS,
    ),
    PromptMigrationResource(
        resource_id="group_history_summary",
        display_name="Group History Summary Prompt",
        legacy_file_path=Path("config") / "prompts" / "group_history_summary.yaml",
        default_file_name="group_history_summary.yaml",
        defaults=GROUP_HISTORY_PROMPT_DEFAULTS,
    ),
)


@dataclass(frozen=True, slots=True)
class PromptPathConfig:
    """迁移脚本使用的提示词文件路径配置。"""

    prompt_dir: Path | None
    komari_chat: Path | None
    komari_memory_summary: Path | None
    group_history_summary: Path | None


def _load_yaml_mapping(path: Path) -> dict[str, Any]:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        msg = f"提示词 YAML 顶层必须是对象: {path}"
        raise TypeError(msg)
    return data


def _strip_inline_comment(value: str) -> str:
    quote: str | None = None
    escaped = False
    for index, char in enumerate(value):
        if escaped:
            escaped = False
            continue
        if char == "\\":
            escaped = True
            continue
        if quote is not None:
            if char == quote:
                quote = None
            continue
        if char in {'"', "'"}:
            quote = char
            continue
        if char == "#" and (index == 0 or value[index - 1].isspace()):
            return value[:index].rstrip()
    return value.strip()


def _unquote_dotenv_value(value: str) -> str:
    value = _strip_inline_comment(value)
    if len(value) < 2 or value[0] not in {'"', "'"} or value[-1] != value[0]:
        return value

    inner = value[1:-1]
    if value[0] == "'":
        return inner
    return bytes(inner, "utf-8").decode("unicode_escape")


def load_dotenv_values(path: Path) -> dict[str, str]:
    """读取 dotenv 文件，不依赖 python-dotenv 或项目配置代码。"""
    if not path.exists():
        msg = f"dotenv 文件不存在: {path}"
        raise FileNotFoundError(msg)

    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        match = _DOTENV_LINE_PATTERN.match(line)
        if match is None:
            logger.debug("忽略无法解析的 dotenv 行: %s", raw_line)
            continue
        values[match.group("key")] = _unquote_dotenv_value(match.group("value"))
    return values


def _dotenv_int(values: dict[str, str], key: str, default: int) -> int:
    raw_value = values.get(key)
    if raw_value is None or not raw_value.strip():
        return default
    return int(raw_value)


@dataclass(frozen=True, slots=True)
class PostgresConfig:
    """迁移脚本独立使用的 PostgreSQL 配置。"""

    host: str
    port: int
    database: str
    user: str
    password: str


def load_postgres_config(dotenv_path: Path) -> PostgresConfig:
    values = load_dotenv_values(dotenv_path)
    return PostgresConfig(
        host=values.get("PG_HOST") or "localhost",
        port=_dotenv_int(values, "PG_PORT", 5432),
        database=values.get("PG_DATABASE") or "komari_bot",
        user=values.get("PG_USER") or "",
        password=values.get("PG_PASSWORD") or "",
    )


def validate_prompt_values(
    defaults: dict[str, str],
    values: "Mapping[str, object]",
) -> dict[str, str]:
    """校验 prompt 字段并返回清洗后的完整数据。"""
    unknown_fields = sorted(set(values) - set(defaults))
    if unknown_fields:
        fields = ", ".join(unknown_fields)
        msg = f"存在未知提示词字段: {fields}"
        raise ValueError(msg)

    cleaned = dict(defaults)
    for key, value in values.items():
        if not isinstance(value, str) or not value.strip():
            msg = f"提示词字段 {key} 必须是非空字符串"
            raise ValueError(msg)
        cleaned[key] = value.rstrip("\n")
    return cleaned


def _resolve_project_path(path: Path) -> Path:
    if path.is_absolute():
        return path
    return PROJECT_ROOT / path


def _resolve_prompt_path(
    resource: PromptMigrationResource,
    prompt_paths: PromptPathConfig,
) -> Path:
    resource_path = getattr(prompt_paths, resource.resource_id)
    if resource_path is not None:
        return _resolve_project_path(resource_path)
    if prompt_paths.prompt_dir is not None:
        return _resolve_project_path(prompt_paths.prompt_dir / resource.default_file_name)
    return PROJECT_ROOT / resource.legacy_file_path


def _collect_prompt_values(
    resource: PromptMigrationResource,
    prompt_path: Path,
) -> dict[str, str] | None:
    if not prompt_path.exists():
        logger.info("跳过不存在的提示词文件: %s", prompt_path)
        return None

    data = _load_yaml_mapping(prompt_path)
    allowed_data = {
        key: value
        for key, value in data.items()
        if key in resource.defaults and isinstance(value, str)
    }
    return validate_prompt_values(resource.defaults, allowed_data)


async def _ensure_prompt_schema(conn: asyncpg.Connection) -> None:
    await conn.execute(_PROMPT_TABLE_DDL)
    await conn.execute(_PROMPT_UPDATED_AT_INDEX_DDL)


async def _upsert_prompt(
    conn: asyncpg.Connection,
    *,
    resource_id: str,
    display_name: str,
    prompt_data: dict[str, str],
) -> None:
    await conn.execute(
        """
        INSERT INTO komari_prompt_configs (
            resource_id,
            display_name,
            prompt_data,
            version
        )
        VALUES ($1, $2, $3::jsonb, $4)
        ON CONFLICT (resource_id) DO UPDATE SET
            display_name = EXCLUDED.display_name,
            prompt_data = EXCLUDED.prompt_data,
            version = EXCLUDED.version,
            updated_at = CURRENT_TIMESTAMP
        """,
        resource_id,
        display_name,
        json.dumps(prompt_data, ensure_ascii=False),
        "1.0",
    )


async def migrate_prompts(
    *,
    dry_run: bool,
    dotenv_path: Path,
    prompt_paths: PromptPathConfig,
) -> dict[str, int]:
    """迁移本轮支持的字符串 prompt。"""
    stats = {"success": 0, "skipped": 0, "failed": 0}
    conn: asyncpg.Connection | None = None
    if not dry_run:
        config = load_postgres_config(dotenv_path)
        conn = cast(
            "asyncpg.Connection",
            await asyncpg.connect(
                host=config.host,
                port=config.port,
                database=config.database,
                user=config.user,
                password=config.password,
            ),
        )
        await _ensure_prompt_schema(conn)

    try:
        for resource in _RESOURCES:
            prompt_path = _resolve_prompt_path(resource, prompt_paths)
            try:
                values = _collect_prompt_values(resource, prompt_path)
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
                    assert conn is not None
                    await _upsert_prompt(
                        conn,
                        resource_id=resource.resource_id,
                        display_name=resource.display_name,
                        prompt_data=values,
                    )
                    logger.info(
                        "已导入提示词: %s -> %s",
                        prompt_path,
                        resource.resource_id,
                    )
            except Exception:
                logger.exception(
                    "迁移提示词失败: %s",
                    prompt_path,
                )
                stats["failed"] += 1
                continue
            stats["success"] += 1
    finally:
        if conn is not None:
            await conn.close()

    return stats


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="将旧版 YAML prompt 导入 PostgreSQL")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="仅输出将导入的资源，不写入 PostgreSQL",
    )
    parser.add_argument(
        "--dotenv",
        type=Path,
        default=DEFAULT_DOTENV_PATH,
        help="dotenv 配置文件路径，默认读取项目根目录 .env",
    )
    parser.add_argument(
        "--prompt-dir",
        type=Path,
        help="提示词 YAML 目录，默认读取项目根目录 config/prompts",
    )
    parser.add_argument(
        "--komari-chat-prompt",
        type=Path,
        help="komari_chat 提示词 YAML 路径",
    )
    parser.add_argument(
        "--komari-memory-summary-prompt",
        type=Path,
        help="komari_memory_summary 提示词 YAML 路径",
    )
    parser.add_argument(
        "--group-history-summary-prompt",
        type=Path,
        help="group_history_summary 提示词 YAML 路径",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    prompt_paths = PromptPathConfig(
        prompt_dir=args.prompt_dir,
        komari_chat=args.komari_chat_prompt,
        komari_memory_summary=args.komari_memory_summary_prompt,
        group_history_summary=args.group_history_summary_prompt,
    )
    stats = asyncio.run(
        migrate_prompts(
            dry_run=args.dry_run,
            dotenv_path=args.dotenv,
            prompt_paths=prompt_paths,
        )
    )
    logger.info(
        "迁移统计: success=%s skipped=%s failed=%s",
        stats["success"],
        stats["skipped"],
        stats["failed"],
    )


if __name__ == "__main__":
    main()
