"""TSK-224 event gate frozen census (test truth source, not test module).

EventCensusRow: event_class, category, family, attribution_source, acceptance_anchor.
QQEventCensusRow: event_class, qualification, acceptance_anchor.
MatcherEntryRow: entry_id, source_path, source_symbol, factory, event_family, effect_ids, acceptance_anchor.

``ONEBOT_EVENT_CENSUS`` / ``MATCHER_ENTRY_CENSUS`` 是严格的确定性枚举：
新增 V11 事件子类或 matcher 注册行必须一并更新本 census，否则对应测试失败。

``ONEBOT_EVENT_CENSUS``: 22 个 OneBot V11 事件子类。
``QQ_EVENT_CENSUS``: QQ 入口允许的精确事件与显式拒绝事件闭集。
``MATCHER_ENTRY_CENSUS``: 24 个 matcher 注册项（TSK-277 退役旧
character_binding 四个 on_command，新增一个 QQ handler on_message）。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class EventCensusRow:
    event_class: str
    category: str
    family: str
    attribution_source: str
    acceptance_anchor: str


@dataclass(frozen=True, slots=True)
class QQEventCensusRow:
    event_class: str
    qualification: str
    acceptance_anchor: str


@dataclass(frozen=True, slots=True)
class MatcherEntryRow:
    entry_id: str
    source_path: str
    source_symbol: str
    factory: str
    event_family: str
    effect_ids: tuple[str, ...]
    acceptance_anchor: str


_CENSUS_ANCHOR = "tests/group_admission/test_entry_gate_census.py::test_event_census_exact_descendants"
_QQ_CENSUS_ANCHOR = "tests/group_admission/test_qq_event_gate.py::test_qq_event_closed_set"
_MATCHER_ANCHOR = "tests/group_admission/test_entry_gate_census.py::test_matcher_census_exact_registrations"
_EFFECT_ID = "group_admission.effect.inbound_matcher_dispatch"

_E = EventCensusRow
_Q = QQEventCensusRow
_M = MatcherEntryRow

ONEBOT_EVENT_CENSUS: tuple[EventCensusRow, ...] = (
    # group_business (12)
    _E("GroupMessageEvent", "group_business", "message", "group_id_field", _CENSUS_ANCHOR),
    _E("GroupUploadNoticeEvent", "group_business", "notice", "group_id_field", _CENSUS_ANCHOR),
    _E("GroupAdminNoticeEvent", "group_business", "notice", "group_id_field", _CENSUS_ANCHOR),
    _E("GroupDecreaseNoticeEvent", "group_business", "notice", "group_id_field", _CENSUS_ANCHOR),
    _E("GroupIncreaseNoticeEvent", "group_business", "notice", "group_id_field", _CENSUS_ANCHOR),
    _E("GroupBanNoticeEvent", "group_business", "notice", "group_id_field", _CENSUS_ANCHOR),
    _E("GroupRecallNoticeEvent", "group_business", "notice", "group_id_field", _CENSUS_ANCHOR),
    _E("NotifyEvent", "group_business", "notice", "group_id_field", _CENSUS_ANCHOR),
    _E("PokeNotifyEvent", "group_business", "notice", "optional_positive_group_id", _CENSUS_ANCHOR),
    _E("LuckyKingNotifyEvent", "group_business", "notice", "group_id_field", _CENSUS_ANCHOR),
    _E("HonorNotifyEvent", "group_business", "notice", "group_id_field", _CENSUS_ANCHOR),
    _E("GroupRequestEvent", "group_business", "request", "group_id_field", _CENSUS_ANCHOR),
    # private_input (1)
    _E("PrivateMessageEvent", "private_input", "message", "none", _CENSUS_ANCHOR),
    # system_meta (3)
    _E("MetaEvent", "system_meta", "meta_event", "none", _CENSUS_ANCHOR),
    _E("LifecycleMetaEvent", "system_meta", "meta_event", "none", _CENSUS_ANCHOR),
    _E("HeartbeatMetaEvent", "system_meta", "meta_event", "none", _CENSUS_ANCHOR),
    # unsupported_business_fail_closed (6)
    _E("FriendAddNoticeEvent", "unsupported_business_fail_closed", "notice", "none", _CENSUS_ANCHOR),
    _E("FriendRecallNoticeEvent", "unsupported_business_fail_closed", "notice", "none", _CENSUS_ANCHOR),
    _E("FriendRequestEvent", "unsupported_business_fail_closed", "request", "none", _CENSUS_ANCHOR),
    _E("MessageEvent", "unsupported_business_fail_closed", "message", "none", _CENSUS_ANCHOR),
    _E("NoticeEvent", "unsupported_business_fail_closed", "notice", "none", _CENSUS_ANCHOR),
    _E("RequestEvent", "unsupported_business_fail_closed", "request", "none", _CENSUS_ANCHOR),
)

QQ_EVENT_CENSUS: tuple[QQEventCensusRow, ...] = (
    _Q("GroupAtMessageCreateEvent", "allowed", _QQ_CENSUS_ANCHOR),
    _Q("GroupMessageCreateEvent", "rejected", _QQ_CENSUS_ANCHOR),
    _Q("C2CMessageCreateEvent", "rejected", _QQ_CENSUS_ANCHOR),
    _Q("MessageCreateEvent", "rejected", _QQ_CENSUS_ANCHOR),
    _Q("DirectMessageCreateEvent", "rejected", _QQ_CENSUS_ANCHOR),
    _Q("InteractionCreateEvent", "rejected", _QQ_CENSUS_ANCHOR),
    _Q("ForgedGroupAtMessageCreateEvent", "rejected", _QQ_CENSUS_ANCHOR),
)

MATCHER_ENTRY_CENSUS: tuple[MatcherEntryRow, ...] = (
    # on_message (3)
    _M("matcher.komari_chat.__init__.matcher", "komari_bot/plugins/komari_chat/__init__.py", "matcher", "on_message", "message", (_EFFECT_ID,), _MATCHER_ANCHOR),
    _M("matcher.character_binding.reply_evidence.reply_evidence_matcher", "komari_bot/plugins/character_binding/reply_evidence.py", "reply_evidence_matcher", "on_message", "message", (_EFFECT_ID,), _MATCHER_ANCHOR),
    _M("matcher.character_binding.qq_commands.bind_qq", "komari_bot/plugins/character_binding/qq_commands.py", "bind_qq", "on_message", "message", (_EFFECT_ID,), _MATCHER_ANCHOR),
    # on_regex (1)
    _M("matcher.group_history_summary.__init__.summary_matcher", "komari_bot/plugins/group_history_summary/__init__.py", "summary_matcher", "on_regex", "message", (_EFFECT_ID,), _MATCHER_ANCHOR),
    # on_notice (1)
    _M("matcher.komari_custom.vote_handler.vote_notice", "komari_bot/plugins/komari_custom/vote_handler.py", "vote_notice", "on_notice", "notice", (_EFFECT_ID,), _MATCHER_ANCHOR),
    # on_command (19)
    _M("matcher.komari_custom.__init__.custom", "komari_bot/plugins/komari_custom/__init__.py", "custom", "on_command", "message", (_EFFECT_ID,), _MATCHER_ANCHOR),
    _M("matcher.komari_custom.__init__.custom_action", "komari_bot/plugins/komari_custom/__init__.py", "custom_action", "on_command", "message", (_EFFECT_ID,), _MATCHER_ANCHOR),
    _M("matcher.komari_debug.commands.debug_root", "komari_bot/plugins/komari_debug/commands.py", "debug_root", "on_command", "message", (_EFFECT_ID,), _MATCHER_ANCHOR),
    _M("matcher.komari_debug.commands.debug_favor_get", "komari_bot/plugins/komari_debug/commands.py", "debug_favor_get", "on_command", "message", (_EFFECT_ID,), _MATCHER_ANCHOR),
    _M("matcher.komari_debug.commands.debug_favor_set", "komari_bot/plugins/komari_debug/commands.py", "debug_favor_set", "on_command", "message", (_EFFECT_ID,), _MATCHER_ANCHOR),
    _M("matcher.komari_debug.commands.debug_bind_set", "komari_bot/plugins/komari_debug/commands.py", "debug_bind_set", "on_command", "message", (_EFFECT_ID,), _MATCHER_ANCHOR),
    _M("matcher.komari_debug.commands.debug_bind_del", "komari_bot/plugins/komari_debug/commands.py", "debug_bind_del", "on_command", "message", (_EFFECT_ID,), _MATCHER_ANCHOR),
    _M("matcher.komari_debug.commands.debug_bind_list", "komari_bot/plugins/komari_debug/commands.py", "debug_bind_list", "on_command", "message", (_EFFECT_ID,), _MATCHER_ANCHOR),
    _M("matcher.komari_debug.commands.debug_reply", "komari_bot/plugins/komari_debug/commands.py", "debug_reply", "on_command", "message", (_EFFECT_ID,), _MATCHER_ANCHOR),
    _M("matcher.komari_debug.commands.debug_summary", "komari_bot/plugins/komari_debug/commands.py", "debug_summary", "on_command", "message", (_EFFECT_ID,), _MATCHER_ANCHOR),
    _M("matcher.komari_debug.commands.debug_notify", "komari_bot/plugins/komari_debug/commands.py", "debug_notify", "on_command", "message", (_EFFECT_ID,), _MATCHER_ANCHOR),
    _M("matcher.komari_help.commands.help_cmd", "komari_bot/plugins/komari_help/commands.py", "help_cmd", "on_command", "message", (_EFFECT_ID,), _MATCHER_ANCHOR),
    _M("matcher.komari_help.commands.help_list_cmd", "komari_bot/plugins/komari_help/commands.py", "help_list_cmd", "on_command", "message", (_EFFECT_ID,), _MATCHER_ANCHOR),
    _M("matcher.komari_help.commands.help_refresh_cmd", "komari_bot/plugins/komari_help/commands.py", "help_refresh_cmd", "on_command", "message", (_EFFECT_ID,), _MATCHER_ANCHOR),
    _M("matcher.user_ban.commands.ban_matcher", "komari_bot/plugins/user_ban/commands.py", "ban_matcher", "on_command", "message", (_EFFECT_ID,), _MATCHER_ANCHOR),
    _M("matcher.user_ban.commands.unban_matcher", "komari_bot/plugins/user_ban/commands.py", "unban_matcher", "on_command", "message", (_EFFECT_ID,), _MATCHER_ANCHOR),
    _M("matcher.sr.__init__.sr", "komari_bot/plugins/sr/__init__.py", "sr", "on_command", "message", (_EFFECT_ID,), _MATCHER_ANCHOR),
    _M("matcher.sr.__init__.sr_custom", "komari_bot/plugins/sr/__init__.py", "sr_custom", "on_command", "message", (_EFFECT_ID,), _MATCHER_ANCHOR),
    _M("matcher.sr.__init__.sr_manage", "komari_bot/plugins/sr/__init__.py", "sr_manage", "on_command", "message", (_EFFECT_ID,), _MATCHER_ANCHOR),
)
