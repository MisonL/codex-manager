from types import SimpleNamespace

from src.core.fingerprint import DEFAULT_BROWSER_IMPERSONATE, DEFAULT_BROWSER_USER_AGENT
from src.core.http_client import HTTPClient, RequestConfig
from src.core.openai.token_refresh import TokenRefreshManager


class _FakeResponse:
    def __init__(self, status_code=200, json_payload=None, text=""):
        self.status_code = status_code
        self._json_payload = json_payload or {}
        self.text = text

    def json(self):
        return self._json_payload


class _FakeSession:
    def __init__(self):
        self.calls = []
        self.cookies = SimpleNamespace(set=lambda *args, **kwargs: None)

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return _FakeResponse(status_code=200, json_payload={"ok": True})

    def get(self, url, **kwargs):
        self.calls.append(("GET", url, kwargs))
        if url.endswith("/api/auth/session"):
            return _FakeResponse(
                status_code=200,
                json_payload={
                    "accessToken": "access-token",
                    "expires": "2026-03-25T00:00:00Z",
                },
            )
        return _FakeResponse(status_code=200, json_payload={"id": "user"})

    def post(self, url, **kwargs):
        self.calls.append(("POST", url, kwargs))
        return _FakeResponse(
            status_code=200,
            json_payload={
                "access_token": "access-token",
                "refresh_token": "refresh-token-2",
                "expires_in": 3600,
            },
        )


def test_http_client_request_uses_profile_impersonate_over_config_override():
    session = _FakeSession()
    client = HTTPClient(
        config=RequestConfig(impersonate="chrome999"),
        session=session,
    )

    client.get("https://example.com/api")

    _, _, kwargs = session.calls[0]
    assert kwargs["impersonate"] == DEFAULT_BROWSER_IMPERSONATE


def test_token_refresh_uses_fingerprint_api_headers(monkeypatch):
    monkeypatch.setattr(
        "src.core.openai.token_refresh.get_settings",
        lambda: SimpleNamespace(
            openai_client_id="client-id",
            openai_redirect_uri="https://example.com/callback",
        ),
    )

    manager = TokenRefreshManager(proxy_url="http://127.0.0.1:8080")
    session = _FakeSession()
    monkeypatch.setattr(manager, "_create_session", lambda: session)

    session_result = manager.refresh_by_session_token("session-token")
    oauth_result = manager.refresh_by_oauth_token("refresh-token")
    valid, error = manager.validate_token("access-token")

    assert session_result.success is True
    assert oauth_result.success is True
    assert valid is True
    assert error is None

    first_headers = session.calls[0][2]["headers"]
    second_headers = session.calls[1][2]["headers"]
    third_headers = session.calls[2][2]["headers"]

    for headers in (first_headers, second_headers, third_headers):
        assert headers["user-agent"] == DEFAULT_BROWSER_USER_AGENT
        assert headers["sec-fetch-dest"] == "empty"
        assert headers["sec-fetch-mode"] == "cors"
        assert headers["sec-fetch-site"] == "same-origin"

    assert second_headers["content-type"] == "application/x-www-form-urlencoded"
    assert third_headers["authorization"] == "Bearer access-token"
