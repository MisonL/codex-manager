"""
系统级传感器路由
"""

import asyncio
import time
from typing import Any, Dict, List

from fastapi import APIRouter, Request
from sqlalchemy import text

from ...config.settings import get_settings
from ...database.session import get_db
from ..task_manager import task_manager

router = APIRouter()

EVENT_LOOP_DEGRADED_MS = 100.0


def _format_uptime(uptime_seconds: float) -> str:
    total_seconds = max(0, int(uptime_seconds))
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def _check_database_health() -> Dict[str, Any]:
    started_at = time.perf_counter()
    try:
        with get_db() as db:
            db.execute(text("SELECT 1"))
        latency_ms = round((time.perf_counter() - started_at) * 1000, 2)
        return {
            "connected": True,
            "latency_ms": latency_ms,
            "status": "ok",
        }
    except Exception as exc:
        latency_ms = round((time.perf_counter() - started_at) * 1000, 2)
        return {
            "connected": False,
            "latency_ms": latency_ms,
            "status": "offline",
            "error": str(exc),
        }


async def _measure_event_loop_health() -> Dict[str, Any]:
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError as exc:
        return {
            "running": False,
            "closed": True,
            "lag_ms": None,
            "status": "offline",
            "error": str(exc),
        }

    if loop.is_closed():
        return {
            "running": False,
            "closed": True,
            "lag_ms": None,
            "status": "offline",
            "error": "event loop is closed",
        }

    started_at = time.perf_counter()
    marker = loop.create_future()
    loop.call_soon(marker.set_result, None)
    await marker
    lag_ms = round((time.perf_counter() - started_at) * 1000, 3)
    status = "degraded" if lag_ms >= EVENT_LOOP_DEGRADED_MS else "ok"
    return {
        "running": True,
        "closed": False,
        "lag_ms": lag_ms,
        "status": status,
    }


def _collect_issues(
    database_health: Dict[str, Any],
    event_loop_health: Dict[str, Any],
    manager_snapshot: Dict[str, Any],
) -> List[str]:
    issues: List[str] = []

    if not database_health["connected"]:
        issues.append("database_unreachable")

    if event_loop_health["status"] == "offline":
        issues.append("event_loop_unavailable")
    elif event_loop_health["status"] == "degraded":
        issues.append("event_loop_lag_high")

    max_worker_count = manager_snapshot.get("max_worker_count") or 0
    active_task_count = manager_snapshot.get("active_task_count", 0)
    if max_worker_count and active_task_count >= max_worker_count:
        issues.append("task_worker_pool_saturated")

    if manager_snapshot.get("websocket_connection_count", 0) == 0 and active_task_count > 0:
        issues.append("tasks_running_without_websocket_observer")

    return issues


@router.get("/health")
async def get_system_health(request: Request) -> Dict[str, Any]:
    settings = get_settings()
    started_at = getattr(request.app.state, "started_at", time.time())
    uptime_seconds = round(max(0.0, time.time() - started_at), 2)
    database_health = _check_database_health()
    event_loop_health = await _measure_event_loop_health()
    manager_snapshot = task_manager.snapshot()
    issues = _collect_issues(database_health, event_loop_health, manager_snapshot)

    system_status = "ok"
    if not database_health["connected"] or event_loop_health["status"] == "offline":
        system_status = "offline"
    elif issues:
        system_status = "degraded"

    return {
        "status": system_status,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "uptime_seconds": uptime_seconds,
        "uptime_hms": _format_uptime(uptime_seconds),
        "version": settings.app_version,
        "database": database_health,
        "task_manager": manager_snapshot,
        "memory_active_tasks": manager_snapshot["active_task_count"],
        "event_loop": event_loop_health,
        "issues": issues,
    }
