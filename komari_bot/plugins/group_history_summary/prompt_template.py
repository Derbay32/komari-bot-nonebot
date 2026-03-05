"""群聊历史总结 YAML 提示词模板加载器（支持热重载）。"""

from __future__ import annotations

from logging import getLogger
from pathlib import Path
from typing import Any

import yaml

logger = getLogger(__name__)

DEFAULTS: dict[str, str] = {
    "system_prompt": "你是一个专业的群聊总结助手，只基于聊天记录归纳事实。",
    "memory_ack": "已收到聊天记录，我先梳理重点。",
    "output_instruction": (
        "请仅输出总结正文，使用 <content></content> 包裹。"
        "正文控制在 120-220 字，尽量清晰、紧凑、客观。"
    ),
    "cot_prefix": "<think>\n我先按时间梳理讨论脉络，再输出总结。\n",
}


class PromptTemplateLoader:
    """提示词模板加载器。"""

    def __init__(self, template_path: Path, defaults: dict[str, str]) -> None:
        self._template_path = template_path
        self._defaults = defaults
        self._cache: dict[str, Any] = {}
        self._cache_mtime: float = 0.0

    def _resolve_path(self) -> Path:
        if self._template_path.is_absolute():
            return self._template_path
        return self._template_path.resolve()

    def get_template(self) -> dict[str, str]:
        """获取最新提示词模板（基于 mtime 热重载）。"""
        path = self._resolve_path()

        try:
            mtime = path.stat().st_mtime
        except OSError:
            if not self._cache:
                logger.warning(
                    "[GroupHistorySummary] 模板文件不存在: %s，使用默认提示词", path
                )
                self._cache = dict(self._defaults)
            return self._cache

        if self._cache and mtime == self._cache_mtime:
            return self._cache

        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}

            merged = dict(self._defaults)
            for key in self._defaults:
                if key in data and isinstance(data[key], str):
                    merged[key] = data[key].rstrip("\n")

            self._cache = merged
            self._cache_mtime = mtime
            logger.info("[GroupHistorySummary] 模板已加载/重载: %s", path)
        except yaml.YAMLError:
            logger.warning("[GroupHistorySummary] 模板 YAML 解析失败，使用缓存/默认值")
            if not self._cache:
                self._cache = dict(self._defaults)
        except OSError:
            logger.warning("[GroupHistorySummary] 模板文件读取失败，使用缓存/默认值")
            if not self._cache:
                self._cache = dict(self._defaults)

        return self._cache


_loader = PromptTemplateLoader(
    template_path=Path("config") / "prompts" / "group_history_summary.yaml",
    defaults=DEFAULTS,
)


def get_template() -> dict[str, str]:
    """兼容入口：获取最新提示词模板。"""
    return _loader.get_template()

