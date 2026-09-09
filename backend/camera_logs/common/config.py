"""集中声明服务环境变量及其安全默认值。"""

import socket
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """从 `.env` 与进程环境读取 API、节点、存储和保留策略配置。"""
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    mongo_uri: str = "mongodb://127.0.0.1:27019/?directConnection=true"
    database_name: str = "camera_logs"
    bootstrap_token: str = ""
    admin_username: str = "admin"
    # 用户指定的首次管理员口令；仅初始化空库，既有账号不会被覆盖。
    admin_password: str = "asdf!234"
    session_seconds: int = Field(default=28800, ge=300, le=604800)
    session_cookie_secure: bool = False
    encryption_key: str = ""
    internal_token: str = ""
    node_id: str = socket.gethostname()
    node_url: str = "http://127.0.0.1:8001"
    log_root: Path = Path("data/logs")
    known_hosts: str = ""
    ssh_verify_host_key: bool = False
    node_capacity: int = 100
    cluster_capacity: int = 500
    retention_days: int = 7
    start_background: bool = True
    psh_mode: str = "disabled"
    psh_serial_character_interval: float = Field(default=.1, ge=0, le=1)
    psh_mock_password_file: Path | None = None
    psh_token_url: str = ""
    psh_api_url: str = ""
    psh_client_id: str = ""
    psh_client_secret: str = ""
    psh_api_key: str = ""
    psh_user_name: str = ""
