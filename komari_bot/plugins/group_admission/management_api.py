"""群聊准入专属管理 HTTP Adapter（ADR-0012「管理 HTTP Adapter」）。

``register_group_admission_api`` 是准入插件顶层暴露的装配入口，由
``komari_management`` 经 ``require("group_admission")`` 取得并传入管理凭据 /
CORS / 审计装配参数。本模块只依赖 ``komari_bot.management`` 的共享鉴权 /
CORS / 审计工具，不反向依赖 ``komari_management`` 插件。

专属路由（精确三个，无通用配置写入口 / reload 路由）：

- ``GET  /api/v2/group-admission/policy``（config:read）
- ``PUT  /api/v2/group-admission/policy``（config:write）
- ``GET  /api/v2/group-admission/status``（config:read）

GET policy 每次请求强制经运行时内部控制面真实持久刷新（不读缓存 / LKG），
返回规范化完整策略 + revision + UTC updated_at 与强 ``ETag: "<revision>"``；
存储不可读 / 持久策略非法 → 503（不回显缓存策略 / 非法正文）。PUT 先严格
校验 If-Match 与 policy，再经运行时单次 strict CAS 持久化并发布本地快照，
只有本地 effective revision 已更新后才返回 200 + 新 ETag + 规范化策略；冲突
409、存储写失败 503（persisted=false）、已持久化未发布 503（persisted=true）。
GET status 只投影进程内不可变运行时状态、零隐藏 I/O，ready/degraded/failed
恒 200。

全部本 Module 409/422/503/500 使用固定白名单外壳
``{"detail": {"code", "message", "configured_revision", "effective_revision",
"persisted"}}``，code 取 7 个封闭值，message 固定不拼异常 / 策略 / revision /
ID；审计只记录旧 / 新 revision、规范化策略 SHA-256 指纹、persisted /
published 与结果码，绝不含策略正文 / 群号。
"""

from __future__ import annotations

import re
from collections.abc import Sequence  # noqa: TC003
from datetime import UTC, datetime
from typing import TYPE_CHECKING, NoReturn

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from starlette import status

from komari_bot.admission_policy import policy_fingerprint
from komari_bot.management.management_api import (
    ManagementPrincipal,
    ManagementTokenSource,
    create_bearer_auth_dependency,
    ensure_management_cors,
)
from komari_bot.management.management_audit import (
    ManagementAuditEvent,
    ManagementAuditRecorder,
    management_audit_span,
    require_management_change_reason,
    resolve_management_request_id,
)

from . import runtime as _runtime_module
from .policy import PolicyCompilationError, compile_policy
from .runtime import (
    _AdmissionRuntime,
    _ControlPlaneApplyResult,
    _ControlPlaneConflictError,
    _ControlPlaneError,
)

if TYPE_CHECKING:
    from collections.abc import Mapping


_POLICY_PATH = "/api/v2/group-admission/policy"
_STATUS_PATH = "/api/v2/group-admission/status"

_AUDIT_ACTION = "group_admission.update_policy"
_AUDIT_RESOURCE = "group_admission"

# 强 ETag：``"<正整数>"``，禁止弱引用（W/）与无引号包裹。
_STRONG_ETAG_RE = re.compile(r'^"([0-9]+)"$')

# 封闭错误码 → 固定 message（绝不拼接异常 / 策略 / revision / ID）。
_ERROR_MESSAGES: dict[str, str] = {
    "invalid_if_match": "If-Match 必须是强 ETag（双引号包裹的修订号）",
    "invalid_policy": "策略格式非法",
    "revision_conflict": "配置修订冲突，请基于最新策略重试",
    "storage_unavailable": "持久化存储当前不可用",
    "stored_policy_invalid": "持久化策略非法",
    "snapshot_publish_failed": "策略已持久化但本地发布失败",
    "internal_error": "内部错误",
}

# 错误码 → (HTTP 状态, persisted 标志)。
_ERROR_STATUS: dict[str, tuple[int, bool]] = {
    "invalid_if_match": (status.HTTP_422_UNPROCESSABLE_CONTENT, False),
    "invalid_policy": (status.HTTP_422_UNPROCESSABLE_CONTENT, False),
    "revision_conflict": (status.HTTP_409_CONFLICT, False),
    "storage_unavailable": (status.HTTP_503_SERVICE_UNAVAILABLE, False),
    "stored_policy_invalid": (status.HTTP_503_SERVICE_UNAVAILABLE, False),
    "snapshot_publish_failed": (status.HTTP_503_SERVICE_UNAVAILABLE, True),
    "internal_error": (status.HTTP_500_INTERNAL_SERVER_ERROR, False),
}

_REGISTRATION_FLAG = "_komari_group_admission_api_registered"


async def _noop_recorder(event: ManagementAuditEvent) -> None:
    """审计 recorder 为 None 时使用的无操作 recorder。"""
    del event


def _policy_fingerprint(policy: Mapping[str, object]) -> str:
    """规范化策略的 SHA-256 指纹（审计安全字段；不含群号明文）。

    TSK-247 起复用共享 ``admission_policy.policy_fingerprint`` 真源：对
    canonical 升序存储形态取摘要，与 CLI / 0013 迁移 / 运行时日志逐字一致。
    """
    return policy_fingerprint(dict(policy))


def _normalize_policy(payload: object) -> dict[str, object]:
    """严格编译并规范化策略（group_ids 去重排序）。

    非法载荷（缺字段 / 额外字段 / 非法 mode / 非正整数群号 / 非对象）抛
    ``PolicyCompilationError``，调用方归一为 422 invalid_policy。
    """
    compiled = compile_policy(payload)
    return {
        "mode": compiled.mode,
        "group_ids": sorted(compiled.group_ids),
    }


def _parse_strong_etag(raw: str | None) -> int:
    """解析强 ETag 为修订号；缺失 / 弱引用 / 无引号 / 非数字 / 非正整数 → ``ValueError``。"""
    if raw is None:
        msg = "缺少 If-Match"
        raise ValueError(msg)
    match = _STRONG_ETAG_RE.fullmatch(raw)
    if not match:
        msg = "If-Match 必须是双引号包裹的正整数修订号"
        raise ValueError(msg)
    revision = int(match.group(1))
    if revision <= 0:
        msg = "If-Match 修订号必须为正整数"
        raise ValueError(msg)
    return revision


def _runtime() -> _AdmissionRuntime:
    """请求时惰性解析准入运行时 singleton（与顶层 ``adjudicate`` 同一接缝）。"""
    return _runtime_module._runtime


def _raise_api_error(code: str, *, runtime: _AdmissionRuntime) -> NoReturn:
    """按运行时当前状态构造并抛出固定白名单错误（``HTTPException``）。"""
    state = runtime.get_state()
    http_status, persisted = _ERROR_STATUS[code]
    raise HTTPException(
        status_code=http_status,
        detail={
            "code": code,
            "message": _ERROR_MESSAGES[code],
            "configured_revision": state.configured_revision,
            "effective_revision": state.effective_revision,
            "persisted": persisted,
        },
    )


def _serialize_status(state: dict[str, object]) -> dict[str, object]:
    """把运行时内存投影的 datetime 字段序列化为 UTC RFC 3339 字符串。"""
    def _convert(value: object) -> object:
        if isinstance(value, datetime):
            return value.astimezone(UTC).isoformat()
        return value

    result: dict[str, object] = {}
    for key, value in state.items():
        if key == "telemetry" and isinstance(value, dict):
            result[key] = {
                sub_key: _convert(sub_value) for sub_key, sub_value in value.items()
            }
        else:
            result[key] = _convert(value)
    return result


def register_group_admission_api(
    app: FastAPI,
    *,
    api_token: ManagementTokenSource,
    allowed_origins: Sequence[str],
    audit_recorder: ManagementAuditRecorder | None = None,
) -> None:
    """装配群聊准入专属管理控制面（幂等）。

    签名冻结为 ``(app, *, api_token, allowed_origins, audit_recorder=None)``。
    重复调用只幂等挂载，不重复路由、不重复 CORS 中间件；``api_token`` 为凭据
    源（凭据 dict 序列或工厂），``allowed_origins`` 为 CORS 白名单序列。
    """
    if getattr(app.state, _REGISTRATION_FLAG, False):
        return

    ensure_management_cors(app, list(allowed_origins))

    read_auth = create_bearer_auth_dependency(api_token, required_permission="config:read")
    write_auth = create_bearer_auth_dependency(
        api_token, required_permission="config:write"
    )

    router = APIRouter()

    @router.get(_POLICY_PATH, dependencies=[Depends(read_auth)])
    async def get_policy() -> JSONResponse:
        """GET policy（config:read）：强制真实持久刷新，返回规范化策略 + 强 ETag。

        存储不可读 → 503 storage_unavailable（按 LKG 降级、不以缓存策略冒充
        200）；持久策略非法 → 503 stored_policy_invalid（不回显正文）。
        """
        rt = _runtime()
        await rt._refresh_persistent_state()
        state = rt.get_state()
        if state.status.value == "ready":
            manager = rt._manager
            if manager is None:
                _raise_api_error("storage_unavailable", runtime=rt)
            snapshot = manager.get_cached_versioned_snapshot()
            normalized = _normalize_policy(snapshot.value.policy)
            body = {
                "policy": normalized,
                "revision": snapshot.revision,
                "updated_at": snapshot.updated_at.astimezone(UTC).isoformat(),
            }
            return JSONResponse(content=body, headers={"etag": f'"{snapshot.revision}"'})
        # 非 ready：故障关闭；绝不回显缓存策略 / 非法正文。
        code = (
            "stored_policy_invalid"
            if state.problem_code == "stored_policy_invalid"
            else "storage_unavailable"
        )
        _raise_api_error(code, runtime=rt)

    @router.put(_POLICY_PATH)
    async def put_policy(
        request: Request,
        principal: ManagementPrincipal = Depends(write_auth),  # noqa: FAST002
        change_reason: str = Depends(require_management_change_reason),  # noqa: FAST002
        request_id: str = Depends(resolve_management_request_id),  # noqa: FAST002
    ) -> JSONResponse:
        """PUT policy（config:write）：严格校验 → 单次 strict CAS → 本地发布。

        缺变更原因 → 400（共享格式，不进白名单）；缺 / 弱 / 非法 If-Match →
        422 invalid_if_match（不写存储）；policy 非法 / 非对象 → 422
        invalid_policy（不写存储）；冲突 409、存储写失败 503
        （persisted=false）、已持久化未发布 503（persisted=true）、未分类
        内部错误 500（白名单，不回显异常）。审计记录安全 metadata：旧 / 新
        revision、规范化策略 SHA-256 指纹、persisted / published、结果码。
        """
        rt = _runtime()

        # 先严格校验 If-Match 与 body（不触存储、不进审计 span）。
        try:
            expected_revision = _parse_strong_etag(request.headers.get("if-match"))
        except ValueError:
            _raise_api_error("invalid_if_match", runtime=rt)
        try:
            payload = await request.json()
        except Exception:
            _raise_api_error("invalid_policy", runtime=rt)
        try:
            normalized = _normalize_policy(payload)
        except PolicyCompilationError:
            _raise_api_error("invalid_policy", runtime=rt)

        # 读取当前快照作为审计旧指纹 / 旧 revision（CAS 前快照）。
        manager = rt._manager
        old_revision: int | None = None
        old_fingerprint = ""
        if manager is not None:
            try:
                old_snapshot = manager.get_cached_versioned_snapshot()
                old_revision = old_snapshot.revision
                old_fingerprint = _policy_fingerprint(
                    _normalize_policy(old_snapshot.value.policy)
                )
            except (RuntimeError, PolicyCompilationError):
                # 旧快照读取失败或旧策略非法：old_fingerprint 保持安全空值，
                # 仍进入审计 span 交由控制面固定错误处理，绝不在此泄露非白名单 500。
                pass

        recorder = audit_recorder if audit_recorder is not None else _noop_recorder
        try:
            async with management_audit_span(
                principal=principal,
                request_id=request_id,
                reason=change_reason,
                action=_AUDIT_ACTION,
                resource=_AUDIT_RESOURCE,
                recorder=recorder,
            ) as span:
                span.metadata["old_revision"] = old_revision
                span.metadata["old_policy_fingerprint"] = old_fingerprint
                span.metadata["new_policy_fingerprint"] = _policy_fingerprint(normalized)
                span.metadata["result_code"] = "succeeded"
                span.metadata["persisted"] = False
                span.metadata["published"] = False

                try:
                    result: _ControlPlaneApplyResult = (
                        await rt._apply_persistent_update(
                            "policy",
                            normalized,
                            expected_revision=expected_revision,
                        )
                    )
                except _ControlPlaneConflictError:
                    span.metadata["new_revision"] = None
                    span.metadata["result_code"] = "revision_conflict"
                    span.status_code = status.HTTP_409_CONFLICT
                    _raise_api_error("revision_conflict", runtime=rt)
                except _ControlPlaneError:
                    # runtime_unavailable / storage_error 均收敛为 storage_unavailable
                    span.metadata["new_revision"] = None
                    span.metadata["result_code"] = "storage_unavailable"
                    span.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
                    _raise_api_error("storage_unavailable", runtime=rt)

                # 严格 CAS 写入成功；已持久化但未本地发布 → 503（persisted=true）。
                if not result.published_locally:
                    span.metadata["new_revision"] = result.new_revision
                    span.metadata["persisted"] = True
                    span.metadata["published"] = False
                    span.metadata["result_code"] = "snapshot_publish_failed"
                    span.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
                    _raise_api_error("snapshot_publish_failed", runtime=rt)

                # 本地已发布：返回 200 + 新 ETag + 规范化策略。
                span.metadata["new_revision"] = result.new_revision
                span.metadata["persisted"] = True
                span.metadata["published"] = True
                span.status_code = status.HTTP_200_OK
                current = rt._manager
                if current is None:
                    _raise_api_error("storage_unavailable", runtime=rt)
                published = current.get_cached_versioned_snapshot()
                body = {
                    "policy": _normalize_policy(published.value.policy),
                    "revision": published.revision,
                    "updated_at": published.updated_at.astimezone(UTC).isoformat(),
                }
                return JSONResponse(
                    content=body, headers={"etag": f'"{published.revision}"'}
                )
        except HTTPException:
            raise
        except Exception:
            # 审计启动 / 控制面之外的未分类内部错误 → 500（白名单，不回显异常）。
            _raise_api_error("internal_error", runtime=rt)

    @router.get(_STATUS_PATH, dependencies=[Depends(read_auth)])
    async def get_status() -> JSONResponse:
        """GET status（config:read）：只读进程内不可变运行时投影，零隐藏 I/O。

        ready / degraded / failed 恒 200；投影 TSK-217 完整固定字段集（精确键
        集 + UTC RFC 3339 时间 + 封闭低基数 telemetry），不返回 mode /
        group_ids / policy / fingerprint / 异常 / 重试计划。
        """
        rt = _runtime()
        return JSONResponse(content=_serialize_status(rt._project_status()))

    app.include_router(router)
    setattr(app.state, _REGISTRATION_FLAG, True)
