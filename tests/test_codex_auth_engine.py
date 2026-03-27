from types import SimpleNamespace

import src.core.register as register_module
from src.core.codex_auth import CodexAuthEngine
from src.services import EmailServiceType


class DummySettings:
    openai_client_id = "client-id"
    openai_auth_url = "https://auth.example.test"
    openai_token_url = "https://token.example.test"
    openai_redirect_uri = "https://callback.example.test"
    openai_scope = "openid profile email"


def _build_engine(monkeypatch, assigned_workspace_id="ws-assigned"):
    monkeypatch.setattr(register_module, "get_settings", lambda: DummySettings())
    email_service = SimpleNamespace(service_type=EmailServiceType.DUCK_MAIL)
    return CodexAuthEngine(
        email_service=email_service,
        email="tester@example.com",
        password="Pass12345",
        assigned_workspace_id=assigned_workspace_id,
    )


def test_select_workspace_for_account_prefers_assigned_workspace(monkeypatch):
    engine = _build_engine(monkeypatch)

    selected_workspace_id = engine._select_workspace_for_account("ws-consent")

    assert selected_workspace_id == "ws-assigned"


def test_resolve_workspace_authorization_injects_assigned_workspace(monkeypatch):
    engine = _build_engine(monkeypatch)
    engine.oauth_start = SimpleNamespace(auth_url="https://auth.example.test/authorize")
    engine._pending_continue_url = "https://auth.example.test/consent"
    chosen_workspace = {}

    monkeypatch.setattr(
        engine,
        "_session_get",
        lambda *args, **kwargs: SimpleNamespace(
            url="https://auth.openai.com/sign-in-with-chatgpt/codex/consent",
            text="<html></html>",
        ),
    )
    monkeypatch.setattr(engine, "_log_timed_http_result", lambda *args, **kwargs: None)
    monkeypatch.setattr(engine, "_extract_workspace_id_from_response", lambda **kwargs: None)
    monkeypatch.setattr(
        engine,
        "_select_workspace",
        lambda workspace_id: (
            chosen_workspace.setdefault("workspace_id", workspace_id),
            "https://continue.example.test",
        )[1],
    )
    monkeypatch.setattr(engine, "_follow_redirects", lambda url: "https://callback.example.test?code=1&state=2")

    phase_result = engine._resolve_workspace_authorization()

    assert phase_result.success is True
    assert chosen_workspace["workspace_id"] == "ws-assigned"
    assert phase_result.metadata["assigned_workspace_id"] == "ws-assigned"
