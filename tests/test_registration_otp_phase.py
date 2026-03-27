import src.core.register as register_module
from src.core.register import (
    ERROR_EMAIL_PROVIDER_RATE_LIMITED,
    ERROR_OTP_TIMEOUT_SECONDARY,
    PHASE_EMAIL_PREPARE,
    PHASE_OTP_SECONDARY,
    PhaseContext,
    PhaseResult,
    RegistrationEngine,
    RegistrationResult,
)
from src.services import EmailServiceType
from src.services.base import EmailProviderBackoffState
from types import SimpleNamespace


class DummySettings:
    openai_client_id = "client-id"
    openai_auth_url = "https://auth.example.test"
    openai_token_url = "https://token.example.test"
    openai_redirect_uri = "https://callback.example.test"
    openai_scope = "openid profile email"


class FakeEmailService:
    def __init__(self, code):
        self.service_type = EmailServiceType.TEMPMAIL
        self.code = code
        self.calls = []

    def get_verification_code(self, **kwargs):
        self.calls.append(kwargs)
        return self.code


class BackoffEmailService:
    def __init__(self, state):
        self.service_type = EmailServiceType.TEMPMAIL
        self.provider_backoff_state = state
        self.last_error = state.last_error
        self.create_email_calls = 0

    def create_email(self):
        self.create_email_calls += 1
        raise AssertionError("create_email should not be called while backoff is open")


class FakeCookies:
    def __init__(self, values):
        self.values = values

    def get(self, name):
        return self.values.get(name)


class FakeSession:
    def __init__(self, cookies=None):
        self.cookies = FakeCookies(cookies or {})
        self.get_calls = []
        self.post_calls = []

    def get(self, *args, **kwargs):
        self.get_calls.append((args, kwargs))
        raise AssertionError("unexpected network call")

    def post(self, *args, **kwargs):
        self.post_calls.append((args, kwargs))
        raise AssertionError("unexpected network call")


class FakeResponse:
    def __init__(self, *, url="", text="", json_payload=None):
        self.url = url
        self.text = text
        self._json_payload = json_payload

    def json(self):
        if isinstance(self._json_payload, Exception):
            raise self._json_payload
        return self._json_payload


def _build_engine(monkeypatch, email_service):
    monkeypatch.setattr(register_module, "get_settings", lambda: DummySettings())
    return RegistrationEngine(email_service=email_service)


def test_phase_otp_secondary_uses_remaining_budget_from_start_timestamp(monkeypatch):
    email_service = FakeEmailService(code="654321")
    engine = _build_engine(monkeypatch, email_service)
    engine.email = "tester@example.com"
    engine.email_info = {"service_id": "svc-1"}

    monkeypatch.setattr(register_module.time, "time", lambda: 120.0)

    code, phase_result = engine._phase_otp_secondary(
        PhaseContext(otp_sent_at=77.0),
        started_at=100.0,
    )

    assert code == "654321"
    assert phase_result.success is True
    assert email_service.calls[0]["timeout"] == 100
    assert email_service.calls[0]["otp_sent_at"] == 77.0
    assert email_service.calls[0]["email"] == "tester@example.com"
    assert email_service.calls[0]["email_id"] == "svc-1"


def test_phase_otp_secondary_returns_dedicated_timeout_error_code(monkeypatch):
    email_service = FakeEmailService(code=None)
    engine = _build_engine(monkeypatch, email_service)
    engine.email = "tester@example.com"
    engine.email_info = {"service_id": "svc-1"}

    monkeypatch.setattr(register_module.time, "time", lambda: 120.0)

    code, phase_result = engine._phase_otp_secondary(
        PhaseContext(otp_sent_at=80.0),
        started_at=100.0,
    )

    assert code is None
    assert phase_result.success is False
    assert phase_result.error_code == ERROR_OTP_TIMEOUT_SECONDARY
    assert engine.phase_history[0].error_code == ERROR_OTP_TIMEOUT_SECONDARY


def test_await_secondary_otp_code_logs_waiting_for_verification_email(monkeypatch):
    email_service = FakeEmailService(code="654321")
    engine = _build_engine(monkeypatch, email_service)
    engine.email = "tester@example.com"
    engine.email_info = {"service_id": "svc-1"}

    monkeypatch.setattr(register_module.time, "time", lambda: 120.0)

    code, phase_result = engine._await_secondary_otp_code(
        PhaseContext(otp_sent_at=77.0),
        started_at=100.0,
    )

    assert code == "654321"
    assert phase_result.success is True
    assert any("正在等待验证邮件..." in entry for entry in engine.logs)
    assert any("正在轮询邮箱 tester@example.com 的验证码邮件..." in entry for entry in engine.logs)


def test_phase_email_prepare_short_circuits_when_provider_backoff_is_open(monkeypatch):
    email_service = BackoffEmailService(
        EmailProviderBackoffState(
            failures=2,
            delay_seconds=60,
            opened_until=160.0,
            last_error="请求失败: 429",
        )
    )
    engine = _build_engine(monkeypatch, email_service)

    monkeypatch.setattr(register_module.time, "time", lambda: 120.0)

    phase_result = engine._phase_email_prepare()

    assert phase_result.success is False
    assert phase_result.error_code == ERROR_EMAIL_PROVIDER_RATE_LIMITED
    assert phase_result.retryable is True
    assert phase_result.next_action == "switch_provider"
    assert phase_result.metadata["backoff_active"] is True
    assert phase_result.metadata["backoff_remaining_seconds"] == 40
    assert email_service.create_email_calls == 0


def test_build_phase_failure_result_preserves_task3_and_task4_metadata(monkeypatch):
    email_service = FakeEmailService(code=None)
    engine = _build_engine(monkeypatch, email_service)
    backoff_state = EmailProviderBackoffState(
        failures=1,
        delay_seconds=30,
        opened_until=130.0,
        last_error="请求失败: 429",
    )
    engine.phase_history = [
        PhaseResult(
            phase=PHASE_EMAIL_PREPARE,
            success=False,
            error_message="创建邮箱失败",
            error_code=ERROR_EMAIL_PROVIDER_RATE_LIMITED,
            retryable=True,
            next_action="switch_provider",
            provider_backoff=backoff_state,
            metadata={"backoff_active": True},
        ),
        PhaseResult(
            phase=PHASE_OTP_SECONDARY,
            success=False,
            error_message="等待验证码超时",
            error_code=ERROR_OTP_TIMEOUT_SECONDARY,
            retryable=True,
            next_action="await_email",
            metadata={"otp_sent_at": 77.0},
        ),
    ]

    result = engine._build_phase_failure_result(
        RegistrationResult(success=False, logs=[]),
        engine.phase_history[0],
    )

    assert result.metadata["provider_backoff"]["failures"] == 1
    assert result.metadata["phase_metadata"]["backoff_active"] is True
    assert result.metadata["phase_results"][PHASE_EMAIL_PREPARE]["provider_backoff"]["delay_seconds"] == 30
    assert result.metadata["phase_results"][PHASE_OTP_SECONDARY]["metadata"]["otp_sent_at"] == 77.0


def test_advance_login_authorization_sets_otp_anchor_before_password_submit(monkeypatch):
    email_service = FakeEmailService(code=None)
    engine = _build_engine(monkeypatch, email_service)
    engine.oauth_start = object()
    engine._otp_sent_at = 10.0

    monkeypatch.setattr(register_module.time, "time", lambda: 456.0)
    monkeypatch.setattr(engine, "_init_session", lambda: True)
    monkeypatch.setattr(engine, "_start_oauth", lambda: True)
    monkeypatch.setattr(engine, "_probe_device_id_validity", lambda stage: True)
    monkeypatch.setattr(engine, "_current_device_id", lambda: "did-probed")
    monkeypatch.setattr(engine, "_try_reenter_login_flow", lambda: True)

    seen_anchors = []

    def fake_submit_login_password_step():
        seen_anchors.append(engine._otp_sent_at)
        return True

    monkeypatch.setattr(engine, "_submit_login_password_step", fake_submit_login_password_step)

    def fake_get_verification_code():
        seen_anchors.append(engine._otp_sent_at)
        return None

    monkeypatch.setattr(engine, "_get_verification_code", fake_get_verification_code)

    workspace_id, callback_url = engine._advance_login_authorization()

    assert workspace_id is None
    assert callback_url is None
    assert engine._otp_sent_at == 456.0
    assert seen_anchors == [456.0, 456.0]


def test_get_device_id_reuses_existing_cookie_without_extra_request(monkeypatch):
    email_service = FakeEmailService(code=None)
    engine = _build_engine(monkeypatch, email_service)
    engine.oauth_start = type("OAuthStart", (), {"auth_url": "https://auth.example.test/authorize"})()
    engine.session = FakeSession(cookies={"oai-did": "did-cached"})

    assert engine._get_device_id() == "did-cached"
    assert engine.session.get_calls == []


def test_current_device_id_does_not_reuse_stale_memory_when_cookie_missing(monkeypatch):
    email_service = FakeEmailService(code=None)
    engine = _build_engine(monkeypatch, email_service)
    engine.device_id = "did-stale"
    engine.session = FakeSession(cookies={})

    assert engine._current_device_id() is None
    assert engine.device_id == "did-stale"


def test_rebuild_session_syncs_memory_device_id_from_new_cookie(monkeypatch):
    email_service = FakeEmailService(code=None)
    engine = _build_engine(monkeypatch, email_service)
    engine.device_id = "did-stale"
    engine.session = FakeSession(cookies={})

    replacement_session = FakeSession(cookies={"oai-did": "did-fresh"})

    class CyclingHTTPClient:
        def __init__(self, session, fingerprint_profile):
            self._session = session
            self.fingerprint_profile = fingerprint_profile
            self.close_calls = 0

        @property
        def session(self):
            return self._session

        def close(self):
            self.close_calls += 1

    engine.http_client = CyclingHTTPClient(replacement_session, engine.fingerprint_profile)

    engine._rebuild_session("test")

    assert engine.session is replacement_session
    assert engine.device_id == "did-fresh"
    assert engine.http_client.close_calls == 1


def test_phase_oauth_reenter_force_refreshes_device_id_after_probe_failure(monkeypatch):
    email_service = FakeEmailService(code=None)
    engine = _build_engine(monkeypatch, email_service)
    engine.oauth_start = SimpleNamespace(auth_url="https://auth.example.test/authorize")

    monkeypatch.setattr(register_module.time, "time", lambda: 789.0)
    monkeypatch.setattr(engine, "_init_session", lambda: True)
    monkeypatch.setattr(engine, "_start_oauth", lambda: True)
    monkeypatch.setattr(engine, "_probe_device_id_validity", lambda stage: False)
    monkeypatch.setattr(engine, "_try_reenter_login_flow", lambda: True)
    monkeypatch.setattr(engine, "_submit_login_password_step", lambda: True)
    rebuild_reasons = []
    monkeypatch.setattr(engine, "_rebuild_session", lambda reason: rebuild_reasons.append(reason))

    seen_force_refresh = []

    def fake_get_device_id(force_refresh=False):
        seen_force_refresh.append(force_refresh)
        return "did-fresh"

    monkeypatch.setattr(engine, "_get_device_id", fake_get_device_id)

    phase_result = engine._phase_oauth_reenter()

    assert phase_result.success is True
    assert seen_force_refresh == [True]
    assert rebuild_reasons == ["oauth_reenter Device ID 探测失败"]
    assert phase_result.metadata["otp_sent_at"] == 789.0


def test_extract_workspace_id_from_response_payload(monkeypatch):
    email_service = FakeEmailService(code=None)
    engine = _build_engine(monkeypatch, email_service)
    response = FakeResponse(
        url="https://auth.example.test/consent?workspace_id=ws-url",
        json_payload={
            "page": {
                "workspace": {
                    "id": "ws-json",
                }
            }
        },
    )

    assert engine._extract_workspace_id_from_response(response=response) == "ws-json"


def test_extract_workspace_id_from_response_text_when_hidden_input_missing(monkeypatch):
    email_service = FakeEmailService(code=None)
    engine = _build_engine(monkeypatch, email_service)
    response = FakeResponse(
        url="https://auth.example.test/consent",
        text='<script>window.__NEXT_DATA__={"activeWorkspaceId":"ws-script"}</script>',
        json_payload=ValueError("not json"),
    )

    assert engine._extract_workspace_id_from_response(response=response) == "ws-script"
