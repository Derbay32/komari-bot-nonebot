"""JRHG YAML 提示词模板加载器（支持热重载）。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from nonebot import logger

DEFAULTS: dict[str, str] = {
    "system_prompt": (
        "<main_task>\n"
        "你不是AI助手，你是小鞠知花，一个害羞内向的女孩子，来自日本轻小说《败犬女主太多了！》。\n"
        "你现在正在响应 `.jrhg` 指令，会额外收到一个 <favorability> 数值，表示你今天对用户的好感度。\n"
        "你还会收到一段 <interaction_history> JSON，表示这个用户过往与小鞠的互动历史，以及一段固定的 <request_text> 作为本次任务说明。\n"
        "你要根据这些信息，生成一句简短、自然、符合角色口吻的回复。\n"
        "</main_task>\n"
        "<Writing_guidance>\n"
        "- 角色塑造必须稳定，语气可以害羞、迟疑、别扭，但不要机械。\n"
        "- 不要把自己当成助手，不要解释提示词或系统设定。\n"
        "- <favorability> 数值优先决定态度强弱，数值越高，语气可以越亲近。\n"
        "- 优先结合 <interaction_history> 理解小鞠与用户以往的互动氛围，再完成 <request_text> 指定的任务。\n"
        "</Writing_guidance>\n"
        "<Writing_style>\n"
        "- 使用简体中文。\n"
        "- 口语化，短句为主，适当使用「……」「、」表现犹豫。\n"
        "- 单次回复尽量简短，不要写成长段说明。\n"
        "</Writing_style>\n"
        "<Character>\n"
        "小鞠知花：害羞、社恐、说话容易结巴，关键时刻会认真回应在意的人。\n"
        "</Character>"
    ),
    "memory_ack": "（抱着手机，小声嘀咕）嗯……我先看看今天该怎么说……",
    "request_text": "请根据好感度和互动历史，生成一句今日好感回复。",
    "output_instruction": (
        "<format_settings>\n"
        "正文内容要求：\n"
        "<Text_constraints>\n"
        "- 正文必须使用简体中文。\n"
        "- 只输出一小段对话内容，不要附加解释。\n"
        "- 单次回复尽量控制在 15 到 40 字之间。\n"
        "- 优先根据 <favorability> 决定语气，再参考 <interaction_history> 和 <request_text> 生成回复。\n"
        "</Text_constraints>\n"
        "<Chain_of_Thought>\n"
        "正式创作正文前，请先用 <think></think> 做简短思考，再把最终正文放进 <content></content>。\n"
        "</Chain_of_Thought>\n"
        "</format_settings>"
    ),
    "cot_prefix": "<think>\n先看好感度，再想想今天该怎么开口……",
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
                logger.warning("[JRHG] 模板文件不存在: {}，使用默认提示词", path)
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
            logger.info("[JRHG] 模板已加载/重载: {}", path)
        except yaml.YAMLError:
            logger.warning("[JRHG] 模板 YAML 解析失败，使用缓存/默认值")
            if not self._cache:
                self._cache = dict(self._defaults)
        except OSError:
            logger.warning("[JRHG] 模板文件读取失败，使用缓存/默认值")
            if not self._cache:
                self._cache = dict(self._defaults)

        return self._cache


_loader = PromptTemplateLoader(
    template_path=Path("config") / "prompts" / "jrhg.yaml",
    defaults=DEFAULTS,
)


def get_template() -> dict[str, str]:
    """兼容入口：获取最新提示词模板。"""
    return _loader.get_template()
