import asyncio
from contextlib import contextmanager

import src.web.routes.settings as settings_routes
from src.database import crud
from src.database.session import DatabaseSessionManager


def _build_fake_get_db(manager):
    @contextmanager
    def fake_get_db():
        with manager.session_scope() as session:
            yield session

    return fake_get_db


def test_delete_disabled_proxy_items_only_removes_disabled_entries(tmp_path, monkeypatch):
    manager = DatabaseSessionManager(f"sqlite:///{tmp_path}/settings-proxies.db")
    manager.create_tables()
    manager.migrate_tables()
    monkeypatch.setattr(settings_routes, "get_db", _build_fake_get_db(manager))

    with manager.session_scope() as session:
        crud.create_proxy(
            session,
            name="enabled-proxy",
            type="http",
            host="127.0.0.1",
            port=8001,
            enabled=True,
        )
        crud.create_proxy(
            session,
            name="disabled-proxy-a",
            type="http",
            host="127.0.0.1",
            port=8002,
            enabled=False,
        )
        crud.create_proxy(
            session,
            name="disabled-proxy-b",
            type="http",
            host="127.0.0.1",
            port=8003,
            enabled=False,
        )

    response = asyncio.run(settings_routes.delete_disabled_proxy_items())

    assert response == {
        "success": True,
        "deleted_count": 2,
        "message": "已删除 2 个禁用代理",
    }

    with manager.session_scope() as session:
        proxies = crud.get_proxies(session)
        assert len(proxies) == 1
        assert proxies[0].name == "enabled-proxy"
