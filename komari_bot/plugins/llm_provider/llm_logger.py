"""LLM Provider 调用日志记录器。

按天记录脱敏后的调用元数据，禁止持久化 prompt、消息正文、模型输出和推理正文。
"""

from __future__ import annotations

import asyncio
import json
import random
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

from nonebot import logger
from nonebot.plugin import require

from komari_bot.common.llm_log_safety import (
    build_content_summary,
    sanitize_persisted_log_record,
    scrub_log_directory,
)

from .config_schema import DynamicConfigSchema

__all__ = ["build_content_summary", "log_llm_call", "scrub_legacy_logs"]

if TYPE_CHECKING:
    from .base_client import UnifiedUsageSchema

# 日志目录
_LOG_DIR = Path("logs") / "llm_provider"

# 写入锁（防止并发写入文件损坏）
_write_lock = asyncio.Lock()

# 安全回退日志保留天数
_FALLBACK_RETENTION_DAYS = 30

# 安全回退日志目录权限
_FALLBACK_LOG_DIR_PERMISSION_MODE = "0o700"

# 清理触发概率（每次写入时有 1% 概率执行清理）
_CLEANUP_PROBABILITY = 0.01

# 日志目录权限收敛缓存时间，避免每次写日志都重复读取动态配置与 stat/chmod
_LOG_DIR_ENSURE_INTERVAL_SECONDS = 60.0


class _LogDirEnsureState:
    """日志目录权限收敛缓存状态。"""

    def __init__(self) -> None:
        self.last_ensured_log_dir: Path | None = None
        self.last_log_dir_ensure_at = 0.0


_log_dir_ensure_state = _LogDirEnsureState()


def _get_runtime_config() -> DynamicConfigSchema:
    """读取 llm_provider 动态配置。"""
    config_manager_plugin = require("config_manager")
    config_manager = config_manager_plugin.get_config_manager(
        "llm_provider", DynamicConfigSchema
    )
    config = config_manager.get()
    if isinstance(config, DynamicConfigSchema):
        return config
    return DynamicConfigSchema.model_validate(config)


def _get_retention_days() -> int:
    """读取运行时日志保留天数，失败时使用安全回退值。"""
    try:
        return _get_runtime_config().llm_log_retention_days
    except Exception:
        logger.warning(
            "[LLM Provider] 读取日志保留天数配置失败，回退为 {} 天",
            _FALLBACK_RETENTION_DAYS,
            exc_info=True,
        )
        return _FALLBACK_RETENTION_DAYS


def _get_log_dir_permission_mode() -> str:
    """读取运行时日志目录权限模式，失败时使用安全回退值。"""
    try:
        return _get_runtime_config().llm_log_dir_permission_mode
    except Exception:
        logger.warning(
            "[LLM Provider] 读取日志目录权限配置失败，回退为 {}",
            _FALLBACK_LOG_DIR_PERMISSION_MODE,
            exc_info=True,
        )
        return _FALLBACK_LOG_DIR_PERMISSION_MODE


def _parse_permission_mode(mode: str) -> int | None:
    """解析八进制权限字符串，空字符串表示禁用 chmod。"""
    if mode == "":
        return None
    if not mode.startswith("0o"):
        logger.warning("[LLM Provider] 日志目录权限模式无效，已跳过 chmod: {}", mode)
        return None
    try:
        parsed_mode = int(mode, 8)
    except ValueError:
        logger.warning("[LLM Provider] 日志目录权限模式无效，已跳过 chmod: {}", mode)
        return None
    if parsed_mode < 0 or parsed_mode > 0o7777:
        logger.warning("[LLM Provider] 日志目录权限模式超出范围，已跳过 chmod: {}", mode)
        return None
    return parsed_mode


def _ensure_private_log_dir() -> None:
    """确保 LLM JSONL 日志目录存在，并按配置收敛权限。"""
    now = time.monotonic()
    if (
        _log_dir_ensure_state.last_ensured_log_dir == _LOG_DIR
        and now - _log_dir_ensure_state.last_log_dir_ensure_at
        < _LOG_DIR_ENSURE_INTERVAL_SECONDS
    ):
        return

    permission_mode = _parse_permission_mode(_get_log_dir_permission_mode())
    mkdir_mode = permission_mode if permission_mode is not None else 0o777

    _LOG_DIR.mkdir(mode=mkdir_mode, parents=True, exist_ok=True)

    _log_dir_ensure_state.last_ensured_log_dir = _LOG_DIR
    _log_dir_ensure_state.last_log_dir_ensure_at = now

    if permission_mode is None:
        return

    try:
        current_mode = _LOG_DIR.stat().st_mode & 0o7777
        if current_mode != permission_mode:
            _LOG_DIR.chmod(permission_mode)
    except OSError:
        logger.warning("[LLM Provider] 日志目录权限收敛失败", exc_info=True)


async def scrub_legacy_logs() -> int:
    """原地移除历史 JSONL 中的正文和 reasoning。"""
    if not _LOG_DIR.exists():
        return 0
    async with _write_lock:
        scrubbed = await asyncio.to_thread(scrub_log_directory, _LOG_DIR)
    if scrubbed:
        logger.warning("[LLM Provider] 已净化 {} 个历史调用日志文件", scrubbed)
    return scrubbed


async def log_llm_call(
    *,
    method: str,
    model: str,
    input_data: object,
    output: str | None = None,
    reasoning_chars: int = 0,
    finish_reason: str | None = None,
    tool_calls_count: int | None = None,
    error: str | None = None,
    error_type: str | None = None,
    duration_ms: float | None = None,
    usage: "UnifiedUsageSchema | None" = None,
) -> None:
    """仅记录一次 LLM 调用的安全元数据。

    Args:
        method: 调用方法名（generate_text / generate_text_with_messages）
        model: 模型名称
        input_data: 输入摘要；即使误传正文，落盘前也只保留哈希和安全字段
        output: LLM 返回文本，仅用于计算长度和哈希
        reasoning_chars: 推理正文字符数，正文不得传入记录器
        finish_reason: LLM 完成原因
        tool_calls_count: 工具调用数量
        error: 错误文本，仅用于计算长度和哈希
        error_type: 异常类型名
        duration_ms: 调用耗时（毫秒）
        usage: 后端实际返回的统一用量信息（仅写入已报告字段）
    """
    try:
        _ensure_private_log_dir()

        now = datetime.now().astimezone()
        today = now.strftime("%Y-%m-%d")
        log_file = _LOG_DIR / f"{today}.jsonl"

        raw_record: dict[str, Any] = {
            "timestamp": now.isoformat(),
            "method": method,
            "model": model,
            "input": input_data,
            "reasoning_chars": max(0, reasoning_chars),
        }
        if output is not None:
            raw_record["output"] = output
        if finish_reason is not None:
            raw_record["finish_reason"] = finish_reason
        if tool_calls_count is not None:
            raw_record["tool_calls_count"] = max(0, tool_calls_count)
        if error is not None:
            raw_record["error"] = error
            raw_record["error_type"] = error_type or "Exception"
        if duration_ms is not None:
            raw_record["duration_ms"] = round(duration_ms, 2)

        # 仅写入后端已报告的 usage 字段，None 不写入 JSONL
        if usage is not None:
            usage_data: dict[str, int] = {}
            if usage.input_tokens is not None:
                usage_data["input_tokens"] = usage.input_tokens
            if usage.cached_input_tokens is not None:
                usage_data["cached_input_tokens"] = usage.cached_input_tokens
            if usage.cache_miss_input_tokens is not None:
                usage_data["cache_miss_input_tokens"] = usage.cache_miss_input_tokens
            if usage.output_tokens is not None:
                usage_data["output_tokens"] = usage.output_tokens
            if usage.reasoning_output_tokens is not None:
                usage_data["reasoning_output_tokens"] = usage.reasoning_output_tokens
            if usage.total_tokens is not None:
                usage_data["total_tokens"] = usage.total_tokens
            if usage_data:
                raw_record["usage"] = usage_data

        record = sanitize_persisted_log_record(raw_record)
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


async def cleanup_old_logs(retention_days: int | None = None) -> None:
    """清理过期日志文件。

    Args:
        retention_days: 保留天数；为 None 时读取动态配置
    """
    try:
        if retention_days is None:
            retention_days = _get_retention_days()

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
