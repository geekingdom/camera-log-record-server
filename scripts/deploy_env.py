"""以标准库为 Linux 一键部署创建仅本机可读的初始环境文件。"""

import argparse
import base64
import os
import secrets
from pathlib import Path


def _fernet_key() -> str:
    """生成符合 Fernet 格式的 URL 安全 Base64 32 字节密钥。"""
    return base64.urlsafe_b64encode(secrets.token_bytes(32)).decode("ascii")


def create_environment(path: Path) -> None:
    """独占写入 0600 环境文件；已有文件由部署脚本保留且不会覆盖。"""
    values = {
        "ENCRYPTION_KEY": _fernet_key(),
        "BOOTSTRAP_TOKEN": secrets.token_urlsafe(48),
        "INTERNAL_TOKEN": secrets.token_urlsafe(48),
        "ADMIN_USERNAME": "admin",
        # 内置管理员首登例外使用固定初始密码，服务端会强制其首次登录后修改。
        "ADMIN_PASSWORD": "asdf!234",
        "SESSION_SECONDS": "28800",
        "SESSION_COOKIE_SECURE": "false",
        "DATABASE_NAME": "camera_logs",
        "LOG_ROOT": "data/logs",
        "NODE_CAPACITY": "100",
        "CLUSTER_CAPACITY": "500",
        "RETENTION_DAYS": "7",
    }
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as output:
        output.write("# 一键部署初始配置；含凭据，仅限本机管理员读取。\n")
        output.writelines(f"{key}={value}\n" for key, value in values.items())


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path(".env"))
    args = parser.parse_args()
    create_environment(args.output)
    print("部署环境文件已创建，未输出任何密钥或密码")
