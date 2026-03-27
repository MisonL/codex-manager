from types import SimpleNamespace

from src.database import crud
from src.database.session import DatabaseSessionManager
from src.core.register import RegistrationResult
import src.web.routes.registration as registration_routes


def _build_manager(tmp_path):
    manager = DatabaseSessionManager(f"sqlite:///{tmp_path}/test.db")
    manager.create_tables()
    manager.migrate_tables()
    return manager


def test_codex_auth_hook_updates_account_on_success(monkeypatch, tmp_path):
    manager = _build_manager(tmp_path)
    log_messages = []

    class FakeCodexAuthEngine:
        @classmethod
        def from_registration_engine(cls, engine, *, email, password, assigned_workspace_id=None):
            return cls()

        def authorize(self):
            return SimpleNamespace(
                success=True,
                workspace_id="ws-hook",
                account_id="acct-hook",
                access_token="access-hook",
                refresh_token="refresh-hook",
                id_token="id-hook",
                session_token="session-hook",
                cse_health_status="healthy",
                metadata={
                    "authorized_at": "2026-03-26T12:00:00",
                    "execution_mode": "curl_cffi",
                },
                error_message="",
            )

    monkeypatch.setattr(registration_routes, "CodexAuthEngine", FakeCodexAuthEngine)

    with manager.session_scope() as session:
        account = crud.create_account(
            session,
            email="hook@example.com",
            email_service="tempmail",
            password="Pass12345",
        )
        result = RegistrationResult(
            success=True,
            email=account.email,
            password=account.password,
            metadata={},
        )
        saved_account = crud.get_account_by_id(session, account.id)
        updated_account = registration_routes._run_codex_official_auth_hook(
            session,
            registration_engine=SimpleNamespace(
                email_service=None,
                proxy_url=None,
                callback_logger=None,
                status_callback=None,
                task_uuid="task-1",
                email_info={},
            ),
            saved_account=saved_account,
            result=result,
            log_callback=log_messages.append,
        )

        assert updated_account is not None
        assert updated_account.codex_auth_status == "authorized"
        assert updated_account.cse_health_status == "healthy"
        assert updated_account.workspace_id == "ws-hook"
        assert updated_account.access_token == "access-hook"
        assert result.metadata["codex_auth_hook"]["success"] is True
        assert result.metadata["postprocess"]["codex_auth"]["success"] is True
        assert result.metadata["postprocess"]["codex_auth"]["workspace_id"] == "ws-hook"
        assert any("官方授权成功" in message for message in log_messages)


def test_codex_auth_hook_marks_degraded_on_failure(monkeypatch, tmp_path):
    manager = _build_manager(tmp_path)
    log_messages = []

    class FakeCodexAuthEngine:
        @classmethod
        def from_registration_engine(cls, engine, *, email, password, assigned_workspace_id=None):
            return cls()

        def authorize(self):
            return SimpleNamespace(
                success=False,
                workspace_id="",
                account_id="",
                access_token="",
                refresh_token="",
                id_token="",
                session_token="",
                cse_health_status="degraded",
                metadata={},
                error_message="workspace authorization failed",
            )

    monkeypatch.setattr(registration_routes, "CodexAuthEngine", FakeCodexAuthEngine)

    with manager.session_scope() as session:
        account = crud.create_account(
            session,
            email="hook-failed@example.com",
            email_service="tempmail",
            password="Pass12345",
        )
        result = RegistrationResult(
            success=True,
            email=account.email,
            password=account.password,
            metadata={},
        )
        saved_account = crud.get_account_by_id(session, account.id)
        updated_account = registration_routes._run_codex_official_auth_hook(
            session,
            registration_engine=SimpleNamespace(
                email_service=None,
                proxy_url=None,
                callback_logger=None,
                status_callback=None,
                task_uuid="task-2",
                email_info={},
            ),
            saved_account=saved_account,
            result=result,
            log_callback=log_messages.append,
        )

        assert updated_account is not None
        assert updated_account.codex_auth_status == "failed"
        assert updated_account.cse_health_status == "degraded"
        assert updated_account.extra_data["codex_auth_error"] == "workspace authorization failed"
        assert result.metadata["codex_auth_hook"]["success"] is False
        assert result.metadata["postprocess"]["codex_auth"]["success"] is False
        assert result.metadata["postprocess"]["codex_auth"]["error_message"] == "workspace authorization failed"
        assert any("官方授权失败" in message for message in log_messages)
