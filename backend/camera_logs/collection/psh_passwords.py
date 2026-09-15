"""按需取得芝麻开门口令，默认禁用且不会记录挑战值或任何密钥。"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx

from .psh_diagnostics import body_preview, diagnostic, started_at, summary

logger = logging.getLogger(__name__)


class PshPasswordError(RuntimeError):
    """口令服务不可用或返回无效数据时的安全错误，不包含敏感上下文。"""

    def __init__(self, message: str, details: Mapping[str, Any] | None = None) -> None:
        self.diagnostic = dict(details or {})
        super().__init__(message if not self.diagnostic else f"{message}：{summary(self.diagnostic)}")


class PshPasswordProvider:
    """通过本地 mock 或受 TLS 保护的 OpenAPI 解密设备挑战口令。"""

    _REQUEST_TIMEOUT = 4.0
    _TOTAL_TIMEOUT = 9.0

    def __init__(self, settings: Any, *, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.settings = settings
        self.transport = transport
        self._token: str | None = None

    async def __call__(self, task: Mapping[str, Any], challenge: str) -> str:
        """按配置模式返回口令；调用方负责将整体 SSH 交互限制在自身超时内。"""
        mode = str(getattr(self.settings, "psh_mode", "disabled")).lower()
        started = started_at()
        if mode == "disabled":
            raise self._failure(task, "CONFIG", "DISABLED", started=started)
        if mode == "mock":
            try:
                return await self._mock_password(task)
            except PshPasswordError as error:
                if error.diagnostic:
                    raise
                raise self._failure(task, "MOCK", "PASSWORD_ERROR", started=started) from None
        if mode == "http":
            if not isinstance(challenge, str) or not challenge:
                raise self._failure(task, "CHALLENGE", "INVALID", started=started)
            try:
                async with asyncio.timeout(self._timeout("psh_total_timeout_seconds", self._TOTAL_TIMEOUT)):
                    return await self._http_password(task, challenge)
            except TimeoutError as error:
                raise self._failure(task, "WORKFLOW", "TOTAL_TIMEOUT", started=started, error=error,
                                    secrets=(challenge,)) from None
        raise self._failure(task, "CONFIG", "INVALID_MODE", started=started)

    @staticmethod
    def _log(task: Mapping[str, Any], level: int, details: Mapping[str, Any]) -> None:
        """记录任务、运行和会话可关联的安全诊断，日志处理异常不改变采集控制流。"""
        identity = {"taskId": task.get("id"), "runId": task.get("runId"), "sessionId": task.get("sessionId")}
        try:
            logger.log(level, "PSH 解密诊断", extra={"context": {key: value for key, value in identity.items() if value} | dict(details)})
        except Exception:  # noqa: BLE001 - 日志不可用时仍必须让 debug 按原失败语义结束。
            return

    def _failure(self, task, stage, reason, *, started, endpoint=None, status=None, payload=None, error=None, secrets=()):
        """创建并记录安全失败；响应正文只由诊断模块提取白名单错误字段。"""
        details = diagnostic(stage, reason, started=started, endpoint=endpoint, status=status, payload=payload,
                             error=error, secrets=secrets)
        self._log(task, logging.ERROR, details)
        messages = {
            "DISABLED": "PSH 口令提供器未启用",
            "AUTH_REJECTED": "PSH 口令服务认证失败",
            "INVALID_JSON": "PSH 口令服务返回无效结果",
            "INVALID_URL": "PSH HTTP URL 无效",
            "CONFIG_MISSING": "PSH HTTP 配置不完整",
            "PASSWORD_MISSING": "PSH mock 口令缺失",
        }
        return PshPasswordError(messages.get(reason, "PSH 口令服务失败"), details)

    def _timeout(self, name: str, default: float) -> float:
        """读取已由 Settings 限制范围的超时；测试夹具缺字段时保持原保守值。"""
        value = getattr(self.settings, name, default)
        return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0 else default

    @staticmethod
    def _task_key(task: Mapping[str, Any]) -> str:
        protocol = task.get("protocol") or task.get("protocolType")
        ip, port = task.get("ip"), task.get("port")
        if not isinstance(protocol, str) or not isinstance(ip, str) or port is None:
            raise ValueError("TASK_INVALID")
        return f"{protocol}:{ip}:{port}"

    async def _mock_password(self, task: Mapping[str, Any]) -> str:
        """每次调用都重新读取 mock 文件，便于测试和本地口令轮换。"""
        path = getattr(self.settings, "psh_mock_password_file", None)
        if not path:
            raise self._failure(task, "MOCK", "FILE_UNCONFIGURED", started=started_at())
        try:
            content = await asyncio.to_thread(Path(path).read_text, encoding="utf-8")
            values = json.loads(content)
        except (OSError, TypeError, ValueError) as error:
            raise self._failure(task, "MOCK", "FILE_UNAVAILABLE", started=started_at(), error=error) from None
        try:
            password = values.get(self._task_key(task)) if isinstance(values, dict) else None
        except ValueError as error:
            raise self._failure(task, "MOCK", "INVALID_TASK", started=started_at(), error=error) from None
        if not isinstance(password, str) or not password:
            raise self._failure(task, "MOCK", "PASSWORD_MISSING", started=started_at())
        return password

    def _required(self, name: str) -> str:
        value = getattr(self.settings, name, "")
        if not isinstance(value, str) or not value:
            raise ValueError(f"MISSING_{name}")
        return value

    def _https_url(self, name: str) -> str:
        """仅接受无 userinfo 的 HTTPS 服务地址，避免配置降级或在 URL 中携带密钥。"""
        value = self._required(name)
        parsed = urlsplit(value)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError(f"INVALID_{name}")
        return value

    async def _http_password(self, task: Mapping[str, Any], challenge: str) -> str:
        """获取令牌并调用解密接口；403003 仅刷新令牌和重放一次请求。"""
        started = started_at()
        try:
            token_url = self._https_url("psh_token_url")
            api_url = self._https_url("psh_api_url")
            client_id = self._required("psh_client_id")
            client_secret = self._required("psh_client_secret")
            api_key = self._required("psh_api_key")
            user_name = self._required("psh_user_name")
        except Exception as error:  # noqa: BLE001 - 配置字段名不含秘密，可安全归类。
            reason = "INVALID_URL" if str(error).startswith("INVALID_") else "CONFIG_MISSING"
            raise self._failure(task, "CONFIG", reason, started=started, error=error,
                                secrets=(challenge,)) from None
        timeout = httpx.Timeout(self._timeout("psh_request_timeout_seconds", self._REQUEST_TIMEOUT))
        async with httpx.AsyncClient(timeout=timeout, transport=self.transport) as client:
            token = self._token or await self._fetch_token(client, task, token_url, client_id, client_secret, challenge,
                                                            known_secrets=(api_key, self._token or ""))
            secrets = (challenge, client_secret, api_key, token)
            response = await self._decrypt(client, task, api_url, client_id, api_key, user_name, token, challenge,
                                           secrets=secrets, retry=False)
            if response.get("code") == "403003":
                previous_token = token
                self._token = None
                self._log(task, logging.WARNING, diagnostic("DECRYPT_RETRY", "AUTH_REFRESH", started=started_at(),
                                                            endpoint=api_url, payload=response, secrets=secrets))
                token = await self._fetch_token(client, task, token_url, client_id, client_secret, challenge,
                                                known_secrets=(api_key, previous_token))
                secrets = (challenge, client_secret, api_key, previous_token, token)
                response = await self._decrypt(client, task, api_url, client_id, api_key, user_name, token, challenge,
                                               secrets=secrets, retry=True)
                if response.get("code") == "403003":
                    raise self._failure(task, "DECRYPT_RETRY_RESPONSE", "AUTH_REJECTED", started=started,
                                        endpoint=api_url, payload=response, secrets=secrets)
            password = response.get("data", {}).get("data") if isinstance(response.get("data"), dict) else None
            if not isinstance(password, str) or not password:
                raise self._failure(task, "DECRYPT_RESPONSE", "INVALID_PASSWORD_RESULT", started=started,
                                    endpoint=api_url, payload=response, secrets=secrets)
            self._log(task, logging.INFO, diagnostic("DECRYPT_RESPONSE", "SUCCESS", started=started,
                                                      endpoint=api_url, payload=response, secrets=secrets + (password,)))
            return password

    async def _fetch_token(
        self, client: httpx.AsyncClient, task: Mapping[str, Any], token_url: str, client_id: str, client_secret: str,
        challenge: str, *, known_secrets: tuple[str, ...] = (),
    ) -> str:
        started = started_at()
        self._log(task, logging.INFO, diagnostic("TOKEN_REQUEST", "STARTED", started=started, endpoint=token_url))
        try:
            response = await client.post(
                token_url,
                data={"grant_type": "client_credentials", "client_id": client_id, "client_secret": client_secret},
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
        except httpx.TimeoutException as error:
            raise self._failure(task, "TOKEN_REQUEST", "TIMEOUT", started=started, endpoint=token_url, error=error,
                                secrets=(challenge, client_secret, *known_secrets)) from None
        except httpx.HTTPError as error:
            raise self._failure(task, "TOKEN_REQUEST", "HTTP_ERROR", started=started, endpoint=token_url, error=error,
                                secrets=(challenge, client_secret, *known_secrets)) from None
        payload = self._response_payload(response)
        if not response.is_success:
            if payload is None:
                details = diagnostic("TOKEN_RESPONSE", "HTTP_STATUS", started=started, endpoint=token_url,
                                     status=response.status_code, secrets=(challenge, client_secret, *known_secrets))
                details.update(body_preview(response.content, response.headers.get("content-type"), (challenge, client_secret, *known_secrets)))
                self._log(task, logging.ERROR, details)
                raise PshPasswordError("PSH 令牌服务请求失败", details)
            raise self._failure(task, "TOKEN_RESPONSE", "HTTP_STATUS", started=started, endpoint=token_url,
                                status=response.status_code, payload=payload, secrets=(challenge, client_secret, *known_secrets))
        if payload is None:
            details = diagnostic("TOKEN_RESPONSE", "INVALID_JSON", started=started, endpoint=token_url,
                                 status=response.status_code, secrets=(challenge, client_secret, *known_secrets))
            details.update(body_preview(response.content, response.headers.get("content-type"), (challenge, client_secret, *known_secrets)))
            self._log(task, logging.ERROR, details)
            raise PshPasswordError("PSH 令牌服务返回非JSON结果", details)
        token = payload.get("access_token")
        if not isinstance(token, str) or not token:
            raise self._failure(task, "TOKEN_RESPONSE", "MISSING_ACCESS_TOKEN", started=started, endpoint=token_url,
                                status=response.status_code, payload=payload, secrets=(challenge, client_secret, *known_secrets))
        self._token = token
        self._log(task, logging.INFO, diagnostic("TOKEN_RESPONSE", "SUCCESS", started=started, endpoint=token_url,
                                                  status=response.status_code, payload=payload,
                                                  secrets=(challenge, client_secret, token, *known_secrets)))
        return token

    @staticmethod
    def _response_payload(response: httpx.Response) -> Mapping[str, Any] | None:
        """尽力取得字典JSON，调用方决定无效JSON时如何记录安全预览。"""
        try:
            payload = response.json()
        except ValueError:
            return None
        return payload if isinstance(payload, Mapping) else None

    async def _decrypt(
        self,
        client: httpx.AsyncClient,
        task: Mapping[str, Any],
        api_url: str,
        client_id: str,
        api_key: str,
        user_name: str,
        token: str,
        challenge: str,
        secrets: tuple[str, ...],
        *, retry: bool,
    ) -> dict[str, Any]:
        started = started_at()
        stage = "DECRYPT_RETRY_REQUEST" if retry else "DECRYPT_REQUEST"
        self._log(task, logging.INFO, diagnostic(stage, "STARTED", started=started, endpoint=api_url))
        try:
            response = await client.post(
                api_url,
                headers={
                    "X-HiCode-Authorization": f"Bearer {token}",
                    "X-CloudApi-ClientId": client_id,
                    "X-CloudApi-ApiKey": api_key,
                },
                json={"source": challenge, "userName": user_name},
            )
        except httpx.TimeoutException as error:
            raise self._failure(task, "DECRYPT_REQUEST", "TIMEOUT", started=started, endpoint=api_url, error=error,
                                secrets=secrets) from None
        except httpx.HTTPError as error:
            raise self._failure(task, "DECRYPT_REQUEST", "HTTP_ERROR", started=started, endpoint=api_url, error=error,
                                secrets=secrets) from None
        payload = self._response_payload(response)
        response_stage = "DECRYPT_RETRY_RESPONSE" if retry else "DECRYPT_RESPONSE"
        if payload is None:
            details = diagnostic(response_stage, "INVALID_JSON", started=started, endpoint=api_url,
                                 status=response.status_code, secrets=secrets)
            details.update(body_preview(response.content, response.headers.get("content-type"), secrets))
            self._log(task, logging.ERROR, details)
            raise PshPasswordError("PSH 口令服务返回非JSON结果", details)
        if not response.is_success:
            raise self._failure(task, response_stage, "HTTP_STATUS", started=started, endpoint=api_url,
                                status=response.status_code, payload=payload, secrets=secrets)
        result = payload.get("data", {}).get("data") if isinstance(payload.get("data"), Mapping) else None
        self._log(task, logging.INFO, diagnostic(response_stage, "RECEIVED", started=started, endpoint=api_url,
                                                  status=response.status_code, payload=payload,
                                                  secrets=secrets + ((result,) if isinstance(result, str) else ())))
        return dict(payload)
