"""Komari Memory 总结 YAML 提示词模板加载器（支持热重载）。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from nonebot import logger

DEFAULTS: dict[str, str] = {
    "summary_prompt": (
        "请总结以下群聊或私聊对话，提取每个用户画像与互动历史的增量操作，并评估对话的重要性。输出必须使用简体中文。\n\n"
        "每条消息格式为 [user_id:xxx] 昵称: 内容。请你在提取时将 user_id 准确关联。\n\n"
        "{{conversation_text}}\n\n"
        "{{existing_context}}"
        "【任务一：用户画像增量操作提取（按用户聚合）】\n"
        "- 提取对话的核心内容，形成 summary（简短总结）。\n"
        "- 输出 `user_profile_operations` 数组，每个元素对应一个用户，字段包含：\n"
        "  - user_id\n"
        "  - display_name（仅作为用户识别提示，可为空字符串）\n"
        "  - operations（数组），每个操作都必须包含 op/field，按需要补充 key/value/category/importance\n"
        "- `op` 只允许：add / replace / delete。\n"
        "- `field` 只允许：trait。\n"
        "- `display_name` 绝对不能作为可修改字段写进 operations；禁止新增、删除、替换 display_name。\n"
        "- 当 `field=trait` 时，必须提供 `key`；若 `op` 为 add 或 replace，还必须提供 `value/category/importance`。\n"
        "- category 仅可取：preference/fact/relation/general。\n"
        "- traits 只保留长期稳定、可复用的画像信息，例如身份、长期偏好、稳定习惯、关系认知、长期事实。\n"
        "- 不要输出短期状态、一次性事件、瞬时情绪、当天安排，也不要把语义相近的 traits 拆成多个近义 key。\n"
        "- 你输出的是“增量操作”，不是最终完整画像；禁止把完整 traits 全量重写出来。\n\n"
        "【任务二：主观互动备忘录增量操作提取】\n"
        '- 你必须基于《败犬女主太多了！》中"小鞠知花"的人设视角，为有明显互动行为的用户，提取出在互动期间该用户的行为记录。这将被作为"小鞠在心里对近期互动过的用户的悄悄记录"。\n'
        "- 输出 `user_interaction_operations` 数组，每个元素对应一个用户，字段包含：user_id、display_name、operations。\n"
        "- `op` 只允许：add / replace / delete。\n"
        "- `field` 只允许：file_type / description / summary / record。\n"
        "- 当 `field=record` 时，`value` 必须是对象，包含 event/result/emotion；record 仅允许 add 或 delete，若要改写旧 record，请先 delete 再 add。\n"
        "- `summary` 只表示本次互动结束后形成的新的整体印象或评价；`records` 只记录本次对话新增或需要删除的互动片段。\n"
        "- 你输出的是“增量操作”，不是最终完整互动历史；禁止把完整 records 全量重写出来。\n\n"
        "【额外硬性约束】\n"
        "- `display_name` 由程序根据 binding 和昵称维护，你只能把它当作识别线索，绝对不要尝试修改它。\n"
        "- 严禁为 `[bot]` 消息、机器人自己、assistant、自身昵称或机器人别名生成任何画像或互动历史操作。\n"
        "- bot 发言只用于理解上下文，不能作为要写入数据库的用户条目。\n\n"
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
        "每个分段总结都已经是结构化结果，包含 summary、user_profile_operations、user_interaction_operations、importance。\n"
        "请你基于这些分段结果，输出一份全局统一的最终 JSON：\n"
        "- 合并重复 user_id 的操作，按时间顺序保留最终需要执行的 add/replace/delete 指令\n"
        "- 对近义或重复 trait 进行合并，统一为更稳定的 key，尽量减少多余操作\n"
        "- 对同一用户的互动 record 操作进行去重，保持 add/delete 的实际执行顺序\n"
        "- `display_name` 只作为识别提示，绝对不要把它写进 operations，也不要尝试修改它\n"
        "- 严禁为 `[bot]` 消息、机器人自己、assistant、自身昵称或机器人别名生成任何操作\n"
        "- 不要输出最终完整画像或完整互动历史，只输出最终需要执行的增量操作\n"
        "- 产出一份整体 summary 和整体 importance\n"
        "- 不要按分段分别输出，不要解释推理过程\n\n"
        "{{chunk_summaries_text}}\n\n"
        "{{existing_context}}"
        "请严格返回以下 JSON 格式：\n"
        "{{json_response_example}}"
    ),
    "existing_context_instruction_block": (
        "【重要指示】\n"
        "- 你输出的是按用户聚合的增量操作，不要输出扁平 entities 列表\n"
        "- 不要输出最终完整画像或完整互动历史，只输出需要程序执行的 add/replace/delete 操作\n"
        "- `display_name` 由程序维护，绝对不要把它写进 operations，也不要尝试修改它\n"
        "- 严禁为 `[bot]` 消息、机器人自己、assistant、自身昵称或机器人别名生成任何操作\n"
        "- 如果对话中发现与已有画像矛盾的新信息，请输出 replace 或 delete 操作\n"
        "- 如果对话中没有提到某个旧特征或旧互动，不要重复输出它\n"
    ),
    "existing_profiles_header": (
        "【已知用户画像（数据库中已有记录）】\n以下是目前已存储的用户画像："
    ),
    "existing_interactions_header": (
        "【已知互动历史（数据库中已有记录）】\n以下是目前已存储的用户互动备忘录："
    ),
    "truncated_context_marker": (
        "【提示】其余已有记录已按 token 上限省略，请仅基于当前提供的已知信息做去重与覆盖。"
    ),
    "json_response_example": (
        '{"summary": "...", "user_profile_operations": '
        '[{"user_id": "12345", "display_name": "阿明", "operations": '
        '[{"op": "add", "field": "trait", "key": "喜欢的食物", "value": "拉面", "category": "preference", "importance": 4}, '
        '{"op": "replace", "field": "trait", "key": "怕冷", "value": "换季时会裹紧外套", "category": "fact", "importance": 3}, '
        '{"op": "delete", "field": "trait", "key": "短期状态"}]}], '
        '"user_interaction_operations": [{"user_id": "12345", "display_name": "阿明", "operations": '
        '[{"op": "add", "field": "record", "value": {"event": "用好吃的诱惑我", "result": "咽了口水，稍微凑近了过去", "emotion": "有点警惕但很想吃"}}, '
        '{"op": "replace", "field": "summary", "value": "是个经常用食物钓我的骗子先生……但也不是坏人。"}, '
        '{"op": "delete", "field": "record", "value": {"event": "旧事件", "result": "旧反应", "emotion": "旧情绪"}}]}], '
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
