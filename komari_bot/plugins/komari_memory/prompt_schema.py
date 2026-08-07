"""Komari Memory 总结提示词强类型表 Schema。

本模块无副作用：只依赖 common 层 typed_config，不导入业务插件包、不访问
数据库，可被 Alembic 迁移环境与 ``typed_config`` 安全加载器直接加载。
``DEFAULTS`` 是该资源全部 Prompt 字段默认值的唯一定义来源，运行时模板
加载器（``services/summary_prompt_template.py``）与管理 API 均从这里导入。
"""

from __future__ import annotations

from typing import ClassVar

from sqlalchemy import Text

from komari_bot.config.typed_config import Field, TypedPromptModel

RESOURCE_ID = "komari_memory_summary"
DISPLAY_NAME = "Komari Memory Summary Prompt"

# 默认模板值（PG 配置缺失或读取失败时使用）
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


class KomariMemorySummaryPromptSchema(TypedPromptModel, table=True):
    """Komari Memory 总结提示词强类型表（单行，由 PromptStorage 管理）。

    正文列统一为 TEXT；非空与内容预算校验继续由
    ``validate_prompt_values`` 在写入前承担，不下沉到模型。
    """

    prompt_resource_id: ClassVar[str] = RESOURCE_ID
    __tablename__ = "komari_prompt_memory_summary"

    memory_summary_common_system: str = Field(
        default="", sa_type=Text, description="记忆总结公共系统提示词"
    )
    profile_agent_workflow_system: str = Field(
        default="", sa_type=Text, description="画像维护 Agent 工作流系统提示词"
    )
    summary_workflow_system: str = Field(
        default="", sa_type=Text, description="对话总结工作流系统提示词"
    )
    json_response_example: str = Field(
        default="", sa_type=Text, description="总结输出 JSON 示例"
    )
