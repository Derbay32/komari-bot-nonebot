"""LLM Provider 调用日志记录器。

按天记录每次 LLM 调用的输入与输出，JSONL 格式，自动清理 30 天前的日志。
"""

from __future__ import annotations

import asyncio
import json
import random
from datetime import datetime, timedelta
from pathlib import Path

from nonebot import logger

# 日志目录
_LOG_DIR = Path("logs") / "llm_provider"

# 写入锁（防止并发写入文件损坏）
_write_lock = asyncio.Lock()

# 日志保留天数
_RETENTION_DAYS = 30

# 清理触发概率（每次写入时有 1% 概率执行清理）
_CLEANUP_PROBABILITY = 0.01


async def log_llm_call(
    *,
    method: str,
    model: str,
    input_data: dict | list | str,
    output: str | None = None,
    error: str | None = None,
    duration_ms: float | None = None,
) -> None:
    """记录一次 LLM 调用的输入与输出。

    Args:
        method: 调用方法名（generate_text / generate_text_with_messages）
        model: 模型名称
        input_data: 输入内容（messages 列表、prompt 字符串或结构化字典）
        output: LLM 返回的文本（成功时）
        error: 错误信息（失败时）
        duration_ms: 调用耗时（毫秒）
    """
    try:
        _LOG_DIR.mkdir(parents=True, exist_ok=True)

        now = datetime.now().astimezone()
        today = now.strftime("%Y-%m-%d")
        log_file = _LOG_DIR / f"{today}.jsonl"

        record = {
            "timestamp": now.isoformat(),
            "method": method,
            "model": model,
            "input": input_data,
        }
        if output is not None:
            record["output"] = output
        if error is not None:
            record["error"] = error
        if duration_ms is not None:
            record["duration_ms"] = round(duration_ms, 2)

        line = json.dumps(record, ensure_ascii=False) + "\n"

        async with _write_lock:
            with log_file.open("a", encoding="utf-8") as f:
                f.write(line)

        logger.debug("[LLM Provider] 日志已记录: method={}, model={}", method, model)

        # 概率触发清理
        if random.random() < _CLEANUP_PROBABILITY:
            await cleanup_old_logs()

    except Exception:
        logger.warning("[LLM Provider] 日志写入失败", exc_info=True)


async def cleanup_old_logs(retention_days: int = _RETENTION_DAYS) -> None:
    """清理过期日志文件。

    Args:
        retention_days: 保留天数，默认 30 天
    """
    try:
        if not _LOG_DIR.exists():
            return

        cutoff = datetime.now().astimezone() - timedelta(days=retention_days)
        cutoff_str = cutoff.strftime("%Y-%m-%d")
        removed = 0

        for log_file in _LOG_DIR.glob("*.jsonl"):
            # 文件名格式: YYYY-MM-DD.jsonl
            date_str = log_file.stem
            if date_str < cutoff_str:
                log_file.unlink()
                removed += 1

        if removed > 0:
            logger.info("[LLM Provider] 已清理 {} 个过期日志文件", removed)
    except Exception:
        logger.warning("[LLM Provider] 日志清理失败", exc_info=True)
