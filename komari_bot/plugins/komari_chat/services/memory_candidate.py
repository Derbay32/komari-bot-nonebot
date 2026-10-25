"""Pure frozen chat memory-candidate document construction.

The builder only reads the already-frozen ``DecisionRequest``.  It never
queries history, logs, Redis or the platform, and it never promotes reference
evidence into an independent candidate.
"""

from __future__ import annotations

import json
from datetime import UTC
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from datetime import datetime

    from komari_bot.decision.jev import DecisionMessage, DecisionRequest
    from komari_bot.plugins.komari_memory import HindsightRetainRequest

CANDIDATE_SOURCE_TYPE = "chat_candidate"
CANDIDATE_SCHEMA_VERSION = "1"
_CANDIDATE_SOURCE_PREFIX = "candidate:"
_EVIDENCE_SCOPE = "initial_known_references"


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _timestamp_text(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _message_fields(message: DecisionMessage) -> dict[str, object]:
    fields: dict[str, object] = {
        "sender_id": message.sender_id,
        "message_id": message.message_id,
        "text": message.text,
        "timestamp": _timestamp_text(message.timestamp),
        "display_name": message.display_name,
        "is_bot": message.is_bot,
        "mention_ids": list(message.mention_ids),
        "reference_ids": list(message.reference_ids),
        "image_present": message.image_present,
    }
    if message.image_description is not None:
        fields["image_description"] = message.image_description
    return fields


def _candidate_fields(request: DecisionRequest) -> dict[str, object]:
    target = request.target
    return {
        "protocol": request.event.protocol,
        "account_id": request.event.account_id,
        "group_id": target.group_id,
        **_message_fields(target),
    }


def _evidence_node(
    request: DecisionRequest,
    message: DecisionMessage,
) -> dict[str, object]:
    return {
        "protocol": request.event.protocol,
        "account_id": request.event.account_id,
        "group_id": message.group_id,
        **_message_fields(message),
    }


def _evidence(
    request: DecisionRequest,
) -> tuple[list[dict[str, object]], list[str], list[dict[str, str]]]:
    """Walk only explicit reference edges inside the frozen request pool."""
    target = request.target
    lookup: dict[str, DecisionMessage] = {}
    for message in request.known_references:
        lookup.setdefault(message.message_id, message)

    nodes: list[dict[str, object]] = []
    unresolved: list[str] = []
    cycles: list[dict[str, str]] = []
    visited: set[str] = set()
    path: set[str] = {target.message_id}

    def _walk(message_id: str, parent_id: str) -> None:
        if not message_id:
            return
        if message_id in path:
            cycles.append(
                {
                    "from_message_id": parent_id,
                    "to_message_id": message_id,
                }
            )
            return
        if message_id in visited:
            return
        message = lookup.get(message_id)
        if message is None:
            if message_id not in unresolved:
                unresolved.append(message_id)
            return
        if message.group_id != target.group_id:
            return
        if message.timestamp > request.received_at:
            return
        visited.add(message_id)
        path.add(message_id)
        nodes.append(_evidence_node(request, message))
        for reference_id in message.reference_ids:
            _walk(reference_id, message.message_id)
        path.remove(message_id)

    for reference_id in target.reference_ids:
        _walk(reference_id, target.message_id)

    return nodes, unresolved, cycles


def build_candidate_document(request: DecisionRequest) -> dict[str, Any]:
    """Serialize the frozen candidate and its initially known evidence."""
    evidence_nodes, unresolved, cycles = _evidence(request)
    return {
        "schema_version": CANDIDATE_SCHEMA_VERSION,
        "source_type": CANDIDATE_SOURCE_TYPE,
        "candidate": _candidate_fields(request),
        "candidate_evidence": {
            "scope": _EVIDENCE_SCOPE,
            "complete": False,
            "nodes": evidence_nodes,
            "unresolved_reference_ids": unresolved,
            "cycle_edges": cycles,
        },
    }


def candidate_source_id(request: DecisionRequest) -> str:
    identity = _canonical_json(
        {
            "protocol": request.event.protocol,
            "account_id": request.event.account_id,
            "group_id": request.event.group_id,
            "message_id": request.event.message_id,
        }
    )
    return f"{_CANDIDATE_SOURCE_PREFIX}{identity}"


def build_candidate_retain_request(
    *,
    request: DecisionRequest,
    target_fingerprint: str,
) -> HindsightRetainRequest:
    """Freeze one shared retain request for the target and its evidence."""
    from komari_bot.plugins.komari_memory import HindsightRetainRequest

    return HindsightRetainRequest.create(
        target_fingerprint=target_fingerprint,
        source_id=candidate_source_id(request),
        content=_canonical_json(build_candidate_document(request)),
        timestamp=request.target.timestamp,
        metadata={
            "schema_version": CANDIDATE_SCHEMA_VERSION,
            "source_type": CANDIDATE_SOURCE_TYPE,
        },
    )


__all__ = [
    "CANDIDATE_SCHEMA_VERSION",
    "CANDIDATE_SOURCE_TYPE",
    "build_candidate_document",
    "build_candidate_retain_request",
    "candidate_source_id",
]
