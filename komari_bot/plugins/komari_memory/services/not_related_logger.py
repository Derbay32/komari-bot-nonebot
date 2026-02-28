"""not related 日志记录器，用于记录 LLM 判断为无关的用户输入，供 BERT 模型优化。"""

from __future__ import annotations

import asyncio
from datetime import datetime
from logging import getLogger
from pathlib import Path

logger = getLogger(__name__)

# 日志目录
_LOG_DIR = Path("logs") / "not_related"

# 写入锁（防止并发写入文件损坏）
_write_lock = asyncio.Lock()

# 标记值（大小写不敏感匹配）
NOT_RELATED_MARKER = "not related"


def is_not_related(reply: str) -> bool:
    """判断 LLM 回复是否为 not related.

    Args:
        reply: 经过 XML 标签提取后的回复内容

    Returns:
        True 表示 LLM 认为该输入与角色无关
    """
    return reply.strip().lower() == NOT_RELATED_MARKER


async def log_not_related(
    user_message: str,
    group_id: str,
    user_id: str,
    score: float | None = None,
) -> None:
    """将 LLM 判断为无关的用户输入追加写入日志文件.

    日志格式: 每行一条 TSV 记录 (timestamp, group_id, user_id, score, message)
    文件按天分割: not_related/2025-02-25.tsv

    Args:
        user_message: 用户原始消息
        group_id: 群组 ID
        user_id: 用户 ID
        score: BERT 评分（可选）
    """
    try:
        _LOG_DIR.mkdir(parents=True, exist_ok=True)

        today = datetime.now().astimezone().strftime("%Y-%m-%d")
        log_file = _LOG_DIR / f"{today}.tsv"

        timestamp = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S")
        score_str = f"{score:.4f}" if score is not None else ""
        # 替换换行符，防止破坏 TSV 格式
        safe_message = user_message.replace("\n", "\\n").replace("\t", "\\t")

        line = f"{timestamp}\t{group_id}\t{user_id}\t{score_str}\t{safe_message}\n"

        async with _write_lock:
            with log_file.open("a", encoding="utf-8") as f:
                f.write(line)

        logger.debug(
            "[KomariMemory] not related 已记录: user=%s, group=%s", user_id, group_id
        )
    except Exception:
        logger.warning("[KomariMemory] not related 日志写入失败", exc_info=True)
