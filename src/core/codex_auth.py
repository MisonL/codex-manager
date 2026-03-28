"""
Codex Auth 登录引擎
复用仓库里已经验证通过的登录状态流，为已有账号生成 Codex CLI 可用的 auth.json。
"""

import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional

from .openai.oauth import OAuthManager
from .register import PhaseContext, PhaseResult, RegistrationEngine
from ..config.constants import (
    CODEX_OAUTH_ORIGINATOR,
    CODEX_OAUTH_REDIRECT_URI,
    CODEX_OAUTH_SCOPE,
)
from ..config.settings import get_settings
from ..services.base import BaseEmailService


@dataclass
class CodexAuthResult:
    """Codex Auth 登录结果"""

    success: bool
    email: str = ""
    workspace_id: str = ""
    account_id: str = ""
    access_token: str = ""
    refresh_token: str = ""
    id_token: str = ""
    session_token: str = ""
    auth_json: Optional[Dict[str, Any]] = None
    error_message: str = ""
    cse_health_status: str = "degraded"
    metadata: Dict[str, Any] = field(default_factory=dict)
    logs: List[str] = field(default_factory=list)


class CodexAuthEngine(RegistrationEngine):
    """
    对已有账号执行 Codex CLI 兼容 OAuth 登录流程。

    这里直接复用 RegistrationEngine 中已经跑通的：
    登录重入 → 密码校验 → OTP 校验 → consent/workspace → callback
    这条链路，避免与成功路径产生分叉。
    """

    def __init__(
        self,
        email: str,
        password: str,
        email_service: BaseEmailService,
        proxy_url: Optional[str] = None,
        callback_logger: Optional[Callable[[str], None]] = None,
        email_service_id: Optional[str] = None,
        assigned_workspace_id: Optional[str] = None,
    ):
        super().__init__(
            email_service=email_service,
            proxy_url=proxy_url,
            callback_logger=callback_logger,
        )
        self.email = email
        self.password = password
        self.email_service_id = email_service_id
        self.assigned_workspace_id = str(assigned_workspace_id or "").strip() or None
        self.email_info = {"email": email}
        if email_service_id:
            self.email_info["service_id"] = email_service_id

        settings = get_settings()
        self.oauth_manager = OAuthManager(
            client_id=settings.openai_client_id,
            auth_url=settings.openai_auth_url,
            token_url=settings.openai_token_url,
            redirect_uri=CODEX_OAUTH_REDIRECT_URI,
            scope=CODEX_OAUTH_SCOPE,
            proxy_url=proxy_url,
            originator=CODEX_OAUTH_ORIGINATOR,
        )

    @classmethod
    def from_registration_engine(
        cls,
        engine: RegistrationEngine,
        *,
        email: str,
        password: str,
        assigned_workspace_id: Optional[str] = None,
    ) -> "CodexAuthEngine":
        email_service_id = None
        if isinstance(getattr(engine, "email_info", None), dict):
            email_service_id = engine.email_info.get("service_id")
        return cls(
            email=email,
            password=password,
            email_service=engine.email_service,
            proxy_url=getattr(engine, "proxy_url", None),
            callback_logger=getattr(engine, "callback_logger", None),
            email_service_id=email_service_id,
            assigned_workspace_id=assigned_workspace_id,
        )

    def _build_auth_json(self, token_info: Dict[str, Any]) -> Dict[str, Any]:
        """构造 Codex CLI 兼容的 auth.json 内容。"""
        now_rfc3339 = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        return {
            "auth_mode": "chatgpt",
            "OPENAI_API_KEY": None,
            "tokens": {
                "id_token": token_info.get("id_token", ""),
                "access_token": token_info.get("access_token", ""),
                "refresh_token": token_info.get("refresh_token", ""),
                "account_id": token_info.get("account_id", ""),
            },
            "last_refresh": now_rfc3339,
        }

    def _select_workspace_for_account(self, consent_workspace_id: Optional[str]) -> Optional[str]:
        return self.assigned_workspace_id or str(consent_workspace_id or "").strip() or None

    def _session_get(
        self,
        url: str,
        *,
        request_kind: str = "",
        headers: Optional[Dict[str, str]] = None,
        **kwargs,
    ):
        if not self.session:
            raise RuntimeError("session not initialized")
        request_kwargs = dict(kwargs)
        if headers:
            request_kwargs["headers"] = headers
        return self.session.get(url, **request_kwargs)

    def _session_post(
        self,
        url: str,
        *,
        request_kind: str = "",
        headers: Optional[Dict[str, str]] = None,
        **kwargs,
    ):
        if not self.session:
            raise RuntimeError("session not initialized")
        request_kwargs = dict(kwargs)
        if headers:
            request_kwargs["headers"] = headers
        return self.session.post(url, **request_kwargs)

    def _resolve_workspace_id(self, consent_url: Optional[str]) -> Optional[str]:
        """
        OTP 校验成功后优先请求 consent 页面提取 workspace。
        若页面未显式暴露 workspace_id，再回退到 Cookie 解析路径。
        """
        if not self.session or not self.oauth_start:
            return None

        auth_target = consent_url or self.oauth_start.auth_url
        try:
            self._log(f"请求 consent 页面: {auth_target[:120]}...")
            started_at = time.time()
            response = self._session_get(auth_target, timeout=20)
            self._log_timed_http_result("获取 consent 页面", started_at, response)

            workspace_id = self._extract_workspace_id_from_response(
                response=response,
                html=response.text or "",
                url=str(getattr(response, "url", "") or "").strip(),
            )
            if workspace_id:
                self._log(f"Workspace ID: {workspace_id}")
                return workspace_id
        except Exception as e:
            self._log(f"请求 consent 页面失败: {e}", "warning")

        self._log("consent 页面缺少 workspace_id，回退到 Cookie 解析路径", "warning")
        return RegistrationEngine._get_workspace_id(self)

    def _resolve_workspace_authorization(self) -> PhaseResult:
        consent_target = self._pending_continue_url or (
            self.oauth_start.auth_url if self.oauth_start else None
        )
        if not consent_target:
            return PhaseResult(
                phase="workspace_authorization",
                success=False,
                error_message="缺少 consent 页面地址",
            )

        try:
            consent_workspace_id = self._resolve_workspace_id(consent_target)
            workspace_id = self._select_workspace_for_account(consent_workspace_id)
            if not workspace_id:
                return PhaseResult(
                    phase="workspace_authorization",
                    success=False,
                    error_message="获取 Workspace ID 失败",
                )

            continue_url = self._select_workspace(workspace_id)
            if not continue_url:
                return PhaseResult(
                    phase="workspace_authorization",
                    success=False,
                    error_message="选择 Workspace 失败",
                )

            callback_url = self._follow_redirects(continue_url)
            if not callback_url:
                return PhaseResult(
                    phase="workspace_authorization",
                    success=False,
                    error_message="获取回调 URL 失败",
                )

            self._resolved_workspace_id = workspace_id
            self._callback_url = callback_url
            return PhaseResult(
                phase="workspace_authorization",
                success=True,
                metadata={
                    "workspace_id": workspace_id,
                    "callback_url": callback_url,
                    "assigned_workspace_id": self.assigned_workspace_id or "",
                },
            )
        except Exception as e:
            return PhaseResult(
                phase="workspace_authorization",
                success=False,
                error_message=str(e),
            )

    def authorize(self) -> CodexAuthResult:
        """执行官方 Codex Auth 授权流程。"""
        result = CodexAuthResult(success=False, email=self.email, logs=self.logs)

        try:
            self._log("=" * 50)
            self._log(f"开始 Codex Auth 登录: {self.email}")
            self._log("=" * 50)

            self._log("1. 初始化会话...")
            if not RegistrationEngine._init_session(self):
                result.error_message = "初始化会话失败"
                return result

            self._log("2. 开始 Codex OAuth 流程...")
            if not RegistrationEngine._start_oauth(self):
                result.error_message = "OAuth 流程启动失败"
                return result

            self._log("3. 获取 Device ID...")
            did = RegistrationEngine._get_device_id(self)
            if not did:
                result.error_message = "获取 Device ID 失败"
                return result

            self._log("4. 重新进入登录流程...")
            if not self._try_reenter_login_flow():
                result.error_message = "进入登录流程失败"
                return result

            self._log("5. 提交密码...")
            self._otp_sent_at = time.time()
            if not self._submit_login_password_step():
                result.error_message = "密码验证失败"
                return result

            self._log("6. 等待验证码...")
            otp_started_at = time.time()
            code, otp_phase = self._phase_otp_secondary(
                PhaseContext(otp_sent_at=self._otp_sent_at),
                started_at=otp_started_at,
            )
            if not code:
                result.error_message = otp_phase.error_message or "获取验证码失败"
                return result

            self._log("7. 验证验证码...")
            otp_valid, consent_url = self._validate_verification_code_and_get_continue_url(code)
            if not otp_valid:
                result.error_message = "验证码校验失败"
                return result
            self._pending_continue_url = consent_url or (
                self.oauth_start.auth_url if self.oauth_start else None
            )

            self._log("8. 解析 Workspace 并执行授权...")
            workspace_phase = self._resolve_workspace_authorization()
            if not workspace_phase.success:
                result.error_message = workspace_phase.error_message or "Workspace 授权失败"
                return result

            self._log("9. 处理 OAuth 回调...")
            token_info = RegistrationEngine._handle_oauth_callback(self, self._callback_url)
            if not token_info:
                result.error_message = "OAuth 回调处理失败"
                return result

            session_cookie = ""
            if self.session is not None and getattr(self.session, "cookies", None) is not None:
                session_cookie = self.session.cookies.get("__Secure-next-auth.session-token") or ""

            result.workspace_id = self._resolved_workspace_id or ""
            result.account_id = str(token_info.get("account_id") or "").strip()
            result.access_token = str(token_info.get("access_token") or "")
            result.refresh_token = str(token_info.get("refresh_token") or "")
            result.id_token = str(token_info.get("id_token") or "")
            result.session_token = session_cookie
            result.auth_json = self._build_auth_json(token_info)
            result.cse_health_status = "healthy"
            result.metadata = {
                "authorized_at": datetime.utcnow().isoformat(),
                "execution_mode": self._resolved_execution_mode(),
                "assigned_workspace_id": self.assigned_workspace_id or "",
            }
            result.success = True

            self._log("=" * 50)
            self._log(f"Codex Auth 登录成功: {self.email}")
            self._log(f"Account ID: {result.account_id}")
            self._log(f"Workspace ID: {result.workspace_id}")
            self._log("=" * 50)
            return result

        except Exception as e:
            self._log(f"Codex Auth 登录异常: {e}", "error")
            result.error_message = str(e)
            return result
        finally:
            if not result.success:
                result.cse_health_status = "degraded"
                result.metadata = {
                    **dict(result.metadata or {}),
                    "execution_mode": self._resolved_execution_mode(),
                }
            try:
                self.http_client.close()
            except Exception:
                pass

    def run(self) -> CodexAuthResult:
        return self.authorize()
