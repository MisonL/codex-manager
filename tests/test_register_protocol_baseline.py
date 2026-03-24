import json
from datetime import date
from types import SimpleNamespace

import src.config.constants as constants_module
import src.core.register as register_module
from src.config.constants import OPENAI_PAGE_TYPES
from src.core.register import PhaseResult, RegistrationEngine, SignupFormResult
from src.services import EmailServiceType


class DummySettings:
    openai_client_id = "client-id"
    openai_auth_url = "https://auth.example.test"
    openai_token_url = "https://token.example.test"
    openai_redirect_uri = "https://callback.example.test"
    openai_scope = "openid profile email"


class FakeResponse:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = text

    def json(self):
        return self._payload


class FakeSession:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append({
            "url": url,
            **kwargs,
        })
        return self.response


def _build_engine(monkeypatch):
    monkeypatch.setattr(register_module, "get_settings", lambda: DummySettings())
    email_service = SimpleNamespace(service_type=EmailServiceType.DUCK_MAIL)
    return RegistrationEngine(email_service=email_service)


def test_submit_signup_form_uses_stable_protocol_body(monkeypatch):
    engine = _build_engine(monkeypatch)
    session = FakeSession(FakeResponse(
        status_code=200,
        payload={"page": {"type": OPENAI_PAGE_TYPES["PASSWORD_REGISTRATION"]}},
    ))
    engine.session = session
    engine.email = "tester@example.com"

    result = engine._submit_signup_form("did-1", None)

    assert result.success is True
    assert result.is_existing_account is False
    assert (
        session.calls[0]["data"]
        == '{"username":{"value":"tester@example.com","kind":"email"},"screen_hint":"signup"}'
    )


def test_register_password_uses_stable_protocol_body(monkeypatch):
    engine = _build_engine(monkeypatch)
    session = FakeSession(FakeResponse(status_code=200))
    engine.session = session
    engine.email = "tester@example.com"
    monkeypatch.setattr(engine, "_generate_password", lambda length=0: "Pass12345")

    success, password = engine._register_password()

    assert success is True
    assert password == "Pass12345"
    assert session.calls[0]["data"] == json.dumps(
        {
            "password": "Pass12345",
            "username": "tester@example.com",
        }
    )


def test_generate_random_user_info_samples_absolute_date_window(monkeypatch):
    monkeypatch.setattr(constants_module, "_get_worldwide_safe_today", lambda: date(2026, 3, 23))
    monkeypatch.setattr(constants_module.random, "choice", lambda names: "James")
    monkeypatch.setattr(constants_module.random, "randint", lambda start, end: end)

    user_info = constants_module.generate_random_user_info()

    assert user_info == {
        "name": "James",
        "birthdate": "2008-03-23",
    }
    assert constants_module.calculate_age_from_birthdate(
        user_info["birthdate"],
        reference_date=date(2026, 3, 23),
    ) == 18


def test_create_user_account_stops_before_post_when_local_age_check_fails(monkeypatch):
    engine = _build_engine(monkeypatch)
    session = FakeSession(FakeResponse(status_code=200))
    engine.session = session
    monkeypatch.setattr(
        register_module,
        "generate_random_user_info",
        lambda: {"name": "Teen", "birthdate": "2010-01-01"},
    )
    monkeypatch.setattr(register_module, "calculate_age_from_birthdate", lambda birthdate: 17)

    success = engine._create_user_account()

    assert success is False
    assert session.calls == []
    assert engine._last_create_account_error == "本地年龄校验失败: birthdate=2010-01-01, age=17"


def test_run_propagates_full_create_account_400_payload(monkeypatch):
    engine = _build_engine(monkeypatch)
    full_payload = '{"error":"registration_disallowed","details":{"reason":"risk"}}'
    engine.session = FakeSession(FakeResponse(status_code=400, text=full_payload))
    monkeypatch.setattr(register_module, "generate_random_user_info", lambda: {
        "name": "Adult",
        "birthdate": "2000-02-20",
    })
    monkeypatch.setattr(register_module, "calculate_age_from_birthdate", lambda birthdate: 26)
    monkeypatch.setattr(engine, "_check_ip_location", lambda: (True, "United States"))

    def fake_phase_email_prepare():
        engine.email = "tester@example.com"
        return True

    monkeypatch.setattr(engine, "_phase_email_prepare", fake_phase_email_prepare)
    monkeypatch.setattr(engine, "_init_session", lambda: True)
    monkeypatch.setattr(engine, "_start_oauth", lambda: True)
    monkeypatch.setattr(engine, "_get_device_id", lambda: "did-1")
    monkeypatch.setattr(engine, "_check_sentinel", lambda did: None)
    monkeypatch.setattr(engine, "_submit_signup_form", lambda did, sen_token: SignupFormResult(success=True))
    monkeypatch.setattr(engine, "_register_password", lambda: (True, "Pass12345"))
    monkeypatch.setattr(engine, "_send_verification_code", lambda: True)
    monkeypatch.setattr(
        engine,
        "_phase_otp_secondary",
        lambda ctx, started_at: ("123456", PhaseResult(phase="otp_secondary", success=True)),
    )
    monkeypatch.setattr(engine, "_validate_verification_code", lambda code: True)

    result = engine.run()

    assert result.success is False
    assert result.error_message == full_payload
