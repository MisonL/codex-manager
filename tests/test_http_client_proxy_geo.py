from types import SimpleNamespace

import pytest
from curl_cffi.requests.exceptions import ConnectionError as CurlConnectionError

from src.core.http_client import HTTPClient, HTTPClientError, OpenAIHTTPClient


class _FakeResponse:
    def __init__(self, status_code=200, json_payload=None, text=""):
        self.status_code = status_code
        self._json_payload = json_payload or {}
        self.text = text

    def json(self):
        return self._json_payload


class _FailingSession:
    def __init__(self):
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        raise CurlConnectionError("SOCKS5 connect failed")


def test_http_client_session_splits_proxy_auth_from_proxy_url(monkeypatch):
    captured = {}

    class _SessionStub:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr("src.core.http_client.Session", _SessionStub)

    client = HTTPClient(proxy_url="socks5://user:pa:ss@proxy.example.com:1080")
    client.session

    assert client.proxy_url == "socks5://proxy.example.com:1080"
    assert client.proxy_auth == ("user", "pa:ss")
    assert captured["proxies"] == {
        "http": "socks5://proxy.example.com:1080",
        "https": "socks5://proxy.example.com:1080",
    }
    assert captured["proxy_auth"] == ("user", "pa:ss")


def test_http_client_request_surfaces_redacted_proxy_for_transport_errors():
    client = HTTPClient(
        proxy_url="socks5://user:secret@127.0.0.1:1080",
        session=_FailingSession(),
    )

    with pytest.raises(HTTPClientError) as excinfo:
        client.get("https://example.com/api")

    message = str(excinfo.value)
    assert "无法通过代理 [socks5://user:***@127.0.0.1:1080] 连接到服务" in message
    assert "https://example.com/api" in message
    assert "secret" not in message


def test_check_ip_location_falls_back_to_cloudflare_after_ip_api_and_ifconfig_failures(monkeypatch):
    client = OpenAIHTTPClient(proxy_url="socks5://user:secret@127.0.0.1:1080")
    calls = []

    def fake_get(url, **kwargs):
        calls.append((url, kwargs))
        if "ip-api.com" in url:
            raise HTTPClientError("主服务超时")
        if "ifconfig.me" in url:
            return _FakeResponse(json_payload={"ip_addr": "203.0.113.10"})
        if "cloudflare.com" in url:
            return _FakeResponse(text="ip=203.0.113.10\nloc=SG\n")
        raise AssertionError(f"unexpected url: {url}")

    monkeypatch.setattr(client, "get", fake_get)

    supported, location = client.check_ip_location()

    assert supported is True
    assert location == "SG"
    assert calls == [
        (
            "http://ip-api.com/json/?fields=status,message,country,countryCode,query",
            {"timeout": 10},
        ),
        ("https://ifconfig.me/all.json", {"timeout": 10}),
        ("https://cloudflare.com/cdn-cgi/trace", {"timeout": 10}),
    ]


def test_check_ip_location_raises_detailed_geo_service_error(monkeypatch):
    client = OpenAIHTTPClient(proxy_url="socks5://user:secret@127.0.0.1:1080")

    def fake_get(url, **kwargs):
        if "ip-api.com" in url:
            raise HTTPClientError("连接超时")
        if "ifconfig.me" in url:
            raise HTTPClientError("连接被拒绝")
        if "cloudflare.com" in url:
            raise HTTPClientError("SOCKS5 connect failed")
        raise AssertionError(f"unexpected url: {url}")

    monkeypatch.setattr(client, "get", fake_get)

    with pytest.raises(HTTPClientError) as excinfo:
        client.check_ip_location()

    message = str(excinfo.value)
    assert "无法通过代理 [socks5://user:***@127.0.0.1:1080] 连接到地理位置服务" in message
    assert "ip-api.com" in message
    assert "ifconfig.me/all.json" in message
    assert "cloudflare.com/cdn-cgi/trace" in message
