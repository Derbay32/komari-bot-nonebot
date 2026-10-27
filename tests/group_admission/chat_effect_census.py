"""TSK-225 聊天即时效果准入 census（测试真源，非测试模块）。

闭合登记 komari_chat 及其直接编排的可独立撤销瞬时效果的稳定 ID、协议桶
与 seam。与 ``acceptance_manifest.ADMISSION_EFFECT_CASES`` 的 komari_chat 行
双向一致，由 ``test_chat_effect_manifest`` 验证（对应 AC-1 / AC-10）。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ChatEffectRow:
    """一条聊天即时效果 census 行。"""

    effect_id: str
    source_symbol: str
    sink_kind: str
    seam: str


_E = ChatEffectRow

CHAT_EFFECT_CENSUS: tuple[ChatEffectRow, ...] = (
    _E(
        "group_admission.effect.chat.llm_round",
        "DshChatService.generate",
        "dsh_chat_task",
        "komari_chat/services/dsh_chat_service.py",
    ),
    _E(
        "group_admission.effect.chat.tool_dispatch",
        "ChatQueryTools.callbacks",
        "tool_body_execution",
        "komari_chat/services/chat_query_tools.py",
    ),
    _E(
        "group_admission.effect.chat.tool_search",
        "ChatQueryTools._search_web_tool",
        "komari_search_http",
        "komari_chat/services/chat_query_tools.py",
    ),
    _E(
        "group_admission.effect.chat.tool_fetch_page",
        "ChatQueryTools._fetch_page_tool",
        "komari_search_http",
        "komari_chat/services/chat_query_tools.py",
    ),
    _E(
        "group_admission.effect.chat.image_download",
        "NativeImageInput.prepare",
        "image_http_download",
        "komari_chat/services/native_image.py",
    ),
    _E(
        "group_admission.effect.chat.recall",
        "MessageHandler._generate_reply_core",
        "hindsight_recall_http",
        "komari_chat/handlers/message_handler.py",
    ),
    _E(
        "group_admission.effect.chat.memory_candidate",
        "MessageHandler._retain_candidate",
        "hindsight_retain_http",
        "komari_chat/handlers/message_handler.py",
    ),
    _E(
        "group_admission.effect.chat.group_read",
        "MessageHandler._refetch_reply",
        "onebot_get_msg",
        "komari_chat/handlers/message_handler.py",
    ),
    _E(
        "group_admission.effect.chat.reaction",
        "MessageHandler._schedule_reply_reaction",
        "onebot_set_msg_emoji_like",
        "komari_chat/handlers/message_handler.py",
    ),
    _E(
        "group_admission.effect.chat.reply_send",
        "OneBotReplySender",
        "onebot_send_group_msg",
        "komari_chat/services/reply_delivery_onebot.py",
    ),
    _E(
        "group_admission.effect.chat.fixed_failure_text",
        "MessageHandler.report_reply_failure",
        "onebot_group_error_text",
        "komari_chat/handlers/message_handler.py",
    ),
    _E(
        "group_admission.effect.chat.debug_public",
        "generate_debug_reply",
        "onebot_debug_public_output",
        "komari_chat/__init__.py",
    ),
)
