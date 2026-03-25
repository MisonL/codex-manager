import json
from datetime import date
from types import SimpleNamespace

import src.config.constants as constants_module
import src.core.register as register_module
from src.config.constants import OPENAI_PAGE_TYPES
from src.core.register import (
    ERROR_INVALID_AUTH_STEP,
    PHASE_ACCOUNT_CREATE,
    PHASE_EMAIL_PREPARE,
    PHASE_IP_CHECK,
    PHASE_OAUTH_CALLBACK,
    PHASE_OAUTH_REENTER,
    PHASE_OTP_PRIMARY,
    PHASE_OTP_SECONDARY,
    PHASE_SIGNUP_PASSWORD,
    PHASE_SIGNUP_SUBMIT,
    PHASE_WORKSPACE_RESOLVE,
    PhaseResult,
    RegistrationEngine,
    SignupFormResult,
)
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
        return PhaseResult(phase=PHASE_EMAIL_PREPARE, success=True)

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
        lambda: (
            setattr(engine, "_email_verified", True)
            or PhaseResult(phase=PHASE_OTP_SECONDARY, success=True)
        ),
    )

    result = engine.run()

    assert result.success is False
    assert result.error_message == full_payload


def test_phase_otp_secondary_marks_email_verified_before_account_create(monkeypatch):
    engine = _build_engine(monkeypatch)
    engine._otp_sent_at = 77.0

    monkeypatch.setattr(
        engine,
        "_await_secondary_otp_code",
        lambda context, started_at, record_phase=False: (
            "654321",
            PhaseResult(
                phase=PHASE_OTP_SECONDARY,
                success=True,
                metadata={"otp_sent_at": 77.0},
            ),
        ),
    )
    monkeypatch.setattr(
        engine,
        "_validate_verification_code_and_get_continue_url",
        lambda code: (True, "https://continue.example.test"),
    )

    phase_result = engine._phase_otp_secondary()

    assert phase_result.success is True
    assert engine._email_verified is True
    assert engine._pending_continue_url == "https://continue.example.test"
    assert phase_result.metadata["email_verified"] is True


def test_phase_account_create_fails_when_email_not_verified(monkeypatch):
    engine = _build_engine(monkeypatch)
    create_calls = []

    def fake_create_user_account():
        create_calls.append("called")
        return True

    monkeypatch.setattr(engine, "_create_user_account", fake_create_user_account)

    phase_result = engine._phase_account_create()

    assert phase_result.success is False
    assert phase_result.error_message == "邮箱尚未完成验证，禁止创建用户账户"
    assert phase_result.error_code == ERROR_INVALID_AUTH_STEP
    assert phase_result.metadata["email_verified"] is False
    assert phase_result.metadata["otp_secondary_completed"] is False
    assert create_calls == []


def test_run_jumps_to_workspace_phase_when_oauth_reenter_is_retryable(monkeypatch):
    engine = _build_engine(monkeypatch)
    engine.session = SimpleNamespace(
        cookies=SimpleNamespace(get=lambda name: None),
    )
    calls = []

    def phase_success(name, **metadata):
        return PhaseResult(phase=name, success=True, metadata=metadata)

    def phase_ip_check():
        calls.append(PHASE_IP_CHECK)
        return phase_success(PHASE_IP_CHECK)

    def phase_email_prepare():
        calls.append(PHASE_EMAIL_PREPARE)
        engine.email = "tester@example.com"
        return phase_success(PHASE_EMAIL_PREPARE)

    def phase_signup_submit():
        calls.append(PHASE_SIGNUP_SUBMIT)
        return phase_success(PHASE_SIGNUP_SUBMIT)

    def phase_signup_password():
        calls.append(PHASE_SIGNUP_PASSWORD)
        engine.password = "Pass12345"
        return phase_success(PHASE_SIGNUP_PASSWORD)

    def phase_otp_primary():
        calls.append(PHASE_OTP_PRIMARY)
        return phase_success(PHASE_OTP_PRIMARY)

    def phase_account_create():
        calls.append(PHASE_ACCOUNT_CREATE)
        return phase_success(PHASE_ACCOUNT_CREATE)

    def phase_oauth_reenter():
        calls.append(PHASE_OAUTH_REENTER)
        return PhaseResult(
            phase=PHASE_OAUTH_REENTER,
            success=False,
            retryable=True,
            next_action=PHASE_WORKSPACE_RESOLVE,
            error_message="fallback to workspace",
        )

    def phase_otp_secondary():
        calls.append(PHASE_OTP_SECONDARY)
        return PhaseResult(phase=PHASE_OTP_SECONDARY, success=True)

    def phase_workspace_resolve():
        calls.append(PHASE_WORKSPACE_RESOLVE)
        engine._resolved_workspace_id = "ws-123"
        engine._callback_url = "https://callback.example.test?code=abc&state=xyz"
        return phase_success(PHASE_WORKSPACE_RESOLVE)

    def phase_oauth_callback():
        calls.append(PHASE_OAUTH_CALLBACK)
        engine._token_info = {
            "account_id": "acct-1",
            "access_token": "access-token",
            "refresh_token": "refresh-token",
            "id_token": "id-token",
        }
        return phase_success(PHASE_OAUTH_CALLBACK)

    monkeypatch.setattr(engine, "_phase_ip_check", phase_ip_check)
    monkeypatch.setattr(engine, "_phase_email_prepare", phase_email_prepare)
    monkeypatch.setattr(engine, "_phase_signup_submit", phase_signup_submit)
    monkeypatch.setattr(engine, "_phase_signup_password", phase_signup_password)
    monkeypatch.setattr(engine, "_phase_otp_primary", phase_otp_primary)
    monkeypatch.setattr(engine, "_phase_account_create", phase_account_create)
    monkeypatch.setattr(engine, "_phase_oauth_reenter", phase_oauth_reenter)
    monkeypatch.setattr(engine, "_phase_otp_secondary", phase_otp_secondary)
    monkeypatch.setattr(engine, "_phase_workspace_resolve", phase_workspace_resolve)
    monkeypatch.setattr(engine, "_phase_oauth_callback", phase_oauth_callback)

    result = engine.run()

    assert result.success is True
    assert result.workspace_id == "ws-123"
    assert result.account_id == "acct-1"
    assert calls == [
        PHASE_IP_CHECK,
        PHASE_EMAIL_PREPARE,
        PHASE_SIGNUP_SUBMIT,
        PHASE_SIGNUP_PASSWORD,
        PHASE_OTP_PRIMARY,
        PHASE_OTP_SECONDARY,
        PHASE_ACCOUNT_CREATE,
        PHASE_OAUTH_REENTER,
        PHASE_WORKSPACE_RESOLVE,
        PHASE_OAUTH_CALLBACK,
    ]
