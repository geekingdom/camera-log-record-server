"""为隔离容器验收生成一次性密钥，拒绝覆盖已有开发环境配置。"""

import argparse
import os
import secrets
from pathlib import Path

from cryptography.fernet import Fernet


def prepare(path: Path) -> None:
    """独占创建权限为 0600 的配置文件，任何已有文件都保持不变。"""
    values = {
        "ENCRYPTION_KEY": Fernet.generate_key().decode(),
        "BOOTSTRAP_TOKEN": secrets.token_urlsafe(48),
        "INTERNAL_TOKEN": secrets.token_urlsafe(48),
        "DATABASE_NAME": "camera_logs",
        "LOG_ROOT": "data/logs",
        "NODE_CAPACITY": "100",
        "CLUSTER_CAPACITY": "500",
        "RETENTION_DAYS": "7",
    }
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as output:
        output.write("# 隔离容器验收配置；禁止提交或输出其中的临时密钥。\n")
        output.writelines(f"{key}={value}\n" for key, value in values.items())


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=Path(".env"))
    args = parser.parse_args()
    prepare(args.output)
    print("隔离容器验收配置已创建，未输出密钥")
