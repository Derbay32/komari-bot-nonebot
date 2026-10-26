# ruff: noqa: N815
"""Restricted HTTP management surface for the DSH model configuration.

Every route is backed by the typed control facade over the real AgentHost. The
router owns its validation-error boundary so a rejected request can never echo
a credential value through FastAPI's Pydantic error payload.
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, Annotated, Any, Literal, Protocol, cast
from urllib.parse import urlsplit

from fastapi import APIRouter, Body, Depends, FastAPI, Header, HTTPException, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field, SecretStr
from starlette import status

from komari_bot.llm.content_budget import (
    CONTENT_TEXT_BUDGET,
    ContentValidationError,
    validate_json_budget,
    validate_text_budget,
)
from komari_bot.management.management_api import (
    ManagementPrincipal,
    create_bearer_auth_dependency,
    ensure_management_cors,
)
from komari_bot.management.management_audit import (
    hash_management_target,
    management_audit_span,
    record_management_audit_event,
    require_management_change_reason,
    resolve_management_request_id,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Coroutine, Mapping, Sequence

    from starlette.requests import Request

    from komari_bot.agent.management import (
        DshBindingResult,
        DshCredentialResult,
        DshDeleteResult,
        DshPromptResult,
        DshProviderListResult,
        DshProviderResult,
    )
    from komari_bot.agent.types import JsonValue
    from komari_bot.management.management_api import ManagementTokenSource
    from komari_bot.management.management_audit import ManagementAuditRecorder

logger = logging.getLogger("komari_agent.management")

API_PREFIX = "/api/v2/komari-agent"
_PROVIDER_ID_PATTERN = re.compile(r"^[a-z][a-z0-9-]{0,63}$")
_ETAG_PATTERN = re.compile(r"^[A-Za-z0-9._~-]{1,256}$")
_MODEL_ID_PATTERN = re.compile(r"^[^\s\u0000-\u001f\u007f]{1,128}$")
_THINKING_LEVELS = frozenset({"off", "minimal", "low", "medium", "high", "xhigh", "max"})
_INPUT_MODALITIES = frozenset({"text", "image"})
_MAX_TIMEOUT_MS = 2_147_483_647
_MAX_CREDENTIAL_CHARS = 12_000

_SAFE_MESSAGES: dict[str, str] = {
    "invalid_request": "The management request is invalid.",
    "invalid_provider": "The provider definition is invalid.",
    "invalid_provider_id": "The provider identifier is invalid.",
    "invalid_binding": "The model binding is invalid.",
    "invalid_prompt": "The preset prompt is invalid.",
    "invalid_credential": "The credential is invalid.",
    "not_found": "The requested resource was not found.",
    "revision_conflict": "The resource changed since it was read; read it again.",
    "active_tasks": "A connection mutation cannot run while a task is live.",
    "provider_in_use": "The provider is still selected by a model binding.",
    "unavailable": "DSH management is unavailable.",
}
_STATUS_BY_CODE: dict[str, int] = {
    "invalid_request": status.HTTP_422_UNPROCESSABLE_CONTENT,
    "invalid_provider": status.HTTP_422_UNPROCESSABLE_CONTENT,
    "invalid_provider_id": status.HTTP_422_UNPROCESSABLE_CONTENT,
    "invalid_binding": status.HTTP_422_UNPROCESSABLE_CONTENT,
    "invalid_prompt": status.HTTP_422_UNPROCESSABLE_CONTENT,
    "invalid_credential": status.HTTP_422_UNPROCESSABLE_CONTENT,
    "not_found": status.HTTP_404_NOT_FOUND,
    "revision_conflict": status.HTTP_409_CONFLICT,
    "active_tasks": status.HTTP_409_CONFLICT,
    "provider_in_use": status.HTTP_409_CONFLICT,
    "unavailable": status.HTTP_503_SERVICE_UNAVAILABLE,
}


class DshManagementClientProtocol(Protocol):
    """The typed facade the HTTP layer consumes."""

    async def list_providers(self) -> DshProviderListResult: ...

    async def get_provider(self, provider: str) -> DshProviderResult: ...

    async def put_provider(
        self,
        provider: str,
        profile: Mapping[str, JsonValue],
        expected_etag: str,
    ) -> DshProviderResult: ...

    async def delete_provider(
        self,
        provider: str,
        expected_etag: str,
    ) -> DshDeleteResult: ...

    async def get_credential(self, provider: str) -> DshCredentialResult: ...

    async def put_credential(
        self,
        provider: str,
        value: str,
        expected_etag: str,
    ) -> DshCredentialResult: ...

    async def delete_credential(
        self,
        provider: str,
        expected_etag: str,
    ) -> DshCredentialResult: ...

    async def get_binding(self, kind: str) -> DshBindingResult: ...

    async def put_binding(
        self,
        kind: str,
        provider: str,
        model: str,
        reasoning_effort: str | None,
        expected_etag: str,
    ) -> DshBindingResult: ...

    async def delete_binding(self, kind: str, expected_etag: str) -> DshBindingResult: ...

    async def get_prompt(self, kind: str) -> DshPromptResult: ...

    async def put_prompt(self, kind: str, value: str, expected_etag: str) -> DshPromptResult: ...


class DshProviderModel(BaseModel):
    """One allowlisted model entry in a provider definition."""

    id: str
    name: str | None = None
    contextWindow: int | None = None
    maxTokens: int | None = None
    input: list[Literal["text", "image"]] | None = None
    reasoningEfforts: bool | dict[str, str | None] | None = None


class DshProviderDefinition(BaseModel):
    """One redacted provider definition; internal credential references never ride."""

    provider: str
    api: str | None = None
    baseURL: str | None = None
    displayName: str | None = None
    models: list[DshProviderModel] | None = None
    defaultContextWindow: int | None = None
    defaultMaxTokens: int | None = None
    defaultInput: list[Literal["text", "image"]] | None = None
    timeoutMs: int | None = None
    streamIdleTimeoutMs: int | None = None


class DshProviderDetail(DshProviderDefinition):
    """One provider definition plus the collection revision token."""

    etag: str


class DshProviderListResponse(BaseModel):
    """Configured providers and the protocol identifiers the adapter supports."""

    items: list[DshProviderDefinition]
    protocols: list[str]
    etag: str


class DshCredentialResponse(BaseModel):
    """Credential presence facts; never the value, suffix, or preview."""

    provider: str
    configured: bool
    source: str | None = None
    writable: bool
    etag: str


class DshBindingResponse(BaseModel):
    """One model selection, excluding prompt text."""

    kind: Literal["chat", "summary"]
    provider: str | None = None
    model: str | None = None
    reasoningEffort: str | None = None
    etag: str


class DshPromptResponse(BaseModel):
    """One literal long-term preset prompt."""

    kind: Literal["chat", "summary"]
    value: str
    etag: str


class DshDeleteResponse(BaseModel):
    """Successful deletion result."""

    deleted: bool = True
    etag: str


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DshProviderModelInput(_StrictModel):
    id: str = Field(min_length=1, max_length=128)
    name: str | None = Field(default=None, min_length=1, max_length=128)
    contextWindow: int | None = Field(default=None, ge=1, le=_MAX_TIMEOUT_MS)
    maxTokens: int | None = Field(default=None, ge=1, le=_MAX_TIMEOUT_MS)
    input: list[Literal["text", "image"]] | None = Field(default=None, min_length=1, max_length=2)
    reasoningEfforts: bool | dict[str, str | None] | None = None


class DshProviderPutRequest(_StrictModel):
    api: str = Field(min_length=1, max_length=64)
    baseURL: str = Field(min_length=1, max_length=2048)
    displayName: str | None = Field(default=None, min_length=1, max_length=128)
    models: list[DshProviderModelInput] = Field(min_length=1, max_length=100)
    defaultContextWindow: int | None = Field(default=None, ge=1, le=_MAX_TIMEOUT_MS)
    defaultMaxTokens: int | None = Field(default=None, ge=1, le=_MAX_TIMEOUT_MS)
    defaultInput: list[Literal["text", "image"]] | None = Field(default=None, min_length=1, max_length=2)
    timeoutMs: int | None = Field(default=None, ge=1, le=_MAX_TIMEOUT_MS)
    streamIdleTimeoutMs: int | None = Field(default=None, ge=1, le=_MAX_TIMEOUT_MS)


class DshBindingPutRequest(_StrictModel):
    provider: str = Field(min_length=1, max_length=64)
    model: str = Field(min_length=1, max_length=256)
    reasoningEffort: str | None = Field(default=None, min_length=1, max_length=32)


class DshPromptPutRequest(_StrictModel):
    value: str = Field(min_length=1)


class DshCredentialPutRequest(_StrictModel):
    value: SecretStr


class _SanitizedAPIRoute(APIRoute):
    """Router-local validation boundary with a fixed body that never echoes input."""

    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        original = super().get_route_handler()

        async def sanitized(request: Request) -> Response:
            try:
                return await original(request)
            except RequestValidationError:
                return JSONResponse(
                    status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                    content={
                        "detail": {
                            "code": "invalid_request",
                            "message": "The management request is invalid.",
                        },
                    },
                )

        return sanitized


def _invalid(code: str) -> HTTPException:
    return HTTPException(
        status_code=_STATUS_BY_CODE[code],
        detail={"code": code, "message": _SAFE_MESSAGES[code]},
    )


def _unavailable() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail={"code": "unavailable", "message": _SAFE_MESSAGES["unavailable"]},
    )


def _raise_control_error(error: Exception) -> None:
    code = getattr(error, "code", "unavailable")
    if code not in _SAFE_MESSAGES:
        code = "unavailable"
    raise HTTPException(
        status_code=_STATUS_BY_CODE[code],
        detail={"code": code, "message": _SAFE_MESSAGES[code]},
    ) from None


async def _safe[T](awaitable: Awaitable[T]) -> T:
    from komari_bot.agent import (
        AgentControlError,
        AgentHostUnavailableError,
        AgentProtocolError,
    )

    try:
        return await awaitable
    except AgentControlError as exc:
        _raise_control_error(exc)
    except (AgentHostUnavailableError, AgentProtocolError):
        raise _unavailable() from None
    except HTTPException:
        raise
    except Exception as exc:
        logger.warning(
            "[komari_agent] 管理请求失败: error_type=%s",
            type(exc).__name__,
        )
        raise _unavailable() from None
    raise AssertionError("unreachable")


def _provider_id(value: str) -> str:
    if _PROVIDER_ID_PATTERN.fullmatch(value) is None:
        raise _invalid("invalid_provider_id")
    return value


def _task_kind(value: str) -> Literal["chat", "summary"]:
    if value not in {"chat", "summary"}:
        raise _invalid("invalid_request")
    return cast("Literal['chat', 'summary']", value)


def _parse_if_match(value: str | None) -> str:
    if value is None:
        raise HTTPException(
            status_code=status.HTTP_428_PRECONDITION_REQUIRED,
            detail={
                "code": "precondition_required",
                "message": "If-Match is required for this write.",
            },
        )
    raw = value.strip()
    if raw in {"", "*"} or raw.startswith("W/") or "," in raw:
        raise _invalid("invalid_request")
    if len(raw) < 2 or raw[0] != '"' or raw[-1] != '"':
        raise _invalid("invalid_request")
    raw = raw[1:-1]
    if _ETAG_PATTERN.fullmatch(raw) is None:
        raise _invalid("invalid_request")
    return raw


def _apply_etag(response: Response, etag: str) -> None:
    response.headers["ETag"] = f'"{etag}"'


def _validate_modalities(
    values: list[Literal["text", "image"]] | None,
    code: str,
) -> None:
    if values is None:
        return
    if not values or len(set(values)) != len(values):
        raise _invalid(code)
    if any(value not in _INPUT_MODALITIES for value in values):
        raise _invalid(code)


def _validate_reasoning_efforts(
    *,
    value: bool | dict[str, str | None] | None,
    code: str,
) -> bool | dict[str, str | None] | None:
    if value is None or value is False:
        return value
    if value is True or not isinstance(value, dict) or not value:
        raise _invalid(code)
    for level, wire in value.items():
        if level not in _THINKING_LEVELS:
            raise _invalid(code)
        if level == "off":
            if wire is not None and not isinstance(wire, str):
                raise _invalid(code)
            continue
        if not isinstance(wire, str) or not wire or len(wire) > 128:
            raise _invalid(code)
    return value


def _validate_base_url(value: str) -> str:
    if re.search(r"[\u0000-\u0020\u007f]", value):
        raise _invalid("invalid_provider")
    try:
        parsed = urlsplit(value)
    except ValueError:
        raise _invalid("invalid_provider") from None
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise _invalid("invalid_provider")
    if parsed.username is not None or parsed.password is not None:
        raise _invalid("invalid_provider")
    if parsed.query or parsed.fragment:
        raise _invalid("invalid_provider")
    return value


def _normalize_provider_payload(payload: DshProviderPutRequest) -> dict[str, JsonValue]:
    code = "invalid_provider"
    if not payload.api.strip() or re.search(r"\s", payload.api):
        raise _invalid(code)
    _validate_base_url(payload.baseURL)
    _validate_modalities(payload.defaultInput, code)
    if payload.displayName is not None and not payload.displayName.strip():
        raise _invalid(code)
    normalized_models: list[dict[str, JsonValue]] = []
    for model in payload.models:
        if _MODEL_ID_PATTERN.fullmatch(model.id) is None:
            raise _invalid(code)
        _validate_modalities(model.input, code)
        _validate_reasoning_efforts(value=model.reasoningEfforts, code=code)
        normalized_models.append(
            cast(
                "dict[str, JsonValue]",
                model.model_dump(mode="json", exclude_none=True),
            ),
        )
    try:
        validate_json_budget(
            payload.model_dump(mode="json", exclude_none=True),
            label="DSH 提供者配置",
        )
    except ContentValidationError:
        raise _invalid(code) from None
    values: dict[str, JsonValue] = {
        "api": payload.api,
        "baseURL": payload.baseURL,
        "models": cast("list[JsonValue]", normalized_models),
    }
    if payload.displayName is not None:
        values["displayName"] = payload.displayName
    if payload.defaultContextWindow is not None:
        values["defaultContextWindow"] = payload.defaultContextWindow
    if payload.defaultMaxTokens is not None:
        values["defaultMaxTokens"] = payload.defaultMaxTokens
    if payload.defaultInput is not None:
        values["defaultInput"] = cast("list[JsonValue]", list(payload.defaultInput))
    if payload.timeoutMs is not None:
        values["timeoutMs"] = payload.timeoutMs
    if payload.streamIdleTimeoutMs is not None:
        values["streamIdleTimeoutMs"] = payload.streamIdleTimeoutMs
    return values


def _validate_binding_payload(payload: DshBindingPutRequest) -> None:
    if _PROVIDER_ID_PATTERN.fullmatch(payload.provider) is None:
        raise _invalid("invalid_binding")
    if _MODEL_ID_PATTERN.fullmatch(payload.model) is None:
        raise _invalid("invalid_binding")
    if payload.reasoningEffort is not None and not payload.reasoningEffort.strip():
        raise _invalid("invalid_binding")
    try:
        validate_json_budget(
            payload.model_dump(mode="json", exclude_none=True),
            label="DSH 模型绑定",
        )
    except ContentValidationError:
        raise _invalid("invalid_binding") from None


def _provider_detail(result: DshProviderResult, response: Response) -> DshProviderDetail:
    _apply_etag(response, result.etag)
    return DshProviderDetail(
        **DshProviderDefinition.model_validate(dict(result.provider)).model_dump(),
        etag=result.etag,
    )


def _binding_response(result: DshBindingResult, response: Response) -> DshBindingResponse:
    _apply_etag(response, result.etag)
    return DshBindingResponse(
        kind=cast("Literal['chat', 'summary']", result.kind),
        provider=result.provider,
        model=result.model,
        reasoningEffort=result.reasoning_effort,
        etag=result.etag,
    )


def _prompt_response(result: DshPromptResult, response: Response) -> DshPromptResponse:
    _apply_etag(response, result.etag)
    return DshPromptResponse(
        kind=cast("Literal['chat', 'summary']", result.kind),
        value=result.value,
        etag=result.etag,
    )


def _credential_response(
    result: DshCredentialResult,
    response: Response,
) -> DshCredentialResponse:
    _apply_etag(response, result.etag)
    return DshCredentialResponse(
        provider=result.provider,
        configured=result.configured,
        source=result.source,
        writable=result.writable,
        etag=result.etag,
    )


def create_dsh_router(
    *,
    api_token: ManagementTokenSource,
    client_getter: Callable[[], object | None],
    audit_recorder: ManagementAuditRecorder | None = None,
) -> APIRouter:
    """Create the restricted DSH management router."""
    config_read = create_bearer_auth_dependency(
        api_token,
        detail="未授权访问 DSH 配置接口",
        required_permission="config:read",
    )
    config_write = create_bearer_auth_dependency(
        api_token,
        detail="未授权修改 DSH 配置",
        required_permission="config:write",
    )
    prompt_read = create_bearer_auth_dependency(
        api_token,
        detail="未授权访问 DSH 提示词接口",
        required_permission="prompt:read",
    )
    prompt_write = create_bearer_auth_dependency(
        api_token,
        detail="未授权修改 DSH 提示词",
        required_permission="prompt:write",
    )
    recorder = audit_recorder or record_management_audit_event
    router = APIRouter(
        prefix=API_PREFIX,
        tags=["komari-agent"],
        route_class=_SanitizedAPIRoute,
    )

    def client() -> DshManagementClientProtocol:
        current = client_getter()
        if current is None:
            raise _unavailable()
        return cast("DshManagementClientProtocol", current)

    @router.get(
        "/providers",
        response_model=DshProviderListResponse,
        dependencies=[Depends(config_read)],
    )
    async def list_providers(response: Response) -> DshProviderListResponse:
        result = await _safe(client().list_providers())
        _apply_etag(response, result.etag)
        return DshProviderListResponse(
            items=[
                DshProviderDefinition.model_validate(dict(item))
                for item in result.items
            ],
            protocols=list(result.protocols),
            etag=result.etag,
        )

    @router.get(
        "/providers/{provider}",
        response_model=DshProviderDetail,
        dependencies=[Depends(config_read)],
    )
    async def get_provider(provider: str, response: Response) -> DshProviderDetail:
        provider_id = _provider_id(provider)
        result = await _safe(client().get_provider(provider_id))
        return _provider_detail(result, response)

    @router.put("/providers/{provider}", response_model=DshProviderDetail)
    async def put_provider(
        provider: str,
        payload: Annotated[DshProviderPutRequest, Body()],
        response: Response,
        if_match: Annotated[str | None, Header(alias="If-Match")] = None,
        _read: ManagementPrincipal = Depends(config_read),  # noqa: FAST002
        principal: ManagementPrincipal = Depends(config_write),  # noqa: FAST002
        reason: str = Depends(require_management_change_reason),  # noqa: FAST002
        request_id: str = Depends(resolve_management_request_id),  # noqa: FAST002
    ) -> DshProviderDetail:
        provider_id = _provider_id(provider)
        profile = _normalize_provider_payload(payload)
        expected = _parse_if_match(if_match)
        async with management_audit_span(
            principal=principal,
            request_id=request_id,
            reason=reason,
            action="dsh.provider.put",
            resource="dsh-provider",
            target_hash=hash_management_target("dsh-provider", provider_id),
            recorder=recorder,
        ):
            result = await _safe(
                client().put_provider(provider_id, profile, expected),
            )
            return _provider_detail(result, response)

    @router.delete("/providers/{provider}", response_model=DshDeleteResponse)
    async def delete_provider(
        provider: str,
        response: Response,
        if_match: Annotated[str | None, Header(alias="If-Match")] = None,
        _read: ManagementPrincipal = Depends(config_read),  # noqa: FAST002
        principal: ManagementPrincipal = Depends(config_write),  # noqa: FAST002
        reason: str = Depends(require_management_change_reason),  # noqa: FAST002
        request_id: str = Depends(resolve_management_request_id),  # noqa: FAST002
    ) -> DshDeleteResponse:
        provider_id = _provider_id(provider)
        expected = _parse_if_match(if_match)
        async with management_audit_span(
            principal=principal,
            request_id=request_id,
            reason=reason,
            action="dsh.provider.delete",
            resource="dsh-provider",
            target_hash=hash_management_target("dsh-provider", provider_id),
            recorder=recorder,
        ):
            result = await _safe(client().delete_provider(provider_id, expected))
            _apply_etag(response, result.etag)
            return DshDeleteResponse(deleted=True, etag=result.etag)

    @router.get(
        "/providers/{provider}/credential",
        response_model=DshCredentialResponse,
        dependencies=[Depends(config_read)],
    )
    async def get_credential(provider: str, response: Response) -> DshCredentialResponse:
        provider_id = _provider_id(provider)
        result = await _safe(client().get_credential(provider_id))
        return _credential_response(result, response)

    @router.put(
        "/providers/{provider}/credential",
        response_model=DshCredentialResponse,
    )
    async def put_credential(
        provider: str,
        payload: Annotated[DshCredentialPutRequest, Body()],
        response: Response,
        if_match: Annotated[str | None, Header(alias="If-Match")] = None,
        _read: ManagementPrincipal = Depends(config_read),  # noqa: FAST002
        principal: ManagementPrincipal = Depends(config_write),  # noqa: FAST002
        reason: str = Depends(require_management_change_reason),  # noqa: FAST002
        request_id: str = Depends(resolve_management_request_id),  # noqa: FAST002
    ) -> DshCredentialResponse:
        provider_id = _provider_id(provider)
        expected = _parse_if_match(if_match)
        secret = payload.value.get_secret_value()
        if not secret or len(secret) > _MAX_CREDENTIAL_CHARS:
            raise _invalid("invalid_credential")
        async with management_audit_span(
            principal=principal,
            request_id=request_id,
            reason=reason,
            action="dsh.credential.put",
            resource="dsh-credential",
            target_hash=hash_management_target("dsh-credential", provider_id),
            recorder=recorder,
        ):
            result = await _safe(client().put_credential(provider_id, secret, expected))
            return _credential_response(result, response)

    @router.delete(
        "/providers/{provider}/credential",
        response_model=DshCredentialResponse,
    )
    async def delete_credential(
        provider: str,
        response: Response,
        if_match: Annotated[str | None, Header(alias="If-Match")] = None,
        _read: ManagementPrincipal = Depends(config_read),  # noqa: FAST002
        principal: ManagementPrincipal = Depends(config_write),  # noqa: FAST002
        reason: str = Depends(require_management_change_reason),  # noqa: FAST002
        request_id: str = Depends(resolve_management_request_id),  # noqa: FAST002
    ) -> DshCredentialResponse:
        provider_id = _provider_id(provider)
        expected = _parse_if_match(if_match)
        async with management_audit_span(
            principal=principal,
            request_id=request_id,
            reason=reason,
            action="dsh.credential.delete",
            resource="dsh-credential",
            target_hash=hash_management_target("dsh-credential", provider_id),
            recorder=recorder,
        ):
            result = await _safe(client().delete_credential(provider_id, expected))
            return _credential_response(result, response)

    @router.get(
        "/bindings/{kind}",
        response_model=DshBindingResponse,
        dependencies=[Depends(config_read)],
    )
    async def get_binding(kind: str, response: Response) -> DshBindingResponse:
        task_kind = _task_kind(kind)
        result = await _safe(client().get_binding(task_kind))
        return _binding_response(result, response)

    @router.put("/bindings/{kind}", response_model=DshBindingResponse)
    async def put_binding(
        kind: str,
        payload: Annotated[DshBindingPutRequest, Body()],
        response: Response,
        if_match: Annotated[str | None, Header(alias="If-Match")] = None,
        _read: ManagementPrincipal = Depends(config_read),  # noqa: FAST002
        principal: ManagementPrincipal = Depends(config_write),  # noqa: FAST002
        reason: str = Depends(require_management_change_reason),  # noqa: FAST002
        request_id: str = Depends(resolve_management_request_id),  # noqa: FAST002
    ) -> DshBindingResponse:
        task_kind = _task_kind(kind)
        _validate_binding_payload(payload)
        expected = _parse_if_match(if_match)
        async with management_audit_span(
            principal=principal,
            request_id=request_id,
            reason=reason,
            action="dsh.binding.put",
            resource="dsh-binding",
            target_hash=hash_management_target("dsh-binding", task_kind),
            recorder=recorder,
        ):
            result = await _safe(
                client().put_binding(
                    task_kind,
                    payload.provider,
                    payload.model,
                    payload.reasoningEffort,
                    expected,
                ),
            )
            return _binding_response(result, response)

    @router.delete("/bindings/{kind}", response_model=DshBindingResponse)
    async def delete_binding(
        kind: str,
        response: Response,
        if_match: Annotated[str | None, Header(alias="If-Match")] = None,
        _read: ManagementPrincipal = Depends(config_read),  # noqa: FAST002
        principal: ManagementPrincipal = Depends(config_write),  # noqa: FAST002
        reason: str = Depends(require_management_change_reason),  # noqa: FAST002
        request_id: str = Depends(resolve_management_request_id),  # noqa: FAST002
    ) -> DshBindingResponse:
        task_kind = _task_kind(kind)
        expected = _parse_if_match(if_match)
        async with management_audit_span(
            principal=principal,
            request_id=request_id,
            reason=reason,
            action="dsh.binding.delete",
            resource="dsh-binding",
            target_hash=hash_management_target("dsh-binding", task_kind),
            recorder=recorder,
        ):
            result = await _safe(client().delete_binding(task_kind, expected))
            return _binding_response(result, response)

    @router.get(
        "/prompts/{kind}",
        response_model=DshPromptResponse,
        dependencies=[Depends(prompt_read)],
    )
    async def get_prompt(kind: str, response: Response) -> DshPromptResponse:
        task_kind = _task_kind(kind)
        result = await _safe(client().get_prompt(task_kind))
        return _prompt_response(result, response)

    @router.put("/prompts/{kind}", response_model=DshPromptResponse)
    async def put_prompt(
        kind: str,
        payload: Annotated[DshPromptPutRequest, Body()],
        response: Response,
        if_match: Annotated[str | None, Header(alias="If-Match")] = None,
        _read: ManagementPrincipal = Depends(prompt_read),  # noqa: FAST002
        principal: ManagementPrincipal = Depends(prompt_write),  # noqa: FAST002
        reason: str = Depends(require_management_change_reason),  # noqa: FAST002
        request_id: str = Depends(resolve_management_request_id),  # noqa: FAST002
    ) -> DshPromptResponse:
        task_kind = _task_kind(kind)
        if not payload.value.strip():
            raise _invalid("invalid_prompt")
        try:
            validate_text_budget(
                payload.value,
                label="系统提示词",
                budget=CONTENT_TEXT_BUDGET,
            )
        except ContentValidationError:
            raise _invalid("invalid_prompt") from None
        expected = _parse_if_match(if_match)
        async with management_audit_span(
            principal=principal,
            request_id=request_id,
            reason=reason,
            action="dsh.prompt.put",
            resource="dsh-prompt",
            target_hash=hash_management_target("dsh-prompt", task_kind),
            recorder=recorder,
        ):
            result = await _safe(
                client().put_prompt(task_kind, payload.value, expected),
            )
            return _prompt_response(result, response)

    return router


def register_dsh_management_api(
    app: FastAPI,
    *,
    api_token: ManagementTokenSource,
    allowed_origins: Sequence[str],
    client_getter: Callable[[], object | None],
    audit_recorder: ManagementAuditRecorder | None = None,
) -> None:
    """Register the DSH management routes once."""
    if getattr(app.state, "komari_agent_management_api_registered", False):
        return
    ensure_management_cors(app, allowed_origins)
    app.include_router(
        create_dsh_router(
            api_token=api_token,
            client_getter=client_getter,
            audit_recorder=audit_recorder,
        )
    )
    app.state.komari_agent_management_api_registered = True


__all__ = [
    "API_PREFIX",
    "DshManagementClientProtocol",
    "create_dsh_router",
    "register_dsh_management_api",
]
