import time
from types import SimpleNamespace

from fastapi.testclient import TestClient

from src.web.app import create_app
from src.web.routes import system as system_routes


def test_system_health_route_returns_expected_fields(monkeypatch):
    monkeypatch.setattr(
        system_routes,
        "get_settings",
        lambda: SimpleNamespace(app_version="9.9.9"),
    )
    monkeypatch.setattr(
        system_routes,
        "_check_database_health",
        lambda: {
            "connected": True,
            "latency_ms": 3.2,
            "status": "ok",
        },
    )

    async def fake_measure_event_loop_health():
        return {
            "running": True,
            "closed": False,
            "lag_ms": 1.25,
            "status": "ok",
        }

    monkeypatch.setattr(system_routes, "_measure_event_loop_health", fake_measure_event_loop_health)
    monkeypatch.setattr(
        system_routes.task_manager,
        "snapshot",
        lambda: {
            "active_task_count": 2,
            "active_tasks": ["task-a", "task-b"],
            "tracked_task_count": 4,
            "finished_task_count": 2,
            "active_batch_count": 1,
            "active_batches": ["batch-a"],
            "tracked_batch_count": 1,
            "websocket_connection_count": 2,
            "log_buffer_count": 3,
            "max_worker_count": 50,
        },
    )

    app = create_app()
    app.state.started_at = time.time() - 65

    with TestClient(app) as client:
        response = client.get("/api/system/health")

    assert response.status_code == 200
    payload = response.json()

    assert payload["status"] == "ok"
    assert payload["version"] == "9.9.9"
    assert payload["uptime_seconds"] >= 65
    assert payload["uptime_hms"].startswith("00:01:")
    assert payload["database"]["connected"] is True
    assert payload["database"]["latency_ms"] == 3.2
    assert payload["memory_active_tasks"] == 2
    assert payload["task_manager"]["active_task_count"] == 2
    assert payload["event_loop"]["status"] == "ok"
    assert payload["issues"] == []
