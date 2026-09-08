"""QQ group admission contract and effect recheck seam.

The QQ adapter has a separate identity space from OneBot.  This module keeps
that distinction explicit and exposes only callback seams to the binding and
ban plugins; it never imports either plugin.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Literal

from nonebot.adapters.qq import Bot as QQBot
from nonebot.adapters.qq.event import GroupAtMessageCreateEvent

from . import runtime as _runtime_module
from .contracts import AdmissionIntent, AdmissionQualification

if TYPE_CHECKING:
    from nonebot.adapters import Bot, Event

type QQScope = Literal["business", "binding_challenge", "binding"]
type QQGroupResolver = Callable[[str, str], Awaitable[int | None]]
type QQInitialBindClaimer = Callable[[QQInitialBindRequest], Awaitable[QQBindClaim | None]]
type QQBindingSessionResolver = Callable[
    [str, str, str], Awaitable["QQVerifiedBindingSession | None"]
]
type QQBanChecker = Callable[[int, str], Awaitable[bool]]
type QQMemberResolver = Callable[[str, str, str], Awaitable[int | None]]
type QQClaimValidator = Callable[[QQAdmissionToken], Awaitable[bool]]

QQ_ADMISSION_STATE_KEY = "komari_bot.group_admission.qq_admission"


@dataclass(frozen=True, slots=True, kw_only=True)
class QQBindClaim:
    session_code: str
    app_id: str
    group_openid: str
    member_openid: str
    qq_message_id: str
    connection_generation: int
    expires_at: datetime
    is_new: bool


@dataclass(frozen=True, slots=True, kw_only=True)
class QQInitialBindRequest:
    app_id: str
    group_openid: str
    member_openid: str
    qq_message_id: str
    command: str


@dataclass(frozen=True, slots=True, kw_only=True)
class QQVerifiedBindingSession:
    session_code: str
    app_id: str
    group_openid: str
    member_openid: str
    qq_message_id: str
    group_id: int
    member_qq: int
    connection_generation: int
    expires_at: datetime


@dataclass(frozen=True, slots=True, kw_only=True)
class QQAdmissionToken:
    scope: QQScope
    app_id: str
    group_openid: str
    member_openid: str
    qq_message_id: str
    group_id: int | None
    member_qq: int | None
    effective_policy_revision: int | None
    connection_generation: int
    claim: QQBindClaim | None
    verified_session: QQVerifiedBindingSession | None


@dataclass(frozen=True, slots=True, kw_only=True)
class QQEffectDecision:
    allowed: bool
    effect: QQScope
    reason_code: str
    effective_policy_revision: int | None


@dataclass(slots=True)
class _QQCallbacks:
    group_resolver: QQGroupResolver | None = None
    initial_bind_claimer: QQInitialBindClaimer | None = None
    binding_session_resolver: QQBindingSessionResolver | None = None
    ban_checker: QQBanChecker | None = None
    member_resolver: QQMemberResolver | None = None
    claim_validator: QQClaimValidator | None = None


_callbacks = _QQCallbacks()


def register_qq_group_resolver(
    resolver: QQGroupResolver | None,
    *,
    member_resolver: QQMemberResolver | None = None,
) -> None:
    """Install the canonical app/group and optional member resolvers."""
    _callbacks.group_resolver = resolver
    _callbacks.member_resolver = member_resolver if resolver is not None else None


def register_qq_initial_bind_claimer(
    claimer: QQInitialBindClaimer | None,
    *,
    validator: QQClaimValidator | None = None,
) -> None:
    """Install the one-shot initial binding challenge claimer."""
    _callbacks.initial_bind_claimer = claimer
    _callbacks.claim_validator = validator if claimer is not None else None


def register_qq_binding_session_resolver(
    resolver: QQBindingSessionResolver | None,
) -> None:
    """Install the evidence-backed temporary binding session resolver."""
    _callbacks.binding_session_resolver = resolver


def register_qq_ban_checker(checker: QQBanChecker | None) -> None:
    """Install a checker that accepts only evidence-proven numeric QQ IDs."""
    _callbacks.ban_checker = checker


def _now() -> datetime:
    return datetime.now(UTC)


def _positive_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and value > 0:
        return value
    if isinstance(value, str) and value.strip().isdigit() and int(value.strip()) > 0:
        return int(value.strip())
    return None


def _event_values(event: GroupAtMessageCreateEvent) -> tuple[str, str, str, str] | None:
    group_openid = getattr(event, "group_openid", None)
    author = getattr(event, "author", None)
    member_openid = getattr(author, "member_openid", None)
    message_id = getattr(event, "id", None)
    command = getattr(event, "content", None)
    if not (
        isinstance(group_openid, str)
        and isinstance(member_openid, str)
        and isinstance(message_id, str)
        and isinstance(command, str)
        and group_openid.strip()
        and member_openid.strip()
        and message_id.strip()
        and command.strip()
    ):
        return None
    return (
        group_openid.strip(),
        member_openid.strip(),
        message_id.strip(),
        command.strip(),
    )


def _qq_app_id(bot: object) -> str | None:
    if not isinstance(bot, QQBot):
        return None
    info = getattr(bot, "bot_info", None)
    app_id = getattr(info, "id", None)
    if not isinstance(app_id, str) or not app_id.strip():
        return None
    return app_id.strip()


async def _current_group(app_id: str, group_openid: str) -> tuple[int | None, bool]:
    """Return (group id, resolver succeeded).  ``None`` is a real miss."""
    resolver = _callbacks.group_resolver
    if resolver is None:
        return None, False
    try:
        group_id = await resolver(app_id, group_openid)
    except Exception:
        return None, False
    canonical = _positive_int(group_id)
    if group_id is not None and canonical is None:
        return None, False
    return canonical, True


def _policy_result(group_id: int) -> tuple[bool, int | None]:
    result = _runtime_module._runtime.adjudicate(
        [group_id], intent=AdmissionIntent.BUSINESS
    )
    return (
        result.qualification is AdmissionQualification.BUSINESS,
        result.effective_revision,
    )


async def _is_banned(member_qq: int | None) -> bool | None:
    checker = _callbacks.ban_checker
    if member_qq is None or checker is None:
        return False
    try:
        return await checker(member_qq, "command")
    except Exception:
        return None


def _valid_verified_session(
    session: QQVerifiedBindingSession,
    *,
    app_id: str,
    group_openid: str,
    member_openid: str,
) -> bool:
    now = _now()
    return (
        session.app_id == app_id
        and session.group_openid == group_openid
        and session.member_openid == member_openid
        and _positive_int(session.group_id) is not None
        and _positive_int(session.member_qq) is not None
        and session.expires_at > now
    )


async def _qualify_mapped_group(
    app_id: str,
    group_openid: str,
    member_openid: str,
    qq_message_id: str,
    group_id: int,
) -> QQAdmissionToken | None:
    member_qq: int | None = None
    member_resolver = _callbacks.member_resolver
    if member_resolver is not None:
        try:
            member_qq = _positive_int(
                await member_resolver(app_id, group_openid, member_openid)
            )
        except Exception:
            return None
    admitted, revision = _policy_result(group_id)
    banned = await _is_banned(member_qq)
    admitted_after, revision_after = _policy_result(group_id)
    if (
        not admitted
        or revision is None
        or banned is None
        or banned
        or not admitted_after
        or revision_after is None
    ):
        return None
    return QQAdmissionToken(
        scope="business",
        app_id=app_id,
        group_openid=group_openid,
        member_openid=member_openid,
        qq_message_id=qq_message_id,
        group_id=group_id,
        member_qq=member_qq,
        effective_policy_revision=revision_after,
        connection_generation=0,
        claim=None,
        verified_session=None,
    )


async def _qualify_verified_session(
    app_id: str,
    group_openid: str,
    member_openid: str,
    qq_message_id: str,
) -> tuple[bool, QQAdmissionToken | None]:
    """Return whether a missing binding session may continue to challenge."""
    resolver = _callbacks.binding_session_resolver
    if resolver is None:
        return True, None
    try:
        verified = await resolver(app_id, group_openid, member_openid)
    except Exception:
        return False, None
    if verified is None:
        return True, None
    valid_session = _valid_verified_session(
        verified,
        app_id=app_id,
        group_openid=group_openid,
        member_openid=member_openid,
    )
    admitted, revision = _policy_result(verified.group_id)
    banned = await _is_banned(verified.member_qq)
    admitted_after, revision_after = _policy_result(verified.group_id)
    if (
        not valid_session
        or not admitted
        or revision is None
        or banned is None
        or banned
        or not admitted_after
        or revision_after is None
    ):
        return False, None
    return (
        False,
        QQAdmissionToken(
            scope="binding",
            app_id=app_id,
            group_openid=group_openid,
            member_openid=member_openid,
            qq_message_id=qq_message_id,
            group_id=verified.group_id,
            member_qq=verified.member_qq,
            effective_policy_revision=revision_after,
            connection_generation=verified.connection_generation,
            claim=None,
            verified_session=verified,
        ),
    )


async def _qualify_challenge(
    app_id: str,
    group_openid: str,
    member_openid: str,
    qq_message_id: str,
    command: str,
) -> QQAdmissionToken | None:
    if command != "/bind":
        return None
    revision = _runtime_module._runtime.get_state().effective_revision
    claimer = _callbacks.initial_bind_claimer
    if revision is None or claimer is None:
        return None
    request = QQInitialBindRequest(
        app_id=app_id,
        group_openid=group_openid,
        member_openid=member_openid,
        qq_message_id=qq_message_id,
        command=command,
    )
    try:
        claim = await claimer(request)
    except Exception:
        return None
    revision = _runtime_module._runtime.get_state().effective_revision
    claim_valid = (
        claim is not None
        and claim.app_id == app_id
        and claim.group_openid == group_openid
        and claim.member_openid == member_openid
        and (not claim.is_new or claim.qq_message_id == qq_message_id)
        and claim.expires_at > _now()
        and claim.connection_generation >= 0
    )
    if revision is None or not claim_valid or claim is None:
        return None
    return QQAdmissionToken(
        scope="binding_challenge",
        app_id=app_id,
        group_openid=group_openid,
        member_openid=member_openid,
        qq_message_id=qq_message_id,
        group_id=None,
        member_qq=None,
        effective_policy_revision=revision,
        connection_generation=claim.connection_generation,
        claim=claim,
        verified_session=None,
    )


async def qualify_qq_event(
    bot: Bot,
    event: Event,
) -> QQAdmissionToken | None:
    """Qualify one native QQ group-at event through explicit authority seams."""
    if type(event) is not GroupAtMessageCreateEvent:
        return None
    app_id = _qq_app_id(bot)
    values = _event_values(event)
    if app_id is None or values is None:
        return None
    group_openid, member_openid, qq_message_id, command = values
    group_id, resolver_ok = await _current_group(app_id, group_openid)
    if not resolver_ok:
        return None
    if group_id is not None:
        return await _qualify_mapped_group(
            app_id,
            group_openid,
            member_openid,
            qq_message_id,
            group_id,
        )
    continue_to_challenge, token = await _qualify_verified_session(
        app_id,
        group_openid,
        member_openid,
        qq_message_id,
    )
    if token is not None or not continue_to_challenge:
        return token
    return await _qualify_challenge(
        app_id,
        group_openid,
        member_openid,
        qq_message_id,
        command,
    )


def get_qq_admission_token(state: object) -> QQAdmissionToken | None:
    """Read the preprocessor handoff token without consuming it."""
    if not isinstance(state, dict):
        return None
    token = state.get(QQ_ADMISSION_STATE_KEY)
    return token if isinstance(token, QQAdmissionToken) else None


async def recheck_qq_effect(
    token: QQAdmissionToken,
    *,
    effect: QQScope,
) -> QQEffectDecision:
    """Re-read all authority needed immediately before a QQ-side effect."""
    if effect not in {"business", "binding_challenge", "binding"}:
        return QQEffectDecision(
            allowed=False,
            effect="business",
            reason_code="invalid_effect",
            effective_policy_revision=None,
        )

    def reject(reason: str) -> QQEffectDecision:
        return QQEffectDecision(
            allowed=False,
            effect=effect,
            reason_code=reason,
            effective_policy_revision=_runtime_module._runtime.get_state().effective_revision,
        )

    if not isinstance(token, QQAdmissionToken) or token.scope != effect:
        return reject("scope_mismatch")
    if effect == "binding_challenge":
        return await _recheck_challenge(token, effect, reject)
    return await _recheck_authorized_effect(token, effect, reject)


async def _recheck_authorized_effect(
    token: QQAdmissionToken,
    effect: QQScope,
    reject: Callable[[str], QQEffectDecision],
) -> QQEffectDecision:
    group_id, member_qq, identity_error = await _resolve_effect_identity(
        token,
        effect,
    )
    if identity_error is not None or group_id is None:
        return reject(identity_error or "group_unavailable")
    banned = await _is_banned(member_qq)
    ban_error = "ban_unavailable" if banned is None else "user_banned" if banned else None
    if ban_error is not None:
        return reject(ban_error)
    if effect == "binding":
        binding_error = await _recheck_binding_authority(
            token,
            group_id,
            member_qq,
        )
        if binding_error is not None:
            return reject(binding_error)
    admitted, revision = _policy_result(group_id)
    if not admitted:
        return QQEffectDecision(
            allowed=False,
            effect=effect,
            reason_code="policy_restricted",
            effective_policy_revision=revision,
        )
    return QQEffectDecision(
        allowed=True,
        effect=effect,
        reason_code="policy_admitted",
        effective_policy_revision=revision,
    )


async def _recheck_challenge(
    token: QQAdmissionToken,
    effect: QQScope,
    reject: Callable[[str], QQEffectDecision],
) -> QQEffectDecision:
    validator = _callbacks.claim_validator
    if validator is None:
        return reject("challenge_unavailable")
    try:
        valid = await validator(token)
    except Exception:
        return reject("challenge_unavailable")
    revision = _runtime_module._runtime.get_state().effective_revision
    if (
        not valid
        or token.claim is None
        or token.claim.expires_at <= _now()
        or revision is None
    ):
        return reject("challenge_expired")
    return QQEffectDecision(
        allowed=True,
        effect=effect,
        reason_code="binding_challenge_allowed",
        effective_policy_revision=revision,
    )


async def _resolve_effect_identity(
    token: QQAdmissionToken,
    effect: QQScope,
) -> tuple[int | None, int | None, str | None]:
    if effect == "business":
        return await _resolve_business_identity(token)
    return await _resolve_binding_identity(token)


async def _resolve_business_identity(
    token: QQAdmissionToken,
) -> tuple[int | None, int | None, str | None]:
    group_id, resolver_ok = await _current_group(
        token.app_id,
        token.group_openid,
    )
    if not resolver_ok or group_id is None:
        return None, None, "group_unavailable"
    if token.group_id != group_id:
        return None, None, "scope_mismatch"
    member_qq = token.member_qq
    member_resolver = _callbacks.member_resolver
    if member_resolver is not None:
        try:
            member_qq = _positive_int(
                await member_resolver(
                    token.app_id,
                    token.group_openid,
                    token.member_openid,
                )
            )
        except Exception:
            return None, None, "member_unavailable"
    return group_id, member_qq, None


async def _resolve_binding_identity(
    token: QQAdmissionToken,
) -> tuple[int | None, int | None, str | None]:
    resolver = _callbacks.binding_session_resolver
    if resolver is None:
        return None, None, "binding_unavailable"
    try:
        session = await resolver(
            token.app_id,
            token.group_openid,
            token.member_openid,
        )
    except Exception:
        return None, None, "binding_unavailable"
    if session is None or not _valid_verified_session(
        session,
        app_id=token.app_id,
        group_openid=token.group_openid,
        member_openid=token.member_openid,
    ):
        return None, None, "binding_expired"
    if (
        token.verified_session is not session
        or session.connection_generation != token.connection_generation
        or token.group_id != session.group_id
        or token.member_qq != session.member_qq
    ):
        return None, None, "scope_mismatch"
    return session.group_id, session.member_qq, None


async def _recheck_binding_authority(
    token: QQAdmissionToken,
    group_id: int,
    member_qq: int | None,
) -> str | None:
    formal_error = await _check_binding_formal_group(token, group_id)
    if formal_error is not None:
        return formal_error
    latest_group_id, latest_member_qq, latest_error = (
        await _resolve_binding_identity(token)
    )
    if latest_error is not None:
        return latest_error
    if latest_group_id != group_id or latest_member_qq != member_qq:
        return "scope_mismatch"
    return None


async def _check_binding_formal_group(
    token: QQAdmissionToken,
    group_id: int,
) -> str | None:
    formal_group_id, resolver_ok = await _current_group(
        token.app_id,
        token.group_openid,
    )
    if not resolver_ok:
        return "group_unavailable"
    if formal_group_id is not None and formal_group_id != group_id:
        return "scope_mismatch"
    return None


__all__ = [
    "QQ_ADMISSION_STATE_KEY",
    "QQAdmissionToken",
    "QQBindClaim",
    "QQEffectDecision",
    "QQInitialBindRequest",
    "QQVerifiedBindingSession",
    "get_qq_admission_token",
    "qualify_qq_event",
    "recheck_qq_effect",
    "register_qq_ban_checker",
    "register_qq_binding_session_resolver",
    "register_qq_group_resolver",
    "register_qq_initial_bind_claimer",
]
