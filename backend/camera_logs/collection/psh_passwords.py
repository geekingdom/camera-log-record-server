"""按需取得芝麻开门口令，默认禁用且不会记录挑战值或任何密钥。"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx


class PshPasswordError(RuntimeError):
    """口令服务不可用或返回无效数据时的安全错误，不包含敏感上下文。"""


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
        if mode == "disabled":
            raise PshPasswordError("PSH 口令提供器未启用")
        if mode == "mock":
            return await self._mock_password(task)
        if mode == "http":
            if not isinstance(challenge, str) or not challenge:
                raise PshPasswordError("PSH 挑战值无效")
            try:
                async with asyncio.timeout(self._timeout("psh_total_timeout_seconds", self._TOTAL_TIMEOUT)):
                    return await self._http_password(challenge)
            except TimeoutError:
                raise PshPasswordError("PSH 口令服务超时") from None
        raise PshPasswordError("PSH 口令提供器模式无效")

    def _timeout(self, name: str, default: float) -> float:
        """读取已由 Settings 限制范围的超时；测试夹具缺字段时保持原保守值。"""
        value = getattr(self.settings, name, default)
        return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0 else default

    @staticmethod
    def _task_key(task: Mapping[str, Any]) -> str:
        protocol = task.get("protocol") or task.get("protocolType")
        ip, port = task.get("ip"), task.get("port")
        if not isinstance(protocol, str) or not isinstance(ip, str) or port is None:
            raise PshPasswordError("PSH mock 任务标识无效")
        return f"{protocol}:{ip}:{port}"

    async def _mock_password(self, task: Mapping[str, Any]) -> str:
        """每次调用都重新读取 mock 文件，便于测试和本地口令轮换。"""
        path = getattr(self.settings, "psh_mock_password_file", None)
        if not path:
            raise PshPasswordError("PSH mock 口令文件未配置")
        try:
            content = await asyncio.to_thread(Path(path).read_text, encoding="utf-8")
            values = json.loads(content)
        except (OSError, TypeError, ValueError):
            raise PshPasswordError("PSH mock 口令文件不可用") from None
        password = values.get(self._task_key(task)) if isinstance(values, dict) else None
        if not isinstance(password, str) or not password:
            raise PshPasswordError("PSH mock 口令缺失")
        return password

    def _required(self, name: str) -> str:
        value = getattr(self.settings, name, "")
        if not isinstance(value, str) or not value:
            raise PshPasswordError("PSH HTTP 配置不完整")
        return value

    def _https_url(self, name: str) -> str:
        """仅接受无 userinfo 的 HTTPS 服务地址，避免配置降级或在 URL 中携带密钥。"""
        value = self._required(name)
        parsed = urlsplit(value)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            raise PshPasswordError("PSH HTTP URL 无效")
        return value

    async def _http_password(self, challenge: str) -> str:
        """获取令牌并调用解密接口；403003 仅刷新令牌和重放一次请求。"""
        token_url = self._https_url("psh_token_url")
        api_url = self._https_url("psh_api_url")
        client_id = self._required("psh_client_id")
        client_secret = self._required("psh_client_secret")
        api_key = self._required("psh_api_key")
        user_name = self._required("psh_user_name")
        timeout = httpx.Timeout(self._timeout("psh_request_timeout_seconds", self._REQUEST_TIMEOUT))
        async with httpx.AsyncClient(timeout=timeout, transport=self.transport) as client:
            token = self._token or await self._fetch_token(client, token_url, client_id, client_secret)
            response = await self._decrypt(client, api_url, client_id, api_key, user_name, token, challenge)
            if response.get("code") == "403003":
                self._token = None
                token = await self._fetch_token(client, token_url, client_id, client_secret)
                response = await self._decrypt(client, api_url, client_id, api_key, user_name, token, challenge)
                if response.get("code") == "403003":
                    raise PshPasswordError("PSH 口令服务认证失败")
            password = response.get("data", {}).get("data") if isinstance(response.get("data"), dict) else None
            if not isinstance(password, str) or not password:
                raise PshPasswordError("PSH 口令服务返回无效结果")
            return password

    async def _fetch_token(
        self, client: httpx.AsyncClient, token_url: str, client_id: str, client_secret: str
    ) -> str:
        try:
            response = await client.post(
                token_url,
                data={"grant_type": "client_credentials", "client_id": client_id, "client_secret": client_secret},
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            response.raise_for_status()
            token = response.json().get("access_token")
        except (httpx.HTTPError, ValueError, AttributeError):
            raise PshPasswordError("PSH 令牌服务请求失败") from None
        if not isinstance(token, str) or not token:
            raise PshPasswordError("PSH 令牌服务返回无效结果")
        self._token = token
        return token

    async def _decrypt(
        self,
        client: httpx.AsyncClient,
        api_url: str,
        client_id: str,
        api_key: str,
        user_name: str,
        token: str,
        challenge: str,
    ) -> dict[str, Any]:
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
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError):
            raise PshPasswordError("PSH 口令服务请求失败") from None
        if not isinstance(payload, dict):
            raise PshPasswordError("PSH 口令服务返回无效结果")
        return payload
