"""
HTTP 客户端封装
基于 curl_cffi 的 HTTP 请求封装，支持代理和错误处理
"""

import time
import json
from typing import Optional, Dict, Any, Union, Tuple
from dataclasses import dataclass
import logging

from curl_cffi import requests as cffi_requests
from curl_cffi.requests import Session, Response

from ..config.constants import ERROR_MESSAGES
from ..config.settings import get_settings


logger = logging.getLogger(__name__)


@dataclass
class RequestConfig:
    """HTTP 请求配置"""
    timeout: int = 30
    max_retries: int = 3
    retry_delay: float = 1.0
    impersonate: str = "chrome120"
    verify_ssl: bool = True
    follow_redirects: bool = True


class HTTPClientError(Exception):
    """HTTP 客户端异常"""
    pass

class OpenAIRiskControlError(HTTPClientError):
    """OpenAI 风控拦截异常"""
    pass


class HTTPClient:
    """
    HTTP 客户端封装
    支持代理、重试、错误处理和会话管理
    """

    def __init__(
        self,
        proxy_url: Optional[str] = None,
        config: Optional[RequestConfig] = None,
        session: Optional[Session] = None
    ):
        self.proxy_url = proxy_url
        self.config = config or RequestConfig()
        self._session = session

    @property
    def proxies(self) -> Optional[Dict[str, str]]:
        if not self.proxy_url:
            return None
        return {"http": self.proxy_url, "https": self.proxy_url}

    @property
    def session(self) -> Session:
        if self._session is None:
            self._session = Session(
                proxies=self.proxies,
                impersonate=self.config.impersonate,
                verify=self.config.verify_ssl,
                timeout=self.config.timeout
            )
        return self._session

    def request(self, method: str, url: str, **kwargs) -> Response:
        kwargs.setdefault("timeout", self.config.timeout)
        kwargs.setdefault("allow_redirects", self.config.follow_redirects)
        if self.proxies and "proxies" not in kwargs:
            kwargs["proxies"] = self.proxies

        last_exception = None
        for attempt in range(self.config.max_retries):
            try:
                response = self.session.request(method, url, **kwargs)
                if response.status_code >= 400:
                    logger.warning(f"HTTP {response.status_code} for {method} {url}")
                    if response.status_code == 403: return response
                    if response.status_code >= 500 and attempt < self.config.max_retries - 1:
                        time.sleep(self.config.retry_delay * (attempt + 1))
                        continue
                return response
            except (cffi_requests.RequestsError, ConnectionError, TimeoutError) as e:
                last_exception = e
                if attempt < self.config.max_retries - 1:
                    time.sleep(self.config.retry_delay * (attempt + 1))
                else: break
        raise HTTPClientError(f"请求失败: {method} {url} - {last_exception}")

    def get(self, url, **kwargs): return self.request("GET", url, **kwargs)
    def post(self, url, data=None, json=None, **kwargs): return self.request("POST", url, data=data, json=json, **kwargs)
    def put(self, url, data=None, json=None, **kwargs): return self.request("PUT", url, data=data, json=json, **kwargs)
    def delete(self, url, **kwargs): return self.request("DELETE", url, **kwargs)
    def head(self, url, **kwargs): return self.request("HEAD", url, **kwargs)
    def options(self, url, **kwargs): return self.request("OPTIONS", url, **kwargs)
    def patch(self, url, data=None, json=None, **kwargs): return self.request("PATCH", url, data=data, json=json, **kwargs)

    def close(self):
        if self._session:
            self._session.close()
            self._session = None

    def __enter__(self): return self
    def __exit__(self, exc_type, exc_val, exc_tb): self.close()


class OpenAIHTTPClient(HTTPClient):
    """OpenAI 专用 HTTP 客户端"""

    def __init__(self, proxy_url=None, config=None):
        super().__init__(proxy_url, config)
        if config is None:
            self.config.impersonate = "chrome120"

        self.default_headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "application/json",
            "Accept-Language": "en-US,en;q=0.9",
            "Accept-Encoding": "gzip, deflate, br",
            "Connection": "keep-alive",
            "Sec-CH-UA": '"Not_A Brand";v="8", "Chromium";v="120", "Google Chrome";v="120"',
            "Sec-CH-UA-Mobile": "?0",
            "Sec-CH-UA-Platform": '"Windows"',
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-site",
        }

    def send_openai_request(self, endpoint, method="POST", data=None, json_data=None, headers=None, **kwargs):
        request_headers = self.default_headers.copy()
        if headers: request_headers.update(headers)
        if json_data is not None: request_headers["Content-Type"] = "application/json"
        elif data is not None: request_headers["Content-Type"] = "application/x-www-form-urlencoded"

        try:
            response = self.request(method, endpoint, data=data, json=json_data, headers=request_headers, **kwargs)
            if response.status_code == 403:
                txt = response.text
                if any(x in txt for x in ["invalidated oauth token", "token_revoked"]):
                    raise OpenAIRiskControlError(f"Token Revoked: {txt}")
                raise OpenAIRiskControlError(f"403 Forbidden: {txt}")
            if response.status_code == 429:
                raise OpenAIRiskControlError(f"429 Quota Exceeded: {response.text}")
            response.raise_for_status()
            try: return response.json()
            except: return {"raw_response": response.text}
        except cffi_requests.RequestsError as e:
            raise HTTPClientError(f"OpenAI Failed: {endpoint} - {e}")

    def check_ip_location(self):
        try:
            r = self.get("https://cloudflare.com/cdn-cgi/trace", timeout=10)
            import re
            m = re.search(r"loc=([A-Z]+)", r.text)
            loc = m.group(1) if m else None
            return loc not in ["CN", "HK", "MO"], loc
        except: return False, None

def create_http_client(proxy_url=None, config=None): return HTTPClient(proxy_url, config)
def create_openai_client(proxy_url=None, config=None): return OpenAIHTTPClient(proxy_url, config)
