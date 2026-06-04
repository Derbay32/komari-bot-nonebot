"""群聊历史总结 PostgreSQL 提示词模板加载器。"""

from __future__ import annotations

from komari_bot.common.prompt_storage import PromptTemplateLoader

DEFAULTS: dict[str, str] = {
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


_RESOURCE_ID = "group_history_summary"

_loader = PromptTemplateLoader(
    resource_id=_RESOURCE_ID,
    display_name="Group History Summary Prompt",
    defaults=DEFAULTS,
    log_prefix="[GroupHistorySummary]",
)


def get_template() -> dict[str, str]:
    """兼容入口：获取最新提示词模板。"""
    return _loader.get_template()
