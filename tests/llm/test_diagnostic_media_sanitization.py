"""neutral 清洗器的媒体字符串边界（TSK-180 §4、TSK-182 判定输入/正文）。

只验证 TSK-182 明确新增的两类媒体字符串处理，并锁住既有行为不被扩大：

- 普通正文里**内联**的 ``data:<mime>;base64,<payload>`` 片段整体替换为固定
  媒体占位符，周围普通文字保留；
- 显式图片字段（键名含 ``image``）里的 HTTP(S) URL 片段即使前面有描述文字也
  被替换为固定媒体占位符；
- 既有「整值是 data URL / 整值是图片 URL → 结构化摘要」的行为保持；
- 不为普通正文里的任意 URL 或任意 base64 样词新增分类器；
- 已知凭据投影保持有效（含媒体占位符附近）。

期望值全部是手写字面量，不导入生产常量。
"""

from __future__ import annotations

import json

import pytest

from komari_bot.llm.diagnostic_sanitizer import (
    sanitize_log_value,
    sanitize_text_field,
)

_MEDIA_MARKER = "[已移除媒体内容]"
_REDACTED = "[已移除凭据]"

_PNG_PAYLOAD = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8AAAwAB/AF+p1Gq"
)
"""一段 1x1 PNG 的 base64 载荷（用于断言原始载荷不残留）。"""

_INLINE_DATA_URI = f"data:image/png;base64,{_PNG_PAYLOAD}"


# ------------------------------ 内联 data URI ------------------------------


def test_inline_data_uri_in_text_is_replaced_and_neighbours_kept() -> None:
    """內联 data URI 只替换自身片段，周围普通文字一字不改。"""
    text = f"前面的话 {_INLINE_DATA_URI} 后面的话"

    sanitized = sanitize_log_value(text)

    assert sanitized == f"前面的话 {_MEDIA_MARKER} 后面的话"
    assert _PNG_PAYLOAD not in sanitized
    assert "前面的话" in sanitized
    assert "后面的话" in sanitized


def test_inline_data_uri_in_nested_body_values_is_replaced() -> None:
    """嵌套结构里的内联 data URI 同样被替换（正文整体投影）。"""
    body = {
        "state": {
            "target": {"text": f"看图 {_INLINE_DATA_URI}"},
            "complaint": {"form_data": f"[{_INLINE_DATA_URI}]"},
        }
    }

    sanitized = sanitize_log_value(body)

    serialized = json.dumps(sanitized, ensure_ascii=False)
    assert _PNG_PAYLOAD not in serialized
    assert sanitized["state"]["target"]["text"] == f"看图 {_MEDIA_MARKER}"
    assert sanitized["state"]["complaint"]["form_data"] == f"[{_MEDIA_MARKER}]"


def test_inline_data_uri_keeps_credential_masking() -> None:
    """同一段文字里的凭据仍被掩码，media 替换不覆盖凭据规则。"""
    text = f"api_key=sk-media-7d1f 并附 {_INLINE_DATA_URI}"

    sanitized = sanitize_log_value(text)

    assert "sk-media-7d1f" not in sanitized
    assert _REDACTED in sanitized
    assert _MEDIA_MARKER in sanitized
    assert _PNG_PAYLOAD not in sanitized


def test_inline_data_uri_with_known_secret_is_fully_masked() -> None:
    """已知凭据投影仍覆盖 media 占位符之外的原始字面量。"""
    secret = "synthetic-inline-secret"
    text = f"{secret} 说明 data:image/png;base64,{_PNG_PAYLOAD}"

    sanitized = sanitize_log_value(text, known_secrets=(secret,))

    assert secret not in sanitized
    assert _MEDIA_MARKER in sanitized
    assert _PNG_PAYLOAD not in sanitized


# ------------------------------ 图片字段 URL 片段 ------------------------------


def test_image_keyed_description_redacts_url_segment_after_description() -> None:
    """显式图片字段：描述在前、URL 在后时仍只替换 URL 片段。"""
    description = "群友发的截图 https://cdn.example.invalid/private-shot.png 说看不懂"

    sanitized = sanitize_log_value(description, key="image_description")

    assert "private-shot.png" not in sanitized
    assert "cdn.example.invalid" not in sanitized
    assert sanitized == f"群友发的截图 {_MEDIA_MARKER} 说看不懂"


def test_image_keyed_field_redacts_multiple_url_segments() -> None:
    """同一图片字段里的多个 URL 片段逐个替换，普通文字保留。"""
    description = "第一次 https://a.example.invalid/1.png 第二次 https://b.example.invalid/2.png 都在这里"

    sanitized = sanitize_log_value(description, key="image_description")

    assert "example.invalid" not in sanitized
    assert sanitized == (f"第一次 {_MEDIA_MARKER} 第二次 {_MEDIA_MARKER} 都在这里")


def test_ordinary_text_url_is_not_classified_as_media() -> None:
    """普通（非图片）字段里的 URL 不被当作媒体裁剪。"""
    text = "参考 https://docs.example.invalid/guide 的说明"

    sanitized = sanitize_log_value(text, key="text")

    assert sanitized == text
    assert _MEDIA_MARKER not in sanitized


def test_ordinary_base64_looking_word_is_not_classified_as_media() -> None:
    """普通字段里没有 data: 前缀的 base64 样子词不被分类为媒体。"""
    text = "口令是 QUJDRA== 请记住"

    sanitized = sanitize_log_value(text, key="text")

    assert sanitized == text
    assert _MEDIA_MARKER not in sanitized


# ------------------------------ 既有整值行为保持 ------------------------------


def test_full_data_url_keeps_structured_summary() -> None:
    """整值是 data URL 时仍是结构化二进制摘要（不改成占位符字符串）。"""
    sanitized = sanitize_log_value(_INLINE_DATA_URI, key="image_description")

    assert isinstance(sanitized, dict)
    assert sanitized["redacted_binary"] is True
    assert sanitized["kind"] == "image_data_url"
    assert sanitized["mime_type"] == "image/png"
    assert _PNG_PAYLOAD not in json.dumps(sanitized, ensure_ascii=False)


def test_full_remote_image_url_keeps_structured_summary() -> None:
    """整值是图片 URL 时仍是结构化远端图片摘要。"""
    sanitized = sanitize_log_value(
        "https://cdn.example.invalid/private-shot.png", key="image_description"
    )

    assert isinstance(sanitized, dict)
    assert sanitized["kind"] == "remote_image_url"
    assert sanitized["mime_type"] == "image/png"


# ------------------------------ 必须保持为文本的字段 ------------------------------


def test_text_field_keeps_ordinary_description() -> None:
    """santize_text_field 对普通描述原样返回（图片事实不被裁剪）。"""
    description = "一张会议截图，白板上写着排期"

    assert sanitize_text_field(description, key="image_description") == description


def test_text_field_replaces_media_value_with_fixed_marker() -> None:
    """整段媒体载荷在必须保持文本的字段里换成固定占位符。"""
    assert sanitize_text_field(_INLINE_DATA_URI, key="image_description") == (
        _MEDIA_MARKER
    )
    assert (
        sanitize_text_field(
            "https://cdn.example.invalid/private-shot.png", key="image_description"
        )
        == _MEDIA_MARKER
    )


def test_text_field_applies_credential_masking() -> None:
    """凭据字段仍按凭据规则掩码，不因为「保持文本」而原样输出。"""
    sanitized = sanitize_text_field("api_key=sk-text-33b1", key="reply_eligibility")

    assert "sk-text-33b1" not in sanitized
    assert sanitized == _REDACTED


@pytest.mark.parametrize("value", [_INLINE_DATA_URI, f"图 {_INLINE_DATA_URI} 后"])
def test_media_sanitization_does_not_mutate_input(value: str) -> None:
    """投影不修改调用方对象（Prompt/请求快照保持原样）。"""
    container = {"image_description": value}
    original = {"image_description": value}

    sanitize_log_value(container)

    assert container == original
