"""Short-lived QQ binding evidence coordinator.

The coordinator owns temporary challenge authority.  It deliberately receives
all storage and clock dependencies from the plugin entry point and never owns
the shared ORM engine.
"""

from __future__ import annotations

import asyncio
import secrets
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal

from komari_bot.plugins.group_admission import (
    QQAdmissionToken,
    QQBindClaim,
    QQEffectDecision,
    QQInitialBindRequest,
    QQVerifiedBindingSession,
    adjudicate,
    get_runtime_state,
    recheck_qq_effect,
    register_qq_ban_checker,
    register_qq_binding_session_resolver,
    register_qq_group_resolver,
    register_qq_initial_bind_claimer,
)

from .reply_evidence import (
    ReplyEvidence,
    ReplyEvidenceCollector,
    register_evidence_receiver,
    set_runtime_collectors,
)

GroupResolver = Callable[[str, str], Awaitable[int | None]]
MemberResolver = Callable[[str, str, str], Awaitable[int | None]]
Clock = Callable[[], datetime]

_MAX_COMPLETED_SENDS: int = 512


@dataclass(slots=True)
class _BindingRecord:
    request: QQInitialBindRequest
    claim: QQBindClaim
    collector: ReplyEvidenceCollector
    verified: QQVerifiedBindingSession | None = None
    evidence: ReplyEvidence | None = None


@dataclass(frozen=True, slots=True)
class _CompletedSend:
    """清理临时会话后，供成功回复做最后一次正式身份重审的凭据。"""

    session: QQVerifiedBindingSession
    generation: int
    expires_at: datetime


class QQBindingCoordinator:
    """Own one generation of QQ binding challenges and accepted evidence."""

    def __init__(
        self,
        *,
        collectors: Sequence[ReplyEvidenceCollector],
        group_resolver: GroupResolver,
        clock: Clock,
        member_resolver: MemberResolver | None = None,
        ban_checker: Callable[[int, str], Awaitable[bool]] | None = None,
    ) -> None:
        self._collectors = tuple(collectors)
        self._collectors_by_app: Mapping[str, ReplyEvidenceCollector] = {
            collector.app_id: collector for collector in self._collectors
        }
        if len(self._collectors_by_app) != len(self._collectors):
            raise ValueError("duplicate QQ collector app_id")  # noqa: TRY003
        self._group_resolver = group_resolver
        self._member_resolver = member_resolver
        self._ban_checker = ban_checker
        self._clock = clock
        self._generation = 0
        self._active = False
        self._counter = 0
        self._records: dict[tuple[str, str, str], _BindingRecord] = {}
        self._claims: dict[str, _BindingRecord] = {}
        self._completed_sends: dict[str, _CompletedSend] = {}
        self._locks: dict[tuple[str, str, str], asyncio.Lock] = {}

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None:
            raise ValueError("clock must return a timezone-aware datetime")  # noqa: TRY003
        return value.astimezone(UTC)

    def _key(self, request: QQInitialBindRequest) -> tuple[str, str, str]:
        return (request.app_id, request.group_openid, request.member_openid)

    def _lock_for(self, key: tuple[str, str, str]) -> asyncio.Lock:
        lock = self._locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[key] = lock
        return lock

    def _record_current(self, record: _BindingRecord) -> bool:
        return (
            self._active
            and record.claim.connection_generation == self._generation
            and record.claim.expires_at > self._now()
            and self._claims.get(record.claim.session_code) is record
        )

    @staticmethod
    def _positive_int(value: object) -> int | None:
        if isinstance(value, bool):
            return None
        if isinstance(value, int) and value > 0:
            return value
        if isinstance(value, str) and value.strip().isdigit():
            parsed = int(value.strip())
            return parsed if parsed > 0 else None
        return None

    async def _resolve_group(
        self,
        app_id: str,
        group_openid: str,
    ) -> tuple[int | None, bool]:
        try:
            raw_group_id = await self._group_resolver(app_id, group_openid)
        except Exception:
            return None, False
        group_id = self._positive_int(raw_group_id)
        if raw_group_id is not None and group_id is None:
            return None, False
        return group_id, True

    async def _claim_authority(
        self,
        request: QQInitialBindRequest,
    ) -> bool:
        group_id, resolver_ok = await self._resolve_group(
            request.app_id,
            request.group_openid,
        )
        if not resolver_ok:
            return False
        if group_id is None:
            return get_runtime_state().effective_revision is not None
        decision = adjudicate([group_id])
        return (
            decision.qualification.value == "business"
            and decision.effective_revision is not None
        )

    async def _is_banned(self, member_qq: int) -> bool | None:
        if self._ban_checker is None:
            return False
        try:
            return await self._ban_checker(member_qq, "command")
        except Exception:
            return None

    async def start(self) -> None:
        if self._active:
            return
        self._active = True
        set_runtime_collectors(self._collectors)
        register_qq_group_resolver(
            self._group_resolver,
            member_resolver=self._member_resolver,
        )
        register_qq_initial_bind_claimer(
            self.claim_initial_bind,
            validator=self._validate_challenge_token,
        )
        register_qq_binding_session_resolver(self.resolve_verified_binding_session)
        register_qq_ban_checker(self._ban_checker)
        register_evidence_receiver(self.accept_reply_evidence)

    async def close(self) -> None:
        should_reset = (
            self._active
            or bool(self._records)
            or bool(self._claims)
            or bool(self._completed_sends)
        )
        self._active = False
        if should_reset:
            self.reset_generation()
        register_qq_group_resolver(None)
        register_qq_initial_bind_claimer(None)
        register_qq_binding_session_resolver(None)
        register_qq_ban_checker(None)
        register_evidence_receiver(None)
        set_runtime_collectors(())

    def reset_generation(self) -> None:
        self._generation += 1
        self._records.clear()
        self._claims.clear()
        self._completed_sends.clear()
        for collector in self._collectors:
            collector.reset_connection()

    async def cancel(self, session_code: str) -> None:
        record = self._claims.pop(str(session_code), None)
        if record is None:
            return
        self._records.pop(self._key(record.request), None)
        record.collector.cancel_session(record.claim.session_code)
        verified = record.verified
        if verified is not None and verified.expires_at > self._now():
            self._completed_sends[verified.session_code] = _CompletedSend(
                session=verified,
                generation=self._generation,
                expires_at=verified.expires_at,
            )
            self._prune_completed_sends()

    def _prune_completed_sends(self) -> None:
        now = self._now()
        for code in [
            code
            for code, completed in self._completed_sends.items()
            if completed.expires_at <= now
        ]:
            self._completed_sends.pop(code, None)
        while len(self._completed_sends) > _MAX_COMPLETED_SENDS:
            oldest = min(
                self._completed_sends,
                key=lambda code: self._completed_sends[code].expires_at,
            )
            self._completed_sends.pop(oldest, None)

    async def claim_initial_bind(
        self,
        request: QQInitialBindRequest,
    ) -> QQBindClaim | None:
        generation = self._generation
        collector = self._collectors_by_app.get(request.app_id)
        if (
            not self._active
            or request.command.strip() != "/bind"
            or collector is None
            or not request.group_openid
            or not request.member_openid
            or not request.qq_message_id
        ):
            return None
        authority_ok = await self._claim_authority(request)
        if not authority_ok or not self._active or generation != self._generation:
            return None
        key = self._key(request)
        async with self._lock_for(key):
            if not self._active or generation != self._generation:
                return None
            authority_ok = await self._claim_authority(request)
            if not authority_ok or not self._active or generation != self._generation:
                return None
            return self._claim_initial_bind_locked(
                request=request,
                collector=collector,
                key=key,
                generation=generation,
            )

    def _claim_initial_bind_locked(
        self,
        *,
        request: QQInitialBindRequest,
        collector: ReplyEvidenceCollector,
        key: tuple[str, str, str],
        generation: int,
    ) -> QQBindClaim | None:
        existing = self._records.get(key)
        if existing is not None and self._record_current(existing):
            return QQBindClaim(
                session_code=existing.claim.session_code,
                app_id=existing.claim.app_id,
                group_openid=existing.claim.group_openid,
                member_openid=existing.claim.member_openid,
                qq_message_id=existing.claim.qq_message_id,
                connection_generation=existing.claim.connection_generation,
                expires_at=existing.claim.expires_at,
                is_new=False,
            )
        if existing is not None:
            self._records.pop(key, None)
            self._claims.pop(existing.claim.session_code, None)
            collector.cancel_session(existing.claim.session_code)

        self._counter += 1
        session_code = f"qq274-{generation}-{self._counter}-{secrets.token_hex(4)}"
        claim = QQBindClaim(
            session_code=session_code,
            app_id=request.app_id,
            group_openid=request.group_openid,
            member_openid=request.member_openid,
            qq_message_id=request.qq_message_id,
            connection_generation=generation,
            expires_at=self._now() + timedelta(minutes=10),
            is_new=True,
        )
        collector.open_session(
            session_code=claim.session_code,
            group_openid=request.group_openid,
            member_openid=request.member_openid,
            original_command=request.command,
            qq_message_id=request.qq_message_id,
        )
        record = _BindingRecord(request=request, claim=claim, collector=collector)
        self._records[key] = record
        self._claims[claim.session_code] = record
        return claim

    async def _validate_challenge_token(self, token: QQAdmissionToken) -> bool:
        claim = token.claim
        if claim is None:
            return False
        record = self._claims.get(claim.session_code)
        if record is None or not self._record_current(record):
            return False
        if (
            record.claim is not claim
            or not claim.is_new
            or token.qq_message_id != claim.qq_message_id
        ):
            return False
        generation = self._generation
        if not await self._claim_authority(record.request):
            return False
        return (
            self._active
            and generation == self._generation
            and token.app_id == record.claim.app_id
            and token.group_openid == record.claim.group_openid
            and token.member_openid == record.claim.member_openid
            and token.connection_generation == self._generation
            and self._record_current(record)
        )

    async def accept_reply_evidence(
        self,
        evidence: ReplyEvidence,
    ) -> QQAdmissionToken | None:
        generation = self._generation
        record = self._claims.get(evidence.session_code)
        if not self._active or record is None or not self._record_current(record):
            return None
        if (
            evidence.app_id != record.claim.app_id
            or evidence.group_openid != record.claim.group_openid
            or evidence.member_openid != record.claim.member_openid
            or evidence.qq_message_id != record.claim.qq_message_id
            or evidence.connection_generation != self._generation
            or evidence.original_command.strip() != "/bind"
        ):
            return None
        identity = self._evidence_identity(evidence)
        if identity is None:
            return None
        group_id, member_qq = identity
        revision = await self._authorize_evidence(
            record=record,
            generation=generation,
            group_id=group_id,
            member_qq=member_qq,
        )
        if revision is None:
            return None
        if not self._active or not self._record_current(record):
            return None
        verified = QQVerifiedBindingSession(
            session_code=record.claim.session_code,
            app_id=record.claim.app_id,
            group_openid=record.claim.group_openid,
            member_openid=record.claim.member_openid,
            qq_message_id=record.claim.qq_message_id,
            group_id=group_id,
            member_qq=member_qq,
            connection_generation=self._generation,
            expires_at=record.claim.expires_at,
        )
        record.evidence = evidence
        record.verified = verified
        return QQAdmissionToken(
            scope="binding",
            app_id=verified.app_id,
            group_openid=verified.group_openid,
            member_openid=verified.member_openid,
            qq_message_id=evidence.qq_message_id,
            group_id=verified.group_id,
            member_qq=verified.member_qq,
            effective_policy_revision=revision,
            connection_generation=verified.connection_generation,
            claim=None,
            verified_session=verified,
        )

    @staticmethod
    def _evidence_identity(evidence: ReplyEvidence) -> tuple[int, int] | None:
        try:
            group_id = int(str(evidence.group_id).strip())
            member_qq = int(str(evidence.member_qq).strip())
        except (TypeError, ValueError):
            return None
        if group_id <= 0 or member_qq <= 0:
            return None
        return group_id, member_qq

    async def _authorize_evidence(
        self,
        *,
        record: _BindingRecord,
        generation: int,
        group_id: int,
        member_qq: int,
    ) -> int | None:
        mapped_group_id, resolver_ok = await self._resolve_group(
            record.claim.app_id,
            record.claim.group_openid,
        )
        if (
            not resolver_ok
            or (mapped_group_id is not None and mapped_group_id != group_id)
            or not self._active
            or generation != self._generation
        ):
            return None
        decision = adjudicate([group_id])
        if decision.qualification.value != "business" or decision.effective_revision is None:
            return None
        banned = await self._is_banned(member_qq)
        if banned is None or banned:
            return None
        decision = adjudicate([group_id])
        if (
            not self._active
            or generation != self._generation
            or decision.qualification.value != "business"
            or decision.effective_revision is None
        ):
            return None
        return decision.effective_revision

    async def resolve_verified_binding_session(
        self,
        app_id: str,
        group_openid: str,
        member_openid: str,
    ) -> QQVerifiedBindingSession | None:
        record = self._records.get((app_id, group_openid, member_openid))
        if record is None or record.verified is None or not self._record_current(record):
            return None
        return record.verified

    async def recheck(
        self,
        token: QQAdmissionToken,
        *,
        effect: str,
    ) -> QQEffectDecision:
        scope: Literal["business", "binding_challenge", "binding"]
        match effect:
            case "business" | "binding_challenge" | "binding":
                scope = effect
            case _:
                scope = "business"
        if not self._active:
            return QQEffectDecision(
                allowed=False,
                effect=scope,
                reason_code="coordinator_closed",
                effective_policy_revision=None,
            )
        if scope == "binding_challenge" and not await self._validate_challenge_token(token):
            return QQEffectDecision(
                allowed=False,
                effect="binding_challenge",
                reason_code="challenge_unavailable",
                effective_policy_revision=None,
            )
        if scope == "binding":
            session = token.verified_session
            if session is None:
                return QQEffectDecision(
                    allowed=False,
                    effect="binding",
                    reason_code="binding_unavailable",
                    effective_policy_revision=None,
                )
            record = self._claims.get(session.session_code)
            if (
                record is not None
                and record.verified is session
                and self._record_current(record)
            ):
                return await recheck_qq_effect(token, effect=scope)
            return await self._recheck_completed_send(token, session)
        return await recheck_qq_effect(token, effect=scope)

    async def _recheck_completed_send(  # noqa: PLR0911
        self,
        token: QQAdmissionToken,
        session: QQVerifiedBindingSession,
    ) -> QQEffectDecision:
        """已清理会话的成功回复：用正式 canonical 身份重新资格。

        临时 claim/evidence 已移除，但成功文案仍需在发送前按最新策略与
        user_ban 复核；这里只信任已验证会话里的正式身份，不复活临时记录。
        """

        def reject(reason: str) -> QQEffectDecision:
            return QQEffectDecision(
                allowed=False,
                effect="binding",
                reason_code=reason,
                effective_policy_revision=None,
            )

        completed = self._completed_sends.get(session.session_code)
        if (
            not self._active
            or completed is None
            or completed.session is not session
            or completed.generation != self._generation
            or completed.expires_at <= self._now()
            or session.connection_generation != self._generation
        ):
            return reject("binding_unavailable")
        group_id, resolver_ok = await self._resolve_group(
            token.app_id,
            token.group_openid,
        )
        if (
            not resolver_ok
            or (group_id is not None and group_id != session.group_id)
            or token.group_id != session.group_id
            or token.member_qq != session.member_qq
        ):
            return reject("scope_mismatch")
        decision = adjudicate([session.group_id])
        if (
            decision.qualification.value != "business"
            or decision.effective_revision is None
        ):
            return reject("policy_restricted")
        banned = await self._is_banned(session.member_qq)
        if banned is None:
            return reject("ban_unavailable")
        if banned:
            return reject("user_banned")
        decision = adjudicate([session.group_id])
        if (
            not self._active
            or completed.generation != self._generation
            or decision.qualification.value != "business"
            or decision.effective_revision is None
        ):
            return reject("policy_restricted")
        return QQEffectDecision(
            allowed=True,
            effect="binding",
            reason_code="completed_send_allowed",
            effective_policy_revision=decision.effective_revision,
        )


__all__ = ["QQBindingCoordinator"]
