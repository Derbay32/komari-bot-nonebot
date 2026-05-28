"""Komari Memory YAML 提示词模板加载器（支持热重载）."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from nonebot import logger

# 默认模板值（YAML 文件缺失或字段缺失时使用）
_DEFAULTS: dict[str, str] = {
    "system_prompt": "你是一个友善的助手。",
    "memory_ack": "好的，我了解了。",
    "memory_ack_role": "assistant",
    "output_instruction": "请将最终回复放在 <content></content> 标签中。",
    "cot_prefix": "<think>\n开始思考。\n",
    "cot_prefix_role": "assistant",
}

# 缓存
_cache: dict[str, Any] = {}
_cache_mtime: float = 0.0

# 模板文件路径
_TEMPLATE_PATH = Path("config") / "prompts" / "komari_memory.yaml"


def _resolve_path() -> Path:
    """解析模板文件绝对路径."""
    if _TEMPLATE_PATH.is_absolute():
        return _TEMPLATE_PATH
    return _TEMPLATE_PATH.resolve()


def get_template() -> dict[str, str]:
    """获取最新的提示词模板（基于文件 mtime 热重载）.

    Returns:
        包含 system_prompt、memory_ack、memory_ack_role、output_instruction、cot_prefix、cot_prefix_role 的字典.

    """
    global _cache, _cache_mtime  # noqa: PLW0603

    path = _resolve_path()

    try:
        mtime = path.stat().st_mtime
    except OSError:
        if not _cache:
            logger.warning("[PromptTemplate] 模板文件不存在: {}，使用默认值", path)
            _cache = dict(_DEFAULTS)
        return _cache

    # 文件未变化，使用缓存
    if mtime == _cache_mtime and _cache:
        return _cache

    # 重新加载
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}

        result = dict(_DEFAULTS)
        for key in _DEFAULTS:
            if key in data and isinstance(data[key], str):
                result[key] = data[key].rstrip("\n")

        _cache = result
        _cache_mtime = mtime
        logger.info("[PromptTemplate] 模板已加载/重载: {}", path)

    except yaml.YAMLError:
        logger.warning(
            "[PromptTemplate] YAML 解析失败，使用缓存或默认值", exc_info=True
        )
        if not _cache:
            _cache = dict(_DEFAULTS)
    except OSError:
        logger.warning("[PromptTemplate] 文件读取失败，使用缓存或默认值", exc_info=True)
        if not _cache:
            _cache = dict(_DEFAULTS)

    return _cache
