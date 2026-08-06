"""Agent Run 日志的凭据与二进制边界。"""

from __future__ import annotations

import base64
import hashlib
import json
import mimetypes
import re
from dataclasses import asdict, is_dataclass
from datetime import date, datetime
from enum import Enum
from typing import Any
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


def sanitize_log_value(  # noqa: PLR0911
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
        return _sanitize_string_credentials(value)
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
                            str(k): sanitize_log_value(
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
            sanitized[child_key] = sanitize_log_value(
                raw_value,
                key=child_key,
                depth=depth + 1,
            )
        return sanitized
    if isinstance(value, (list, tuple, set, frozenset)):
        return [
            sanitize_log_value(item, key=key, depth=depth + 1) for item in value
        ]
    try:
        json.dumps(value)
    except (TypeError, ValueError, RecursionError):
        return repr(value)
    return value


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
