from contextlib import contextmanager
from types import SimpleNamespace

from src.core.register import RegistrationResult
from src.database import crud
from src.database.models import Base
from src.database.session import DatabaseSessionManager
from src.services import EmailServiceType
from src.web.routes import registration as registration_routes


def test_run_sync_registration_task_persists_failure_result_payload(monkeypatch, tmp_path):
    db_path = tmp_path / "registration-task.db"
    manager = DatabaseSessionManager(f"sqlite:///{db_path}")
    Base.metadata.create_all(bind=manager.engine)

    with manager.session_scope() as session:
        crud.create_registration_task(session, task_uuid="task-400")

    @contextmanager
    def fake_get_db():
        session = manager.SessionLocal()
        try:
            yield session
        finally:
            session.close()

    payload = '{"error":"registration_disallowed","details":{"reason":"risk"}}'
    fake_engine = SimpleNamespace(save_to_database=lambda result: False)

    monkeypatch.setattr(registration_routes, "get_db", fake_get_db)
    monkeypatch.setattr(
        registration_routes,
        "_build_email_service_candidates",
        lambda db, service_type, actual_proxy_url, email_service_id, email_service_config: [{
            "service_type": EmailServiceType.DUCK_MAIL,
            "config": {},
            "db_service": None,
        }],
    )
    monkeypatch.setattr(
        registration_routes.EmailServiceFactory,
        "create",
        lambda *args, **kwargs: SimpleNamespace(service_type=EmailServiceType.DUCK_MAIL),
    )
    monkeypatch.setattr(
        registration_routes,
        "_run_registration_engine_attempt",
        lambda **kwargs: (
            fake_engine,
            RegistrationResult(success=False, error_message=payload, logs=[]),
            None,
            None,
            None,
        ),
    )

    registration_routes._run_sync_registration_task(
        task_uuid="task-400",
        email_service_type="duck_mail",
        proxy=None,
        email_service_config=None,
    )

    with manager.session_scope() as session:
        task = crud.get_registration_task_by_uuid(session, "task-400")

        assert task.status == "failed"
        assert task.error_message == payload
        assert task.result["success"] is False
        assert task.result["error_message"] == payload
        assert task.result["email_service"] == "duck_mail"
