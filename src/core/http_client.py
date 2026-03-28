"""
HTTP 客户端封装 (CSE 工业加固版 v3)
引入动态指纹熵与集群自适应保护
"""
import time
import json
import random
import logging
from typing import Optional, Dict, Any, Union, Tuple
from dataclasses import dataclass
from curl_cffi import requests as cffi_requests
from curl_cffi.requests import Session, Response

logger = logging.getLogger(__name__)

# 现代浏览器指纹池，增加系统熵 (Entropy)
FINGERPRINT_POOL = [
    {"id": "chrome110", "ua": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/110.0.0.0 Safari/537.36", "ver": "110"},
    {"id": "chrome116", "ua": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/116.0.0.0 Safari/537.36", "ver": "116"},
    {"id": "chrome120", "ua": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36", "ver": "120"},
    {"id": "chrome124", "ua": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36", "ver": "124"},
    {"id": "edge120", "ua": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36 Edg/120.0.0.0", "ver": "120"}
]

@dataclass
class RequestConfig:
    timeout: int = 30
    max_retries: int = 2
    retry_delay: float = 3.0
    verify_ssl: bool = True
    follow_redirects: bool = True

class HTTPClientError(Exception): pass
class OpenAIRiskControlError(HTTPClientError): pass

# 全局熔断状态 (Cluster-level Control Plane)
class GlobalCircuitBreaker:
    _error_count = 0
    _last_error_time = 0
    _lock_until = 0

    @classmethod
    def check(cls):
        if time.time() < cls._lock_until:
            wait_min = int((cls._lock_until - time.time()) / 60)
            raise OpenAIRiskControlError(f"SYSTEM_SLEEPING: Global circuit breaker active. Wait {wait_min}m.")

    @classmethod
    def report_error(cls):
        now = time.time()
        if now - cls._last_error_time < 600: # 10分钟探测窗口
            cls._error_count += 1
        else:
            cls._error_count = 1
        cls._last_error_time = now
        
        if cls._error_count >= 3:
            cls._lock_until = now + 1800 # 触发 30 分钟休眠 (Cool-down)
            print("!!! GLOBAL CIRCUIT BREAKER TRIGGERED !!!")

class HTTPClient:
    def __init__(self, proxy_url=None, config=None, session=None):
        self.proxy_url = proxy_url
        self.config = config or RequestConfig()
        self._session = session

    @property
    def proxies(self):
        return {"http": self.proxy_url, "https": self.proxy_url} if self.proxy_url else None

    def close(self):
        if self._session:
            self._session.close()
            self._session = None

class OpenAIHTTPClient(HTTPClient):
    def __init__(self, proxy_url=None, config=None):
        super().__init__(proxy_url, config)
        self.fp = random.choice(FINGERPRINT_POOL) # 随机指纹

    @property
    def session(self) -> Session:
        if self._session is None:
            self._session = Session(
                proxies=self.proxies,
                impersonate=self.fp["id"],
                verify=self.config.verify_ssl,
                timeout=self.config.timeout
            )
            self._session.headers.update({
                "User-Agent": self.fp["ua"],
                "Sec-CH-UA": f'"Chromium";v="{self.fp["ver"]}", "Not-A.Brand";v="99"',
                "Sec-CH-UA-Mobile": "?0",
                "Sec-CH-UA-Platform": '"Windows"',
                "Accept-Language": "en-US,en;q=0.9",
                "Sec-Fetch-Dest": "empty",
                "Sec-Fetch-Mode": "cors",
                "Sec-Fetch-Site": "same-site",
            })
        return self._session

    def send_openai_request(self, endpoint, method="POST", data=None, json_data=None, headers=None, **kwargs):
        GlobalCircuitBreaker.check()
        
        request_headers = {}
        if headers: request_headers.update(headers)
        
        try:
            # 引入 0.5s - 1.5s 的随机抖动，打破规律性
            time.sleep(random.uniform(0.5, 1.5))
            
            response = self.session.request(
                method, endpoint, data=data, json=json_data, 
                headers=request_headers, **kwargs
            )
            
            if response.status_code == 403:
                GlobalCircuitBreaker.report_error()
                raise OpenAIRiskControlError(f"403 Forbidden (Blocked): {response.text[:100]}")
            
            if response.status_code == 429:
                raise OpenAIRiskControlError("Quota Exceeded / Rate Limited")
                
            response.raise_for_status()
            try: return response.json()
            except: return {"raw": response.text}
            
        except Exception as e:
            if isinstance(e, OpenAIRiskControlError): raise
            raise HTTPClientError(f"Request Error: {e}")

    def check_ip_location(self):
        try:
            r = self.session.get("https://cloudflare.com/cdn-cgi/trace", timeout=10)
            import re
            m = re.search(r"loc=([A-Z]+)", r.text)
            loc = m.group(1) if m else None
            return loc not in ["CN", "HK", "MO"], loc
        except: return False, None

def create_openai_client(proxy_url=None, config=None):
    return OpenAIHTTPClient(proxy_url, config)
