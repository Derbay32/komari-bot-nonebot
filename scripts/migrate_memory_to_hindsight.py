"""One-off export/import of komari-bot database memory into Hindsight.

The script is deliberately standalone: it never imports ``komari_bot`` and the
bot runtime never imports it.  Export reads only the three official memory
tables from PostgreSQL inside one repeatable-read, read-only transaction.
Import writes each exported record as one asynchronous Hindsight retain and
verifies completion through the official client only.

The detailed export manifest intentionally contains the source records.  It is
written with private permissions; ordinary logs only contain safe codes,
counts and opaque identifiers.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import fcntl
import hashlib
import json
import math
import os
import re
import sys
import tempfile
import time
import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from datetime import time as datetime_time
from pathlib import Path
from typing import Any, NoReturn
from urllib.parse import urlsplit

import asyncpg

MANIFEST_KIND = "komari-memory-export"
MANIFEST_SCHEMA_VERSION = 1
PROGRESS_KIND = "komari-memory-hindsight-import-progress"
PROGRESS_SCHEMA_VERSION = 1
REPORT_KIND = "komari-memory-hindsight-import-report"
REPORT_SCHEMA_VERSION = 1
PROGRESS_LOCK_SUFFIX = ".lock"

TABLE_CONVERSATIONS = "komari_memory_conversations"
TABLE_PROFILES = "komari_memory_user_profile"
TABLE_INTERACTIONS = "komari_memory_interaction_history"
TABLE_ENTITY = "komari_memory_entity"
LEGACY_INTERACTION_PREFIX = "komari_memory_interaction_history_legacy_"

ERROR_INTERNAL = "internal_error"
ERROR_USAGE = "usage_error"
ERROR_SOURCE_ID_INVALID = "source_id_invalid"
ERROR_SOURCE_DSN_MISSING = "source_dsn_missing"
ERROR_SOURCE_DSN_INVALID = "source_dsn_invalid"
ERROR_SOURCE_TABLE_MISSING = "source_table_missing"
ERROR_EXPORT_UNSUPPORTED_SOURCE = "export_unsupported_source"
ERROR_EXPORT_WRITE_FAILED = "export_write_failed"
ERROR_EXPORT_JSON_INVALID = "export_json_invalid"
ERROR_EXPORT_OUTPUT_INVALID = "export_output_invalid"
ERROR_MANIFEST_MISSING = "manifest_missing"
ERROR_MANIFEST_INVALID = "manifest_invalid"
ERROR_PROGRESS_INVALID = "progress_invalid"
ERROR_PROGRESS_BINDING_MISMATCH = "progress_binding_mismatch"
ERROR_PROGRESS_LOCKED = "progress_locked"
ERROR_PROGRESS_WRITE_FAILED = "progress_write_failed"
ERROR_REPORT_WRITE_FAILED = "report_write_failed"
ERROR_TARGET_ENV_MISSING = "target_env_missing"
ERROR_TARGET_CONFIG_INVALID = "target_config_invalid"
ERROR_TARGET_VERSION_UNSUPPORTED = "target_version_unsupported"
ERROR_TARGET_BANK_MISSING = "target_bank_missing"
ERROR_TARGET_BANK_CHECK_FAILED = "target_bank_check_failed"
ERROR_TARGET_DOCUMENT_CONFLICT = "target_document_conflict"
ERROR_RECEIPT_INVALID = "receipt_invalid"
ERROR_OPERATION_FAILED = "operation_failed"
ERROR_OPERATION_UNKNOWN = "operation_unknown"
ERROR_TARGET_DOCUMENT_MISSING = "target_document_missing"
ERROR_TARGET_DOCUMENT_TEXT_MISSING = "target_document_text_missing"
ERROR_TARGET_DOCUMENT_TEXT_MISMATCH = "target_document_text_mismatch"
ERROR_TARGET_DOCUMENT_ID_MISMATCH = "target_document_id_mismatch"
ERROR_TARGET_DOCUMENT_BANK_MISMATCH = "target_document_bank_mismatch"
ERROR_TARGET_DOCUMENT_METADATA_MISMATCH = "target_document_metadata_mismatch"
ERROR_TARGET_DOCUMENT_READ_FAILED = "target_document_read_failed"
ERROR_TARGET_VERSION_CHECK_FAILED = "target_version_check_failed"
ERROR_RETAIN_OUTCOME_UNKNOWN = "retain_outcome_unknown"
ERROR_RETAIN_REJECTED = "retain_rejected"
ERROR_SDK_UNAVAILABLE = "sdk_unavailable"
ERROR_SDK_VERSION_UNSUPPORTED = "sdk_version_unsupported"

RECORD_STATUS_COMPLETED = "completed"
RECORD_STATUS_FAILED = "failed"
RECORD_STATUS_UNKNOWN = "unknown"
RECORD_STATUS_CONFLICT = "conflict"
RECORD_STATUS_PENDING_SUBMIT = "pending_submit"
RECORD_STATUS_SUBMITTED = "submitted"
RECORD_STATUS_UNCERTAIN = "uncertain"

MINIMUM_TARGET_API_VERSION = (1, 1, 0)
HINDSIGHT_CLIENT_VERSION = "1.1.0"

_SOURCE_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_VERSION_PATTERN = re.compile(r"^(\d+)\.(\d+)\.(\d+)")


@dataclass(frozen=True, slots=True)
class TableSpec:
    """Explicit non-vector columns of one migrated source table."""

    table: str
    kind: str
    primary_key: tuple[str, ...]
    columns: tuple[str, ...]
    provenance_columns: tuple[str, ...]
    json_columns: tuple[str, ...] = ()

    def select_sql(self) -> str:
        columns = ", ".join(self.columns)
        ordering = ", ".join(self.primary_key)
        return f"SELECT {columns} FROM {self.table} ORDER BY {ordering}"


TABLE_SPECS: tuple[TableSpec, ...] = (
    TableSpec(
        table=TABLE_CONVERSATIONS,
        kind="conversation",
        primary_key=("id",),
        columns=(
            "id",
            "group_id",
            "summary",
            "participants",
            "dedup_key",
            "start_time",
            "end_time",
            "importance",
            "importance_initial",
            "importance_current",
            "last_accessed",
            "is_fuzzy",
            "created_at",
        ),
        provenance_columns=("group_id",),
    ),
    TableSpec(
        table=TABLE_PROFILES,
        kind="user_profile",
        primary_key=("user_id", "group_id"),
        columns=(
            "user_id",
            "group_id",
            "version",
            "display_name",
            "traits",
            "updated_at",
            "importance",
            "access_count",
            "last_accessed",
        ),
        provenance_columns=("group_id",),
        json_columns=("traits",),
    ),
    TableSpec(
        table=TABLE_INTERACTIONS,
        kind="interaction_history",
        primary_key=("id",),
        columns=(
            "id",
            "user_id",
            "display_name",
            "event_summary",
            "source_dedup_key",
            "source_message_count",
            "first_seen_at",
            "last_seen_at",
            "importance",
            "importance_initial",
            "importance_current",
            "last_accessed",
            "is_fuzzy",
            "created_at",
        ),
        provenance_columns=(),
    ),
)

TABLE_SPEC_BY_NAME = {spec.table: spec for spec in TABLE_SPECS}


class MigrationError(Exception):
    """Safe, operator-facing failure with a stable code."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = dict(details) if details else {}


@dataclass(frozen=True, slots=True)
class TargetConfig:
    """Validated, non-secret Hindsight target identity."""

    base_url: str
    api_key: str = field(repr=False)
    bank_id: str


SourceConnector = Callable[[str, float], Awaitable[Any]]
ClientFactory = Callable[[TargetConfig, float], Any]
Clock = Callable[[], float]
Sleeper = Callable[[float], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class MigrationBoundaries:
    """Injectable external boundaries used by the real CLI orchestration."""

    connect_source: SourceConnector
    create_client: ClientFactory
    monotonic: Clock = time.monotonic
    sleep: Sleeper = asyncio.sleep


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _positive_finite(value: float, label: str) -> float:
    if not math.isfinite(value) or value <= 0:
        raise MigrationError(ERROR_USAGE, f"{label} must be a positive finite number")
    return value


def canonical_json(value: Any) -> str:
    """Deterministic JSON used by manifests, hashes and source text."""
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _decode_json_column(value: Any) -> Any:
    if value is None or isinstance(value, (dict, list)):
        return value
    if isinstance(value, (str, bytes, bytearray)):
        try:
            return json.loads(value)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise MigrationError(
                ERROR_EXPORT_JSON_INVALID,
                "a JSONB source column is not valid JSON",
            ) from exc
    raise MigrationError(
        ERROR_EXPORT_JSON_INVALID,
        "a JSONB source column has an unsupported driver type",
    )


def _json_value(value: Any) -> Any:
    """Convert driver values into deterministic JSON-native values."""
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, datetime_time):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    msg = f"unsupported source value type: {type(value).__name__}"
    raise TypeError(msg)


def _validate_source_id(raw: str) -> str:
    if not raw or not _SOURCE_ID_PATTERN.fullmatch(raw):
        raise MigrationError(
            ERROR_SOURCE_ID_INVALID,
            "source-id must be a non-secret identifier using letters, digits, "
            "dot, underscore, colon or hyphen (maximum 128 characters)",
        )
    return raw


def _identity_for(spec: TableSpec, fields: Mapping[str, Any]) -> dict[str, Any]:
    return {column: fields[column] for column in spec.primary_key}


def _provenance_for(spec: TableSpec, fields: Mapping[str, Any]) -> dict[str, Any]:
    return {column: fields[column] for column in spec.provenance_columns}


def _document_id_for(
    source_id: str, spec: TableSpec, identity: Mapping[str, Any]
) -> str:
    material = canonical_json(
        {
            "source_id": source_id,
            "table": spec.table,
            "primary_key": [identity[column] for column in spec.primary_key],
        }
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def build_source_text(
    *,
    kind: str,
    identity: Mapping[str, Any],
    provenance: Mapping[str, Any],
    fields: Mapping[str, Any],
) -> str:
    """Build the deterministic extraction text for one source record."""
    return "\n".join(
        (
            f"record_kind: {kind}",
            f"identity: {canonical_json(identity)}",
            f"provenance: {canonical_json(provenance)}",
            f"content: {canonical_json(fields)}",
        )
    )


def _content_digest(source_text: str) -> str:
    return hashlib.sha256(source_text.encode("utf-8")).hexdigest()


def _scalar_sort_key(value: Any) -> tuple[int, Any]:
    if isinstance(value, bool):
        return (0, int(value))
    if isinstance(value, int):
        return (0, value)
    if isinstance(value, str):
        return (1, value)
    return (2, canonical_json(value))


def _record_sort_key(spec: TableSpec, fields: Mapping[str, Any]) -> tuple[Any, ...]:
    return tuple(_scalar_sort_key(fields[column]) for column in spec.primary_key)


def _normalize_source_dsn(raw: str) -> str:
    parts = urlsplit(raw)
    if parts.scheme.lower() not in {"postgresql", "postgresql+asyncpg"}:
        raise MigrationError(
            ERROR_SOURCE_DSN_INVALID,
            "SQLALCHEMY_DATABASE_URL must use postgresql:// or postgresql+asyncpg://",
        )
    if not parts.hostname or not parts.path.lstrip("/"):
        raise MigrationError(
            ERROR_SOURCE_DSN_INVALID,
            "SQLALCHEMY_DATABASE_URL is missing a host or database name",
        )
    return "postgresql://" + raw.split("://", 1)[1]


def _quote_identifier(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


async def _table_exists(connection: Any, table_name: str) -> bool:
    return bool(
        await connection.fetchval("SELECT to_regclass($1) IS NOT NULL", table_name)
    )


async def _inspect_unsupported_sources(
    connection: Any,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Detect legacy memory tables without reading them as memory content."""
    unsupported: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []

    if await _table_exists(connection, TABLE_ENTITY):
        count = int(
            await connection.fetchval(
                f"SELECT COUNT(*) FROM {_quote_identifier(TABLE_ENTITY)}"
            )
            or 0
        )
        entry = {"table": TABLE_ENTITY, "row_count": count}
        if count:
            unsupported.append(entry)
        else:
            excluded.append(entry)

    legacy_rows = await connection.fetch(
        """
        SELECT c.relname
        FROM pg_class AS c
        WHERE pg_table_is_visible(c.oid)
          AND c.relkind IN ('r', 'p', 'v', 'm', 'f')
          AND c.relname LIKE $1
        ORDER BY c.relname
        """,
        f"{LEGACY_INTERACTION_PREFIX}%",
    )
    for row in legacy_rows:
        table_name = str(row["relname"])
        count = int(
            await connection.fetchval(
                f"SELECT COUNT(*) FROM {_quote_identifier(table_name)}"
            )
            or 0
        )
        entry = {"table": table_name, "row_count": count}
        if count:
            unsupported.append(entry)
        else:
            excluded.append(entry)

    return unsupported, excluded


async def _collect_export_records(
    connection: Any, source_id: str
) -> tuple[list[dict[str, Any]], dict[str, int], list[dict[str, Any]]]:
    unsupported, excluded = await _inspect_unsupported_sources(connection)
    if unsupported:
        raise MigrationError(
            ERROR_EXPORT_UNSUPPORTED_SOURCE,
            "source contains unsupported legacy memory rows; export aborted",
            details={"unsupported_sources": unsupported},
        )

    records: list[dict[str, Any]] = []
    counts: dict[str, int] = {}
    seen_document_ids: set[str] = set()
    seen_primary_keys: set[tuple[str, tuple[Any, ...]]] = set()

    for spec in TABLE_SPECS:
        if not await _table_exists(connection, spec.table):
            raise MigrationError(
                ERROR_SOURCE_TABLE_MISSING,
                f"required source table is missing: {spec.table}",
            )
        raw_rows = await connection.fetch(spec.select_sql())
        rows = [
            {
                column: _json_value(
                    _decode_json_column(row[column])
                    if column in spec.json_columns
                    else row[column]
                )
                for column in spec.columns
            }
            for row in raw_rows
        ]
        rows.sort(key=lambda fields: _record_sort_key(spec, fields))
        counts[spec.table] = len(rows)

        for fields in rows:
            identity = _identity_for(spec, fields)
            provenance = _provenance_for(spec, fields)
            document_id = _document_id_for(source_id, spec, identity)
            primary_key = (
                spec.table,
                tuple(identity[column] for column in spec.primary_key),
            )
            if document_id in seen_document_ids:
                raise MigrationError(
                    ERROR_EXPORT_WRITE_FAILED,
                    "duplicate stable document id detected",
                )
            if primary_key in seen_primary_keys:
                raise MigrationError(
                    ERROR_EXPORT_WRITE_FAILED,
                    "duplicate source primary key detected",
                )
            seen_document_ids.add(document_id)
            seen_primary_keys.add(primary_key)

            source_text = build_source_text(
                kind=spec.kind,
                identity=identity,
                provenance=provenance,
                fields=fields,
            )
            records.append(
                {
                    "document_id": document_id,
                    "table": spec.table,
                    "kind": spec.kind,
                    "primary_key": identity,
                    "provenance": provenance,
                    "fields": fields,
                    "source_text": source_text,
                    "content_sha256": _content_digest(source_text),
                }
            )

    return records, counts, excluded


def _manifest_content_hash(
    *,
    records: Sequence[Mapping[str, Any]],
    counts: Mapping[str, int],
    excluded_sources: Sequence[Mapping[str, Any]],
) -> str:
    payload = {
        "records": list(records),
        "counts": dict(counts),
        "excluded_sources": list(excluded_sources),
    }
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def build_manifest(
    *,
    source_id: str,
    records: list[dict[str, Any]],
    counts: dict[str, int],
    excluded_sources: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "kind": MANIFEST_KIND,
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "source_id": source_id,
        "counts": counts,
        "excluded_sources": excluded_sources,
        "records": records,
        "content_sha256": _manifest_content_hash(
            records=records,
            counts=counts,
            excluded_sources=excluded_sources,
        ),
    }


def _atomic_write_bytes(
    path: Path,
    payload: bytes,
    *,
    error_code: str,
) -> None:
    """Write a private file atomically, preserving an existing file on failure."""
    parent = path.parent
    if not parent.exists() or not parent.is_dir():
        raise MigrationError(
            error_code,
            "output parent directory does not exist",
        )
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=parent,
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            os.fchmod(handle.fileno(), 0o600)
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        temporary_path.replace(path)
    except Exception:
        with contextlib.suppress(OSError):
            temporary_path.unlink(missing_ok=True)
        raise
    else:
        try:
            directory_descriptor = os.open(parent, os.O_RDONLY)
        except OSError:
            return
        try:
            with contextlib.suppress(OSError):
                os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)


def _atomic_write_json(
    path: Path,
    payload: Mapping[str, Any],
    *,
    error_code: str,
) -> None:
    try:
        _atomic_write_bytes(
            path,
            (canonical_json(payload) + "\n").encode("utf-8"),
            error_code=error_code,
        )
    except MigrationError:
        raise
    except OSError as exc:
        raise MigrationError(
            error_code,
            "private file could not be written",
        ) from exc


async def _default_connect_source(dsn: str, request_timeout: float) -> Any:
    return await asyncpg.connect(dsn=dsn, command_timeout=request_timeout)


def _load_hindsight_client() -> type[Any]:
    try:
        package_version = __import__(
            "importlib.metadata", fromlist=["version"]
        ).version("hindsight-client")
    except Exception as exc:
        raise MigrationError(
            ERROR_SDK_UNAVAILABLE,
            "hindsight-client is not installed",
        ) from exc
    if package_version != HINDSIGHT_CLIENT_VERSION:
        raise MigrationError(
            ERROR_SDK_VERSION_UNSUPPORTED,
            f"hindsight-client must be exactly {HINDSIGHT_CLIENT_VERSION}",
        )
    try:
        module = __import__("hindsight_client", fromlist=["Hindsight"])
    except Exception as exc:
        raise MigrationError(
            ERROR_SDK_UNAVAILABLE,
            "hindsight-client could not be imported",
        ) from exc
    return module.Hindsight


def _default_create_client(target: TargetConfig, timeout: float) -> Any:
    client_type = _load_hindsight_client()
    return client_type(
        base_url=target.base_url,
        api_key=target.api_key,
        timeout=timeout,
        max_attempts=1,
    )


def _default_boundaries() -> MigrationBoundaries:
    return MigrationBoundaries(
        connect_source=_default_connect_source,
        create_client=_default_create_client,
        monotonic=time.monotonic,
        sleep=asyncio.sleep,
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Offline komari-bot database-memory migration to Hindsight",
    )
    subparsers = parser.add_subparsers(dest="phase", required=True)

    export_parser = subparsers.add_parser(
        "export",
        help="Export the three database memory tables into a private manifest",
    )
    export_parser.add_argument(
        "--source-id",
        required=True,
        help="Explicit non-secret deployment identifier for this source",
    )
    export_parser.add_argument(
        "--output",
        "--manifest",
        dest="output",
        required=True,
        type=Path,
        help="Manifest output path (written atomically with mode 0600)",
    )
    export_parser.add_argument(
        "--db-timeout",
        type=float,
        default=60.0,
        help="asyncpg command timeout in seconds (default: 60)",
    )

    import_parser = subparsers.add_parser(
        "import",
        help="Import a private manifest into an existing Hindsight bank",
    )
    import_parser.add_argument("--manifest", required=True, type=Path)
    import_parser.add_argument("--progress", required=True, type=Path)
    import_parser.add_argument("--report", required=True, type=Path)
    import_parser.add_argument(
        "--apply",
        action="store_true",
        required=True,
        help="Required acknowledgement that this command writes to Hindsight",
    )
    import_parser.add_argument(
        "--request-timeout",
        type=float,
        default=30.0,
        help="Hindsight request timeout in seconds (default: 30)",
    )
    import_parser.add_argument(
        "--operation-timeout",
        type=float,
        default=300.0,
        help="Maximum operation polling window in seconds (default: 300)",
    )
    import_parser.add_argument(
        "--poll-interval",
        type=float,
        default=2.0,
        help="Operation polling interval in seconds (default: 2)",
    )
    return parser


async def _run_export(args: argparse.Namespace, boundaries: MigrationBoundaries) -> int:
    source_id = _validate_source_id(args.source_id)
    output_path = Path(args.output)
    if output_path.exists() and output_path.is_dir():
        raise MigrationError(
            ERROR_EXPORT_OUTPUT_INVALID,
            "manifest output path is a directory",
        )

    dsn = os.environ.get("SQLALCHEMY_DATABASE_URL", "").strip()
    if not dsn:
        raise MigrationError(
            ERROR_SOURCE_DSN_MISSING,
            "SQLALCHEMY_DATABASE_URL is required for export",
        )
    normalized_dsn = _normalize_source_dsn(dsn)
    _positive_finite(float(args.db_timeout), "db timeout")

    connection = await boundaries.connect_source(normalized_dsn, float(args.db_timeout))
    try:
        async with connection.transaction(
            isolation="repeatable_read",
            readonly=True,
        ):
            records, counts, excluded_sources = await _collect_export_records(
                connection, source_id
            )
    finally:
        await connection.close()

    manifest = build_manifest(
        source_id=source_id,
        records=records,
        counts=counts,
        excluded_sources=excluded_sources,
    )
    _atomic_write_json(output_path, manifest, error_code=ERROR_EXPORT_WRITE_FAILED)
    return 0


def _read_field(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def _api_status_code(error: Exception) -> int | None:
    for attribute in ("status", "status_code", "code"):
        raw = getattr(error, attribute, None)
        if isinstance(raw, int) and not isinstance(raw, bool):
            return raw
    return None


def _normalize_base_url(raw: str) -> str:
    try:
        parts = urlsplit(raw)
        port = parts.port
    except ValueError as exc:
        raise MigrationError(
            ERROR_TARGET_CONFIG_INVALID,
            "HINDSIGHT_BASE_URL is malformed",
        ) from exc
    if parts.scheme.lower() not in {"http", "https"}:
        raise MigrationError(
            ERROR_TARGET_CONFIG_INVALID,
            "HINDSIGHT_BASE_URL must use http:// or https://",
        )
    if not parts.hostname:
        raise MigrationError(
            ERROR_TARGET_CONFIG_INVALID,
            "HINDSIGHT_BASE_URL is missing a host",
        )
    if parts.username is not None or parts.password is not None:
        raise MigrationError(
            ERROR_TARGET_CONFIG_INVALID,
            "HINDSIGHT_BASE_URL must not embed credentials",
        )
    if parts.query or parts.fragment:
        raise MigrationError(
            ERROR_TARGET_CONFIG_INVALID,
            "HINDSIGHT_BASE_URL must not contain a query or fragment",
        )
    host = parts.hostname
    rendered_host = f"[{host}]" if ":" in host else host
    netloc = rendered_host if port is None else f"{rendered_host}:{port}"
    return f"{parts.scheme.lower()}://{netloc}{parts.path.rstrip('/')}"


def _load_target_config() -> TargetConfig:
    base_url = os.environ.get("HINDSIGHT_BASE_URL", "").strip()
    api_key = os.environ.get("HINDSIGHT_API_KEY", "")
    bank_id = os.environ.get("HINDSIGHT_BANK_ID", "").strip()
    if not base_url or not api_key or not bank_id:
        raise MigrationError(
            ERROR_TARGET_ENV_MISSING,
            "HINDSIGHT_BASE_URL, HINDSIGHT_API_KEY and HINDSIGHT_BANK_ID "
            "are required for import",
        )
    if any(ord(character) < 32 for character in bank_id):
        raise MigrationError(
            ERROR_TARGET_CONFIG_INVALID,
            "HINDSIGHT_BANK_ID contains control characters",
        )
    return TargetConfig(
        base_url=_normalize_base_url(base_url),
        api_key=api_key,
        bank_id=bank_id,
    )


def _parse_api_version(raw: str) -> tuple[int, int, int] | None:
    match = _VERSION_PATTERN.match(raw)
    if match is None:
        return None
    return (int(match.group(1)), int(match.group(2)), int(match.group(3)))


def _validate_manifest_record(
    record: Any,
    *,
    source_id: str,
    seen_document_ids: set[str],
    seen_primary_keys: set[tuple[str, tuple[Any, ...]]],
) -> None:
    if not isinstance(record, Mapping):
        raise MigrationError(ERROR_MANIFEST_INVALID, "manifest record is malformed")
    table = record.get("table")
    if not isinstance(table, str):
        raise MigrationError(ERROR_MANIFEST_INVALID, "manifest record table is invalid")
    spec = TABLE_SPEC_BY_NAME.get(table)
    if spec is None:
        raise MigrationError(ERROR_MANIFEST_INVALID, "manifest record table is unknown")
    if record.get("kind") != spec.kind:
        raise MigrationError(ERROR_MANIFEST_INVALID, "manifest record kind is invalid")

    primary_key = record.get("primary_key")
    if not isinstance(primary_key, Mapping) or set(primary_key) != set(
        spec.primary_key
    ):
        raise MigrationError(
            ERROR_MANIFEST_INVALID,
            "manifest record primary key is invalid",
        )
    fields = record.get("fields")
    if not isinstance(fields, Mapping) or set(fields) != set(spec.columns):
        raise MigrationError(
            ERROR_MANIFEST_INVALID,
            "manifest record fields are incomplete",
        )
    expected_identity = _identity_for(spec, fields)
    if dict(primary_key) != expected_identity:
        raise MigrationError(
            ERROR_MANIFEST_INVALID,
            "manifest record primary key does not match fields",
        )
    expected_provenance = _provenance_for(spec, fields)
    raw_provenance = record.get("provenance")
    if (
        not isinstance(raw_provenance, Mapping)
        or dict(raw_provenance) != expected_provenance
    ):
        raise MigrationError(
            ERROR_MANIFEST_INVALID,
            "manifest record provenance is invalid",
        )

    expected_document_id = _document_id_for(source_id, spec, expected_identity)
    if record.get("document_id") != expected_document_id:
        raise MigrationError(
            ERROR_MANIFEST_INVALID,
            "manifest document id is invalid",
        )
    source_text = record.get("source_text")
    if not isinstance(source_text, str):
        raise MigrationError(ERROR_MANIFEST_INVALID, "manifest source text is invalid")
    expected_source_text = build_source_text(
        kind=spec.kind,
        identity=expected_identity,
        provenance=expected_provenance,
        fields=fields,
    )
    if source_text != expected_source_text:
        raise MigrationError(
            ERROR_MANIFEST_INVALID,
            "manifest source text does not match fields",
        )
    if record.get("content_sha256") != _content_digest(source_text):
        raise MigrationError(
            ERROR_MANIFEST_INVALID,
            "manifest content hash is invalid",
        )

    if expected_document_id in seen_document_ids:
        raise MigrationError(
            ERROR_MANIFEST_INVALID,
            "manifest contains duplicate document ids",
        )
    primary_key_tuple = (
        spec.table,
        tuple(expected_identity[column] for column in spec.primary_key),
    )
    if primary_key_tuple in seen_primary_keys:
        raise MigrationError(
            ERROR_MANIFEST_INVALID,
            "manifest contains duplicate source primary keys",
        )
    seen_document_ids.add(expected_document_id)
    seen_primary_keys.add(primary_key_tuple)


def _validate_manifest(data: Any) -> None:
    if not isinstance(data, Mapping):
        raise MigrationError(ERROR_MANIFEST_INVALID, "manifest is not an object")
    if data.get("kind") != MANIFEST_KIND:
        raise MigrationError(ERROR_MANIFEST_INVALID, "manifest kind is invalid")
    if data.get("schema_version") != MANIFEST_SCHEMA_VERSION:
        raise MigrationError(ERROR_MANIFEST_INVALID, "manifest schema is unsupported")
    source_id = data.get("source_id")
    if not isinstance(source_id, str) or not _SOURCE_ID_PATTERN.fullmatch(source_id):
        raise MigrationError(ERROR_MANIFEST_INVALID, "manifest source id is invalid")

    records = data.get("records")
    counts = data.get("counts")
    excluded_sources = data.get("excluded_sources")
    if not isinstance(records, list):
        raise MigrationError(ERROR_MANIFEST_INVALID, "manifest records are invalid")
    if not isinstance(counts, Mapping) or any(
        not isinstance(value, int) or isinstance(value, bool)
        for value in counts.values()
    ):
        raise MigrationError(ERROR_MANIFEST_INVALID, "manifest counts are invalid")
    if not isinstance(excluded_sources, list):
        raise MigrationError(
            ERROR_MANIFEST_INVALID,
            "manifest excluded sources are invalid",
        )

    expected_counts = {spec.table: 0 for spec in TABLE_SPECS}
    seen_document_ids: set[str] = set()
    seen_primary_keys: set[tuple[str, tuple[Any, ...]]] = set()
    for record in records:
        _validate_manifest_record(
            record,
            source_id=source_id,
            seen_document_ids=seen_document_ids,
            seen_primary_keys=seen_primary_keys,
        )
        expected_counts[str(record["table"])] += 1

    if dict(counts) != expected_counts:
        raise MigrationError(
            ERROR_MANIFEST_INVALID, "manifest counts do not match records"
        )

    for entry in excluded_sources:
        if not isinstance(entry, Mapping):
            raise MigrationError(
                ERROR_MANIFEST_INVALID,
                "manifest excluded source entry is malformed",
            )
        table = entry.get("table")
        row_count = entry.get("row_count")
        if (
            not isinstance(table, str)
            or not isinstance(row_count, int)
            or isinstance(row_count, bool)
        ):
            raise MigrationError(
                ERROR_MANIFEST_INVALID,
                "manifest excluded source entry is malformed",
            )
        if table != TABLE_ENTITY and not table.startswith(LEGACY_INTERACTION_PREFIX):
            raise MigrationError(
                ERROR_MANIFEST_INVALID,
                "manifest excluded source table is not a legacy memory table",
            )
        if row_count != 0:
            raise MigrationError(
                ERROR_MANIFEST_INVALID,
                "manifest excluded source table is not empty",
            )

    expected_content_hash = _manifest_content_hash(
        records=records,
        counts=counts,
        excluded_sources=excluded_sources,
    )
    if data.get("content_sha256") != expected_content_hash:
        raise MigrationError(
            ERROR_MANIFEST_INVALID,
            "manifest content checksum is invalid",
        )


def _load_manifest(path: Path) -> tuple[dict[str, Any], str]:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise MigrationError(
            ERROR_MANIFEST_MISSING,
            "manifest file is missing or unreadable",
        ) from exc
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise MigrationError(
            ERROR_MANIFEST_INVALID, "manifest is not valid JSON"
        ) from exc
    _validate_manifest(data)
    if not isinstance(data, dict):
        raise MigrationError(ERROR_MANIFEST_INVALID, "manifest is not an object")
    return data, hashlib.sha256(raw).hexdigest()


def _request_identity(
    record: Mapping[str, Any], manifest_sha256: str
) -> dict[str, Any]:
    return {
        "manifest_sha256": manifest_sha256,
        "table": record["table"],
        "primary_key": record["primary_key"],
        "document_id": record["document_id"],
        "content_sha256": record["content_sha256"],
    }


def _new_progress_entry(
    record: Mapping[str, Any],
    *,
    manifest: Mapping[str, Any],
    manifest_sha256: str,
    operation_id: str | None,
    status: str,
) -> dict[str, Any]:
    return {
        "request_identity": {
            "source_id": manifest["source_id"],
            **_request_identity(record, manifest_sha256),
        },
        "table": record["table"],
        "primary_key": record["primary_key"],
        "document_id": record["document_id"],
        "content_sha256": record["content_sha256"],
        "operation_id": operation_id,
        "status": status,
        "attempts": 0,
        "last_error_code": None,
    }


def _new_progress_state(
    *,
    manifest: Mapping[str, Any],
    manifest_sha256: str,
    target: TargetConfig,
) -> dict[str, Any]:
    return {
        "kind": PROGRESS_KIND,
        "schema_version": PROGRESS_SCHEMA_VERSION,
        "source_id": manifest["source_id"],
        "manifest_sha256": manifest_sha256,
        "target": {
            "base_url": target.base_url,
            "bank_id": target.bank_id,
        },
        "records": {},
    }


def _validate_progress(
    data: Any,
    *,
    manifest: Mapping[str, Any],
    manifest_sha256: str,
    target: TargetConfig,
) -> dict[str, Any]:
    if not isinstance(data, Mapping):
        raise MigrationError(ERROR_PROGRESS_INVALID, "progress file is malformed")
    if data.get("kind") != PROGRESS_KIND:
        raise MigrationError(ERROR_PROGRESS_INVALID, "progress kind is invalid")
    if data.get("schema_version") != PROGRESS_SCHEMA_VERSION:
        raise MigrationError(ERROR_PROGRESS_INVALID, "progress schema is unsupported")
    if data.get("manifest_sha256") != manifest_sha256:
        raise MigrationError(
            ERROR_PROGRESS_BINDING_MISMATCH,
            "progress is bound to a different source manifest",
        )
    if data.get("source_id") != manifest["source_id"]:
        raise MigrationError(
            ERROR_PROGRESS_BINDING_MISMATCH,
            "progress is bound to a different source deployment",
        )
    expected_target = {
        "base_url": target.base_url,
        "bank_id": target.bank_id,
    }
    if data.get("target") != expected_target:
        raise MigrationError(
            ERROR_PROGRESS_BINDING_MISMATCH,
            "progress is bound to a different Hindsight target",
        )

    records = data.get("records")
    if not isinstance(records, Mapping):
        raise MigrationError(ERROR_PROGRESS_INVALID, "progress records are invalid")
    manifest_records = {record["document_id"]: record for record in manifest["records"]}
    seen_operation_ids: set[str] = set()
    allowed_statuses = {
        RECORD_STATUS_PENDING_SUBMIT,
        RECORD_STATUS_SUBMITTED,
        RECORD_STATUS_UNCERTAIN,
        RECORD_STATUS_COMPLETED,
        RECORD_STATUS_FAILED,
        RECORD_STATUS_CONFLICT,
    }
    for document_id, entry in records.items():
        record = manifest_records.get(document_id)
        if record is None or not isinstance(entry, Mapping):
            raise MigrationError(
                ERROR_PROGRESS_INVALID,
                "progress contains a record outside the manifest",
            )
        if entry.get("document_id") != document_id:
            raise MigrationError(
                ERROR_PROGRESS_INVALID, "progress document id is invalid"
            )
        if entry.get("table") != record["table"]:
            raise MigrationError(ERROR_PROGRESS_INVALID, "progress table is invalid")
        if entry.get("primary_key") != record["primary_key"]:
            raise MigrationError(
                ERROR_PROGRESS_INVALID,
                "progress primary key is invalid",
            )
        if entry.get("content_sha256") != record["content_sha256"]:
            raise MigrationError(
                ERROR_PROGRESS_INVALID,
                "progress content identity is invalid",
            )
        expected_request_identity = {
            "source_id": manifest["source_id"],
            **_request_identity(record, manifest_sha256),
        }
        if entry.get("request_identity") != expected_request_identity:
            raise MigrationError(
                ERROR_PROGRESS_INVALID,
                "progress request identity is invalid",
            )
        status = entry.get("status")
        if status not in allowed_statuses:
            raise MigrationError(ERROR_PROGRESS_INVALID, "progress status is invalid")
        operation_id = entry.get("operation_id")
        if operation_id is not None:
            if not isinstance(operation_id, str):
                raise MigrationError(
                    ERROR_PROGRESS_INVALID,
                    "progress operation id is invalid",
                )
            try:
                parsed_operation_id = uuid.UUID(operation_id)
            except ValueError as exc:
                raise MigrationError(
                    ERROR_PROGRESS_INVALID,
                    "progress operation id is invalid",
                ) from exc
            if str(parsed_operation_id) != operation_id:
                raise MigrationError(
                    ERROR_PROGRESS_INVALID,
                    "progress operation id is not canonical",
                )
            if operation_id in seen_operation_ids:
                raise MigrationError(
                    ERROR_PROGRESS_INVALID,
                    "progress reuses an operation id across records",
                )
            seen_operation_ids.add(operation_id)
        if status != RECORD_STATUS_CONFLICT and operation_id is None:
            raise MigrationError(
                ERROR_PROGRESS_INVALID,
                "progress is missing a frozen operation id",
            )
        attempts = entry.get("attempts")
        if not isinstance(attempts, int) or isinstance(attempts, bool) or attempts < 0:
            raise MigrationError(
                ERROR_PROGRESS_INVALID, "progress attempts are invalid"
            )
        last_error_code = entry.get("last_error_code")
        if last_error_code is not None and not isinstance(last_error_code, str):
            raise MigrationError(
                ERROR_PROGRESS_INVALID,
                "progress error code is invalid",
            )

    return dict(data)


@dataclass(slots=True)
class _ProgressStore:
    path: Path
    state: dict[str, Any]

    @property
    def records(self) -> dict[str, Any]:
        records = self.state.get("records")
        if not isinstance(records, dict):
            raise MigrationError(ERROR_PROGRESS_INVALID, "progress records are invalid")
        return records

    def save(self) -> None:
        _atomic_write_json(
            self.path,
            self.state,
            error_code=ERROR_PROGRESS_WRITE_FAILED,
        )


class _ProgressLock:
    def __init__(self, progress_path: Path) -> None:
        self.lock_path = progress_path.with_name(
            progress_path.name + PROGRESS_LOCK_SUFFIX
        )
        self._handle: Any = None

    def __enter__(self) -> "_ProgressLock":
        try:
            descriptor = os.open(
                self.lock_path,
                os.O_CREAT | os.O_RDWR,
                0o600,
            )
        except OSError as exc:
            raise MigrationError(
                ERROR_PROGRESS_WRITE_FAILED,
                "progress lock file could not be opened",
            ) from exc
        try:
            handle = os.fdopen(descriptor, "a+")
        except Exception:
            os.close(descriptor)
            raise
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            handle.close()
            raise MigrationError(
                ERROR_PROGRESS_LOCKED,
                "another import holds the progress lock",
            ) from exc
        except OSError as exc:
            handle.close()
            raise MigrationError(
                ERROR_PROGRESS_WRITE_FAILED,
                "progress lock could not be acquired",
            ) from exc
        with contextlib.suppress(OSError):
            os.fchmod(handle.fileno(), 0o600)
        self._handle = handle
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        del exc_type, exc, tb
        handle = self._handle
        self._handle = None
        if handle is None:
            return
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()


def _load_progress_if_exists(
    path: Path,
    *,
    manifest: Mapping[str, Any],
    manifest_sha256: str,
    target: TargetConfig,
) -> _ProgressStore | None:
    if not path.exists():
        return None
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise MigrationError(
            ERROR_PROGRESS_INVALID,
            "progress file is unreadable",
        ) from exc
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise MigrationError(
            ERROR_PROGRESS_INVALID, "progress is not valid JSON"
        ) from exc
    validated = _validate_progress(
        data,
        manifest=manifest,
        manifest_sha256=manifest_sha256,
        target=target,
    )
    return _ProgressStore(path=path, state=validated)


def _ensure_progress(
    path: Path,
    *,
    manifest: Mapping[str, Any],
    manifest_sha256: str,
    target: TargetConfig,
    existing: _ProgressStore | None,
) -> _ProgressStore:
    if existing is not None:
        return existing
    state = _new_progress_state(
        manifest=manifest,
        manifest_sha256=manifest_sha256,
        target=target,
    )
    _atomic_write_json(path, state, error_code=ERROR_PROGRESS_WRITE_FAILED)
    return _ProgressStore(path=path, state=state)


@dataclass(frozen=True, slots=True)
class _RecordOutcome:
    status: str
    error_code: str | None
    operation_id: str | None


def _outcome(
    status: str, error_code: str | None, operation_id: str | None
) -> _RecordOutcome:
    return _RecordOutcome(
        status=status,
        error_code=error_code,
        operation_id=operation_id,
    )


def _mark_failed(
    store: _ProgressStore,
    entry: dict[str, Any],
    error_code: str,
) -> _RecordOutcome:
    entry["status"] = RECORD_STATUS_FAILED
    entry["last_error_code"] = error_code
    store.save()
    return _outcome(RECORD_STATUS_FAILED, error_code, entry.get("operation_id"))


def _mark_unknown(
    store: _ProgressStore,
    entry: dict[str, Any],
    error_code: str,
) -> _RecordOutcome:
    entry["status"] = RECORD_STATUS_UNCERTAIN
    entry["last_error_code"] = error_code
    store.save()
    return _outcome(RECORD_STATUS_UNKNOWN, error_code, entry.get("operation_id"))


def _mark_unfinished(
    store: _ProgressStore,
    entry: dict[str, Any],
    error_code: str,
) -> _RecordOutcome:
    entry["status"] = RECORD_STATUS_SUBMITTED
    entry["last_error_code"] = error_code
    store.save()
    return _outcome(RECORD_STATUS_UNKNOWN, error_code, entry.get("operation_id"))


def _mark_conflict(
    store: _ProgressStore,
    entry: dict[str, Any],
    error_code: str,
) -> _RecordOutcome:
    entry["status"] = RECORD_STATUS_CONFLICT
    entry["last_error_code"] = error_code
    store.save()
    return _outcome(RECORD_STATUS_CONFLICT, error_code, entry.get("operation_id"))


def _migration_metadata(
    record: Mapping[str, Any],
    *,
    source_id: str,
    manifest_sha256: str,
) -> dict[str, str]:
    return {
        "komari_migration_source_id": source_id,
        "komari_migration_table": str(record["table"]),
        "komari_migration_primary_key": canonical_json(record["primary_key"]),
        "komari_migration_content_sha256": str(record["content_sha256"]),
        "komari_migration_manifest_sha256": manifest_sha256,
    }


def _document_match_error(  # noqa: PLR0911
    document: Any,
    record: Mapping[str, Any],
    *,
    source_id: str,
    manifest_sha256: str,
    bank_id: str,
) -> str | None:
    actual_id = _read_field(document, "id")
    if actual_id is None or str(actual_id) != record["document_id"]:
        return ERROR_TARGET_DOCUMENT_ID_MISMATCH
    actual_bank = _read_field(document, "bank_id")
    if actual_bank is None or str(actual_bank) != bank_id:
        return ERROR_TARGET_DOCUMENT_BANK_MISMATCH
    original_text = _read_field(document, "original_text")
    if original_text is None:
        return ERROR_TARGET_DOCUMENT_TEXT_MISSING
    if not isinstance(original_text, str) or original_text != record["source_text"]:
        return ERROR_TARGET_DOCUMENT_TEXT_MISMATCH
    metadata = _read_field(document, "document_metadata")
    if not isinstance(metadata, Mapping):
        return ERROR_TARGET_DOCUMENT_METADATA_MISMATCH
    expected = _migration_metadata(
        record,
        source_id=source_id,
        manifest_sha256=manifest_sha256,
    )
    for key, value in expected.items():
        if metadata.get(key) != value:
            return ERROR_TARGET_DOCUMENT_METADATA_MISMATCH
    return None


async def _validate_target_version(client: Any) -> None:
    try:
        version = await client.aget_version()
    except Exception as exc:
        raise MigrationError(
            ERROR_TARGET_VERSION_CHECK_FAILED,
            "target API version could not be checked",
        ) from exc
    api_version = _read_field(version, "api_version")
    if not isinstance(api_version, str):
        raise MigrationError(
            ERROR_TARGET_VERSION_UNSUPPORTED,
            "target API version is missing",
        )
    parsed = _parse_api_version(api_version)
    if parsed is None or parsed < MINIMUM_TARGET_API_VERSION:
        raise MigrationError(
            ERROR_TARGET_VERSION_UNSUPPORTED,
            "target API version does not support the 1.1.0 operation-id contract",
        )


async def _require_bank(
    client: Any,
    target: TargetConfig,
    request_timeout: float,
) -> None:
    try:
        bank_config = await client.banks.get_bank_config(
            target.bank_id,
            _request_timeout=request_timeout,
        )
    except Exception as exc:
        if _api_status_code(exc) == 404:
            raise MigrationError(
                ERROR_TARGET_BANK_MISSING,
                "target Hindsight bank does not exist",
            ) from exc
        raise MigrationError(
            ERROR_TARGET_BANK_CHECK_FAILED,
            "target Hindsight bank could not be verified",
        ) from exc
    returned_bank_id = _read_field(bank_config, "bank_id")
    if not isinstance(returned_bank_id, str) or returned_bank_id != target.bank_id:
        raise MigrationError(
            ERROR_TARGET_BANK_CHECK_FAILED,
            "target Hindsight bank identity does not match",
        )


async def _read_document(
    client: Any,
    target: TargetConfig,
    document_id: str,
    request_timeout: float,
) -> tuple[Any | None, str | None]:
    try:
        document = await client.documents.get_document(
            target.bank_id,
            document_id,
            _request_timeout=request_timeout,
        )
    except Exception as exc:
        if _api_status_code(exc) == 404:
            return None, None
        return None, ERROR_TARGET_DOCUMENT_READ_FAILED
    return document, None


async def _read_operation_status(
    client: Any,
    target: TargetConfig,
    operation_id: str,
    request_timeout: float,
) -> str:
    try:
        response = await client.operations.get_operation_status(
            target.bank_id,
            operation_id,
            _request_timeout=request_timeout,
        )
    except Exception as exc:
        if _api_status_code(exc) == 404:
            return "not_found"
        return "unknown"
    returned_operation_id = _read_field(response, "operation_id")
    if (
        not isinstance(returned_operation_id, str)
        or returned_operation_id != operation_id
    ):
        return "unknown"
    status = _read_field(response, "status", "unknown")
    if not isinstance(status, str):
        return "unknown"
    return status.lower()


async def _poll_operation(
    client: Any,
    target: TargetConfig,
    operation_id: str,
    *,
    request_timeout: float,
    deadline: float,
    interval: float,
    monotonic: Clock,
    sleep: Sleeper,
) -> str:
    while True:
        remaining = deadline - monotonic()
        if remaining <= 0:
            return "unknown"
        status_timeout = min(request_timeout, remaining)
        try:
            async with asyncio.timeout(status_timeout):
                status = await _read_operation_status(
                    client,
                    target,
                    operation_id,
                    status_timeout,
                )
        except TimeoutError:
            return "unknown"
        now = monotonic()
        if now >= deadline:
            return "unknown"
        if status in {"completed", "failed", "cancelled", "not_found"}:
            return status
        if now + interval > deadline:
            return "unknown"
        await sleep(interval)


def _verify_receipt(
    response: Any,
    *,
    bank_id: str,
    operation_id: str,
) -> None:
    if _read_field(response, "success") is not True:
        raise MigrationError(ERROR_RECEIPT_INVALID, "retain receipt is not successful")
    if str(_read_field(response, "bank_id")) != bank_id:
        raise MigrationError(ERROR_RECEIPT_INVALID, "retain receipt bank is incorrect")
    if _read_field(response, "var_async") is not True:
        raise MigrationError(
            ERROR_RECEIPT_INVALID, "retain receipt is not asynchronous"
        )
    items_count = _read_field(response, "items_count")
    if (
        not isinstance(items_count, int)
        or isinstance(items_count, bool)
        or items_count != 1
    ):
        raise MigrationError(
            ERROR_RECEIPT_INVALID, "retain receipt item count is incorrect"
        )
    if str(_read_field(response, "operation_id")) != operation_id:
        raise MigrationError(
            ERROR_RECEIPT_INVALID,
            "retain receipt operation id is incorrect",
        )


async def _submit_record(
    *,
    client: Any,
    target: TargetConfig,
    manifest: Mapping[str, Any],
    manifest_sha256: str,
    record: Mapping[str, Any],
    entry: dict[str, Any],
    store: _ProgressStore,
    request_timeout: float,
) -> _RecordOutcome | None:
    await _require_bank(client, target, request_timeout)
    operation_id = entry.get("operation_id")
    if not isinstance(operation_id, str):
        raise MigrationError(
            ERROR_PROGRESS_INVALID,
            "progress is missing a frozen operation id",
        )
    entry["attempts"] = int(entry.get("attempts", 0)) + 1
    entry["status"] = RECORD_STATUS_PENDING_SUBMIT
    entry["last_error_code"] = None
    store.save()

    try:
        response = await client.aretain_batch(
            bank_id=target.bank_id,
            items=[
                {
                    "content": record["source_text"],
                    "metadata": _migration_metadata(
                        record,
                        source_id=str(manifest["source_id"]),
                        manifest_sha256=manifest_sha256,
                    ),
                }
            ],
            document_id=record["document_id"],
            retain_async=True,
            operation_id=operation_id,
        )
    except Exception as exc:
        status_code = _api_status_code(exc)
        if (
            status_code is not None
            and 400 <= status_code < 500
            and status_code not in {408, 409, 425, 429}
        ):
            return _mark_failed(store, entry, ERROR_RETAIN_REJECTED)
        return _mark_unknown(store, entry, ERROR_RETAIN_OUTCOME_UNKNOWN)

    try:
        _verify_receipt(response, bank_id=target.bank_id, operation_id=operation_id)
    except MigrationError:
        return _mark_unknown(store, entry, ERROR_RECEIPT_INVALID)

    entry["status"] = RECORD_STATUS_SUBMITTED
    entry["last_error_code"] = None
    store.save()
    return None


async def _complete_and_verify(
    *,
    client: Any,
    target: TargetConfig,
    manifest: Mapping[str, Any],
    manifest_sha256: str,
    record: Mapping[str, Any],
    entry: dict[str, Any],
    store: _ProgressStore,
    request_timeout: float,
) -> _RecordOutcome:
    document, read_error = await _read_document(
        client,
        target,
        str(record["document_id"]),
        request_timeout,
    )
    if read_error is not None:
        return _mark_unknown(store, entry, read_error)
    if document is None:
        return _mark_failed(store, entry, ERROR_TARGET_DOCUMENT_MISSING)
    mismatch = _document_match_error(
        document,
        record,
        source_id=str(manifest["source_id"]),
        manifest_sha256=manifest_sha256,
        bank_id=target.bank_id,
    )
    if mismatch is not None:
        return _mark_failed(store, entry, mismatch)
    entry["status"] = RECORD_STATUS_COMPLETED
    entry["last_error_code"] = None
    store.save()
    return _outcome(
        RECORD_STATUS_COMPLETED,
        None,
        entry.get("operation_id"),
    )


async def _wait_for_terminal(  # noqa: PLR0911
    *,
    client: Any,
    target: TargetConfig,
    manifest: Mapping[str, Any],
    manifest_sha256: str,
    record: Mapping[str, Any],
    entry: dict[str, Any],
    store: _ProgressStore,
    request_timeout: float,
    operation_timeout: float,
    poll_interval: float,
    monotonic: Clock,
    sleep: Sleeper,
    resubmitted: bool,
) -> _RecordOutcome:
    deadline = monotonic() + operation_timeout
    timeout = asyncio.timeout(operation_timeout)
    try:
        async with timeout:
            while True:
                operation_id = entry.get("operation_id")
                if not isinstance(operation_id, str):
                    return _mark_failed(store, entry, ERROR_PROGRESS_INVALID)
                final_status = await _poll_operation(
                    client,
                    target,
                    operation_id,
                    request_timeout=request_timeout,
                    deadline=deadline,
                    interval=poll_interval,
                    monotonic=monotonic,
                    sleep=sleep,
                )
                if final_status == "completed":
                    return await _complete_and_verify(
                        client=client,
                        target=target,
                        manifest=manifest,
                        manifest_sha256=manifest_sha256,
                        record=record,
                        entry=entry,
                        store=store,
                        request_timeout=request_timeout,
                    )
                if final_status in {"failed", "cancelled"}:
                    return _mark_failed(store, entry, ERROR_OPERATION_FAILED)
                if final_status == "not_found":
                    if resubmitted:
                        return _mark_unfinished(store, entry, ERROR_OPERATION_UNKNOWN)
                    document, read_error = await _read_document(
                        client,
                        target,
                        str(record["document_id"]),
                        request_timeout,
                    )
                    if read_error is not None:
                        return _mark_unknown(store, entry, read_error)
                    if document is not None:
                        mismatch = _document_match_error(
                            document,
                            record,
                            source_id=str(manifest["source_id"]),
                            manifest_sha256=manifest_sha256,
                            bank_id=target.bank_id,
                        )
                        if mismatch is not None:
                            return _mark_conflict(store, entry, mismatch)
                    submit_outcome = await _submit_record(
                        client=client,
                        target=target,
                        manifest=manifest,
                        manifest_sha256=manifest_sha256,
                        record=record,
                        entry=entry,
                        store=store,
                        request_timeout=request_timeout,
                    )
                    if submit_outcome is not None:
                        return submit_outcome
                    resubmitted = True
                    continue
                return _mark_unfinished(store, entry, ERROR_OPERATION_UNKNOWN)
    except TimeoutError:
        if not timeout.expired():
            raise
        return _mark_unfinished(store, entry, ERROR_OPERATION_UNKNOWN)


async def _submit_and_wait(
    *,
    client: Any,
    target: TargetConfig,
    manifest: Mapping[str, Any],
    manifest_sha256: str,
    record: Mapping[str, Any],
    entry: dict[str, Any],
    store: _ProgressStore,
    request_timeout: float,
    operation_timeout: float,
    poll_interval: float,
    monotonic: Clock,
    sleep: Sleeper,
    resubmitted: bool,
) -> _RecordOutcome:
    submit_outcome = await _submit_record(
        client=client,
        target=target,
        manifest=manifest,
        manifest_sha256=manifest_sha256,
        record=record,
        entry=entry,
        store=store,
        request_timeout=request_timeout,
    )
    if submit_outcome is not None:
        return submit_outcome
    return await _wait_for_terminal(
        client=client,
        target=target,
        manifest=manifest,
        manifest_sha256=manifest_sha256,
        record=record,
        entry=entry,
        store=store,
        request_timeout=request_timeout,
        operation_timeout=operation_timeout,
        poll_interval=poll_interval,
        monotonic=monotonic,
        sleep=sleep,
        resubmitted=resubmitted,
    )


async def _revalidate_completed(
    *,
    client: Any,
    target: TargetConfig,
    manifest: Mapping[str, Any],
    manifest_sha256: str,
    record: Mapping[str, Any],
    entry: dict[str, Any],
    store: _ProgressStore,
    request_timeout: float,
) -> _RecordOutcome:
    operation_id = entry.get("operation_id")
    if not isinstance(operation_id, str):
        return _mark_failed(store, entry, ERROR_PROGRESS_INVALID)
    status = await _read_operation_status(client, target, operation_id, request_timeout)
    if status in {"failed", "cancelled"}:
        return _mark_failed(store, entry, ERROR_OPERATION_FAILED)
    if status != "completed":
        return _mark_unknown(store, entry, ERROR_OPERATION_UNKNOWN)
    return await _complete_and_verify(
        client=client,
        target=target,
        manifest=manifest,
        manifest_sha256=manifest_sha256,
        record=record,
        entry=entry,
        store=store,
        request_timeout=request_timeout,
    )


async def _process_record(  # noqa: PLR0911
    *,
    client: Any,
    target: TargetConfig,
    manifest: Mapping[str, Any],
    manifest_sha256: str,
    record: Mapping[str, Any],
    store: _ProgressStore,
    request_timeout: float,
    operation_timeout: float,
    poll_interval: float,
    monotonic: Clock,
    sleep: Sleeper,
) -> _RecordOutcome:
    document_id = str(record["document_id"])
    entry = store.records.get(document_id)

    if entry is None:
        document, read_error = await _read_document(
            client,
            target,
            document_id,
            request_timeout,
        )
        if read_error is not None:
            return _outcome(RECORD_STATUS_UNKNOWN, read_error, None)
        if document is not None:
            conflict_entry = _new_progress_entry(
                record,
                manifest=manifest,
                manifest_sha256=manifest_sha256,
                operation_id=None,
                status=RECORD_STATUS_CONFLICT,
            )
            conflict_entry["last_error_code"] = ERROR_TARGET_DOCUMENT_CONFLICT
            store.records[document_id] = conflict_entry
            store.save()
            return _outcome(
                RECORD_STATUS_CONFLICT,
                ERROR_TARGET_DOCUMENT_CONFLICT,
                None,
            )
        new_entry = _new_progress_entry(
            record,
            manifest=manifest,
            manifest_sha256=manifest_sha256,
            operation_id=str(uuid.uuid4()),
            status=RECORD_STATUS_PENDING_SUBMIT,
        )
        store.records[document_id] = new_entry
        store.save()
        return await _submit_and_wait(
            client=client,
            target=target,
            manifest=manifest,
            manifest_sha256=manifest_sha256,
            record=record,
            entry=new_entry,
            store=store,
            request_timeout=request_timeout,
            operation_timeout=operation_timeout,
            poll_interval=poll_interval,
            monotonic=monotonic,
            sleep=sleep,
            resubmitted=False,
        )

    if entry.get("status") == RECORD_STATUS_CONFLICT:
        return _outcome(
            RECORD_STATUS_CONFLICT,
            entry.get("last_error_code") or ERROR_TARGET_DOCUMENT_CONFLICT,
            entry.get("operation_id"),
        )
    if entry.get("status") == RECORD_STATUS_FAILED:
        return _outcome(
            RECORD_STATUS_FAILED,
            entry.get("last_error_code") or ERROR_OPERATION_FAILED,
            entry.get("operation_id"),
        )
    if entry.get("status") == RECORD_STATUS_COMPLETED:
        return await _revalidate_completed(
            client=client,
            target=target,
            manifest=manifest,
            manifest_sha256=manifest_sha256,
            record=record,
            entry=entry,
            store=store,
            request_timeout=request_timeout,
        )

    document, read_error = await _read_document(
        client,
        target,
        document_id,
        request_timeout,
    )
    if read_error is not None:
        return _mark_unknown(store, entry, read_error)
    if document is not None:
        mismatch = _document_match_error(
            document,
            record,
            source_id=str(manifest["source_id"]),
            manifest_sha256=manifest_sha256,
            bank_id=target.bank_id,
        )
        if mismatch is not None:
            return _mark_conflict(store, entry, mismatch)

    operation_id = entry.get("operation_id")
    if not isinstance(operation_id, str):
        return _mark_failed(store, entry, ERROR_PROGRESS_INVALID)
    operation_status = await _read_operation_status(
        client,
        target,
        operation_id,
        request_timeout,
    )
    if operation_status in {"failed", "cancelled"}:
        return _mark_failed(store, entry, ERROR_OPERATION_FAILED)
    if operation_status == "completed":
        return await _complete_and_verify(
            client=client,
            target=target,
            manifest=manifest,
            manifest_sha256=manifest_sha256,
            record=record,
            entry=entry,
            store=store,
            request_timeout=request_timeout,
        )
    if operation_status == "not_found":
        return await _submit_and_wait(
            client=client,
            target=target,
            manifest=manifest,
            manifest_sha256=manifest_sha256,
            record=record,
            entry=entry,
            store=store,
            request_timeout=request_timeout,
            operation_timeout=operation_timeout,
            poll_interval=poll_interval,
            monotonic=monotonic,
            sleep=sleep,
            resubmitted=True,
        )
    return await _wait_for_terminal(
        client=client,
        target=target,
        manifest=manifest,
        manifest_sha256=manifest_sha256,
        record=record,
        entry=entry,
        store=store,
        request_timeout=request_timeout,
        operation_timeout=operation_timeout,
        poll_interval=poll_interval,
        monotonic=monotonic,
        sleep=sleep,
        resubmitted=False,
    )


def _outcome_from_error(error_code: str, operation_id: str | None) -> _RecordOutcome:
    if error_code == ERROR_TARGET_DOCUMENT_CONFLICT:
        return _outcome(RECORD_STATUS_CONFLICT, error_code, operation_id)
    if error_code in {
        ERROR_OPERATION_UNKNOWN,
        ERROR_TARGET_DOCUMENT_READ_FAILED,
        ERROR_RETAIN_OUTCOME_UNKNOWN,
        ERROR_RECEIPT_INVALID,
    }:
        return _outcome(RECORD_STATUS_UNKNOWN, error_code, operation_id)
    return _outcome(RECORD_STATUS_FAILED, error_code, operation_id)


def _build_report(
    *,
    manifest: Mapping[str, Any] | None,
    manifest_sha256: str | None,
    target: TargetConfig | None,
    outcomes: Mapping[str, _RecordOutcome],
    global_error_code: str | None,
) -> dict[str, Any]:
    records: list[dict[str, Any]] = []
    counts: dict[str, int] = {
        RECORD_STATUS_COMPLETED: 0,
        RECORD_STATUS_FAILED: 0,
        RECORD_STATUS_UNKNOWN: 0,
        RECORD_STATUS_CONFLICT: 0,
    }
    if manifest is not None:
        for record in manifest["records"]:
            document_id = str(record["document_id"])
            outcome = outcomes.get(
                document_id,
                _outcome(RECORD_STATUS_FAILED, global_error_code, None),
            )
            counts[outcome.status] = counts.get(outcome.status, 0) + 1
            records.append(
                {
                    "table": record["table"],
                    "primary_key": record["primary_key"],
                    "document_id": document_id,
                    "status": outcome.status,
                    "operation_id": outcome.operation_id,
                    "error_code": outcome.error_code,
                }
            )
    success = (
        global_error_code is None
        and manifest is not None
        and all(entry["status"] == RECORD_STATUS_COMPLETED for entry in records)
    )
    return {
        "kind": REPORT_KIND,
        "schema_version": REPORT_SCHEMA_VERSION,
        "outcome": "success" if success else "failure",
        "global_error_code": global_error_code,
        "source_id": manifest.get("source_id") if manifest is not None else None,
        "manifest_sha256": manifest_sha256,
        "target": {
            "base_url": target.base_url if target is not None else None,
            "bank_id": target.bank_id if target is not None else None,
        },
        "counts": counts,
        "records": records,
        "generated_at": _now_iso(),
    }


def _write_global_failure_report(
    report_path: Path,
    *,
    manifest: Mapping[str, Any] | None,
    manifest_sha256: str | None,
    target: TargetConfig | None,
    outcomes: Mapping[str, _RecordOutcome],
    error_code: str,
) -> None:
    try:
        payload = _build_report(
            manifest=manifest,
            manifest_sha256=manifest_sha256,
            target=target,
            outcomes=outcomes,
            global_error_code=error_code,
        )
        _atomic_write_json(
            report_path,
            payload,
            error_code=ERROR_REPORT_WRITE_FAILED,
        )
    except MigrationError as report_error:
        _write_error(report_error)


def _try_write_failure_report(
    report_path: Path,
    *,
    manifest: Mapping[str, Any] | None,
    manifest_sha256: str | None,
    target: TargetConfig | None,
    error_code: str,
) -> None:
    _write_global_failure_report(
        report_path,
        manifest=manifest,
        manifest_sha256=manifest_sha256,
        target=target,
        outcomes={},
        error_code=error_code,
    )


def _validate_import_arguments(args: argparse.Namespace) -> None:
    _positive_finite(float(args.request_timeout), "request timeout")
    _positive_finite(float(args.operation_timeout), "operation timeout")
    _positive_finite(float(args.poll_interval), "poll interval")
    manifest_path = Path(args.manifest).resolve()
    progress_path = Path(args.progress).resolve()
    report_path = Path(args.report).resolve()
    if progress_path == manifest_path:
        raise MigrationError(
            ERROR_USAGE,
            "progress path must differ from the manifest path",
        )
    if report_path in (progress_path, manifest_path):
        raise MigrationError(
            ERROR_USAGE,
            "report path must differ from manifest and progress paths",
        )
    if not report_path.parent.is_dir():
        raise MigrationError(
            ERROR_REPORT_WRITE_FAILED,
            "report parent directory does not exist",
        )
    if report_path.exists() and report_path.is_dir():
        raise MigrationError(
            ERROR_REPORT_WRITE_FAILED,
            "report path is a directory",
        )


async def _run_import_with_client(
    *,
    args: argparse.Namespace,
    boundaries: MigrationBoundaries,
    manifest: Mapping[str, Any],
    manifest_sha256: str,
    target: TargetConfig,
    existing_progress: _ProgressStore | None,
    progress_path: Path,
    report_path: Path,
) -> int:
    request_timeout = float(args.request_timeout)
    client = boundaries.create_client(target, request_timeout)
    outcomes: dict[str, _RecordOutcome] = {}
    try:
        try:
            await _validate_target_version(client)
            await _require_bank(client, target, request_timeout)
            store = _ensure_progress(
                progress_path,
                manifest=manifest,
                manifest_sha256=manifest_sha256,
                target=target,
                existing=existing_progress,
            )
            for record in manifest["records"]:
                document_id = str(record["document_id"])
                try:
                    outcome = await _process_record(
                        client=client,
                        target=target,
                        manifest=manifest,
                        manifest_sha256=manifest_sha256,
                        record=record,
                        store=store,
                        request_timeout=request_timeout,
                        operation_timeout=float(args.operation_timeout),
                        poll_interval=float(args.poll_interval),
                        monotonic=boundaries.monotonic,
                        sleep=boundaries.sleep,
                    )
                except MigrationError as exc:
                    if exc.code == ERROR_PROGRESS_WRITE_FAILED:
                        raise
                    outcome = _outcome_from_error(
                        exc.code,
                        store.records.get(document_id, {}).get("operation_id"),
                    )
                except Exception:
                    outcome = _outcome(
                        RECORD_STATUS_UNKNOWN,
                        ERROR_INTERNAL,
                        None,
                    )
                outcomes[document_id] = outcome

            report = _build_report(
                manifest=manifest,
                manifest_sha256=manifest_sha256,
                target=target,
                outcomes=outcomes,
                global_error_code=None,
            )
            _atomic_write_json(
                report_path,
                report,
                error_code=ERROR_REPORT_WRITE_FAILED,
            )
            return 0 if report["outcome"] == "success" else 1
        except MigrationError as exc:
            if exc.code == ERROR_USAGE:
                raise
            _write_global_failure_report(
                report_path,
                manifest=manifest,
                manifest_sha256=manifest_sha256,
                target=target,
                outcomes=outcomes,
                error_code=exc.code,
            )
            _write_error(exc)
            return 1
        except Exception as exc:
            _write_global_failure_report(
                report_path,
                manifest=manifest,
                manifest_sha256=manifest_sha256,
                target=target,
                outcomes=outcomes,
                error_code=ERROR_INTERNAL,
            )
            _write_error(
                MigrationError(
                    ERROR_INTERNAL,
                    f"unexpected {type(exc).__name__} failure",
                )
            )
            return 1
    finally:
        with contextlib.suppress(Exception):
            await client.aclose()


async def _run_import(args: argparse.Namespace, boundaries: MigrationBoundaries) -> int:
    manifest_path = Path(args.manifest).resolve()
    progress_path = Path(args.progress).resolve()
    report_path = Path(args.report).resolve()
    manifest: dict[str, Any] | None = None
    manifest_sha256: str | None = None
    target: TargetConfig | None = None
    try:
        _validate_import_arguments(args)
        manifest, manifest_sha256 = _load_manifest(manifest_path)
        target = _load_target_config()
        with _ProgressLock(progress_path):
            existing_progress = _load_progress_if_exists(
                progress_path,
                manifest=manifest,
                manifest_sha256=manifest_sha256,
                target=target,
            )
            return await _run_import_with_client(
                args=args,
                boundaries=boundaries,
                manifest=manifest,
                manifest_sha256=manifest_sha256,
                target=target,
                existing_progress=existing_progress,
                progress_path=progress_path,
                report_path=report_path,
            )
    except MigrationError as exc:
        if exc.code in {ERROR_PROGRESS_LOCKED, ERROR_USAGE}:
            raise
        _try_write_failure_report(
            report_path,
            manifest=manifest,
            manifest_sha256=manifest_sha256,
            target=target,
            error_code=exc.code,
        )
        _write_error(exc)
        return 1
    except Exception as exc:
        _try_write_failure_report(
            report_path,
            manifest=manifest,
            manifest_sha256=manifest_sha256,
            target=target,
            error_code=ERROR_INTERNAL,
        )
        _write_error(
            MigrationError(
                ERROR_INTERNAL,
                f"unexpected {type(exc).__name__} failure",
            )
        )
        return 1


async def run_cli(
    argv: Sequence[str] | None = None,
    *,
    boundaries: MigrationBoundaries | None = None,
) -> int:
    args = _build_parser().parse_args(argv)
    active_boundaries = boundaries or _default_boundaries()
    try:
        if args.phase == "export":
            return await _run_export(args, active_boundaries)
        if args.phase == "import":
            return await _run_import(args, active_boundaries)
    except MigrationError as exc:
        _write_error(exc)
        return 1
    except Exception as exc:
        _write_error(
            MigrationError(
                ERROR_INTERNAL,
                f"unexpected {type(exc).__name__} failure",
            )
        )
        return 1
    raise MigrationError(ERROR_USAGE, "unknown phase")


def _write_error(error: MigrationError) -> None:
    sys.stderr.write(f"error: {error.code}: {error.message}\n")
    if error.code == ERROR_EXPORT_UNSUPPORTED_SOURCE:
        inventory = error.details.get("unsupported_sources", [])
        sys.stderr.write(f"unsupported_source_inventory: {canonical_json(inventory)}\n")


def main(argv: Sequence[str] | None = None) -> NoReturn:
    raise SystemExit(asyncio.run(run_cli(argv)))


if __name__ == "__main__":
    main()
