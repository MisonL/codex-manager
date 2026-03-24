"""
注册流程引擎
从 main.py 中提取并重构的注册流程
"""

import base64
import math
import re
import json
import time
import logging
import secrets
from typing import Optional, Dict, Any, Tuple, Callable
from dataclasses import dataclass, field
from datetime import datetime

from curl_cffi import requests as cffi_requests

from .openai.oauth import OAuthManager, OAuthStart
from .http_client import OpenAIHTTPClient, HTTPClientError
from ..services import EmailServiceFactory, BaseEmailService, EmailServiceType
from ..services.base import EmailProviderBackoffState
from ..database import crud
from ..database.session import get_db
from ..config.constants import (
    OPENAI_API_ENDPOINTS,
    OPENAI_PAGE_TYPES,
    calculate_age_from_birthdate,
    generate_random_user_info,
    OTP_CODE_PATTERN,
    DEFAULT_PASSWORD_LENGTH,
    PASSWORD_CHARSET,
    AccountStatus,
    TaskStatus,
)
from ..config.settings import get_settings


logger = logging.getLogger(__name__)

OTP_SECONDARY_TIMEOUT_SECONDS = 120
PHASE_IP_CHECK = "ip_check"
PHASE_EMAIL_PREPARE = "email_prepare"
PHASE_SIGNUP_SUBMIT = "signup_submit"
PHASE_SIGNUP_PASSWORD = "signup_password"
PHASE_OTP_PRIMARY = "otp_primary"
PHASE_ACCOUNT_CREATE = "account_create"
PHASE_OAUTH_REENTER = "oauth_reenter"
PHASE_OTP_SECONDARY = "otp_secondary"
PHASE_WORKSPACE_RESOLVE = "workspace_resolve"
PHASE_OAUTH_CALLBACK = "oauth_callback"
ERROR_EMAIL_PROVIDER_RATE_LIMITED = "EMAIL_PROVIDER_RATE_LIMITED"
ERROR_OTP_TIMEOUT_SECONDARY = "OTP_TIMEOUT_SECONDARY"


@dataclass
class RegistrationResult:
    """注册结果"""
    success: bool
    email: str = ""
    password: str = ""  # 注册密码
    account_id: str = ""
    workspace_id: str = ""
    access_token: str = ""
    refresh_token: str = ""
    id_token: str = ""
    session_token: str = ""  # 会话令牌
    cookies: str = ""  # 浏览器完整 Cookie 字符串
    error_message: str = ""
    error_code: str = ""
    logs: list = None
    metadata: dict = None
    source: str = "register"  # 'register' 或 'login'，区分账号来源

    def to_dict(self) -> Dict[str, Any]:
        """转换为字典"""
        return {
            "success": self.success,
            "email": self.email,
            "password": self.password,
            "account_id": self.account_id,
            "workspace_id": self.workspace_id,
            "access_token": self.access_token[:20] + "..." if self.access_token else "",
            "refresh_token": self.refresh_token[:20] + "..." if self.refresh_token else "",
            "id_token": self.id_token[:20] + "..." if self.id_token else "",
            "session_token": self.session_token[:20] + "..." if self.session_token else "",
            "cookies": self.cookies[:20] + "..." if self.cookies else "",
            "error_message": self.error_message,
            "error_code": self.error_code,
            "logs": self.logs or [],
            "metadata": self.metadata or {},
            "source": self.source,
        }


@dataclass
class SignupFormResult:
    """提交注册表单的结果"""
    success: bool
    page_type: str = ""  # 响应中的 page.type 字段
    is_existing_account: bool = False  # 是否为已注册账号
    response_data: Dict[str, Any] = None  # 完整的响应数据
    error_message: str = ""


@dataclass(frozen=True)
class Budget:
    """阶段超时预算"""

    timeout_seconds: int
    started_at: float

    def remaining_seconds(self, now: Optional[float] = None) -> int:
        current = now if now is not None else time.time()
        remaining = self.timeout_seconds - max(0.0, current - self.started_at)
        return max(0, math.ceil(remaining))


@dataclass(frozen=True)
class PhaseContext:
    """阶段执行上下文"""

    otp_sent_at: Optional[float] = None


@dataclass
class PhaseResult:
    """阶段执行结果"""

    phase: str
    success: bool
    error_message: str = ""
    error_code: str = ""
    retryable: bool = False
    next_action: str = ""
    provider_backoff: Optional[EmailProviderBackoffState] = None
    metadata: Dict[str, Any] = field(default_factory=dict)


class RegistrationEngine:
    """
    注册引擎
    负责协调邮箱服务、OAuth 流程和 OpenAI API 调用
    """

    def __init__(
        self,
        email_service: BaseEmailService,
        proxy_url: Optional[str] = None,
        callback_logger: Optional[Callable[[str], None]] = None,
        status_callback: Optional[Callable[[Dict[str, Any]], None]] = None,
        task_uuid: Optional[str] = None,
    ):
        """
        初始化注册引擎

        Args:
            email_service: 邮箱服务实例
            proxy_url: 代理 URL
            callback_logger: 日志回调函数
            status_callback: 状态回调函数
            task_uuid: 任务 UUID（用于数据库记录）
        """
        self.email_service = email_service
        self.proxy_url = proxy_url
        self.callback_logger = callback_logger or (lambda msg: logger.info(msg))
        self.status_callback = status_callback
        self.task_uuid = task_uuid

        # 创建 HTTP 客户端
        self.http_client = OpenAIHTTPClient(proxy_url=proxy_url)

        # 创建 OAuth 管理器
        settings = get_settings()
        self.oauth_manager = OAuthManager(
            client_id=settings.openai_client_id,
            auth_url=settings.openai_auth_url,
            token_url=settings.openai_token_url,
            redirect_uri=settings.openai_redirect_uri,
            scope=settings.openai_scope,
            proxy_url=proxy_url  # 传递代理配置
        )

        # 状态变量
        self.email: Optional[str] = None
        self.password: Optional[str] = None  # 注册密码
        self.email_info: Optional[Dict[str, Any]] = None
        self.oauth_start: Optional[OAuthStart] = None
        self.session: Optional[cffi_requests.Session] = None
        self.device_id: Optional[str] = None
        self.session_token: Optional[str] = None  # 会话令牌
        self.logs: list = []
        self._otp_sent_at: Optional[float] = None  # OTP 发送时间戳
        self._is_existing_account: bool = False  # 是否为已注册账号（用于自动登录）
        self.phase_history: list[PhaseResult] = []
        self._last_create_account_error: str = ""
        self._pending_continue_url: Optional[str] = None
        self._resolved_workspace_id: Optional[str] = None
        self._callback_url: Optional[str] = None
        self._token_info: Optional[Dict[str, Any]] = None

    def _log(self, message: str, level: str = "info"):
        """记录日志"""
        timestamp = datetime.now().strftime("%H:%M:%S")
        log_message = f"[{timestamp}] {message}"

        # 添加到日志列表
        self.logs.append(log_message)

        # 调用回调函数
        if self.callback_logger:
            self.callback_logger(log_message)

        # 记录到数据库（如果有关联任务）
        if self.task_uuid:
            try:
                with get_db() as db:
                    crud.append_task_log(db, self.task_uuid, log_message)
            except Exception as e:
                logger.warning(f"记录任务日志失败: {e}")

        # 根据级别记录到日志系统
        if level == "error":
            logger.error(message)
        elif level == "warning":
            logger.warning(message)
        else:
            logger.info(message)

    def _generate_password(self, length: int = DEFAULT_PASSWORD_LENGTH) -> str:
        """生成随机密码"""
        return ''.join(secrets.choice(PASSWORD_CHARSET) for _ in range(length))

    def _emit_status(self, phase: str, detail: str, **extra):
        """向外部上报阶段进度。"""
        if not self.status_callback:
            return

        payload = {
            "phase": phase,
            "phase_detail": detail,
        }
        if self.email:
            payload["email"] = self.email
        payload.update({key: value for key, value in extra.items() if value is not None})

        try:
            self.status_callback(payload)
        except Exception as e:
            logger.warning(f"上报任务阶段状态失败: {e}")

    def _current_device_id(self) -> Optional[str]:
        """优先复用现有 Device ID，避免重复触发慢请求。"""
        if self.device_id:
            return self.device_id
        if not self.session:
            return None

        did = self.session.cookies.get("oai-did")
        if did:
            self.device_id = did
        return did

    def _log_timed_http_result(
        self,
        action: str,
        started_at: float,
        response: Optional[Any] = None,
    ):
        """记录 HTTP 调用的耗时与结果。"""
        elapsed = max(0.0, time.time() - started_at)
        parts = [f"{action} 完成，耗时 {elapsed:.1f} 秒"]
        if response is not None:
            status_code = getattr(response, "status_code", None)
            response_url = str(getattr(response, "url", "") or "").strip()
            if status_code is not None:
                parts.append(f"HTTP {status_code}")
            if response_url:
                parts.append(f"URL: {response_url[:120]}...")
        self._log("，".join(parts))

    def _record_phase_result(self, phase_result: PhaseResult) -> PhaseResult:
        self.phase_history = [
            item for item in self.phase_history
            if item.phase != phase_result.phase
        ]
        self.phase_history.append(phase_result)
        return phase_result

    def _get_phase_result(self, phase_name: str) -> Optional[PhaseResult]:
        for phase_result in reversed(self.phase_history):
            if phase_result.phase == phase_name:
                return phase_result
        return None

    def _serialize_provider_backoff(
        self,
        provider_backoff: Optional[EmailProviderBackoffState],
    ) -> Optional[Dict[str, Any]]:
        if provider_backoff is None:
            return None
        return provider_backoff.to_dict()

    def _serialize_phase_result(self, phase_result: PhaseResult) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "success": phase_result.success,
        }
        if phase_result.error_message:
            payload["error_message"] = phase_result.error_message
        if phase_result.error_code:
            payload["error_code"] = phase_result.error_code
        if phase_result.retryable:
            payload["retryable"] = True
        if phase_result.next_action:
            payload["next_action"] = phase_result.next_action
        provider_backoff = self._serialize_provider_backoff(phase_result.provider_backoff)
        if provider_backoff is not None:
            payload["provider_backoff"] = provider_backoff
        if phase_result.metadata:
            payload["metadata"] = dict(phase_result.metadata)
        return payload

    def _collect_phase_history_metadata(self) -> Dict[str, Any]:
        if not self.phase_history:
            return {}
        return {
            "phase_results": {
                phase_result.phase: self._serialize_phase_result(phase_result)
                for phase_result in self.phase_history
            }
        }

    def _complete_phase(
        self,
        phase: str,
        *,
        success: bool,
        error_message: str = "",
        error_code: str = "",
        retryable: bool = False,
        next_action: str = "",
        provider_backoff: Optional[EmailProviderBackoffState] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> PhaseResult:
        return self._record_phase_result(
            PhaseResult(
                phase=phase,
                success=success,
                error_message=error_message,
                error_code=error_code,
                retryable=retryable,
                next_action=next_action,
                provider_backoff=provider_backoff,
                metadata=metadata or {},
            )
        )

    def _phase_ip_check(self) -> PhaseResult:
        ip_ok, location = self._check_ip_location()
        if not ip_ok:
            self._log(f"IP 检查失败: {location}", "error")
            return self._complete_phase(
                PHASE_IP_CHECK,
                success=False,
                error_message=f"IP 地理位置不支持: {location}",
                metadata={"location": location},
            )

        self._log(f"IP 位置: {location}")
        return self._complete_phase(
            PHASE_IP_CHECK,
            success=True,
            metadata={"location": location},
        )

    def _phase_email_prepare(self) -> PhaseResult:
        provider_backoff = getattr(self.email_service, "provider_backoff_state", None)
        if provider_backoff is not None and provider_backoff.is_open():
            remaining_seconds = max(
                0,
                math.ceil(provider_backoff.opened_until - time.time()),
            )
            error_message = (
                f"邮箱服务退避中，需等待 {remaining_seconds} 秒"
                if not provider_backoff.last_error
                else f"{provider_backoff.last_error} (退避剩余 {remaining_seconds} 秒)"
            )
            self._log(error_message, "warning")
            return self._complete_phase(
                PHASE_EMAIL_PREPARE,
                success=False,
                error_message=error_message,
                error_code=ERROR_EMAIL_PROVIDER_RATE_LIMITED,
                retryable=True,
                next_action="switch_provider",
                provider_backoff=provider_backoff,
                metadata={
                    "backoff_active": True,
                    "backoff_remaining_seconds": remaining_seconds,
                },
            )

        success = self._create_email()
        provider_backoff = getattr(self.email_service, "provider_backoff_state", None)

        if success:
            return self._complete_phase(
                PHASE_EMAIL_PREPARE,
                success=True,
                provider_backoff=provider_backoff,
                metadata={"email": self.email},
            )

        error_message = getattr(self.email_service, "last_error", None) or "创建邮箱失败"
        is_rate_limited = bool(
            provider_backoff
            and provider_backoff.failures > 0
            and provider_backoff.delay_seconds > 0
        )
        return self._complete_phase(
            PHASE_EMAIL_PREPARE,
            success=False,
            error_message=error_message,
            error_code=ERROR_EMAIL_PROVIDER_RATE_LIMITED if is_rate_limited else "",
            retryable=is_rate_limited,
            next_action="switch_provider" if is_rate_limited else "",
            provider_backoff=provider_backoff,
        )

    def _phase_signup_submit(self) -> PhaseResult:
        if not self._init_session():
            return self._complete_phase(
                PHASE_SIGNUP_SUBMIT,
                success=False,
                error_message="初始化会话失败",
            )

        if not self._start_oauth():
            return self._complete_phase(
                PHASE_SIGNUP_SUBMIT,
                success=False,
                error_message="开始 OAuth 流程失败",
            )

        did = self._get_device_id()
        if not did:
            return self._complete_phase(
                PHASE_SIGNUP_SUBMIT,
                success=False,
                error_message="获取 Device ID 失败",
            )

        sen_token = self._check_sentinel(did)
        if sen_token:
            self._log("Sentinel 检查通过")
        else:
            self._log("Sentinel 检查失败或未启用", "warning")

        signup_result = self._submit_signup_form(did, sen_token)
        if not signup_result.success:
            return self._complete_phase(
                PHASE_SIGNUP_SUBMIT,
                success=False,
                error_message=f"提交注册表单失败: {signup_result.error_message}",
            )

        return self._complete_phase(
            PHASE_SIGNUP_SUBMIT,
            success=True,
            metadata={
                "device_id": did,
                "page_type": signup_result.page_type,
                "is_existing_account": signup_result.is_existing_account,
            },
        )

    def _phase_signup_password(self) -> PhaseResult:
        if self._is_existing_account:
            self._log("已注册账号跳过密码设置，OTP 已自动发送")
            return self._complete_phase(
                PHASE_SIGNUP_PASSWORD,
                success=True,
                metadata={"skipped": True, "is_existing_account": True},
            )

        password_ok, _ = self._register_password()
        if not password_ok:
            return self._complete_phase(
                PHASE_SIGNUP_PASSWORD,
                success=False,
                error_message="注册密码失败",
            )

        return self._complete_phase(PHASE_SIGNUP_PASSWORD, success=True)

    def _phase_otp_primary(self) -> PhaseResult:
        if self._is_existing_account:
            self._otp_sent_at = time.time()
            self._log("已注册账号跳过发送验证码，使用自动发送的 OTP")
            return self._complete_phase(
                PHASE_OTP_PRIMARY,
                success=True,
                metadata={"skipped": True, "otp_sent_at": self._otp_sent_at},
            )

        if not self._send_verification_code():
            return self._complete_phase(
                PHASE_OTP_PRIMARY,
                success=False,
                error_message="发送验证码失败",
            )

        return self._complete_phase(
            PHASE_OTP_PRIMARY,
            success=True,
            metadata={"otp_sent_at": self._otp_sent_at},
        )

    def _check_ip_location(self) -> Tuple[bool, Optional[str]]:
        """检查 IP 地理位置"""
        try:
            return self.http_client.check_ip_location()
        except Exception as e:
            self._log(f"检查 IP 地理位置失败: {e}", "error")
            return False, None

    def _create_email(self) -> bool:
        """创建邮箱"""
        try:
            self._log(f"正在创建 {self.email_service.service_type.value} 邮箱...")
            self.email_info = self.email_service.create_email()

            if not self.email_info or "email" not in self.email_info:
                self._log("创建邮箱失败: 返回信息不完整", "error")
                return False

            self.email = self.email_info["email"]
            self._log(f"成功创建邮箱: {self.email}")
            return True

        except Exception as e:
            self._log(f"创建邮箱失败: {e}", "error")
            return False

    def _start_oauth(self) -> bool:
        """开始 OAuth 流程"""
        try:
            self._log("开始 OAuth 授权流程...")
            self.oauth_start = self.oauth_manager.start_oauth()
            self._log(f"OAuth URL 已生成: {self.oauth_start.auth_url[:80]}...")
            return True
        except Exception as e:
            self._log(f"生成 OAuth URL 失败: {e}", "error")
            return False

    def _init_session(self) -> bool:
        """初始化会话"""
        try:
            self.session = self.http_client.session
            return True
        except Exception as e:
            self._log(f"初始化会话失败: {e}", "error")
            return False

    def close(self):
        """关闭注册流程占用的资源"""
        if self.session:
            try:
                self.session.close()
            except Exception as e:
                self._log(f"关闭注册会话失败: {e}", "warning")
            finally:
                self.session = None

        try:
            self.http_client.close()
        except Exception as e:
            self._log(f"关闭 HTTP 客户端失败: {e}", "warning")

        close_email_service = getattr(self.email_service, "close", None)
        if callable(close_email_service):
            try:
                close_email_service()
            except Exception as e:
                self._log(f"关闭邮箱服务失败: {e}", "warning")

    def _get_device_id(self) -> Optional[str]:
        """获取 Device ID"""
        if not self.oauth_start:
            return None

        cached_did = self._current_device_id()
        if cached_did:
            self._log(f"复用已有 Device ID: {cached_did}")
            return cached_did

        max_attempts = 3
        for attempt in range(1, max_attempts + 1):
            try:
                if not self.session:
                    self.session = self.http_client.session

                self._emit_status(
                    "oauth_device_id",
                    f"获取 Device ID（第 {attempt}/{max_attempts} 次）",
                    attempt=attempt,
                    max_attempts=max_attempts,
                )
                started_at = time.time()
                response = self.session.get(
                    self.oauth_start.auth_url,
                    timeout=20
                )
                self._log_timed_http_result("获取 Device ID 请求", started_at, response)
                did = self.session.cookies.get("oai-did")

                if did:
                    self.device_id = did
                    self._log(f"Device ID: {did}")
                    return did

                self._log(
                    f"获取 Device ID 失败: 未返回 oai-did Cookie (HTTP {response.status_code}, 第 {attempt}/{max_attempts} 次)",
                    "warning" if attempt < max_attempts else "error"
                )
            except Exception as e:
                self._log(
                    f"获取 Device ID 失败: {e} (第 {attempt}/{max_attempts} 次)",
                    "warning" if attempt < max_attempts else "error"
                )

            if attempt < max_attempts:
                time.sleep(attempt)
                self.http_client.close()
                self.session = self.http_client.session

        return None

    def _check_sentinel(self, did: str) -> Optional[str]:
        """检查 Sentinel 拦截"""
        try:
            device_id = did or self._current_device_id()
            if not device_id:
                self._log("Sentinel 检查跳过: 缺少 Device ID", "warning")
                return None

            self._emit_status("sentinel", "请求 Sentinel 校验令牌")
            sen_req_body = f'{{"p":"","id":"{device_id}","flow":"authorize_continue"}}'

            started_at = time.time()
            response = self.http_client.post(
                OPENAI_API_ENDPOINTS["sentinel"],
                headers={
                    "origin": "https://sentinel.openai.com",
                    "referer": "https://sentinel.openai.com/backend-api/sentinel/frame.html?sv=20260219f9f6",
                    "content-type": "text/plain;charset=UTF-8",
                },
                data=sen_req_body,
            )
            self._log_timed_http_result("Sentinel 校验", started_at, response)

            if response.status_code == 200:
                sen_token = response.json().get("token")
                self._log(f"Sentinel token 获取成功")
                return sen_token
            else:
                self._log(f"Sentinel 检查失败: {response.status_code}", "warning")
                return None

        except Exception as e:
            self._log(f"Sentinel 检查异常: {e}", "warning")
            return None

    def _submit_signup_form(self, did: str, sen_token: Optional[str]) -> SignupFormResult:
        """
        提交注册表单

        Returns:
            SignupFormResult: 提交结果，包含账号状态判断
        """
        try:
            signup_body = f'{{"username":{{"value":"{self.email}","kind":"email"}},"screen_hint":"signup"}}'

            headers = {
                "referer": "https://auth.openai.com/create-account",
                "accept": "application/json",
                "content-type": "application/json",
            }

            if sen_token:
                sentinel = f'{{"p": "", "t": "", "c": "{sen_token}", "id": "{did}", "flow": "authorize_continue"}}'
                headers["openai-sentinel-token"] = sentinel

            response = self.session.post(
                OPENAI_API_ENDPOINTS["signup"],
                headers=headers,
                data=signup_body,
            )

            self._log(f"提交注册表单状态: {response.status_code}")

            if response.status_code != 200:
                return SignupFormResult(
                    success=False,
                    error_message=f"HTTP {response.status_code}: {response.text[:200]}"
                )

            # 解析响应判断账号状态
            try:
                response_data = response.json()
                page_type = response_data.get("page", {}).get("type", "")
                self._log(f"响应页面类型: {page_type}")

                # 判断是否为已注册账号
                is_existing = page_type == OPENAI_PAGE_TYPES["EMAIL_OTP_VERIFICATION"]

                if is_existing:
                    self._log(f"检测到已注册账号，将自动切换到登录流程")
                    self._is_existing_account = True

                return SignupFormResult(
                    success=True,
                    page_type=page_type,
                    is_existing_account=is_existing,
                    response_data=response_data
                )

            except Exception as parse_error:
                self._log(f"解析响应失败: {parse_error}", "warning")
                # 无法解析，默认成功
                return SignupFormResult(success=True)

        except Exception as e:
            self._log(f"提交注册表单失败: {e}", "error")
            return SignupFormResult(success=False, error_message=str(e))

    def _register_password(self) -> Tuple[bool, Optional[str]]:
        """注册密码"""
        try:
            # 生成密码
            password = self._generate_password()
            self.password = password  # 保存密码到实例变量
            self._log(f"生成密码: {password}")

            # 提交密码注册
            register_body = json.dumps({
                "password": password,
                "username": self.email
            })

            response = self.session.post(
                OPENAI_API_ENDPOINTS["register"],
                headers={
                    "referer": "https://auth.openai.com/create-account/password",
                    "accept": "application/json",
                    "content-type": "application/json",
                },
                data=register_body,
            )

            self._log(f"提交密码状态: {response.status_code}")

            if response.status_code != 200:
                error_text = response.text[:500]
                self._log(f"密码注册失败: {error_text}", "warning")

                # 解析错误信息，判断是否是邮箱已注册
                try:
                    error_json = response.json()
                    error_msg = error_json.get("error", {}).get("message", "")
                    error_code = error_json.get("error", {}).get("code", "")

                    # 检测邮箱已注册的情况
                    if "already" in error_msg.lower() or "exists" in error_msg.lower() or error_code == "user_exists":
                        self._log(f"邮箱 {self.email} 可能已在 OpenAI 注册过", "error")
                        # 标记此邮箱为已注册状态
                        self._mark_email_as_registered()
                except Exception:
                    pass

                return False, None

            return True, password

        except Exception as e:
            self._log(f"密码注册失败: {e}", "error")
            return False, None

    def _mark_email_as_registered(self):
        """标记邮箱为已注册状态（用于防止重复尝试）"""
        try:
            with get_db() as db:
                # 检查是否已存在该邮箱的记录
                existing = crud.get_account_by_email(db, self.email)
                if not existing:
                    # 创建一个失败记录，标记该邮箱已注册过
                    crud.create_account(
                        db,
                        email=self.email,
                        password="",  # 空密码表示未成功注册
                        email_service=self.email_service.service_type.value,
                        email_service_id=self.email_info.get("service_id") if self.email_info else None,
                        status="failed",
                        extra_data={"register_failed_reason": "email_already_registered_on_openai"}
                    )
                    self._log(f"已在数据库中标记邮箱 {self.email} 为已注册状态")
        except Exception as e:
            logger.warning(f"标记邮箱状态失败: {e}")

    def _send_verification_code(
        self,
        referer: str = "https://auth.openai.com/create-account/password",
    ) -> bool:
        """发送验证码"""
        try:
            # 记录发送时间戳
            self._otp_sent_at = time.time()

            response = self.session.get(
                OPENAI_API_ENDPOINTS["send_otp"],
                headers={
                    "referer": referer,
                    "accept": "application/json",
                },
            )

            self._log(f"验证码发送状态: {response.status_code}")
            return response.status_code == 200

        except Exception as e:
            self._log(f"发送验证码失败: {e}", "error")
            return False

    def _get_verification_code(self) -> Optional[str]:
        """获取验证码"""
        code, _ = self._await_secondary_otp_code(
            PhaseContext(otp_sent_at=self._otp_sent_at),
            started_at=time.time(),
        )
        return code

    def _await_secondary_otp_code(
        self,
        context: PhaseContext,
        started_at: Optional[float] = None,
        *,
        record_phase: bool = True,
    ) -> Tuple[Optional[str], PhaseResult]:
        """等待二次验证码邮件并做超时归因。"""
        try:
            self._log(f"正在等待邮箱 {self.email} 的验证码...")

            email_id = self.email_info.get("service_id") if self.email_info else None
            budget = Budget(
                timeout_seconds=OTP_SECONDARY_TIMEOUT_SECONDS,
                started_at=started_at if started_at is not None else time.time(),
            )
            remaining_timeout = budget.remaining_seconds()

            if remaining_timeout <= 0:
                phase_result = PhaseResult(
                    phase=PHASE_OTP_SECONDARY,
                    success=False,
                    error_message="等待验证码超时",
                    error_code=ERROR_OTP_TIMEOUT_SECONDARY,
                    retryable=True,
                    next_action="await_email",
                    metadata={
                        "budget_started_at": budget.started_at,
                        "budget_timeout_seconds": budget.timeout_seconds,
                        "otp_sent_at": context.otp_sent_at,
                    },
                )
                if record_phase:
                    phase_result = self._record_phase_result(phase_result)
                self._log(phase_result.error_message, "error")
                return None, phase_result

            code = self.email_service.get_verification_code(
                email=self.email,
                email_id=email_id,
                timeout=remaining_timeout,
                pattern=OTP_CODE_PATTERN,
                otp_sent_at=context.otp_sent_at,
            )

            if code:
                self._log(f"成功获取验证码: {code}")
                phase_result = PhaseResult(
                    phase=PHASE_OTP_SECONDARY,
                    success=True,
                    metadata={
                        "budget_started_at": budget.started_at,
                        "budget_timeout_seconds": budget.timeout_seconds,
                        "otp_sent_at": context.otp_sent_at,
                    },
                )
                if record_phase:
                    phase_result = self._record_phase_result(phase_result)
                return code, phase_result

            phase_result = PhaseResult(
                phase=PHASE_OTP_SECONDARY,
                success=False,
                error_message="等待验证码超时",
                error_code=ERROR_OTP_TIMEOUT_SECONDARY,
                retryable=True,
                next_action="await_email",
                metadata={
                    "budget_started_at": budget.started_at,
                    "budget_timeout_seconds": budget.timeout_seconds,
                    "otp_sent_at": context.otp_sent_at,
                },
            )
            if record_phase:
                phase_result = self._record_phase_result(phase_result)
            self._log(phase_result.error_message, "error")
            return None, phase_result

        except Exception as e:
            self._log(f"获取验证码失败: {e}", "error")
            phase_result = PhaseResult(
                phase=PHASE_OTP_SECONDARY,
                success=False,
                error_message=str(e),
                metadata={"otp_sent_at": context.otp_sent_at},
            )
            if record_phase:
                phase_result = self._record_phase_result(phase_result)
            return None, phase_result

    def _phase_otp_secondary(
        self,
        context: Optional[PhaseContext] = None,
        started_at: Optional[float] = None,
    ):
        if context is not None or started_at is not None:
            return self._await_secondary_otp_code(
                context or PhaseContext(otp_sent_at=self._otp_sent_at),
                started_at=started_at,
                record_phase=True,
            )

        code, wait_phase = self._await_secondary_otp_code(
            PhaseContext(otp_sent_at=self._otp_sent_at),
            started_at=time.time(),
            record_phase=False,
        )
        if not code:
            return self._record_phase_result(wait_phase)

        valid, continue_url = self._validate_verification_code_and_get_continue_url(code)
        if not valid:
            return self._complete_phase(
                PHASE_OTP_SECONDARY,
                success=False,
                error_message="验证验证码失败",
                metadata={"otp_sent_at": self._otp_sent_at},
            )

        self._pending_continue_url = continue_url or None
        return self._complete_phase(
            PHASE_OTP_SECONDARY,
            success=True,
            metadata={
                **wait_phase.metadata,
                "continue_url": self._pending_continue_url,
            },
        )

    def _validate_verification_code(self, code: str) -> bool:
        """验证验证码"""
        try:
            code_body = f'{{"code":"{code}"}}'

            response = self.session.post(
                OPENAI_API_ENDPOINTS["validate_otp"],
                headers={
                    "referer": "https://auth.openai.com/email-verification",
                    "accept": "application/json",
                    "content-type": "application/json",
                },
                data=code_body,
            )

            self._log(f"验证码校验状态: {response.status_code}")
            return response.status_code == 200

        except Exception as e:
            self._log(f"验证验证码失败: {e}", "error")
            return False

    def _create_user_account(self) -> bool:
        """创建用户账户"""
        try:
            self._last_create_account_error = ""
            user_info = generate_random_user_info()
            age = calculate_age_from_birthdate(user_info["birthdate"])
            self._log(f"生成用户信息: {user_info['name']}, 生日: {user_info['birthdate']}")

            if age < 18:
                self._last_create_account_error = (
                    f"本地年龄校验失败: birthdate={user_info['birthdate']}, age={age}"
                )
                self._log(self._last_create_account_error, "error")
                return False

            create_account_body = json.dumps(user_info)

            response = self.session.post(
                OPENAI_API_ENDPOINTS["create_account"],
                headers={
                    "referer": "https://auth.openai.com/about-you",
                    "accept": "application/json",
                    "content-type": "application/json",
                },
                data=create_account_body,
            )

            self._log(f"账户创建状态: {response.status_code}")

            if response.status_code == 400:
                self._last_create_account_error = response.text
                self._log(f"账户创建失败: {response.text}", "warning")
                return False

            if response.status_code != 200:
                self._last_create_account_error = f"HTTP {response.status_code}: {response.text[:200]}"
                self._log(f"账户创建失败: {self._last_create_account_error}", "warning")
                return False

            return True

        except Exception as e:
            self._last_create_account_error = str(e)
            self._log(f"创建账户失败: {e}", "error")
            return False

    def _phase_account_create(self) -> PhaseResult:
        if self._is_existing_account:
            self._log("已注册账号跳过创建用户账户")
            return self._complete_phase(
                PHASE_ACCOUNT_CREATE,
                success=True,
                metadata={"skipped": True, "is_existing_account": True},
            )

        if not self._create_user_account():
            return self._complete_phase(
                PHASE_ACCOUNT_CREATE,
                success=False,
                error_message=self._last_create_account_error or "创建用户账户失败",
            )

        return self._complete_phase(PHASE_ACCOUNT_CREATE, success=True)

    def _get_workspace_id(self) -> Optional[str]:
        """获取 Workspace ID"""
        try:
            cookie_names = (
                "oai-client-auth-session",
                "oai_client_auth_session",
                "oai-client-auth-info",
                "oai_client_auth_info",
            )
            found_cookie = False

            for cookie_name in cookie_names:
                auth_cookie = self.session.cookies.get(cookie_name)
                if not auth_cookie:
                    continue

                found_cookie = True
                workspace_id = self._extract_workspace_id_from_cookie(auth_cookie)
                if workspace_id:
                    self._log(f"Workspace ID: {workspace_id}")
                    return workspace_id

            if not found_cookie:
                self._log("未能获取到授权 Cookie", "error")
                return None

            self._log("授权 Cookie 里没有 workspace 信息", "error")
            return None

        except Exception as e:
            self._log(f"获取 Workspace ID 失败: {e}", "error")
            return None

    def _extract_workspace_id_from_cookie(self, cookie_value: str) -> Optional[str]:
        """从授权 Cookie 中提取 Workspace ID。"""
        for auth_json in self._decode_cookie_json_candidates(cookie_value):
            workspace_id = self._extract_workspace_id_from_auth_json(auth_json)
            if workspace_id:
                return workspace_id
        return None

    def _extract_workspace_id_from_text(self, text: str) -> Optional[str]:
        """从 HTML/脚本文本中提取 Workspace ID。"""
        if not text:
            return None

        patterns = [
            r'"workspace_id"\s*:\s*"([^"]+)"',
            r'"workspaceId"\s*:\s*"([^"]+)"',
            r'"default_workspace_id"\s*:\s*"([^"]+)"',
            r'"defaultWorkspaceId"\s*:\s*"([^"]+)"',
            r'"active_workspace_id"\s*:\s*"([^"]+)"',
            r'"activeWorkspaceId"\s*:\s*"([^"]+)"',
            r'"workspace"\s*:\s*\{[^{}]*"id"\s*:\s*"([^"]+)"',
            r'"default_workspace"\s*:\s*\{[^{}]*"id"\s*:\s*"([^"]+)"',
            r'"active_workspace"\s*:\s*\{[^{}]*"id"\s*:\s*"([^"]+)"',
        ]
        for pattern in patterns:
            match = re.search(pattern, text)
            if match:
                workspace_id = str(match.group(1) or "").strip()
                if workspace_id:
                    return workspace_id
        return None

    def _extract_workspace_id_from_url(self, url: str) -> Optional[str]:
        """从 URL 查询参数或片段中提取 Workspace ID。"""
        if not url:
            return None

        import urllib.parse

        parsed = urllib.parse.urlparse(url)
        for raw_query in (parsed.query, parsed.fragment):
            query = urllib.parse.parse_qs(raw_query)
            for key in (
                "workspace_id",
                "workspaceId",
                "default_workspace_id",
                "active_workspace_id",
            ):
                values = query.get(key) or []
                if values:
                    workspace_id = str(values[0] or "").strip()
                    if workspace_id:
                        return workspace_id
        return None

    def _decode_cookie_json_candidates(self, cookie_value: str) -> list[Dict[str, Any]]:
        """尝试从完整 Cookie 或其分段中解码出 JSON。"""
        decoded_objects = []
        candidates = [cookie_value]

        if "." in cookie_value:
            candidates.extend(cookie_value.split("."))

        for candidate in candidates:
            raw = (candidate or "").strip()
            if not raw:
                continue

            pad = "=" * ((4 - (len(raw) % 4)) % 4)
            try:
                decoded = base64.urlsafe_b64decode((raw + pad).encode("ascii"))
            except Exception:
                continue

            try:
                payload = json.loads(decoded.decode("utf-8"))
            except Exception:
                continue

            if isinstance(payload, dict):
                decoded_objects.append(payload)

        return decoded_objects

    def _extract_workspace_id_from_auth_json(self, auth_json: Dict[str, Any]) -> Optional[str]:
        """从解码后的授权 JSON 中提取 Workspace ID。"""
        workspaces = auth_json.get("workspaces") or []
        if isinstance(workspaces, list):
            for workspace in workspaces:
                if not isinstance(workspace, dict):
                    continue

                workspace_id = str(workspace.get("id") or "").strip()
                if workspace_id:
                    return workspace_id

        for key in (
            "workspace_id",
            "workspaceId",
            "default_workspace_id",
            "defaultWorkspaceId",
            "active_workspace_id",
            "activeWorkspaceId",
        ):
            workspace_id = str(auth_json.get(key) or "").strip()
            if workspace_id:
                return workspace_id

        for key in (
            "workspace",
            "default_workspace",
            "active_workspace",
            "defaultWorkspace",
            "activeWorkspace",
        ):
            workspace = auth_json.get(key)
            if not isinstance(workspace, dict):
                continue

            workspace_id = str(workspace.get("id") or "").strip()
            if workspace_id:
                return workspace_id

        return None

    def _extract_workspace_id_from_response(
        self,
        response: Optional[Any] = None,
        html: Optional[str] = None,
        url: Optional[str] = None,
    ) -> Optional[str]:
        """统一从响应 JSON、HTML、脚本内容和 URL 中提取 Workspace ID。"""
        response_url = str(getattr(response, "url", "") or "").strip()
        response_text = html if html is not None else str(getattr(response, "text", "") or "")
        candidate_url = url or response_url

        if response is not None:
            try:
                payload = response.json()
            except Exception:
                payload = None
            workspace_id = self._extract_workspace_id_from_response_payload(payload)
            if workspace_id:
                return workspace_id

        for extractor in (
            lambda: self._extract_workspace_id_from_html(response_text),
            lambda: self._extract_workspace_id_from_text(response_text),
            lambda: self._extract_workspace_id_from_url(candidate_url),
        ):
            workspace_id = extractor()
            if workspace_id:
                return workspace_id

        return None

    def _extract_workspace_id_from_response_payload(self, payload: Any, depth: int = 0) -> Optional[str]:
        """递归扫描响应载荷中的 Workspace ID。"""
        if payload is None or depth > 5:
            return None

        if isinstance(payload, dict):
            workspace_id = self._extract_workspace_id_from_auth_json(payload)
            if workspace_id:
                return workspace_id
            for value in payload.values():
                workspace_id = self._extract_workspace_id_from_response_payload(value, depth + 1)
                if workspace_id:
                    return workspace_id
            return None

        if isinstance(payload, list):
            for item in payload:
                workspace_id = self._extract_workspace_id_from_response_payload(item, depth + 1)
                if workspace_id:
                    return workspace_id

        return None

    def _select_workspace(self, workspace_id: str) -> Optional[str]:
        """选择 Workspace"""
        try:
            select_body = f'{{"workspace_id":"{workspace_id}"}}'

            response = self.session.post(
                OPENAI_API_ENDPOINTS["select_workspace"],
                headers={
                    "referer": "https://auth.openai.com/sign-in-with-chatgpt/codex/consent",
                    "content-type": "application/json",
                },
                data=select_body,
            )

            if response.status_code != 200:
                self._log(f"选择 workspace 失败: {response.status_code}", "error")
                self._log(f"响应: {response.text[:200]}", "warning")
                return None

            continue_url = str((response.json() or {}).get("continue_url") or "").strip()
            if not continue_url:
                self._log("workspace/select 响应里缺少 continue_url", "error")
                return None

            self._log(f"Continue URL: {continue_url[:100]}...")
            return continue_url

        except Exception as e:
            self._log(f"选择 Workspace 失败: {e}", "error")
            return None

    def _extract_workspace_id_from_html(self, html: str) -> Optional[str]:
        if not html:
            return None

        patterns = [
            r'name="workspace_id"[^>]*value="([^"]+)"',
            r"name='workspace_id'[^>]*value='([^']+)'",
        ]
        for pattern in patterns:
            match = re.search(pattern, html)
            if match:
                workspace_id = str(match.group(1) or "").strip()
                if workspace_id:
                    return workspace_id
        return None

    def _extract_hidden_input_value(self, html: str, input_name: str) -> Optional[str]:
        if not html or not input_name:
            return None

        escaped = re.escape(input_name)
        patterns = [
            rf'name="{escaped}"[^>]*value="([^"]+)"',
            rf"name='{escaped}'[^>]*value='([^']+)'",
        ]
        for pattern in patterns:
            match = re.search(pattern, html)
            if match:
                value = str(match.group(1) or "").strip()
                if value:
                    return value
        return None

    def _extract_consent_verifier(self, url: str) -> Optional[str]:
        if not url:
            return None

        import urllib.parse

        parsed = urllib.parse.urlparse(url)
        query = urllib.parse.parse_qs(parsed.query)
        values = query.get("consent_verifier") or []
        if values:
            return str(values[0] or "").strip() or None
        return None

    def _try_reenter_login_flow(self) -> bool:
        if not self.oauth_start:
            return False

        try:
            self._emit_status("login_reentry", "重新进入登录流程")
            did = self._current_device_id()
            sen_token = self._check_sentinel(did) if did else None
            self._log("登录重入：请求 authorize 页面以确认当前表单状态")
            started_at = time.time()
            response = self.session.get(
                self.oauth_start.auth_url,
                timeout=15,
            )
            self._log_timed_http_result("登录重入 authorize 页面", started_at, response)
            html = response.text or ""

            if "/log-in/password" in str(getattr(response, "url", "") or "") or 'action="/log-in/password"' in html:
                self._log("重新进入登录流程：检测到密码页")
                return True

            if "/log-in" in str(getattr(response, "url", "") or "") or 'action="/log-in"' in html:
                login_data = {
                    "username": {
                        "kind": "email",
                        "value": self.email,
                    }
                }
                self._emit_status("login_reentry", "提交邮箱以推进到密码页")
                self._log("登录重入：提交邮箱到 authorize/continue")
                started_at = time.time()
                login_response = self.session.post(
                    "https://auth.openai.com/api/accounts/authorize/continue",
                    headers={
                        "referer": "https://auth.openai.com/log-in",
                        "accept": "application/json",
                        "content-type": "application/json",
                        **(
                            {
                                "openai-sentinel-token": json.dumps(
                                    {
                                        "p": "",
                                        "t": "",
                                        "c": sen_token,
                                        "id": did,
                                        "flow": "authorize_continue",
                                    }
                                )
                            }
                            if sen_token and did
                            else {}
                        ),
                    },
                    data=json.dumps(login_data),
                    timeout=15,
                )
                self._log_timed_http_result("登录重入邮箱提交", started_at, login_response)
                login_json = login_response.json() if login_response.status_code == 200 else {}
                page_type = str((login_json or {}).get("page", {}).get("type") or "").strip()
                continue_url = str((login_json or {}).get("continue_url") or "").strip()
                self._log(
                    f"登录重入响应: page_type={page_type or 'unknown'}, "
                    f"continue_url={continue_url[:100] + '...' if continue_url else 'none'}"
                )
                if continue_url:
                    try:
                        self._emit_status("login_reentry", "跟进登录 continue_url")
                        started_at = time.time()
                        self.session.get(continue_url, timeout=15)
                        self._log_timed_http_result("登录重入 continue_url", started_at)
                    except Exception:
                        pass
                if login_response.status_code == 200 and page_type in {"password", "login_password"}:
                    self._log("重新进入登录流程：已推进到密码页")
                    return True
                if login_response.status_code == 200 and "/log-in/password" in continue_url:
                    self._log("重新进入登录流程：已推进到密码页")
                    return True
            return False
        except Exception as e:
            self._log(f"重新进入登录流程失败: {e}", "warning")
            return False

    def _submit_login_password_step(self) -> bool:
        if not self.email or not self.password:
            return False

        try:
            self._emit_status("login_password", "提交登录密码")
            did = self._current_device_id()
            sen_token = self._check_sentinel(did) if did else None
            started_at = time.time()
            response = self.session.post(
                "https://auth.openai.com/api/accounts/password/verify",
                headers={
                    "referer": "https://auth.openai.com/log-in/password",
                    "accept": "application/json",
                    "content-type": "application/json",
                    **(
                        {
                            "openai-sentinel-token": json.dumps(
                                {
                                    "p": "",
                                    "t": "",
                                    "c": sen_token,
                                    "id": did,
                                    "flow": "password_verify",
                                }
                            )
                        }
                        if sen_token and did
                        else {}
                    ),
                },
                data=json.dumps({
                    "password": self.password,
                }),
                timeout=15,
            )
            self._log_timed_http_result("登录密码提交", started_at, response)
            self._log(f"登录密码提交状态: {response.status_code}")
            if response.status_code == 200:
                try:
                    payload = response.json() or {}
                except Exception:
                    payload = {}
                continue_url = str(payload.get("continue_url") or "").strip()
                if continue_url:
                    try:
                        self._emit_status("login_password", "跟进密码校验 continue_url")
                        started_at = time.time()
                        self.session.get(continue_url, timeout=15)
                        self._log_timed_http_result("密码校验 continue_url", started_at)
                    except Exception:
                        pass
            return response.status_code in (200, 302, 303)
        except Exception as e:
            self._log(f"登录密码提交失败: {e}", "warning")
            return False

    def _submit_login_password_step_and_get_continue_url(self) -> Tuple[bool, Optional[str]]:
        if not self.email or not self.password:
            return False, None

        try:
            did = self._current_device_id()
            sen_token = self._check_sentinel(did) if did else None
            response = self.session.post(
                "https://auth.openai.com/api/accounts/password/verify",
                headers={
                    "referer": "https://auth.openai.com/log-in/password",
                    "accept": "application/json",
                    "content-type": "application/json",
                    **(
                        {
                            "openai-sentinel-token": json.dumps(
                                {
                                    "p": "",
                                    "t": "",
                                    "c": sen_token,
                                    "id": did,
                                    "flow": "password_verify",
                                }
                            )
                        }
                        if sen_token and did
                        else {}
                    ),
                },
                data=json.dumps({
                    "password": self.password,
                }),
                timeout=15,
            )
            self._log(f"登录密码提交状态: {response.status_code}")
            if response.status_code not in (200, 302, 303):
                return False, None

            try:
                payload = response.json() or {}
            except Exception:
                payload = {}
            continue_url = str(payload.get("continue_url") or "").strip() or None
            if continue_url:
                try:
                    self.session.get(continue_url, timeout=15)
                except Exception:
                    pass
            return True, continue_url
        except Exception as e:
            self._log(f"登录密码提交失败: {e}", "warning")
            return False, None

    def _validate_verification_code_and_get_continue_url(self, code: str) -> Tuple[bool, Optional[str]]:
        try:
            code_body = f'{{"code":"{code}"}}'

            response = self.session.post(
                OPENAI_API_ENDPOINTS["validate_otp"],
                headers={
                    "referer": "https://auth.openai.com/email-verification",
                    "accept": "application/json",
                    "content-type": "application/json",
                },
                data=code_body,
            )

            self._log(f"验证码校验状态: {response.status_code}")
            if response.status_code != 200:
                return False, None

            try:
                payload = response.json() or {}
            except Exception:
                payload = {}
            continue_url = str(payload.get("continue_url") or "").strip() or None
            return True, continue_url
        except Exception as e:
            self._log(f"验证验证码失败: {e}", "error")
            return False, None

    def _advance_login_authorization(self) -> Tuple[Optional[str], Optional[str]]:
        if not self.oauth_start:
            return None, None

        if not self._init_session():
            self._log("重新初始化登录会话失败", "warning")
            return None, None

        self._emit_status("oauth_reentry", "重新初始化 OAuth 登录会话")
        if not self._start_oauth():
            self._log("重新开始 OAuth 登录流程失败", "warning")
            return None, None

        if not self._get_device_id():
            self._log("重新登录流程获取 Device ID 失败", "warning")
            return None, None

        if not self._try_reenter_login_flow():
            self._log("未能重新进入登录流程", "warning")
            return None, None

        self._otp_sent_at = time.time()
        self._emit_status("otp_secondary", "等待登录验证码邮件")

        if not self._submit_login_password_step():
            return None, None

        code = self._get_verification_code()
        if not code:
            self._log("登录流程获取验证码失败", "warning")
            return None, None

        valid, consent_url = self._validate_verification_code_and_get_continue_url(code)
        if not valid:
            self._log("登录流程验证码校验失败", "warning")
            return None, None

        auth_target = consent_url or self.oauth_start.auth_url
        self._emit_status("workspace_extract", "请求 consent 页面并提取 Workspace ID")
        self._log(f"请求 consent 页面: {auth_target[:120]}...")
        started_at = time.time()
        auth_response = self.session.get(auth_target, timeout=20)
        self._log_timed_http_result("获取 consent 页面", started_at, auth_response)
        current_url = str(getattr(auth_response, "url", "") or "")
        html = auth_response.text or ""

        if "sign-in-with-chatgpt/codex/consent" in current_url or 'action="/sign-in-with-chatgpt/codex/consent"' in html:
            workspace_id = self._extract_workspace_id_from_response(response=auth_response, html=html, url=current_url)
            if not workspace_id:
                self._log("consent 页面缺少 workspace_id，回退到 Cookie 解析路径", "warning")
                return None, None

            continue_url = self._select_workspace(workspace_id)
            if not continue_url:
                return None, None

            callback_url = self._follow_redirects(continue_url)
            return workspace_id, callback_url

        return None, None

    def _phase_oauth_reenter(self) -> PhaseResult:
        if self._is_existing_account:
            self._log("已注册账号跳过 OAuth 重入")
            return self._complete_phase(
                PHASE_OAUTH_REENTER,
                success=True,
                metadata={"skipped": True, "is_existing_account": True},
            )

        self._pending_continue_url = None
        self._callback_url = None
        self._resolved_workspace_id = None

        if not self._init_session():
            self._log("重新初始化登录会话失败", "warning")
            return self._complete_phase(
                PHASE_OAUTH_REENTER,
                success=False,
                error_message="重新初始化登录会话失败",
                retryable=True,
                next_action=PHASE_WORKSPACE_RESOLVE,
            )

        if not self._start_oauth():
            self._log("重新开始 OAuth 登录流程失败", "warning")
            return self._complete_phase(
                PHASE_OAUTH_REENTER,
                success=False,
                error_message="重新开始 OAuth 登录流程失败",
                retryable=True,
                next_action=PHASE_WORKSPACE_RESOLVE,
            )

        if not self._get_device_id():
            self._log("重新登录流程获取 Device ID 失败", "warning")
            return self._complete_phase(
                PHASE_OAUTH_REENTER,
                success=False,
                error_message="重新登录流程获取 Device ID 失败",
                retryable=True,
                next_action=PHASE_WORKSPACE_RESOLVE,
            )

        if not self._try_reenter_login_flow():
            self._log("未能重新进入登录流程", "warning")
            return self._complete_phase(
                PHASE_OAUTH_REENTER,
                success=False,
                error_message="未能重新进入登录流程",
                retryable=True,
                next_action=PHASE_WORKSPACE_RESOLVE,
            )

        self._otp_sent_at = time.time()
        if not self._submit_login_password_step():
            return self._complete_phase(
                PHASE_OAUTH_REENTER,
                success=False,
                error_message="登录密码提交失败",
                retryable=True,
                next_action=PHASE_WORKSPACE_RESOLVE,
            )

        return self._complete_phase(
            PHASE_OAUTH_REENTER,
            success=True,
            metadata={"otp_sent_at": self._otp_sent_at},
        )

    def _phase_workspace_resolve(self) -> PhaseResult:
        self._resolved_workspace_id = None
        self._callback_url = None

        consent_target = self._pending_continue_url or (
            self.oauth_start.auth_url if self.oauth_start else None
        )
        if consent_target:
            self._log(f"请求 consent 页面: {consent_target[:120]}...")
            try:
                started_at = time.time()
                auth_response = self.session.get(consent_target, timeout=20)
                self._log_timed_http_result("获取 consent 页面", started_at, auth_response)
                current_url = str(getattr(auth_response, "url", "") or "")
                html = auth_response.text or ""

                if (
                    "sign-in-with-chatgpt/codex/consent" in current_url
                    or 'action="/sign-in-with-chatgpt/codex/consent"' in html
                ):
                    workspace_id = self._extract_workspace_id_from_response(
                        response=auth_response,
                        html=html,
                        url=current_url,
                    )
                    if workspace_id:
                        continue_url = self._select_workspace(workspace_id)
                        if not continue_url:
                            return self._complete_phase(
                                PHASE_WORKSPACE_RESOLVE,
                                success=False,
                                error_message="选择 Workspace 失败",
                            )

                        callback_url = self._follow_redirects(continue_url)
                        if not callback_url:
                            return self._complete_phase(
                                PHASE_WORKSPACE_RESOLVE,
                                success=False,
                                error_message="跟随重定向链失败",
                            )

                        self._resolved_workspace_id = workspace_id
                        self._callback_url = callback_url
                        return self._complete_phase(
                            PHASE_WORKSPACE_RESOLVE,
                            success=True,
                            metadata={
                                "workspace_id": workspace_id,
                                "callback_url": callback_url,
                                "resolved_from": "consent",
                            },
                        )
            except Exception as e:
                self._log(f"请求 consent 页面失败，回退到 Cookie 解析路径: {e}", "warning")

        workspace_id = self._get_workspace_id()
        if not workspace_id:
            return self._complete_phase(
                PHASE_WORKSPACE_RESOLVE,
                success=False,
                error_message="获取 Workspace ID 失败",
            )

        continue_url = self._select_workspace(workspace_id)
        if not continue_url:
            return self._complete_phase(
                PHASE_WORKSPACE_RESOLVE,
                success=False,
                error_message="选择 Workspace 失败",
            )

        callback_url = self._follow_redirects(continue_url)
        if not callback_url:
            return self._complete_phase(
                PHASE_WORKSPACE_RESOLVE,
                success=False,
                error_message="跟随重定向链失败",
            )

        self._resolved_workspace_id = workspace_id
        self._callback_url = callback_url
        return self._complete_phase(
            PHASE_WORKSPACE_RESOLVE,
            success=True,
            metadata={
                "workspace_id": workspace_id,
                "callback_url": callback_url,
                "resolved_from": "cookie",
            },
        )

    def _follow_redirects(self, start_url: str) -> Optional[str]:
        """跟随重定向链，寻找回调 URL"""
        try:
            current_url = start_url
            max_redirects = 6

            for i in range(max_redirects):
                self._emit_status(
                    "redirect_chain",
                    f"跟随重定向 {i + 1}/{max_redirects}",
                    redirect_index=i + 1,
                    redirect_total=max_redirects,
                    redirect_url=current_url[:200],
                )
                self._log(f"重定向 {i+1}/{max_redirects}: {current_url[:100]}...")

                started_at = time.time()
                response = self.session.get(
                    current_url,
                    allow_redirects=False,
                    timeout=15
                )
                self._log_timed_http_result(f"重定向跳转 {i + 1}/{max_redirects}", started_at, response)

                location = response.headers.get("Location") or ""

                # 如果不是重定向状态码，停止
                if response.status_code not in [301, 302, 303, 307, 308]:
                    self._log(f"非重定向状态码: {response.status_code}")
                    break

                if not location:
                    self._log("重定向响应缺少 Location 头")
                    break

                # 构建下一个 URL
                import urllib.parse
                next_url = urllib.parse.urljoin(current_url, location)
                self._log(f"重定向下一跳: {next_url[:100]}...")

                # 检查是否包含回调参数
                if "code=" in next_url and "state=" in next_url:
                    self._log(f"找到回调 URL: {next_url[:100]}...")
                    return next_url

                current_url = next_url

            self._log("未能在重定向链中找到回调 URL", "error")
            return None

        except Exception as e:
            self._log(f"跟随重定向失败: {e}", "error")
            return None

    def _handle_oauth_callback(self, callback_url: str) -> Optional[Dict[str, Any]]:
        """处理 OAuth 回调"""
        try:
            if not self.oauth_start:
                self._log("OAuth 流程未初始化", "error")
                return None

            self._emit_status("oauth_callback", "处理 OAuth 回调并交换令牌")
            self._log("处理 OAuth 回调...")
            started_at = time.time()
            token_info = self.oauth_manager.handle_callback(
                callback_url=callback_url,
                expected_state=self.oauth_start.state,
                code_verifier=self.oauth_start.code_verifier
            )
            elapsed = max(0.0, time.time() - started_at)
            self._log(
                f"OAuth 回调处理完成，耗时 {elapsed:.1f} 秒，"
                f"account_id={str(token_info.get('account_id') or '').strip() or 'unknown'}"
            )

            self._log("OAuth 授权成功")
            return token_info

        except Exception as e:
            self._log(f"处理 OAuth 回调失败: {e}", "error")
            return None

    def _phase_oauth_callback(self) -> PhaseResult:
        if not self._callback_url:
            return self._complete_phase(
                PHASE_OAUTH_CALLBACK,
                success=False,
                error_message="处理 OAuth 回调失败",
            )

        token_info = self._handle_oauth_callback(self._callback_url)
        if not token_info:
            return self._complete_phase(
                PHASE_OAUTH_CALLBACK,
                success=False,
                error_message="处理 OAuth 回调失败",
            )

        self._token_info = token_info
        return self._complete_phase(
            PHASE_OAUTH_CALLBACK,
            success=True,
            metadata={"account_id": token_info.get("account_id", "")},
        )

    def _resolved_execution_mode(self) -> str:
        return "curl_cffi"

    def _build_phase_failure_result(
        self,
        result: RegistrationResult,
        phase_result: PhaseResult,
    ) -> RegistrationResult:
        result.error_message = phase_result.error_message or f"{phase_result.phase} 失败"
        result.error_code = phase_result.error_code
        result.metadata = {
            "failed_phase": phase_result.phase,
            "retryable": phase_result.retryable,
            "next_action": phase_result.next_action,
        }
        provider_backoff = self._serialize_provider_backoff(phase_result.provider_backoff)
        if provider_backoff is not None:
            result.metadata["provider_backoff"] = provider_backoff
        if phase_result.metadata:
            result.metadata["phase_metadata"] = dict(phase_result.metadata)
        result.metadata.update(self._collect_phase_history_metadata())
        return result

    def run(self) -> RegistrationResult:
        """
        执行完整的注册流程

        支持已注册账号自动登录：
        - 如果检测到邮箱已注册，自动切换到登录流程
        - 已注册账号跳过：设置密码、发送验证码、创建用户账户
        - 共用步骤：获取验证码、验证验证码、Workspace 和 OAuth 回调

        Returns:
            RegistrationResult: 注册结果
        """
        result = RegistrationResult(success=False, logs=self.logs)
        phase_sequence = [
            (PHASE_IP_CHECK, "检查 IP 地理位置", self._phase_ip_check),
            (PHASE_EMAIL_PREPARE, "创建邮箱地址", self._phase_email_prepare),
            (PHASE_SIGNUP_SUBMIT, "提交注册表单", self._phase_signup_submit),
            (PHASE_SIGNUP_PASSWORD, "提交注册密码", self._phase_signup_password),
            (PHASE_OTP_PRIMARY, "发送或确认首轮验证码", self._phase_otp_primary),
            (PHASE_ACCOUNT_CREATE, "创建 OpenAI 账户资料", self._phase_account_create),
            (PHASE_OAUTH_REENTER, "重入 Codex OAuth 流程", self._phase_oauth_reenter),
            (PHASE_OTP_SECONDARY, "等待并校验二次验证码", self._phase_otp_secondary),
            (PHASE_WORKSPACE_RESOLVE, "解析 Workspace 与授权回调", self._phase_workspace_resolve),
            (PHASE_OAUTH_CALLBACK, "处理 OAuth 回调", self._phase_oauth_callback),
        ]
        phase_index_map = {
            phase_name: index for index, (phase_name, _, _) in enumerate(phase_sequence)
        }

        try:
            self._log("=" * 60)
            self._log("开始注册流程")
            self._log("=" * 60)
            phase_pointer = 0
            while phase_pointer < len(phase_sequence):
                phase_name, detail, handler = phase_sequence[phase_pointer]
                step_index = phase_pointer + 1
                self._log(f"{step_index}. {detail}...")
                self._emit_status(phase_name, detail, step_index=step_index)

                phase_result = handler()
                if phase_result.success:
                    if phase_name == PHASE_EMAIL_PREPARE:
                        result.email = self.email or ""
                    if phase_name == PHASE_WORKSPACE_RESOLVE:
                        result.workspace_id = self._resolved_workspace_id or ""
                    phase_pointer += 1
                    continue

                next_phase_index = phase_index_map.get(phase_result.next_action, -1)
                if (
                    phase_result.retryable
                    and next_phase_index > phase_pointer
                ):
                    self._log(
                        f"阶段 {phase_name} 失败，按 next_action 跳转到 {phase_result.next_action}",
                        "warning",
                    )
                    phase_pointer = next_phase_index
                    continue

                return self._build_phase_failure_result(result, phase_result)

            token_info = self._token_info or {}
            result.account_id = token_info.get("account_id", "")
            result.access_token = token_info.get("access_token", "")
            result.refresh_token = token_info.get("refresh_token", "")
            result.id_token = token_info.get("id_token", "")
            result.password = self.password or ""
            result.workspace_id = self._resolved_workspace_id or result.workspace_id
            result.source = "login" if self._is_existing_account else "register"

            session_cookie = self.session.cookies.get("__Secure-next-auth.session-token")
            if session_cookie:
                self.session_token = session_cookie
                result.session_token = session_cookie
                self._log("获取到 Session Token")

            self._log("=" * 60)
            if self._is_existing_account:
                self._log("登录成功! (已注册账号)")
            else:
                self._log("注册成功!")
            self._log(f"邮箱: {result.email}")
            self._log(f"Account ID: {result.account_id}")
            self._log(f"Workspace ID: {result.workspace_id}")
            self._log("=" * 60)

            result.success = True
            result.metadata = {
                "email_service": self.email_service.service_type.value,
                "proxy_used": self.proxy_url,
                "registered_at": datetime.now().isoformat(),
                "is_existing_account": self._is_existing_account,
                "registration_mode": self._resolved_execution_mode(),
            }
            result.metadata.update(self._collect_phase_history_metadata())

            return result

        except Exception as e:
            self._log(f"注册过程中发生未预期错误: {e}", "error")
            result.error_message = str(e)
            return result

    def save_to_database(self, result: RegistrationResult) -> bool:
        """
        保存注册结果到数据库

        Args:
            result: 注册结果

        Returns:
            是否保存成功
        """
        if not result.success:
            return False

        try:
            # 获取默认 client_id
            settings = get_settings()

            with get_db() as db:
                # 保存账户信息
                account = crud.create_account(
                    db,
                    email=result.email,
                    password=result.password,
                    client_id=settings.openai_client_id,
                    session_token=result.session_token,
                    email_service=self.email_service.service_type.value,
                    email_service_id=self.email_info.get("service_id") if self.email_info else None,
                    account_id=result.account_id,
                    workspace_id=result.workspace_id,
                    access_token=result.access_token,
                    refresh_token=result.refresh_token,
                    id_token=result.id_token,
                    cookies=result.cookies,
                    proxy_used=self.proxy_url,
                    extra_data=result.metadata,
                    source=result.source
                )

                self._log(f"账户已保存到数据库，ID: {account.id}")
                return True

        except Exception as e:
            self._log(f"保存到数据库失败: {e}", "error")
            return False
