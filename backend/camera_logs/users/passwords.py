"""用独立随机盐和 PBKDF2 存储密码，昂贵计算交由有限并发的线程执行。"""

import asyncio
import hashlib
import secrets

ITERATIONS = 600_000


def hash_password(password: str) -> str:
    """仅返回带版本参数的哈希串，调用者不得输出输入密码或哈希。"""
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, ITERATIONS)
    return f"pbkdf2_sha256${ITERATIONS}${salt.hex()}${digest.hex()}"


def verify_password(password: str, encoded: str) -> bool:
    """恒定时间比较；损坏记录拒绝认证，未知用户名仍承担一次哈希成本。"""
    try:
        algorithm, rounds, salt, digest = encoded.split("$")
        if algorithm != "pbkdf2_sha256" or int(rounds) != ITERATIONS:
            return False
        actual = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), int(rounds))
        return secrets.compare_digest(actual.hex(), digest)
    except (ValueError, TypeError):
        return False


async def password_work(repo, function, *args):
    """每个 API 进程最多同时计算四次密码，避免占用采集连接事件循环。"""
    if not hasattr(repo, "password_slots"):
        repo.password_slots = asyncio.Semaphore(4)
    async with repo.password_slots:
        return await asyncio.to_thread(function, *args)
