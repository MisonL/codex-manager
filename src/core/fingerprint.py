"""
OpenAI 浏览器指纹配置。
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import tempfile
import threading
import urllib.parse
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Dict, Optional


DEFAULT_BROWSER_IMPERSONATE = "chrome124"
DEFAULT_BROWSER_MAJOR_VERSION = "124"
DEFAULT_BROWSER_FULL_VERSION = "124.0.0.0"
DEFAULT_BROWSER_PLATFORM = "Windows"
DEFAULT_BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
DEFAULT_SEC_CH_UA = (
    '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"'
)
DEFAULT_ACCEPT_LANGUAGE = "en-US,en;q=0.9"
DEFAULT_ACCEPT_ENCODING = "gzip, deflate, br, zstd"
PROFILE_SCHEMA_VERSION = 1

_PROFILE_LOCK = threading.Lock()
_PROFILE_CACHE: Dict[str, "FingerprintProfile"] = {}
_PROFILE_STORE_PATH = Path(__file__).resolve().parents[2] / "data" / "fingerprint_profiles.json"


def _registrable_domain(hostname: str) -> str:
    parts = [part for part in (hostname or "").split(".") if part]
    if len(parts) < 2:
        return hostname or ""
    return ".".join(parts[-2:])


def _origin_from_url(url: Optional[str]) -> Optional[str]:
    raw = str(url or "").strip()
    if not raw:
        return None

    parsed = urllib.parse.urlparse(raw)
    if not parsed.scheme or not parsed.netloc:
        return None
    return f"{parsed.scheme}://{parsed.netloc}"


def _site_context(target_url: str, source_url: Optional[str]) -> str:
    target_origin = _origin_from_url(target_url)
    source_origin = _origin_from_url(source_url)

    if not target_origin or not source_origin:
        return "same-origin"

    if target_origin == source_origin:
        return "same-origin"

    target_host = urllib.parse.urlparse(target_origin).hostname or ""
    source_host = urllib.parse.urlparse(source_origin).hostname or ""
    if _registrable_domain(target_host) == _registrable_domain(source_host):
        return "same-site"
    return "cross-site"


def _normalize_proxy_binding_key(proxy_url: Optional[str]) -> str:
    raw = str(proxy_url or "").strip()
    if not raw:
        return "direct"

    parsed = urllib.parse.urlparse(raw)
    if not parsed.hostname:
        return raw

    scheme = parsed.scheme or "http"
    host = parsed.hostname
    port = parsed.port
    if port is None:
        return f"{scheme}://{host}"
    return f"{scheme}://{host}:{port}"


def _load_profile_store() -> Dict[str, Dict[str, Any]]:
    if not _PROFILE_STORE_PATH.exists():
        return {}

    try:
        return json.loads(_PROFILE_STORE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_profile_store(payload: Dict[str, Dict[str, Any]]) -> None:
    _PROFILE_STORE_PATH.parent.mkdir(parents=True, exist_ok=True)
    content = json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True)
    temp_path: Optional[Path] = None

    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=_PROFILE_STORE_PATH.parent,
            delete=False,
            prefix=f"{_PROFILE_STORE_PATH.name}.",
            suffix=".tmp",
        ) as temp_file:
            temp_file.write(content)
            temp_file.flush()
            os.fsync(temp_file.fileno())
            temp_path = Path(temp_file.name)

        os.replace(temp_path, _PROFILE_STORE_PATH)
        temp_path = None
    finally:
        if temp_path is not None:
            try:
                temp_path.unlink()
            except FileNotFoundError:
                pass


@dataclass(frozen=True)
class FingerprintProfile:
    binding_key: str
    profile_id: str
    impersonate: str = DEFAULT_BROWSER_IMPERSONATE
    browser_major_version: str = DEFAULT_BROWSER_MAJOR_VERSION
    browser_full_version: str = DEFAULT_BROWSER_FULL_VERSION
    user_agent: str = DEFAULT_BROWSER_USER_AGENT
    sec_ch_ua: str = DEFAULT_SEC_CH_UA
    sec_ch_ua_mobile: str = "?0"
    sec_ch_ua_platform: str = DEFAULT_BROWSER_PLATFORM
    accept_language: str = DEFAULT_ACCEPT_LANGUAGE
    accept_encoding: str = DEFAULT_ACCEPT_ENCODING
    tls_seed: str = ""
    schema_version: int = PROFILE_SCHEMA_VERSION

    @classmethod
    def create(cls, binding_key: str) -> "FingerprintProfile":
        return cls(
            binding_key=binding_key,
            profile_id=secrets.token_hex(8),
            tls_seed=secrets.token_hex(16),
        )

    @classmethod
    def from_dict(cls, payload: Dict[str, Any]) -> "FingerprintProfile":
        normalized = dict(payload)
        normalized.setdefault("impersonate", DEFAULT_BROWSER_IMPERSONATE)
        normalized.setdefault("browser_major_version", DEFAULT_BROWSER_MAJOR_VERSION)
        normalized.setdefault("browser_full_version", DEFAULT_BROWSER_FULL_VERSION)
        normalized.setdefault("user_agent", DEFAULT_BROWSER_USER_AGENT)
        normalized.setdefault("sec_ch_ua", DEFAULT_SEC_CH_UA)
        normalized.setdefault("sec_ch_ua_mobile", "?0")
        normalized.setdefault("sec_ch_ua_platform", DEFAULT_BROWSER_PLATFORM)
        normalized.setdefault("accept_language", DEFAULT_ACCEPT_LANGUAGE)
        normalized.setdefault("accept_encoding", DEFAULT_ACCEPT_ENCODING)
        normalized.setdefault("schema_version", PROFILE_SCHEMA_VERSION)
        if not normalized.get("profile_id"):
            normalized["profile_id"] = secrets.token_hex(8)
        if not normalized.get("tls_seed"):
            normalized["tls_seed"] = secrets.token_hex(16)
        return cls(**normalized)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    def session_headers(self) -> Dict[str, str]:
        return {
            "user-agent": self.user_agent,
            "accept-language": self.accept_language,
            "accept-encoding": self.accept_encoding,
            "sec-ch-ua": self.sec_ch_ua,
            "sec-ch-ua-mobile": self.sec_ch_ua_mobile,
            "sec-ch-ua-platform": f'"{self.sec_ch_ua_platform}"',
        }

    def extra_fp(self) -> Dict[str, Any]:
        seed = int(hashlib.sha256(self.tls_seed.encode("ascii")).hexdigest()[:8], 16)
        return {
            "tls_grease": True,
            "tls_permute_extensions": bool(seed & 1),
            "tls_cert_compression": "brotli",
            "http2_stream_weight": 32 + (seed % 128),
            "http2_stream_exclusive": seed % 2,
        }

    def build_headers(
        self,
        *,
        url: str,
        request_kind: str,
        headers: Optional[Dict[str, str]] = None,
    ) -> Dict[str, str]:
        merged = self.session_headers()
        overrides = {
            str(key).lower(): value
            for key, value in (headers or {}).items()
            if value is not None
        }
        referer = str(overrides.get("referer") or "").strip() or None
        origin = str(overrides.get("origin") or "").strip() or _origin_from_url(referer)

        if request_kind == "navigate":
            merged.update(
                {
                    "accept": (
                        "text/html,application/xhtml+xml,application/xml;q=0.9,"
                        "image/avif,image/webp,image/apng,*/*;q=0.8"
                    ),
                    "sec-fetch-dest": "document",
                    "sec-fetch-mode": "navigate",
                    "sec-fetch-site": _site_context(url, referer) if referer else "none",
                    "sec-fetch-user": "?1",
                    "upgrade-insecure-requests": "1",
                }
            )
        else:
            source_url = origin or referer
            merged.update(
                {
                    "accept": "application/json",
                    "sec-fetch-dest": "empty",
                    "sec-fetch-mode": "cors",
                    "sec-fetch-site": _site_context(url, source_url),
                }
            )
            merged.pop("sec-fetch-user", None)
            merged.pop("upgrade-insecure-requests", None)

        merged.update(overrides)
        return merged

    def request_kwargs(
        self,
        *,
        url: str,
        request_kind: str,
        headers: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        return {
            "headers": self.build_headers(url=url, request_kind=request_kind, headers=headers),
            "impersonate": self.impersonate,
            "extra_fp": self.extra_fp(),
            "default_headers": False,
        }

    def session_kwargs(self, *, timeout: int, verify: bool = True) -> Dict[str, Any]:
        return {
            "headers": self.session_headers(),
            "timeout": timeout,
            "verify": verify,
            "impersonate": self.impersonate,
            "extra_fp": self.extra_fp(),
            "default_headers": False,
        }


def get_fingerprint_profile(proxy_url: Optional[str] = None) -> FingerprintProfile:
    binding_key = _normalize_proxy_binding_key(proxy_url)

    with _PROFILE_LOCK:
        cached = _PROFILE_CACHE.get(binding_key)
        if cached is not None:
            return cached

        store = _load_profile_store()
        payload = store.get(binding_key)
        if payload:
            profile = FingerprintProfile.from_dict(payload)
        else:
            profile = FingerprintProfile.create(binding_key)
            store[binding_key] = profile.to_dict()
            _save_profile_store(store)

        _PROFILE_CACHE[binding_key] = profile
        return profile


def reset_fingerprint_profile_cache() -> None:
    with _PROFILE_LOCK:
        _PROFILE_CACHE.clear()
