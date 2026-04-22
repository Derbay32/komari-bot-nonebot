"""Uptime Kuma API 客户端封装。"""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import TYPE_CHECKING, Any, cast

from nonebot import logger

from .renderer import MaintenanceInfo, MonitorInfo, StatusData

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable

    from uptime_kuma_api import UptimeKumaApi

    from .config_schema import StatusConfig


def _local_timezone():
    return datetime.now().astimezone().tzinfo


def _aware_datetime_max() -> datetime:
    return datetime.max.replace(tzinfo=_local_timezone())


def _aware_datetime_min() -> datetime:
    return datetime.min.replace(tzinfo=_local_timezone())


class StatusQueryError(RuntimeError):
    """状态查询失败。"""


def _parse_datetime(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    if not normalized:
        return None

    iso_candidate = normalized.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(iso_candidate)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=_local_timezone())
    return parsed


def _safe_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return round(value)
    return None


def _safe_float(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _monitor_status_text(status: object) -> tuple[str, str]:
    raw_name = getattr(status, "name", None)
    raw_value = getattr(status, "value", status)

    if raw_name == "UP" or raw_value == 1:
        return "🟢", "正常"
    if raw_name == "DOWN" or raw_value == 0:
        return "🔴", "故障"
    if raw_name == "PENDING" or raw_value == 2:
        return "🟡", "待检测"
    if raw_name == "MAINTENANCE" or raw_value == 3:
        return "🟠", "维护中"
    return "⚪", "未知"


def _maintenance_status_label(status: object) -> str:
    if not isinstance(status, str):
        return "已计划"
    normalized = status.strip().lower()
    mapping = {
        "active": "进行中",
        "in-progress": "进行中",
        "under-maintenance": "进行中",
        "pending": "已计划",
        "scheduled": "已计划",
        "upcoming": "已计划",
    }
    return mapping.get(normalized, "已计划")


def _should_display_maintenance(
    status: object, start_at: datetime | None, end_at: datetime | None
) -> bool:
    if isinstance(status, str):
        normalized = status.strip().lower()
        if normalized in {"ended", "inactive", "paused", "deleted", "cancelled"}:
            return False
        if normalized in {
            "active",
            "in-progress",
            "under-maintenance",
            "pending",
            "scheduled",
            "upcoming",
        }:
            return True

    now = datetime.now().astimezone()
    if end_at is not None and end_at < now:
        return False
    return start_at is not None or end_at is not None


def _pick_maintenance_window(
    maintenance: dict[str, Any],
) -> tuple[datetime | None, datetime | None]:
    timeslots = maintenance.get("timeslotList")
    if isinstance(timeslots, list):
        windows: list[tuple[datetime | None, datetime | None]] = []
        for item in timeslots:
            if not isinstance(item, dict):
                continue
            windows.append(
                (
                    _parse_datetime(item.get("startDate")),
                    _parse_datetime(item.get("endDate")),
                )
            )
        valid_windows = [
            window
            for window in windows
            if window[0] is not None or window[1] is not None
        ]
        if valid_windows:
            valid_windows.sort(
                key=lambda item: item[0] or item[1] or _aware_datetime_max()
            )
            now = datetime.now().astimezone()
            for start_at, end_at in valid_windows:
                if end_at is not None and end_at >= now:
                    return start_at, end_at
            return valid_windows[0]

    date_range = maintenance.get("dateRange")
    if isinstance(date_range, list) and len(date_range) >= 2:
        return _parse_datetime(date_range[0]), _parse_datetime(date_range[1])
    return None, None


def _extract_status_page_monitor_ids(status_page: dict[str, Any]) -> list[int]:
    monitor_ids: list[int] = []
    groups = status_page.get("publicGroupList")
    if not isinstance(groups, list):
        return monitor_ids
    for group in groups:
        if not isinstance(group, dict):
            continue
        monitor_list = group.get("monitorList")
        if not isinstance(monitor_list, list):
            continue
        for monitor in monitor_list:
            if not isinstance(monitor, dict):
                continue
            monitor_id = _safe_int(monitor.get("id"))
            if monitor_id is not None:
                monitor_ids.append(monitor_id)
    return monitor_ids


def _extract_status_page_maintenance_ids(status_page: dict[str, Any]) -> set[int]:
    maintenance_ids: set[int] = set()
    raw_maintenances = status_page.get("maintenanceList")
    if not isinstance(raw_maintenances, list):
        return maintenance_ids
    for maintenance in raw_maintenances:
        if not isinstance(maintenance, dict):
            continue
        maintenance_id = _safe_int(maintenance.get("id"))
        if maintenance_id is not None:
            maintenance_ids.add(maintenance_id)
    return maintenance_ids


class UptimeKumaClient:
    """Uptime Kuma 状态查询客户端。"""

    def __init__(self, config_getter: Callable[[], StatusConfig]) -> None:
        self._config_getter = config_getter
        self._cache: StatusData | None = None
        self._cache_expires_at: float = 0.0
        self._lock = asyncio.Lock()

    async def fetch_status(self) -> StatusData:
        """获取完整状态信息。"""
        config = self._config_getter()
        loop = asyncio.get_running_loop()
        now = loop.time()
        if self._cache is not None and now < self._cache_expires_at:
            return self._cache

        async with self._lock:
            now = loop.time()
            if self._cache is not None and now < self._cache_expires_at:
                return self._cache

            try:
                status_data = await asyncio.to_thread(self._fetch_status_sync, config)
            except Exception as exc:
                logger.warning(f"[Komari Status] 状态查询失败: {exc}")
                if self._cache is not None:
                    logger.warning("[Komari Status] 使用缓存结果回退")
                    return self._cache
                raise StatusQueryError("状态查询暂时不可用，请稍后再试") from exc

            self._cache = status_data
            self._cache_expires_at = loop.time() + max(config.cache_ttl, 0)
            return status_data

    def clear_cache(self) -> None:
        """清理内存缓存。"""
        self._cache = None
        self._cache_expires_at = 0.0

    def _fetch_status_sync(self, config: StatusConfig) -> StatusData:
        from uptime_kuma_api import UptimeKumaApi

        page_title = config.status_page_slug
        page_monitor_ids: list[int] = []
        page_maintenance_ids: set[int] = set()
        all_maintenances: list[dict[str, Any]]

        with UptimeKumaApi(
            config.uptime_kuma_url,
            timeout=float(config.request_timeout),
            ssl_verify=config.ssl_verify,
        ) as api:
            api.login(config.uptime_kuma_username, config.uptime_kuma_password)

            try:
                status_page = cast(
                    "dict[str, Any]", api.get_status_page(config.status_page_slug)
                )
            except Exception as exc:
                logger.warning(
                    f"[Komari Status] 获取状态页失败，改为展示全部监控: {exc}"
                )
                status_page = {}
            else:
                page_title = str(status_page.get("title") or page_title)
                page_monitor_ids = _extract_status_page_monitor_ids(status_page)
                page_maintenance_ids = _extract_status_page_maintenance_ids(status_page)

            raw_monitors = cast("list[dict[str, Any]]", api.get_monitors())
            raw_heartbeats = cast(
                "dict[Any, list[dict[str, Any]]]", api.get_heartbeats()
            )
            raw_uptime = cast("dict[Any, dict[Any, Any]]", api.uptime())
            all_maintenances = cast("list[dict[str, Any]]", api.get_maintenances())

            if status_page and not page_maintenance_ids:
                page_maintenance_ids = self._resolve_page_maintenance_ids(
                    api, all_maintenances, status_page
                )

        monitors = self._build_monitor_infos(
            raw_monitors, raw_heartbeats, raw_uptime, page_monitor_ids
        )
        maintenances = self._build_maintenance_infos(
            all_maintenances, page_maintenance_ids
        )
        return StatusData(
            title=page_title,
            monitors=monitors,
            maintenances=maintenances,
            status_page_url=config.status_page_url,
            fetched_at=datetime.now().astimezone(),
        )

    def _resolve_page_maintenance_ids(
        self,
        api: UptimeKumaApi,
        maintenances: Iterable[dict[str, Any]],
        status_page: dict[str, Any],
    ) -> set[int]:
        page_id = _safe_int(status_page.get("id"))
        if page_id is None:
            return set()

        maintenance_ids: set[int] = set()
        for maintenance in maintenances:
            maintenance_id = _safe_int(maintenance.get("id"))
            if maintenance_id is None:
                continue
            try:
                related_pages = cast(
                    "list[dict[str, Any]]",
                    api.get_status_page_maintenance(maintenance_id),
                )
            except Exception:
                continue
            if any(_safe_int(page.get("id")) == page_id for page in related_pages):
                maintenance_ids.add(maintenance_id)
        return maintenance_ids

    def _build_monitor_infos(
        self,
        monitors: list[dict[str, Any]],
        heartbeats: dict[Any, list[dict[str, Any]]],
        uptime_data: dict[Any, dict[Any, Any]],
        page_monitor_ids: list[int],
    ) -> list[MonitorInfo]:
        heartbeat_map = {str(key): value for key, value in heartbeats.items()}
        uptime_map = {str(key): value for key, value in uptime_data.items()}
        included_ids = {str(item) for item in page_monitor_ids}

        raw_items: list[tuple[int, MonitorInfo]] = []
        for index, monitor in enumerate(monitors):
            monitor_id = _safe_int(monitor.get("id"))
            if monitor_id is None:
                continue
            if included_ids and str(monitor_id) not in included_ids:
                continue

            monitor_heartbeats = heartbeat_map.get(str(monitor_id), [])
            latest_heartbeat = self._get_latest_heartbeat(monitor_heartbeats)
            status_icon, status_text = _monitor_status_text(
                latest_heartbeat.get("status") if latest_heartbeat else None
            )

            monitor_uptime = uptime_map.get(str(monitor_id), {})
            raw_items.append(
                (
                    index,
                    MonitorInfo(
                        id=monitor_id,
                        name=str(monitor.get("name") or f"监控项 {monitor_id}"),
                        status_text=status_text,
                        status_icon=status_icon,
                        response_time_ms=_safe_int(
                            latest_heartbeat.get("ping") if latest_heartbeat else None
                        ),
                        uptime_24h=_safe_float(
                            monitor_uptime.get(24) or monitor_uptime.get("24")
                        ),
                        uptime_30d=_safe_float(
                            monitor_uptime.get(720) or monitor_uptime.get("720")
                        ),
                    ),
                )
            )

        if included_ids:
            order_map = {
                str(monitor_id): position
                for position, monitor_id in enumerate(page_monitor_ids)
            }
            raw_items.sort(key=lambda item: order_map.get(str(item[1].id), item[0]))
        return [item[1] for item in raw_items]

    def _build_maintenance_infos(
        self,
        maintenances: list[dict[str, Any]],
        page_maintenance_ids: set[int],
    ) -> list[MaintenanceInfo]:
        result: list[MaintenanceInfo] = []
        for maintenance in maintenances:
            maintenance_id = _safe_int(maintenance.get("id"))
            if maintenance_id is None:
                continue
            if page_maintenance_ids and maintenance_id not in page_maintenance_ids:
                continue

            start_at, end_at = _pick_maintenance_window(maintenance)
            status = maintenance.get("status")
            if not _should_display_maintenance(status, start_at, end_at):
                continue

            description = maintenance.get("description")
            result.append(
                MaintenanceInfo(
                    id=maintenance_id,
                    title=str(maintenance.get("title") or f"维护 {maintenance_id}"),
                    description=str(description).strip()
                    if isinstance(description, str) and description.strip()
                    else None,
                    status_label=_maintenance_status_label(status),
                    start_at=start_at,
                    end_at=end_at,
                )
            )

        result.sort(
            key=lambda item: item.start_at or item.end_at or _aware_datetime_max()
        )
        return result

    def _get_latest_heartbeat(
        self, heartbeats: list[dict[str, Any]]
    ) -> dict[str, Any] | None:
        if not heartbeats:
            return None
        return max(
            heartbeats,
            key=lambda item: (
                _parse_datetime(item.get("time")) or _aware_datetime_min(),
                _safe_int(item.get("id")) or 0,
            ),
        )


_client_state: dict[str, UptimeKumaClient | None] = {"instance": None}


def configure_status_client(
    config_getter: Callable[[], StatusConfig],
) -> UptimeKumaClient:
    """配置全局客户端实例。"""
    client = UptimeKumaClient(config_getter)
    _client_state["instance"] = client
    return client


def get_status_client() -> UptimeKumaClient:
    """获取全局客户端实例。"""
    client = _client_state["instance"]
    if client is None:
        msg = "状态客户端尚未初始化"
        raise RuntimeError(msg)
    return client
