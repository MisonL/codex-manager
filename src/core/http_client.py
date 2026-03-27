"""
HTTP 客户端封装
基于 curl_cffi 的 HTTP 请求封装，支持代理和错误处理
"""

import time
import json
from typing import Optional, Dict, Any, Union, Tuple
from dataclasses import dataclass
import logging
import re
from urllib.parse import unquote, urlsplit

from curl_cffi import requests as cffi_requests
from curl_cffi.requests import Session, Response
from curl_cffi.requests.exceptions import (
    ConnectionError as CurlConnectionError,
    ProxyError as CurlProxyError,
    RequestException as CurlRequestException,
    Timeout as CurlTimeout,
)

from ..config.constants import ERROR_MESSAGES
from ..config.settings import get_settings
from .fingerprint import (
    DEFAULT_BROWSER_IMPERSONATE,
    FingerprintProfile,
    get_fingerprint_profile,
)


logger = logging.getLogger(__name__)
BLOCKED_COUNTRY_CODES = {"CN", "HK", "MO"}


def _split_proxy_credentials(proxy_url: Optional[str]) -> Tuple[Optional[str], Optional[Tuple[str, str]]]:
    raw_proxy_url = str(proxy_url or "").strip()
    if not raw_proxy_url:
        return None, None

    parsed = urlsplit(raw_proxy_url)
    if not parsed.scheme or not parsed.hostname:
        return raw_proxy_url, None

    username = parsed.username
    password = parsed.password
    host = parsed.hostname
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    port = f":{parsed.port}" if parsed.port is not None else ""
    sanitized_proxy_url = f"{parsed.scheme}://{host}{port}"

    if username is None:
        return sanitized_proxy_url, None

    return sanitized_proxy_url, (unquote(username), unquote(password or ""))


def _redact_proxy_url(proxy_url: Optional[str]) -> str:
    raw_proxy_url = str(proxy_url or "").strip()
    if not raw_proxy_url:
        return ""

    parsed = urlsplit(raw_proxy_url)
    if not parsed.scheme or not parsed.hostname:
        return raw_proxy_url

    host = parsed.hostname
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    port = f":{parsed.port}" if parsed.port is not None else ""

    if parsed.username is None:
        return f"{parsed.scheme}://{host}{port}"

    return f"{parsed.scheme}://{unquote(parsed.username)}:***@{host}{port}"


@dataclass
class RequestConfig:
    """HTTP 请求配置"""
    timeout: int = 30
    max_retries: int = 3
    retry_delay: float = 1.0
    impersonate: str = DEFAULT_BROWSER_IMPERSONATE
    verify_ssl: bool = True
    follow_redirects: bool = True


class HTTPClientError(Exception):
    """HTTP 客户端异常"""
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
        """
        初始化 HTTP 客户端

        Args:
            proxy_url: 代理 URL，如 "http://127.0.0.1:7890"
            config: 请求配置
            session: 可重用的会话对象
        """
        self.raw_proxy_url = str(proxy_url or "").strip() or None
        self.proxy_url, self.proxy_auth = _split_proxy_credentials(proxy_url)
        self.config = config or RequestConfig()
        self._session = session
        self.fingerprint_profile: FingerprintProfile = get_fingerprint_profile(proxy_url)

    @property
    def proxies(self) -> Optional[Dict[str, str]]:
        """获取代理配置"""
        if not self.proxy_url:
            return None
        return {
            "http": self.proxy_url,
            "https": self.proxy_url,
        }

    @property
    def session(self) -> Session:
        """获取会话对象（单例）"""
        if self._session is None:
            self._session = Session(
                proxies=self.proxies,
                proxy_auth=self.proxy_auth,
                **self.fingerprint_profile.session_kwargs(
                    timeout=self.config.timeout,
                    verify=self.config.verify_ssl,
                ),
            )
        return self._session

    def request(
        self,
        method: str,
        url: str,
        **kwargs
    ) -> Response:
        """
        发送 HTTP 请求

        Args:
            method: HTTP 方法 (GET, POST, PUT, DELETE, etc.)
            url: 请求 URL
            **kwargs: 其他请求参数

        Returns:
            Response 对象

        Raises:
            HTTPClientError: 请求失败
        """
        request_headers = self.fingerprint_profile.session_headers()
        if "headers" in kwargs and kwargs["headers"]:
            request_headers.update(
                {str(key).lower(): value for key, value in kwargs["headers"].items()}
            )
        kwargs["headers"] = request_headers

        # 设置默认参数
        kwargs.setdefault("timeout", self.config.timeout)
        kwargs.setdefault("allow_redirects", self.config.follow_redirects)
        kwargs.setdefault("impersonate", self.fingerprint_profile.impersonate)
        kwargs.setdefault("extra_fp", self.fingerprint_profile.extra_fp())
        kwargs.setdefault("default_headers", False)

        # 添加代理配置
        if self.proxies and "proxies" not in kwargs:
            kwargs["proxies"] = self.proxies
        if self.proxy_auth and "proxy_auth" not in kwargs:
            kwargs["proxy_auth"] = self.proxy_auth

        last_exception = None
        last_error_message = ""
        for attempt in range(self.config.max_retries):
            try:
                response = self.session.request(method, url, **kwargs)

                # 检查响应状态码
                if response.status_code >= 400:
                    logger.warning(
                        f"HTTP {response.status_code} for {method} {url}"
                        f" (attempt {attempt + 1}/{self.config.max_retries})"
                    )

                    # 如果是服务器错误，重试
                    if response.status_code >= 500 and attempt < self.config.max_retries - 1:
                        time.sleep(self.config.retry_delay * (attempt + 1))
                        continue

                return response

            except (CurlRequestException, ConnectionError, TimeoutError) as e:
                last_exception = e
                if isinstance(
                    e,
                    (CurlProxyError, CurlConnectionError, CurlTimeout, ConnectionError, TimeoutError),
                ):
                    if self.proxy_url:
                        last_error_message = (
                            f"无法通过代理 [{_redact_proxy_url(self.raw_proxy_url or self.proxy_url)}] "
                            f"连接到服务 [{url}]: {e}"
                        )
                    else:
                        last_error_message = f"连接到服务 [{url}] 失败: {e}"
                else:
                    last_error_message = f"请求失败: {method} {url} - {e}"
                logger.warning(
                    f"请求失败: {method} {url} (attempt {attempt + 1}/{self.config.max_retries}): {e}"
                )

                if attempt < self.config.max_retries - 1:
                    time.sleep(self.config.retry_delay * (attempt + 1))
                else:
                    break

        raise HTTPClientError(
            last_error_message or f"请求失败，最大重试次数已达: {method} {url} - {last_exception}"
        )

    def get(self, url: str, **kwargs) -> Response:
        """发送 GET 请求"""
        return self.request("GET", url, **kwargs)

    def post(self, url: str, data: Any = None, json: Any = None, **kwargs) -> Response:
        """发送 POST 请求"""
        return self.request("POST", url, data=data, json=json, **kwargs)

    def put(self, url: str, data: Any = None, json: Any = None, **kwargs) -> Response:
        """发送 PUT 请求"""
        return self.request("PUT", url, data=data, json=json, **kwargs)

    def delete(self, url: str, **kwargs) -> Response:
        """发送 DELETE 请求"""
        return self.request("DELETE", url, **kwargs)

    def head(self, url: str, **kwargs) -> Response:
        """发送 HEAD 请求"""
        return self.request("HEAD", url, **kwargs)

    def options(self, url: str, **kwargs) -> Response:
        """发送 OPTIONS 请求"""
        return self.request("OPTIONS", url, **kwargs)

    def patch(self, url: str, data: Any = None, json: Any = None, **kwargs) -> Response:
        """发送 PATCH 请求"""
        return self.request("PATCH", url, data=data, json=json, **kwargs)

    def download_file(self, url: str, filepath: str, chunk_size: int = 8192) -> None:
        """
        下载文件

        Args:
            url: 文件 URL
            filepath: 保存路径
            chunk_size: 块大小

        Raises:
            HTTPClientError: 下载失败
        """
        try:
            response = self.get(url, stream=True)
            response.raise_for_status()

            with open(filepath, 'wb') as f:
                for chunk in response.iter_content(chunk_size=chunk_size):
                    if chunk:
                        f.write(chunk)

        except Exception as e:
            raise HTTPClientError(f"下载文件失败: {url} - {e}")

    def check_proxy(self, test_url: str = "https://httpbin.org/ip") -> bool:
        """
        检查代理是否可用

        Args:
            test_url: 测试 URL

        Returns:
            bool: 代理是否可用
        """
        if not self.proxy_url:
            return False

        try:
            response = self.get(test_url, timeout=10)
            return response.status_code == 200
        except Exception:
            return False

    def close(self):
        """关闭会话"""
        if self._session:
            self._session.close()
            self._session = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()


class OpenAIHTTPClient(HTTPClient):
    """
    OpenAI 专用 HTTP 客户端
    包含 OpenAI API 特定的请求方法
    """

    def __init__(
        self,
        proxy_url: Optional[str] = None,
        config: Optional[RequestConfig] = None
    ):
        """
        初始化 OpenAI HTTP 客户端

        Args:
            proxy_url: 代理 URL
            config: 请求配置
        """
        super().__init__(proxy_url, config)

        # OpenAI 特定的默认配置
        if config is None:
            self.config.timeout = 30
            self.config.max_retries = 3

        # 默认请求头
        self.default_headers = {
            **self.fingerprint_profile.build_headers(
                url="https://api.openai.com",
                request_kind="api",
            ),
            "connection": "keep-alive",
        }

    def check_ip_location(self) -> Tuple[bool, Optional[str]]:
        """
        检查 IP 地理位置

        Returns:
            Tuple[是否支持, 位置信息]
        """
        attempt_errors = []

        for service_name, checker in (
            ("ip-api.com", self._check_ip_location_with_ip_api),
            ("ifconfig.me/all.json", self._check_ip_location_with_ifconfig),
            ("cloudflare.com/cdn-cgi/trace", self._check_ip_location_with_cloudflare),
        ):
            try:
                supported, location = checker()
                return supported, location
            except HTTPClientError as exc:
                attempt_errors.append(self._format_geo_service_error(service_name, exc))

        error_message = "; ".join(attempt_errors) if attempt_errors else "IP 地理位置检查失败"
        logger.error(f"检查 IP 地理位置失败: {error_message}")
        raise HTTPClientError(error_message)

    def _format_geo_service_error(self, service_name: str, error: Exception) -> str:
        detail = str(error)
        redacted_proxy_url = _redact_proxy_url(self.raw_proxy_url or self.proxy_url)
        if redacted_proxy_url:
            return f"无法通过代理 [{redacted_proxy_url}] 连接到地理位置服务 [{service_name}]: {detail}"
        return f"无法连接到地理位置服务 [{service_name}]: {detail}"

    def _check_ip_location_with_ip_api(self) -> Tuple[bool, str]:
        response = self.get(
            "http://ip-api.com/json/?fields=status,message,country,countryCode,query",
            timeout=10,
        )
        payload = response.json()
        if payload.get("status") != "success":
            message = payload.get("message") or "未知错误"
            raise HTTPClientError(f"ip-api.com 返回失败状态: {message}")
        return self._build_ip_location_result(
            country_code=payload.get("countryCode"),
            country_name=payload.get("country"),
            source="ip-api.com",
        )

    def _check_ip_location_with_ifconfig(self) -> Tuple[bool, str]:
        response = self.get("https://ifconfig.me/all.json", timeout=10)
        payload = response.json()
        country_code = payload.get("country_code") or payload.get("countryCode") or payload.get("country_iso")
        if not country_code:
            public_ip = payload.get("ip_addr") or payload.get("ip")
            detail = "ifconfig.me/all.json 响应缺少国家字段"
            if public_ip:
                detail = f"{detail}，出口 IP: {public_ip}"
            raise HTTPClientError(detail)
        return self._build_ip_location_result(
            country_code=country_code,
            country_name=payload.get("country"),
            source="ifconfig.me/all.json",
        )

    def _check_ip_location_with_cloudflare(self) -> Tuple[bool, str]:
        response = self.get("https://cloudflare.com/cdn-cgi/trace", timeout=10)
        trace_text = response.text
        loc_match = re.search(r"loc=([A-Z]+)", trace_text)
        if not loc_match:
            raise HTTPClientError("cloudflare trace 响应缺少 loc 字段")
        return self._build_ip_location_result(
            country_code=loc_match.group(1),
            country_name=None,
            source="cloudflare.com/cdn-cgi/trace",
        )

    def _build_ip_location_result(
        self,
        *,
        country_code: Optional[str],
        country_name: Optional[str],
        source: str,
    ) -> Tuple[bool, str]:
        code = str(country_code or "").strip().upper()
        if not code:
            raise HTTPClientError(f"{source} 响应缺少国家代码")

        location = code if not country_name else f"{country_name} ({code})"
        if code in BLOCKED_COUNTRY_CODES:
            return False, location
        return True, location

    def send_openai_request(
        self,
        endpoint: str,
        method: str = "POST",
        data: Optional[Dict[str, Any]] = None,
        json_data: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None,
        **kwargs
    ) -> Dict[str, Any]:
        """
        发送 OpenAI API 请求

        Args:
            endpoint: API 端点
            method: HTTP 方法
            data: 表单数据
            json_data: JSON 数据
            headers: 请求头
            **kwargs: 其他参数

        Returns:
            响应 JSON 数据

        Raises:
            HTTPClientError: 请求失败
        """
        header_overrides: Dict[str, str] = {}
        if headers:
            header_overrides.update(headers)

        if json_data is not None and "Content-Type" not in header_overrides:
            header_overrides["Content-Type"] = "application/json"
        elif data is not None and "Content-Type" not in header_overrides:
            header_overrides["Content-Type"] = "application/x-www-form-urlencoded"

        request_headers = self.fingerprint_profile.build_headers(
            url=endpoint,
            request_kind="api",
            headers=header_overrides,
        )

        try:
            response = self.request(
                method,
                endpoint,
                data=data,
                json=json_data,
                headers=request_headers,
                **kwargs
            )

            # 检查响应状态码
            response.raise_for_status()

            # 尝试解析 JSON
            try:
                return response.json()
            except json.JSONDecodeError:
                return {"raw_response": response.text}

        except cffi_requests.RequestsError as e:
            raise HTTPClientError(f"OpenAI 请求失败: {endpoint} - {e}")

    def check_sentinel(self, did: str, proxies: Optional[Dict] = None) -> Optional[str]:
        """
        检查 Sentinel 拦截

        Args:
            did: Device ID
            proxies: 代理配置

        Returns:
            Sentinel token 或 None
        """
        from ..config.constants import OPENAI_API_ENDPOINTS

        try:
            sen_req_body = f'{{"p":"","id":"{did}","flow":"authorize_continue"}}'

            response = self.post(
                OPENAI_API_ENDPOINTS["sentinel"],
                headers=self.fingerprint_profile.build_headers(
                    url=OPENAI_API_ENDPOINTS["sentinel"],
                    request_kind="api",
                    headers={
                        "origin": "https://sentinel.openai.com",
                        "referer": "https://sentinel.openai.com/backend-api/sentinel/frame.html?sv=20260219f9f6",
                        "content-type": "text/plain;charset=UTF-8",
                    },
                ),
                data=sen_req_body,
            )

            if response.status_code == 200:
                return response.json().get("token")
            else:
                logger.warning(f"Sentinel 检查失败: {response.status_code}")
                return None

        except Exception as e:
            logger.error(f"Sentinel 检查异常: {e}")
            return None


def create_http_client(
    proxy_url: Optional[str] = None,
    config: Optional[RequestConfig] = None
) -> HTTPClient:
    """
    创建 HTTP 客户端工厂函数

    Args:
        proxy_url: 代理 URL
        config: 请求配置

    Returns:
        HTTPClient 实例
    """
    return HTTPClient(proxy_url, config)


def create_openai_client(
    proxy_url: Optional[str] = None,
    config: Optional[RequestConfig] = None
) -> OpenAIHTTPClient:
    """
    创建 OpenAI HTTP 客户端工厂函数

    Args:
        proxy_url: 代理 URL
        config: 请求配置

    Returns:
        OpenAIHTTPClient 实例
    """
    return OpenAIHTTPClient(proxy_url, config)
