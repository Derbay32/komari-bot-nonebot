"""诊断日志的凭据与二进制边界（不依赖机器人框架与插件）。"""

from __future__ import annotations

import base64
import hashlib
import json
import mimetypes
import re
from dataclasses import asdict, is_dataclass
from datetime import date, datetime
from enum import Enum
from typing import Any, Final
from urllib.parse import urlsplit

_SECRET_KEYS = frozenset(
    {
        "api_key",
        "apikey",
        "api_token",
        "apitoken",
        "authorization",
        "authorization_header",
        "proxy_authorization",
        "cookie",
        "set_cookie",
        "client_secret",
        "clientsecret",
        "dsn",
        "password",
        "redis_password",
        "access_token",
        "refresh_token",
        "bearer_token",
        "private_key",
        "secret",
        "token",
        "x_api_key",
    }
)
_BASE64_KEY_PATTERN = re.compile(r"(?:^|_)(?:base64|image_base64)(?:_|$)")
_DATA_URL_PATTERN = re.compile(
    r"^data:(?P<mime>[-\w.+/]+)?;base64,(?P<payload>[A-Za-z0-9+/=\s]+)$",
    re.IGNORECASE,
)
_INLINE_DATA_URL_PATTERN = re.compile(
    r"data:(?P<mime>[-\w.+/]+)?;base64,(?P<payload>[A-Za-z0-9+/=]+)",
    re.IGNORECASE,
)
"""正文里**内联**的 data URI 片段；只认显式 ``;base64,`` 前缀。"""
_HTTP_URL_SEGMENT_PATTERN = re.compile(
    r"https?://[A-Za-z0-9\-._~:/?#\[\]@!$&'()*+,;=%]+",
    re.IGNORECASE,
)
"""按 RFC3986 字符集切出的 HTTP(S) URL 片段；碰到空白与 CJK 标点即停。"""
_CREDENTIAL_TEXT_PATTERN = re.compile(
    r"(?i)\b(?:api[_-]?(?:key|token)|password|密码|client[_-]?secret|"
    r"access[_-]?token|refresh[_-]?token)\s*[:=]\s*"
    r"(?:\"[^\"]*\"|'[^']*'|[^\s,;]+)"
)
_AUTHORIZATION_TEXT_PATTERN = re.compile(
    r"(?i)\b(?:proxy[_-]?)?authorization\s*[:=]\s*"
    r"(?:bearer\s+)?[^\s,;]+"
)
_COOKIE_TEXT_PATTERN = re.compile(
    r"(?im)\b(?:set-cookie|cookie)\s*:\s*[^\r\n]+"
)
_MAX_DEPTH = 64
_REDACTED = "[已移除凭据]"

MEDIA_REDACTED_MARKER: Final = "[已移除媒体内容]"
"""媒体载荷的固定占位符（媒体片段、必须保持文本的字段、生成摘要以外的场合）。"""


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _binary_summary(
    raw: bytes,
    *,
    kind: str,
    mime_type: str | None = None,
    source_chars: int | None = None,
) -> dict[str, Any]:
    summary: dict[str, Any] = {
        "redacted_binary": True,
        "kind": kind,
        "bytes": len(raw),
        "sha256": _sha256_bytes(raw),
    }
    if mime_type:
        summary["mime_type"] = mime_type
    if source_chars is not None:
        summary["source_chars"] = source_chars
    return summary


def _summarize_base64_text(value: str) -> dict[str, Any]:
    compact = "".join(value.split())
    try:
        decoded = base64.b64decode(compact, validate=True)
    except (ValueError, TypeError):
        decoded = value.encode("utf-8", errors="replace")
    return _binary_summary(
        decoded,
        kind="base64_text",
        source_chars=len(value),
    )


def _summarize_data_url(value: str) -> dict[str, Any] | None:
    match = _DATA_URL_PATTERN.fullmatch(value.strip())
    if match is None:
        return None
    compact_payload = "".join(match.group("payload").split())
    try:
        decoded = base64.b64decode(compact_payload, validate=True)
    except (ValueError, TypeError):
        decoded = compact_payload.encode("ascii", errors="ignore")
    return _binary_summary(
        decoded,
        kind="image_data_url",
        mime_type=match.group("mime") or None,
        source_chars=len(value),
    )


def _summarize_image_url(value: str) -> dict[str, Any]:
    data_summary = _summarize_data_url(value)
    if data_summary is not None:
        return data_summary
    encoded = value.encode("utf-8", errors="replace")
    mime_type, _encoding = mimetypes.guess_type(urlsplit(value).path)
    return {
        "redacted_binary": True,
        "kind": "remote_image_url",
        "mime_type": mime_type,
        "source_chars": len(value),
        "estimated_bytes": None,
        "sha256": _sha256_bytes(encoded),
    }


def _sanitize_string_credentials(value: str) -> str:
    sanitized = _CREDENTIAL_TEXT_PATTERN.sub("[已移除凭据]", value)
    sanitized = _AUTHORIZATION_TEXT_PATTERN.sub("[已移除凭据]", sanitized)
    return _COOKIE_TEXT_PATTERN.sub("[已移除凭据]", sanitized)


def _redact_inline_media(value: str, *, image_keyed: bool) -> str:
    """把正文里内联的媒体片段换成固定占位符，保留周围普通文字。

    只处理两类显式信号：``data:<mime>;base64,<payload>`` 形式的内联载荷，
    以及**显式图片字段**（键名含 ``image``）里的 HTTP(S) URL 片段。不为普通
    正文里的任意 URL 或任意 base64 样词新增分类器。
    """
    redacted = value
    if image_keyed:
        redacted = _HTTP_URL_SEGMENT_PATTERN.sub(MEDIA_REDACTED_MARKER, redacted)
    return _INLINE_DATA_URL_PATTERN.sub(MEDIA_REDACTED_MARKER, redacted)


def _normalize_object(value: object) -> object:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if is_dataclass(value) and not isinstance(value, type):
        return asdict(value)
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        return model_dump(mode="json", exclude_none=True)
    return value


def sanitize_log_value(
    value: object,
    *,
    key: str = "",
    depth: int = 0,
    known_secrets: tuple[str, ...] = (),
) -> Any:
    """生成安全投影，再移除全部已知凭据，包括媒体摘要中的字符串。"""
    projected = _sanitize_log_value(value, key=key, depth=depth)
    secrets = sorted({secret for secret in known_secrets if secret}, key=len, reverse=True)
    if not secrets:
        return projected
    pattern = re.compile("|".join(re.escape(secret) for secret in secrets))
    return _redact_known_projection(projected, pattern)


def _redact_known_projection(value: Any, pattern: re.Pattern[str]) -> Any:
    """只替换原始投影中的字面量，不对生成的掩码再次替换。"""
    if isinstance(value, str):
        return pattern.sub(_REDACTED, value)
    if isinstance(value, dict):
        return {
            pattern.sub(_REDACTED, key): _redact_known_projection(item, pattern)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact_known_projection(item, pattern) for item in value]
    return value


def _sanitize_log_value(  # noqa: PLR0911
    value: object,
    *,
    key: str = "",
    depth: int = 0,
) -> Any:
    """递归移除显式凭据和大块二进制，保留其余完整正文。"""
    if depth > _MAX_DEPTH:
        return "[超过日志嵌套深度限制]"

    normalized_key = key.strip().lower().replace("-", "_")
    if normalized_key in _SECRET_KEYS or normalized_key.endswith(
        ("_password", "_secret")
    ):
        return "[已移除凭据]"

    value = _normalize_object(value)
    if isinstance(value, bytes):
        return _binary_summary(value, kind="bytes")
    if isinstance(value, bytearray):
        return _binary_summary(bytes(value), kind="bytearray")
    if isinstance(value, str):
        if _BASE64_KEY_PATTERN.search(normalized_key):
            return _summarize_base64_text(value)
        data_summary = _summarize_data_url(value)
        if data_summary is not None:
            return data_summary
        if "image" in normalized_key and value.startswith(("http://", "https://")):
            return _summarize_image_url(value)
        return _sanitize_string_credentials(
            _redact_inline_media(value, image_keyed="image" in normalized_key)
        )
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, dict):
        part_type = str(value.get("type", "")).lower()
        sanitized: dict[str, Any] = {}
        for raw_key, raw_value in value.items():
            child_key = str(raw_key)
            if part_type in {"image", "image_url", "input_image"} and child_key in {
                "url",
                "image_url",
                "data",
            }:
                if isinstance(raw_value, dict) and "url" in raw_value:
                    sanitized[child_key] = {
                        **{
                            str(k): _sanitize_log_value(
                                v,
                                key=str(k),
                                depth=depth + 1,
                            )
                            for k, v in raw_value.items()
                            if str(k) != "url"
                        },
                        "url": _summarize_image_url(str(raw_value["url"])),
                    }
                else:
                    sanitized[child_key] = _summarize_image_url(str(raw_value))
                continue
            sanitized[child_key] = _sanitize_log_value(
                raw_value,
                key=child_key,
                depth=depth + 1,
            )
        return sanitized
    if isinstance(value, (list, tuple, set, frozenset)):
        return [
            _sanitize_log_value(item, key=key, depth=depth + 1) for item in value
        ]
    try:
        json.dumps(value)
    except (TypeError, ValueError, RecursionError):
        return repr(value)
    return value


def sanitize_text_field(
    value: str,
    *,
    key: str = "",
    known_secrets: tuple[str, ...] = (),
) -> str:
    """把一段必须保持为文本的字段脱敏；整段媒体载荷换成固定占位符。

    调用方（判定输入与 Prompt 文本）要求字段类型仍是 ``str``：媒体载荷的具体
    字节不能转发，但整段通用摘要字典会改变字段形状并连带丢掉该字段承载的固定
    问题定义。因此这里只把「整值是媒体」的情况换成 :data:`MEDIA_REDACTED_MARKER`，
    普通文字与片段级处理仍走 :func:`sanitize_log_value`。
    """
    sanitized = sanitize_log_value(value, key=key, known_secrets=known_secrets)
    return sanitized if isinstance(sanitized, str) else MEDIA_REDACTED_MARKER


def build_content_summary(value: object) -> dict[str, int | str]:
    """生成列表预览和兼容 API 使用的内容摘要。"""
    sanitized = sanitize_log_value(value)
    serialized = json.dumps(
        sanitized,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=repr,
    )
    return {
        "chars": len(serialized),
        "sha256": hashlib.sha256(serialized.encode("utf-8")).hexdigest(),
    }


def build_preview(value: object, *, limit: int = 240) -> str:
    """把已过滤的值转换为单行短预览。"""
    if value in (None, "", [], {}):
        return ""
    sanitized = sanitize_log_value(value)
    if isinstance(sanitized, str):
        text = sanitized
    else:
        text = json.dumps(sanitized, ensure_ascii=False, default=repr)
    normalized = " ".join(text.split())
    return normalized if len(normalized) <= limit else f"{normalized[:limit]}..."
