"""TSK-225 聊天即时效果 census <-> manifest 一致性验收（AC-1 / AC-10).

census（``chat_effect_census``）与本票登记进 ``ADMISSION_EFFECT_CASES`` 的
``group_admission.effect.chat.*`` 行双向一致：census 是闭合枚举，manifest 是
登记。每个 chat 效果行稳定、非空、前缀受控，work_category 属瞬时互动。
"""

from __future__ import annotations

import pytest

from tests.group_admission.acceptance_manifest import (
    ADMISSION_EFFECT_CASES,
    AdmissionEffectCase,
)
from tests.group_admission.chat_effect_census import CHAT_EFFECT_CENSUS

pytestmark = pytest.mark.group_admission_acceptance

PREFIX = "group_admission.effect.chat."


def _chat_manifest_rows() -> list[AdmissionEffectCase]:
    return [c for c in ADMISSION_EFFECT_CASES if c.effect_id.startswith(PREFIX)]


def _census_id_set() -> set[str]:
    return {r.effect_id for r in CHAT_EFFECT_CENSUS}


def _manifest_id_set() -> set[str]:
    return {c.effect_id for c in _chat_manifest_rows()}


def _manifest_index() -> dict[str, AdmissionEffectCase]:
    return {c.effect_id: c for c in _chat_manifest_rows()}


def _assert_row_stable(case: AdmissionEffectCase) -> None:
    assert case.effect_id.startswith(PREFIX), case.effect_id
    assert case.owner_module == "komari_bot.plugins.komari_chat"
    assert case.intent == "business"
    assert case.work_category == "transient_interaction"
    assert case.attribution_source.strip()
    assert case.sink_kind.strip()
    assert case.acceptance_anchor.startswith("tests/group_admission/")


def test_chat_census_matches_manifest_id_set() -> None:
    """census 与 manifest 的 chat 效果 ID 集合双向一致。"""
    assert _census_id_set() == _manifest_id_set()


def test_every_chat_census_row_registered_in_manifest() -> None:
    registered = _manifest_id_set()
    missing = _census_id_set() - registered
    assert missing == set(), f"census 行未登记进 manifest: {missing}"


def test_no_stray_chat_rows_outside_census() -> None:
    extra = _manifest_id_set() - _census_id_set()
    assert extra == set(), f"manifest 出现 census 之外的 chat 行: {extra}"


def test_chat_manifest_rows_shape_stable() -> None:
    for case in _chat_manifest_rows():
        _assert_row_stable(case)


def _require_chat_effect(effect_id: str) -> AdmissionEffectCase:
    index = _manifest_index()
    assert effect_id in index, f"缺少 chat 效果登记: {effect_id}"
    return index[effect_id]


def test_tool_fetch_page_effect_registered() -> None:
    case = _require_chat_effect("group_admission.effect.chat.tool_fetch_page")
    _assert_row_stable(case)
    assert case.sink_kind == "komari_search_http"


def test_vision_completion_effect_registered() -> None:
    case = _require_chat_effect("group_admission.effect.chat.vision_completion")
    _assert_row_stable(case)
    assert case.sink_kind == "vision_llm_round"


def test_image_download_effect_registered() -> None:
    case = _require_chat_effect("group_admission.effect.chat.image_download")
    _assert_row_stable(case)
    assert case.sink_kind == "image_http_download"


def test_embedding_effect_registered() -> None:
    case = _require_chat_effect("group_admission.effect.chat.embedding")
    _assert_row_stable(case)
    assert case.sink_kind == "embedding_https"


def test_reply_send_effect_registered() -> None:
    case = _require_chat_effect("group_admission.effect.chat.reply_send")
    _assert_row_stable(case)
    assert case.sink_kind == "onebot_send_group_msg"


def test_debug_public_effect_registered() -> None:
    case = _require_chat_effect("group_admission.effect.chat.debug_public")
    _assert_row_stable(case)
    assert case.sink_kind == "onebot_debug_public_output"
