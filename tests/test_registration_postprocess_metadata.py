from contextlib import contextmanager
from types import SimpleNamespace

from src.database import crud
from src.database.session import DatabaseSessionManager
from src.database.models import RegistrationTask
from src.core.register import RegistrationResult
from src.web.routes import registration as registration_routes


class DummyTaskManager:
    def is_cancelled(self, task_uuid):
        return False

    def update_status(self, *args, **kwargs):
        return None

    def create_log_callback(self, task_uuid, prefix="", batch_id=""):
        def callback(message):
            return None
        return callback


def _build_manager(tmp_path):
    manager = DatabaseSessionManager(f"sqlite:///{tmp_path}/test.db")
    manager.create_tables()
    manager.migrate_tables()
    return manager


def test_newapi_upload_results_are_persisted_in_postprocess_metadata(monkeypatch, tmp_path):
    manager = _build_manager(tmp_path)

    with manager.session_scope() as session:
        service = crud.create_email_service(
            session,
            service_type="tempmail",
            name="tempmail-db",
            config={"base_url": "https://mail.example/api"},
        )
        newapi_service = crud.create_newapi_service(
            session,
            name="newapi-primary",
            api_url="https://newapi.example.test",
            api_key="root-token",
            enabled=True,
        )
        crud.create_registration_task(session, task_uuid="task-newapi-postprocess")
        email_service_id = service.id
        newapi_service_id = newapi_service.id

    @contextmanager
    def fake_get_db():
        session = manager.SessionLocal()
        try:
            yield session
        finally:
            session.close()

    class FakeRegistrationEngine:
        def __init__(self, email_service, proxy_url=None, callback_logger=None, status_callback=None, task_uuid=None, **kwargs):
            self.email_service = email_service

        def run(self):
            return RegistrationResult(
                success=True,
                email="newapi@example.com",
                password="Pass12345",
                access_token="access-token",
                workspace_id="ws-123",
                metadata={},
            )

        def save_to_database(self, result):
            with manager.session_scope() as session:
                crud.create_account(
                    session,
                    email=result.email,
                    email_service="tempmail",
                    password=result.password,
                    access_token=result.access_token,
                    workspace_id=result.workspace_id,
                )
            return True

    upload_calls = []

    monkeypatch.setattr(registration_routes, "get_db", fake_get_db)
    monkeypatch.setattr(registration_routes, "task_manager", DummyTaskManager())
    monkeypatch.setattr(registration_routes, "RegistrationEngine", FakeRegistrationEngine)
    monkeypatch.setattr(
        registration_routes.EmailServiceFactory,
        "create",
        lambda service_type, config, name=None: SimpleNamespace(
            service_type=service_type,
            config=config,
            name=name or service_type.value,
        ),
    )
    monkeypatch.setattr(registration_routes, "update_proxy_usage", lambda db, proxy_id: None)

    import src.core.upload.newapi_upload as newapi_upload_module

    def fake_upload_to_newapi(account, api_url, api_key, channel_type=None, channel_base_url=None, channel_models=None):
        upload_calls.append((account.email, api_url))
        return True, "上传成功"

    monkeypatch.setattr(newapi_upload_module, "upload_to_newapi", fake_upload_to_newapi)

    registration_routes._run_sync_registration_task(
        task_uuid="task-newapi-postprocess",
        email_service_type="tempmail",
        proxy=None,
        email_service_config=None,
        email_service_id=email_service_id,
        auto_upload_newapi=True,
        newapi_service_ids=[newapi_service_id],
    )

    assert upload_calls == [("newapi@example.com", "https://newapi.example.test")]

    with manager.session_scope() as session:
        task = crud.get_registration_task_by_uuid(session, "task-newapi-postprocess")
        assert task is not None
        attempts = task.result["metadata"]["postprocess"]["newapi"]["attempts"]
        assert attempts == [
            {
                "service_id": newapi_service_id,
                "service_name": "newapi-primary",
                "success": True,
                "error_message": "",
                "channel_type": 57,
            }
        ]
