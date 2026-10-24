"""neutral 诊断日志清洗器（komari_bot.llm.diagnostic_sanitizer）的共享契约测试。"""

from __future__ import annotations

import copy
import hashlib
import json
import subprocess
import sys
from dataclasses import dataclass
from datetime import UTC, date, datetime
from enum import Enum
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from komari_bot.llm import diagnostic_sanitizer as neutral_sanitizer_module
from komari_bot.llm.diagnostic_sanitizer import (
    build_content_summary,
    build_preview,
    sanitize_log_value,
)

_REDACTED = "[已移除凭据]"
_DEPTH_MARKER = "[超过日志嵌套深度限制]"
_REPO_ROOT = Path(__file__).resolve().parents[2]

_TOP_SECRET = "sk-test-7f3a91c4d2"
_NESTED_SECRET = "nested-secret-44c1"
_KEY_SECRET = "key-secret-9ab2"
_DATACLASS_SECRET = "objsecret5e77"
_MODEL_SECRET = "model-secret-2d8c"
_REPR_SECRET = "repr-secret-6b0f"
_LONG_SECRET = "sk-live-abcdef123"
_SHORT_SECRET = "sk-live"


class _Color(Enum):
    RED = "red"


class _Opaque:
    def __init__(self, payload: str) -> None:
        self.payload = payload

    def __repr__(self) -> str:
        return f"Opaque(payload={self.payload!r})"


class _PayloadModel(BaseModel):
    text: str
    detail: dict[str, str]


def _deeply_nested(depth: int) -> dict[str, Any]:
    root: dict[str, Any] = {}
    cursor = root
    for _ in range(depth):
        child: dict[str, Any] = {}
        cursor["k"] = child
        cursor = child
    return root


def test_secret_like_keys_are_masked_and_full_text_is_kept() -> None:
    value = sanitize_log_value(
        {
            "authorization": "Bearer fake-token-value",
            "api-key": "fake-value",
            "user_password": "fake-value",
            "nested_secret": "fake-value",
            "message": "完整正文\n第二行",
        }
    )

    assert value == {
        "authorization": _REDACTED,
        "api-key": _REDACTED,
        "user_password": _REDACTED,
        "nested_secret": _REDACTED,
        "message": "完整正文\n第二行",
    }


def test_credential_authorization_and_cookie_text_stay_masked() -> None:
    assert sanitize_log_value("api_key=sk-fake password: pw-fake token = tk") == (
        f"{_REDACTED} {_REDACTED} token = tk"
    )
    assert sanitize_log_value("Authorization: Bearer fake-bearer") == _REDACTED
    assert sanitize_log_value("Cookie: session=fake-session\nnext") == (
        f"{_REDACTED}\nnext"
    )


def test_binary_and_image_values_keep_stable_metadata() -> None:
    data_url = "data:image/png;base64,QUJDRA=="
    value = sanitize_log_value(
        {
            "image_base64": "QUJDRA==",
            "part": {"type": "image_url", "image_url": {"url": data_url}},
            "rendered_image": b"ABCD",
            "bytearray_image": bytearray(b"ABCD"),
            "normal_text": "保留正文",
        }
    )

    expected_hash = hashlib.sha256(b"ABCD").hexdigest()
    assert value["image_base64"] == {
        "redacted_binary": True,
        "kind": "base64_text",
        "bytes": 4,
        "sha256": expected_hash,
        "source_chars": len("QUJDRA=="),
    }
    assert value["rendered_image"] == {
        "redacted_binary": True,
        "kind": "bytes",
        "bytes": 4,
        "sha256": expected_hash,
    }
    assert value["bytearray_image"] == {
        "redacted_binary": True,
        "kind": "bytearray",
        "bytes": 4,
        "sha256": expected_hash,
    }
    remote_image = value["part"]["image_url"]["url"]
    assert remote_image["kind"] == "image_data_url"
    assert remote_image["mime_type"] == "image/png"
    assert remote_image["bytes"] == 4
    assert remote_image["sha256"] == expected_hash
    assert remote_image["source_chars"] == len(data_url)
    assert value["normal_text"] == "保留正文"


def test_remote_image_and_scalar_values_stay_stable() -> None:
    remote_url = "https://example.com/private-image.png"
    value = sanitize_log_value(
        {"image_url": remote_url, "n": None, "b": True, "i": 3, "f": 1.5}
    )

    summary = value["image_url"]
    assert summary["kind"] == "remote_image_url"
    assert summary["mime_type"] == "image/png"
    assert summary["source_chars"] == len(remote_url)
    assert summary["estimated_bytes"] is None
    assert value["n"] is None
    assert value["b"] is True
    assert value["i"] == 3
    assert value["f"] == 1.5


def test_enum_date_and_containers_are_normalized() -> None:
    value = sanitize_log_value(
        {
            "e": _Color.RED,
            "d": date(2024, 1, 2),
            "dt": datetime(2024, 1, 2, 3, 4, 5, tzinfo=UTC),
            "s": ({"a"}, [1, "b"]),
        }
    )

    assert value == {
        "e": "red",
        "d": "2024-01-02",
        "dt": "2024-01-02T03:04:05+00:00",
        "s": [["a"], [1, "b"]],
    }


def test_nesting_depth_limit_is_preserved() -> None:
    assert _DEPTH_MARKER in json.dumps(
        sanitize_log_value(_deeply_nested(70)), ensure_ascii=False
    )
    assert sanitize_log_value(_deeply_nested(10)) == _deeply_nested(10)


def test_content_summary_and_preview_stay_stable() -> None:
    serialized = json.dumps(
        {"text": "正文"}, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )

    assert build_content_summary({"text": "正文"}) == {
        "chars": len(serialized),
        "sha256": hashlib.sha256(serialized.encode("utf-8")).hexdigest(),
    }
    assert build_preview(None) == ""
    assert build_preview([]) == ""
    assert build_preview({"t": ""}) == '{"t": ""}'
    assert build_preview("a b\n c") == "a b c"

    long_preview = build_preview({"t": "a" * 300})
    assert long_preview.endswith("...")
    assert len(long_preview) == 240 + len("...")


def test_known_secret_is_removed_from_top_level_text() -> None:
    text = f"认证前 {_TOP_SECRET} 认证后"

    value = sanitize_log_value(text, known_secrets=(_TOP_SECRET,))

    assert _TOP_SECRET not in value
    assert value.startswith("认证前 ")
    assert value.endswith(" 认证后")
    assert sanitize_log_value(text) == text


def test_known_secret_is_removed_from_nested_values_and_keys() -> None:
    payload = {
        "outer": {"inner": [f"a {_NESTED_SECRET} b", {_NESTED_SECRET: "保留值"}]},
        "plain": "保留正文",
    }

    value = sanitize_log_value(payload, known_secrets=(_NESTED_SECRET,))

    serialized = json.dumps(value, ensure_ascii=False, default=repr)
    assert _NESTED_SECRET not in serialized
    preserved = value["outer"]["inner"][0]
    assert preserved.startswith("a ")
    assert preserved.endswith(" b")
    kept = value["outer"]["inner"][1]
    assert len(kept) == 1
    assert all(_NESTED_SECRET not in key for key in kept)
    assert list(kept.values()) == ["保留值"]
    assert value["plain"] == "保留正文"


def test_known_secret_is_removed_from_dictionary_keys() -> None:
    value = sanitize_log_value(
        {f"field_{_KEY_SECRET}_name": "保留值"},
        known_secrets=(_KEY_SECRET,),
    )

    assert len(value) == 1
    (only_key,) = value
    assert _KEY_SECRET not in only_key
    assert only_key.startswith("field_")
    assert only_key.endswith("_name")
    assert list(value.values()) == ["保留值"]


def test_known_secret_is_removed_from_dataclass_and_pydantic_objects() -> None:
    @dataclass(frozen=True)
    class _Leaf:
        text: str

    @dataclass(frozen=True)
    class _Record:
        # 字段名本身包含凭据，覆盖 asdict 规范化后的字典键脱敏。
        objsecret5e77_label: str
        leaf: _Leaf

    record = _Record(
        objsecret5e77_label=f"v {_DATACLASS_SECRET}",
        leaf=_Leaf(text=f"deep {_DATACLASS_SECRET}"),
    )
    model = _PayloadModel(
        text=f"m {_MODEL_SECRET}",
        detail={f"k_{_MODEL_SECRET}": f"v {_MODEL_SECRET}"},
    )
    model_snapshot = model.model_dump(mode="json")

    dataclass_value = sanitize_log_value(record, known_secrets=(_DATACLASS_SECRET,))
    model_value = sanitize_log_value(model, known_secrets=(_MODEL_SECRET,))

    dataclass_serialized = json.dumps(dataclass_value, ensure_ascii=False, default=repr)
    model_serialized = json.dumps(model_value, ensure_ascii=False, default=repr)
    assert _DATACLASS_SECRET not in dataclass_serialized
    assert _MODEL_SECRET not in model_serialized
    assert len(dataclass_value) == 2
    assert any(key.endswith("_label") for key in dataclass_value)
    assert dataclass_value["leaf"]["text"].startswith("deep ")
    assert set(model_value) == {"text", "detail"}
    assert model_value["text"].startswith("m ")
    assert next(iter(model_value["detail"].values())).startswith("v ")
    assert model.model_dump(mode="json") == model_snapshot
    assert record.leaf.text == f"deep {_DATACLASS_SECRET}"


def test_longer_known_secrets_are_removed_before_shorter() -> None:
    text = f"认证 {_LONG_SECRET} 结束"

    value = sanitize_log_value(text, known_secrets=(_SHORT_SECRET, _LONG_SECRET))
    standalone = sanitize_log_value(
        f"only {_SHORT_SECRET} here", known_secrets=(_SHORT_SECRET, _LONG_SECRET)
    )

    assert _LONG_SECRET not in value
    assert "abcdef123" not in value
    assert value.startswith("认证 ")
    assert value.endswith(" 结束")
    assert _SHORT_SECRET not in standalone
    assert standalone.startswith("only ")
    assert standalone.endswith(" here")


def test_empty_and_duplicate_known_secrets_are_ignored() -> None:
    payload = {"message": "保留 正文\n  多空白 ", "authorization": "Bearer fake-value"}
    text = f"前缀 {_TOP_SECRET} 后缀"

    assert sanitize_log_value(payload, known_secrets=("",)) == sanitize_log_value(
        payload
    )
    assert sanitize_log_value(payload)["message"] == "保留 正文\n  多空白 "
    assert sanitize_log_value(text, known_secrets=(_TOP_SECRET, _TOP_SECRET)) == (
        sanitize_log_value(text, known_secrets=(_TOP_SECRET,))
    )


def test_known_secret_is_removed_from_fallback_repr() -> None:
    payload = f"opaque {_REPR_SECRET} value"

    value = sanitize_log_value(
        {"obj": _Opaque(payload), "top": _Opaque(payload)},
        known_secrets=(_REPR_SECRET,),
    )
    top_level = sanitize_log_value(_Opaque(payload), known_secrets=(_REPR_SECRET,))

    serialized = json.dumps(value, ensure_ascii=False, default=repr)
    assert _REPR_SECRET not in serialized
    assert _REPR_SECRET not in str(top_level)
    assert "opaque " in serialized
    assert "opaque " in str(top_level)


def test_known_secrets_do_not_mutate_caller_objects() -> None:
    payload = {
        "message": f"x {_TOP_SECRET}",
        "labels": [f"l {_TOP_SECRET}"],
        f"key_{_KEY_SECRET}": "保留值",
    }
    snapshot = copy.deepcopy(payload)

    @dataclass
    class _Mutable:
        text: str

    mutable = _Mutable(text=f"v {_TOP_SECRET}")

    value = sanitize_log_value(payload, known_secrets=(_TOP_SECRET, _KEY_SECRET))
    sanitize_log_value(mutable, known_secrets=(_TOP_SECRET,))

    assert payload == snapshot
    assert mutable.text == f"v {_TOP_SECRET}"
    assert _TOP_SECRET not in json.dumps(value, ensure_ascii=False, default=repr)
    assert _KEY_SECRET not in json.dumps(value, ensure_ascii=False, default=repr)


def test_neutral_module_import_does_not_require_nonebot() -> None:
    code = (
        "import sys\n"
        "import komari_bot.llm.diagnostic_sanitizer as m\n"
        "assert callable(m.sanitize_log_value)\n"
        "assert callable(m.build_content_summary)\n"
        "assert callable(m.build_preview)\n"
        "print('nonebot' in sys.modules)\n"
    )

    completed = subprocess.run(
        [sys.executable, "-c", code],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "False"

    module_file = neutral_sanitizer_module.__file__
    source = Path(module_file or "").read_text(encoding="utf-8")
    assert "nonebot" not in source
    assert "komari_bot.plugins" not in source


def test_known_secret_is_removed_from_generated_media_summary() -> None:
    secret = "synthetic-mime-secret"
    value = sanitize_log_value(
        {"image": f"data:{secret};base64,YWJj"}, known_secrets=(secret,)
    )
    assert secret not in json.dumps(value, ensure_ascii=False)
    assert value["image"]["redacted_binary"] is True
    assert value["image"]["bytes"] == 3


def test_known_secret_replacement_does_not_reprocess_mask_text() -> None:
    value = sanitize_log_value(
        "long-secret and 移除", known_secrets=("long-secret", "移除")
    )
    assert value == "[已移除凭据] and [已移除凭据]"
