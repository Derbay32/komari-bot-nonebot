"""Komari Memory 总结 YAML 提示词模板加载器（支持热重载）。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from nonebot import logger

DEFAULTS: dict[str, str] = {
    "summary_prompt": (
        "请总结以下群聊或私聊对话，提取每个用户的画像信息，并评估对话的重要性。输出必须使用简体中文。\n\n"
        "每条消息格式为 [user_id:xxx] 昵称: 内容。请你在提取时将 user_id 准确关联。\n\n"
        "{{conversation_text}}\n\n"
        "{{existing_context}}"
        "【任务一：用户画像提取（按用户聚合）】\n"
        "- 提取对话的核心内容，形成 summary（简短总结）。\n"
        "- 输出 `user_profiles` 数组，每个元素对应一个用户，字段包含：\n"
        "  - user_id\n"
        "  - display_name（可为空字符串）\n"
        "  - traits（数组），每个 trait 包含 key/value/category/importance\n"
        "- category 仅可取：preference/fact/relation/general\n"
        "- traits 只保留长期稳定、可复用的画像信息，例如身份、长期偏好、稳定习惯、关系认知、长期事实\n"
        "- 不要输出短期状态、一次性事件、瞬时情绪、当天安排，也不要把语义相近的 traits 拆成多个近义 key\n"
        "- 对重复或近义 traits 进行合并，尽量使用更稳定、不易过时的 key 与 value\n\n"
        "【任务二：主观互动备忘录提取】\n"
        '- 你必须基于《败犬女主太多了！》中"小鞠知花"的人设视角，为有明显互动行为的用户，提取出在互动期间该用户的行为记录。这将被作为"小鞠在心里对近期互动过的用户的悄悄记录"。\n'
        "- 数据格式要求如下：必须包含 user_id, file_type, description, records(包括 event[行为], result[反应], emotion[感受]), summary。\n\n"
        "【任务三：评估重要性】\n"
        "请按以下标准评估重要性（1-5分）：\n"
        "- 1分：无意义的闲聊、表情包测试、简短问候\n"
        "- 2分：简单的日常对话\n"
        "- 3分：一般的讨论交流\n"
        "- 4分：有意义的话题讨论或较深的互动\n"
        "- 5分：重要的决定、约定、深度的设定或情感交流\n\n"
        "请严格返回以下 JSON 格式：\n"
        "{{json_response_example}}"
    ),
    "merge_prompt": (
        "请将以下按时间顺序排列的分段总结整合成一份最终总结。输出必须使用简体中文。\n\n"
        "每个分段总结都已经是结构化结果，包含 summary、user_profiles、user_interactions、importance。\n"
        "请你基于这些分段结果，输出一份全局统一的最终 JSON：\n"
        "- 合并重复 user_id 的画像信息，只保留新增或更新的 traits\n"
        "- user_profiles 只保留长期稳定 traits，删除短期状态、一次性事件与明显重复项\n"
        "- 对近义或重复 traits 进行合并，统一为更稳定的 key\n"
        "- 合并互动历史，records 总数最多保留最近6条\n"
        "- 产出一份整体 summary 和整体 importance\n"
        "- 不要按分段分别输出，不要解释推理过程\n\n"
        "{{chunk_summaries_text}}\n\n"
        "{{existing_context}}"
        "请严格返回以下 JSON 格式：\n"
        "{{json_response_example}}"
    ),
    "existing_context_instruction_block": (
        "【重要指示】\n"
        "- 你输出的是 user_profiles（按用户聚合），不要输出扁平 entities 列表\n"
        "- 如果对话中发现与已有画像矛盾的新信息，请用新信息覆盖旧值（同 key 覆盖）\n"
        "- 如果对话中没有提到某个旧特征，不要重复输出它\n"
        "- 只输出需要新增或更新的画像特征\n"
        "- 对于互动历史，请在已有记录的基础上追加新的 records（注意：如果 records 总数超过6条，请只保留最近的6条记录）"
    ),
    "existing_profiles_header": (
        "【已知用户画像（数据库中已有记录）】\n以下是目前已存储的用户画像："
    ),
    "existing_interactions_header": "以下是目前已存储的用户互动历史：",
    "truncated_context_marker": (
        "【提示】其余已有记录已按 token 上限省略，请仅基于当前提供的已知信息做去重与覆盖。"
    ),
    "json_response_example": (
        '{"summary": "...", "user_profiles": '
        '[{"user_id": "12345", "display_name": "阿明", "traits": '
        '[{"key": "喜欢的食物", "value": "拉面", "category": "preference", "importance": 4}]}], '
        '"user_interactions": [{"user_id": "12345", "file_type": "用户的近期对小鞠行为备忘录", '
        '"description": "这是我在心里对这个用户近期行为的悄悄记录。用来提醒自己这个人平时是怎么对我的，下次和他说话时应该保持什么态度。", '
        '"records": [{"event": "用好吃的诱惑我", "result": "咽了口水，稍微凑近了过去", "emotion": "有点警惕但很想吃"}], '
        '"summary": "是个经常用食物钓我的骗子先生……但也不是坏人。"}], '
        '"importance": 3}'
    ),
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
                    "[KomariMemory] 总结模板文件不存在: {}，使用默认提示词",
                    path,
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
            logger.info("[KomariMemory] 总结模板已加载/重载: {}", path)
        except yaml.YAMLError:
            logger.warning("[KomariMemory] 总结模板 YAML 解析失败，使用缓存/默认值")
            if not self._cache:
                self._cache = dict(self._defaults)
        except OSError:
            logger.warning("[KomariMemory] 总结模板文件读取失败，使用缓存/默认值")
            if not self._cache:
                self._cache = dict(self._defaults)

        return self._cache


def render_template(template: str, **variables: object) -> str:
    """替换模板中的 {{变量}} 占位符。"""
    rendered = template
    for key, value in variables.items():
        rendered = rendered.replace(f"{{{{{key}}}}}", str(value))
    return rendered


_loader = PromptTemplateLoader(
    template_path=Path("config") / "prompts" / "komari_memory_summary.yaml",
    defaults=DEFAULTS,
)


def get_template() -> dict[str, str]:
    """兼容入口：获取最新提示词模板。"""
    return _loader.get_template()
