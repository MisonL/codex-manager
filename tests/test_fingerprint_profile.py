import json

import src.core.fingerprint as fingerprint_module


def test_get_fingerprint_profile_persists_binding_for_same_proxy(tmp_path, monkeypatch):
    store_path = tmp_path / "fingerprint_profiles.json"
    monkeypatch.setattr(fingerprint_module, "_PROFILE_STORE_PATH", store_path)
    fingerprint_module.reset_fingerprint_profile_cache()

    profile_a = fingerprint_module.get_fingerprint_profile("http://user:pass@1.2.3.4:8080")
    fingerprint_module.reset_fingerprint_profile_cache()
    profile_b = fingerprint_module.get_fingerprint_profile("http://1.2.3.4:8080")

    assert profile_a.binding_key == "http://1.2.3.4:8080"
    assert profile_b.binding_key == "http://1.2.3.4:8080"
    assert profile_a.profile_id == profile_b.profile_id
    assert profile_a.tls_seed == profile_b.tls_seed

    persisted = json.loads(store_path.read_text(encoding="utf-8"))
    assert persisted["http://1.2.3.4:8080"]["profile_id"] == profile_a.profile_id


def test_build_headers_uses_fetch_semantics_for_api_requests():
    profile = fingerprint_module.FingerprintProfile.create("direct")

    headers = profile.build_headers(
        url="https://auth.openai.com/api/accounts/password/verify",
        request_kind="api",
        headers={
            "referer": "https://auth.openai.com/log-in/password",
            "content-type": "application/json",
        },
    )

    assert headers["sec-fetch-dest"] == "empty"
    assert headers["sec-fetch-mode"] == "cors"
    assert headers["sec-fetch-site"] == "same-origin"
    assert "upgrade-insecure-requests" not in headers
    assert "sec-fetch-user" not in headers
