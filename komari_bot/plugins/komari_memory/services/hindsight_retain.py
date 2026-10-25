"""Immutable caller-owned Hindsight retain request and derived identity.

The value type freezes every piece of document input before any network I/O.
It never stores credentials and derives the document, payload and operation
identities from canonical JSON so a process restart cannot change them for the
same serialized input.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import re
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Self

from hindsight_client import Hindsight, RetainResponse
from hindsight_client_api.exceptions import NotFoundException
from hindsight_client_api.models.document_response import DocumentResponse

PROTOCOL_VERSION = 1
RESERVED_METADATA_PREFIX = "komari_online_"
RESERVED_METADATA_KEYS: tuple[str, ...] = (
    "komari_online_schema",
    "komari_online_source_id",
    "komari_online_payload_sha256",
    "komari_online_operation_id",
    "komari_online_target_fingerprint",
)
DOCUMENT_ID_PREFIX = "komari-online-v1-"
MINIMUM_API_VERSION = (1, 1, 0)

_TARGET_FINGERPRINT_PATTERN = re.compile(r"^[0-9a-fA-F]{64}$")
_SERIALIZED_FIELDS = frozenset(
    {
        "version",
        "target_fingerprint",
        "source_id",
        "content",
        "timestamp",
        "metadata",
    }
)

_MSG_TARGET_FINGERPRINT = "target fingerprint must be 64 hex characters"
_MSG_SOURCE_ID = "source id must be a non-blank string"
_MSG_CONTENT = "content must be a non-blank string"
_MSG_TIMESTAMP = "timestamp must be a finite timezone-aware datetime"
_MSG_METADATA = "metadata must be a string-to-string object without reserved keys"
_MSG_TEXT = "text must be valid UTF-8 without NUL characters"
_MSG_SERIALIZED = "serialized retain request is invalid"
_API_VERSION_PATTERN = re.compile(r"^(\d+)\.(\d+)\.(\d+)")


def canonical_json(value: object) -> str:
    """Render a value as deterministic compact UTF-8 JSON text."""
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def derive_target_fingerprint(*, base_url: str, bank_id: str) -> str:
    """Hash only the normalized endpoint and bank identity."""
    material = canonical_json({"bank_id": bank_id, "base_url": base_url})
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _validate_text(value: str) -> str:
    if "\x00" in value:
        raise ValueError(_MSG_TEXT)
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise ValueError(_MSG_TEXT) from None
    return value


def _freeze_timestamp(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, datetime):
        raise ValueError(_MSG_TIMESTAMP)  # noqa: TRY004
    try:
        offset = value.utcoffset()
        numeric = value.timestamp()
    except (OSError, OverflowError, ValueError):
        raise ValueError(_MSG_TIMESTAMP) from None
    if offset is None or not math.isfinite(numeric):
        raise ValueError(_MSG_TIMESTAMP)
    return value.astimezone(UTC)


def _serialize_timestamp(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _freeze_metadata(metadata: Mapping[str, str]) -> tuple[tuple[str, str], ...]:
    if not isinstance(metadata, Mapping):
        raise ValueError(_MSG_METADATA)  # noqa: TRY004
    items: list[tuple[str, str]] = []
    for key, value in metadata.items():
        if not isinstance(key, str) or not isinstance(value, str):
            raise ValueError(_MSG_METADATA)  # noqa: TRY004
        if key.startswith(RESERVED_METADATA_PREFIX):
            raise ValueError(_MSG_METADATA)
        _validate_text(key)
        _validate_text(value)
        items.append((key, value))
    items.sort(key=lambda item: item[0])
    return tuple(items)


@dataclass(frozen=True, slots=True)
class HindsightRetainRequest:
    """Frozen document input with deterministic derived identities."""

    _target_fingerprint: str = field(repr=False)
    _source_id: str = field(repr=False)
    _content: str = field(repr=False)
    _timestamp: datetime | None = field(repr=False)
    _metadata_items: tuple[tuple[str, str], ...] = field(repr=False)

    @classmethod
    def create(
        cls,
        *,
        target_fingerprint: str,
        source_id: str,
        content: str,
        timestamp: datetime | None,
        metadata: Mapping[str, str],
    ) -> Self:
        """Validate and freeze one caller-owned retain request."""
        if not isinstance(target_fingerprint, str) or not (
            _TARGET_FINGERPRINT_PATTERN.fullmatch(target_fingerprint)
        ):
            raise ValueError(_MSG_TARGET_FINGERPRINT)
        if not isinstance(source_id, str) or not source_id.strip():
            raise ValueError(_MSG_SOURCE_ID)
        _validate_text(source_id)
        if not isinstance(content, str) or not content.strip():
            raise ValueError(_MSG_CONTENT)
        _validate_text(content)
        return cls(
            _target_fingerprint=target_fingerprint.lower(),
            _source_id=source_id,
            _content=content,
            _timestamp=_freeze_timestamp(timestamp),
            _metadata_items=_freeze_metadata(metadata),
        )

    @classmethod
    def from_json(cls, value: object) -> Self:
        """Strictly reconstruct a request from its serialized representation."""
        if not isinstance(value, Mapping) or set(value) != _SERIALIZED_FIELDS:
            raise ValueError(_MSG_SERIALIZED)
        version = value["version"]
        if type(version) is not int or version != PROTOCOL_VERSION:
            raise ValueError(_MSG_SERIALIZED)

        target_fingerprint = value["target_fingerprint"]
        source_id = value["source_id"]
        content = value["content"]
        metadata = value["metadata"]
        if not (
            isinstance(target_fingerprint, str)
            and isinstance(source_id, str)
            and isinstance(content, str)
            and isinstance(metadata, Mapping)
        ):
            raise ValueError(_MSG_SERIALIZED)  # noqa: TRY004

        raw_timestamp = value["timestamp"]
        timestamp: datetime | None = None
        if raw_timestamp is not None:
            if not isinstance(raw_timestamp, str):
                raise ValueError(_MSG_TIMESTAMP)
            try:
                timestamp = datetime.fromisoformat(raw_timestamp)
            except ValueError:
                raise ValueError(_MSG_TIMESTAMP) from None

        request = cls.create(
            target_fingerprint=target_fingerprint,
            source_id=source_id,
            content=content,
            timestamp=timestamp,
            metadata=metadata,
        )
        if request.to_json() != dict(value):
            raise ValueError(_MSG_SERIALIZED)
        return request

    @property
    def target_fingerprint(self) -> str:
        return self._target_fingerprint

    @property
    def source_id(self) -> str:
        return self._source_id

    @property
    def content(self) -> str:
        return self._content

    @property
    def timestamp(self) -> datetime | None:
        return self._timestamp

    @property
    def metadata(self) -> Mapping[str, str]:
        return dict(self._metadata_items)

    @property
    def document_id(self) -> str:
        material = canonical_json(
            {
                "source_id": self._source_id,
                "target_fingerprint": self._target_fingerprint,
            }
        )
        digest = hashlib.sha256(material.encode("utf-8")).hexdigest()
        return f"{DOCUMENT_ID_PREFIX}{digest}"

    @property
    def payload_sha256(self) -> str:
        material = canonical_json(self.to_json())
        return hashlib.sha256(material.encode("utf-8")).hexdigest()

    @property
    def operation_id(self) -> str:
        name = (
            "komari:hindsight:retain:v1:"
            f"{self._target_fingerprint}:{self.document_id}:{self.payload_sha256}"
        )
        return str(uuid.uuid5(uuid.NAMESPACE_URL, name))

    def to_json(self) -> dict[str, object]:
        """Return a fresh JSON-ready representation with no credentials."""
        return {
            "version": PROTOCOL_VERSION,
            "target_fingerprint": self._target_fingerprint,
            "source_id": self._source_id,
            "content": self._content,
            "timestamp": _serialize_timestamp(self._timestamp),
            "metadata": dict(self._metadata_items),
        }


@dataclass(frozen=True, slots=True)
class HindsightRetainOutcome:
    """A safe retain attempt result plus session-local version state."""

    failure_code: str | None
    version_verified: bool


def _deadline_expired(deadline: float) -> bool:
    return time.monotonic() >= deadline


def _timeout_outcome(*, version_verified: bool) -> HindsightRetainOutcome:
    return HindsightRetainOutcome("request_timeout", version_verified)


def _read_field(value: object, name: str) -> object:
    if isinstance(value, Mapping):
        return value.get(name)
    return getattr(value, name, None)


def _parse_api_version(raw: str) -> tuple[int, int, int] | None:
    match = _API_VERSION_PATTERN.match(raw)
    if match is None:
        return None
    return (int(match.group(1)), int(match.group(2)), int(match.group(3)))


def _reserved_metadata(request: HindsightRetainRequest) -> dict[str, str]:
    return {
        "komari_online_schema": "1",
        "komari_online_source_id": request.source_id,
        "komari_online_payload_sha256": request.payload_sha256,
        "komari_online_operation_id": request.operation_id,
        "komari_online_target_fingerprint": request.target_fingerprint,
    }


async def _check_version(client: Hindsight) -> str | None:
    try:
        response = await client.aget_version()
    except asyncio.CancelledError:
        raise
    except TimeoutError:
        return "request_timeout"
    except Exception:
        return "retain_failed"

    raw_version = _read_field(response, "api_version")
    if not isinstance(raw_version, str):
        return "target_version_unsupported"
    parsed = _parse_api_version(raw_version)
    if parsed is None or parsed < MINIMUM_API_VERSION:
        return "target_version_unsupported"
    return None


def _looks_like_document(document: object) -> bool:
    return (
        isinstance(document, DocumentResponse | Mapping)
        or _read_field(document, "id") is not None
        or _read_field(document, "bank_id") is not None
    )


def _document_matches(
    document: object,
    *,
    bank_id: str,
    request: HindsightRetainRequest,
) -> bool:
    actual_id = _read_field(document, "id")
    if not isinstance(actual_id, str) or actual_id != request.document_id:
        return False
    actual_bank = _read_field(document, "bank_id")
    if not isinstance(actual_bank, str) or actual_bank != bank_id:
        return False
    original_text = _read_field(document, "original_text")
    if not isinstance(original_text, str) or original_text != request.content:
        return False
    metadata = _read_field(document, "document_metadata")
    if not isinstance(metadata, Mapping):
        return False
    expected = {**request.metadata, **_reserved_metadata(request)}
    return all(metadata.get(key) == value for key, value in expected.items())


async def _check_existing_document(
    client: Hindsight,
    *,
    bank_id: str,
    request: HindsightRetainRequest,
    timeout_seconds: float,
) -> tuple[str | None, bool]:
    try:
        document = await client.documents.get_document(
            bank_id,
            request.document_id,
            _request_timeout=timeout_seconds,
        )
    except asyncio.CancelledError:
        raise
    except NotFoundException:
        return None, False
    except TimeoutError:
        return "request_timeout", False
    except Exception:
        return "document_read_failed", False

    if not _looks_like_document(document):
        return "document_read_failed", False
    if _document_matches(document, bank_id=bank_id, request=request):
        return None, True
    return "document_conflict", False


async def _check_bank_config(
    client: Hindsight,
    *,
    bank_id: str,
    timeout_seconds: float,
) -> str | None:
    try:
        response = await client.banks.get_bank_config(
            bank_id,
            _request_timeout=timeout_seconds,
        )
    except asyncio.CancelledError:
        raise
    except TimeoutError:
        return "request_timeout"
    except Exception:
        return "bank_unavailable"

    returned_bank_id = _read_field(response, "bank_id")
    if not isinstance(returned_bank_id, str) or returned_bank_id != bank_id:
        return "bank_identity_mismatch"
    return None


def _retain_kwargs(
    *,
    bank_id: str,
    request: HindsightRetainRequest,
) -> dict[str, Any]:
    item: dict[str, Any] = {
        "content": request.content,
        "metadata": {
            **request.metadata,
            **_reserved_metadata(request),
        },
    }
    if request.timestamp is not None:
        item["timestamp"] = _serialize_timestamp(request.timestamp)
    return {
        "bank_id": bank_id,
        "items": [item],
        "document_id": request.document_id,
        "retain_async": True,
        "operation_id": request.operation_id,
    }


def _verify_receipt(  # noqa: PLR0911
    response: object,
    *,
    bank_id: str,
    request: HindsightRetainRequest,
) -> str | None:
    if not isinstance(response, RetainResponse):
        return "response_invalid"
    if response.success is not True:
        return "retain_failed"
    if response.bank_id != bank_id:
        return "response_invalid"
    if response.var_async is not True:
        return "response_invalid"
    if type(response.items_count) is not int or response.items_count != 1:
        return "response_invalid"
    if response.operation_id != request.operation_id:
        return "response_invalid"
    return None


async def perform_retain(  # noqa: PLR0911
    client: Hindsight,
    *,
    bank_id: str,
    request: HindsightRetainRequest,
    timeout_seconds: float,
    version_verified: bool,
    submission_guard: Callable[[], bool],
    is_session_open: Callable[[], bool],
) -> HindsightRetainOutcome:
    """Run one bounded retain attempt with no retry queue or polling."""
    deadline = time.monotonic() + timeout_seconds

    if not submission_guard():
        return HindsightRetainOutcome("submission_not_admitted", version_verified)
    if _deadline_expired(deadline):
        return _timeout_outcome(version_verified=version_verified)

    try:
        async with asyncio.timeout(timeout_seconds):
            if not version_verified:
                failure = await _check_version(client)
                if not is_session_open():
                    return HindsightRetainOutcome(
                        "session_closed",
                        version_verified,
                    )
                if failure is None:
                    version_verified = True
                if _deadline_expired(deadline):
                    return _timeout_outcome(version_verified=version_verified)
                if failure is not None:
                    return HindsightRetainOutcome(failure, version_verified)

            failure, accepted = await _check_existing_document(
                client,
                bank_id=bank_id,
                request=request,
                timeout_seconds=timeout_seconds,
            )
            if not is_session_open():
                return HindsightRetainOutcome("session_closed", version_verified)
            if _deadline_expired(deadline):
                return _timeout_outcome(version_verified=version_verified)
            if failure is not None:
                return HindsightRetainOutcome(failure, version_verified)
            if accepted:
                return HindsightRetainOutcome(None, version_verified)

            failure = await _check_bank_config(
                client,
                bank_id=bank_id,
                timeout_seconds=timeout_seconds,
            )
            if not is_session_open():
                return HindsightRetainOutcome("session_closed", version_verified)
            if _deadline_expired(deadline):
                return _timeout_outcome(version_verified=version_verified)
            if failure is not None:
                return HindsightRetainOutcome(failure, version_verified)

            kwargs = _retain_kwargs(bank_id=bank_id, request=request)
            # Shutdown and admission are the final synchronous gates. The
            # deadline is rechecked after the caller guard and immediately
            # before the async submission; no await separates them.
            if not is_session_open():
                return HindsightRetainOutcome("session_closed", version_verified)
            if not submission_guard():
                return HindsightRetainOutcome(
                    "submission_not_admitted",
                    version_verified,
                )
            if _deadline_expired(deadline):
                return _timeout_outcome(version_verified=version_verified)
            try:
                response = await client.aretain_batch(**kwargs)
            except asyncio.CancelledError:
                raise
            except TimeoutError:
                return _timeout_outcome(version_verified=version_verified)
            except Exception:
                return HindsightRetainOutcome(
                    "retain_failed",
                    version_verified,
                )
            if _deadline_expired(deadline):
                return _timeout_outcome(version_verified=version_verified)
            failure = _verify_receipt(
                response,
                bank_id=bank_id,
                request=request,
            )
            return HindsightRetainOutcome(failure, version_verified)
    except TimeoutError:
        return _timeout_outcome(version_verified=version_verified)


__all__ = [
    "DOCUMENT_ID_PREFIX",
    "MINIMUM_API_VERSION",
    "PROTOCOL_VERSION",
    "RESERVED_METADATA_KEYS",
    "RESERVED_METADATA_PREFIX",
    "HindsightRetainOutcome",
    "HindsightRetainRequest",
    "canonical_json",
    "derive_target_fingerprint",
    "perform_retain",
]
