"""Single authority for the roulette runtime / observation reason codes.

The runtime publishes a *closed* set of lifecycle reasons and the observability
projection renders a *closed* set of fault reasons.  Both sets are declared
once here so a foreign string can never drift into an operator-facing
projection: the runtime imports :data:`RUNTIME_REASON_CODES`, the projection
imports :data:`OBSERVATION_REASON_CODES` (their union), and every unknown value
collapses to a fixed member instead of being copied through.

The module has no imports of its own on purpose; it is a leaf contract shared by
``runtime.py`` and ``observability.py`` without either depending on the other.
"""

from __future__ import annotations

#: The three lifecycle statuses the runtime can publish.
RUNTIME_STATUS_VALUES: frozenset[str] = frozenset({"ready", "disabled", "failed"})

#: Legal runtime lifecycle / authority reason codes (TSK-279 contract §9.1).
RUNTIME_REASON_CODES: frozenset[str] = frozenset(
    {
        "config_unavailable",
        "admission_unavailable",
        "storage_unavailable",
        "recovery_failed",
        "plugin_disabled",
        "policy_restricted",
        "policy_admitted",
        "not_ready",
    }
)

#: Legal observation fault reason codes.
FAULT_REASON_CODES: frozenset[str] = frozenset(
    {
        "storage_unavailable",
        "recovery_failed",
        "scan_failed",
        "cleanup_failed",
        "pending_unavailable",
        "config_unavailable",
        "admission_unavailable",
        "runtime_failed",
        "unexpected_fault",
    }
)

#: The one shared closed set both the runtime reasons and the fault reasons map
#: into; a second drifting definition is exactly what TSK-279 F4 forbids.
OBSERVATION_REASON_CODES: frozenset[str] = RUNTIME_REASON_CODES | FAULT_REASON_CODES

#: Fixed member an unrecognized runtime reason is normalized to.
UNKNOWN_RUNTIME_REASON = "not_ready"

#: Fixed member an unrecognized fault is normalized to.
UNEXPECTED_FAULT_REASON = "unexpected_fault"

#: Fixed member a failed pending read is aggregated under.  It must never fall
#: back to the generic ``RuntimeError -> recovery_failed`` mapping.
PENDING_UNAVAILABLE = "pending_unavailable"

__all__ = [
    "FAULT_REASON_CODES",
    "OBSERVATION_REASON_CODES",
    "PENDING_UNAVAILABLE",
    "RUNTIME_REASON_CODES",
    "RUNTIME_STATUS_VALUES",
    "UNEXPECTED_FAULT_REASON",
    "UNKNOWN_RUNTIME_REASON",
]
