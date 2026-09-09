"""验证部署后内置管理员改密、只读子账号授权和停用会话即时失效。"""

import argparse
import asyncio
import ipaddress
import secrets
from pathlib import Path

import httpx

CSRF = {"X-Requested-With": "XMLHttpRequest"}


def _environment_value(path: Path, key: str) -> str:
    """读取单个部署配置值，脚本绝不输出密码、令牌或完整环境文件。"""
    prefix = key + "="
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith(prefix):
            return line.removeprefix(prefix)
    raise ValueError(f"环境文件缺少 {key}")


def _source_network(address: str) -> str:
    """将代理确认的 IPv4 或 IPv6 来源收窄为单主机 CIDR，避免扩大隔离验收访问范围。"""
    source = ipaddress.ip_address(address)
    return f"{source}/{source.max_prefixlen}"


async def _request(client, method, path, expected, **kwargs):
    """请求正式认证 API 并将非预期状态转为不含敏感正文的失败。"""
    response = await client.request(method, path, **kwargs)
    if response.status_code != expected:
        raise AssertionError(f"{method} {path} 返回 {response.status_code}，期望 {expected}")
    return response


async def verify(url: str, env_file: Path) -> dict:
    """执行管理员首登改密、子账号只读授权和停用会话失效的完整链路。"""
    username = _environment_value(env_file, "ADMIN_USERNAME")
    initial_password = _environment_value(env_file, "ADMIN_PASSWORD")
    changed_password = secrets.token_urlsafe(24)
    operator_password = secrets.token_urlsafe(24)
    suffix = secrets.token_hex(6)
    operator_name = f"smoke-reader-{suffix}"
    async with httpx.AsyncClient(base_url=url.rstrip("/"), timeout=30) as admin:
        login = await _request(admin, "POST", "/api/v1/auth/login", 200, headers=CSRF,
                               json={"username": username, "password": initial_password})
        if login.json()["user"].get("mustChangePassword") is not True:
            raise AssertionError("内置管理员首次登录未要求修改密码")
        changed = await _request(admin, "POST", "/api/v1/auth/password", 200, headers=CSRF, json={
            "currentPassword": initial_password, "newPassword": changed_password,
        })
        if changed.json()["user"].get("mustChangePassword"):
            raise AssertionError("管理员修改初始密码后仍被限制")
        # 通过正式 Nginx 代理读取来源，随后用精确单主机规则验证真实 IP 白名单链路。
        observed = await _request(admin, "GET", "/api/v1/admin/ip-policy", 200)
        policy_version = None
        try:
            enabled = await _request(admin, "PATCH", "/api/v1/admin/ip-policy", 200, headers=CSRF, json={
                "version": observed.json()["version"], "enabled": True,
                "rules": [{"label": "隔离验收来源", "network": _source_network(observed.json()["clientIp"]),
                           "scopes": ["admin", "tasks:read"]}],
            })
            policy_version = enabled.json()["version"]
            forged = await _request(admin, "GET", "/api/v1/admin/ip-policy", 200,
                                    headers={"X-Forwarded-For": "192.0.2.123"})
            if observed.json()["clientIp"] != forged.json()["clientIp"]:
                raise AssertionError("代理信任了客户端伪造的来源地址")
            created = await _request(admin, "POST", "/api/v1/users", 201, headers=CSRF, json={
                "username": operator_name,
                "displayName": "部署验收只读账号",
                "password": operator_password,
                "scopes": ["tasks:read"],
            })
            operator = created.json()
            if operator.get("isAdmin") or operator.get("mustChangePassword") is not True:
                raise AssertionError("子账号角色或初始改密策略错误")
            async with httpx.AsyncClient(base_url=url.rstrip("/"), timeout=30) as reader:
                await _request(reader, "POST", "/api/v1/auth/login", 200, headers=CSRF,
                               json={"username": operator_name, "password": operator_password})
                reader_password = secrets.token_urlsafe(24)
                reader_changed = await _request(reader, "POST", "/api/v1/auth/password", 200, headers=CSRF, json={
                    "currentPassword": operator_password, "newPassword": reader_password,
                })
                await _request(reader, "GET", "/api/v1/tasks", 200)
                await _request(reader, "GET", "/api/v1/users", 403)
                version = reader_changed.json()["user"]["version"]
                await _request(admin, "DELETE", f"/api/v1/users/{operator['id']}?version={version}",
                               204, headers=CSRF)
                await _request(reader, "GET", "/api/v1/auth/me", 401)
        finally:
            if policy_version is not None:
                await _request(admin, "PATCH", "/api/v1/admin/ip-policy", 200, headers=CSRF, json={
                    "version": policy_version, "enabled": False, "rules": [],
                })
    return {"adminPasswordChanged": True, "ipPolicyVerified": True,
            "readOnlyPermissionVerified": True, "sessionRevoked": True}


def parse_args():
    """解析正式 API 地址与一键部署创建的环境文件路径。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:5173")
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    parser.add_argument("--isolated", action="store_true", help="确认目标是可销毁的隔离环境，会修改管理员密码")
    args = parser.parse_args()
    if not args.isolated:
        parser.error("此脚本会轮换管理员密码，仅允许显式 --isolated 的隔离验收环境")
    return args


if __name__ == "__main__":
    options = parse_args()
    try:
        result = asyncio.run(verify(options.url, options.env_file))
        print("用户认证验收通过：" + ", ".join(key for key, value in result.items() if value))
    except Exception as error:  # noqa: BLE001 - CLI 以非零状态报告验收失败，避免打印响应或凭据。
        print(f"用户认证验收失败：{type(error).__name__}")
        raise SystemExit(2)
