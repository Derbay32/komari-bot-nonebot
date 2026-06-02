"""Komari Memory 总结 YAML 提示词模板加载器（支持热重载）。"""

from pathlib import Path
from typing import Any

import yaml
from nonebot import logger

DEFAULTS: dict[str, str] = {
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
