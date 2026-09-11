"""集中声明服务环境变量及其安全默认值。"""

import socket
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_NODE_CAPACITY = 100
MAX_NODE_CAPACITY = 10_000
DEFAULT_CLUSTER_CAPACITY = 500
MAX_CLUSTER_CAPACITY = 10_000


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
    # 独立节点部署可自定义监听地址/端口；node_url 仍是后端实际访问的公布地址。
    node_bind_ip: str = "0.0.0.0"
    node_port: int = Field(default=8001, ge=1, le=65535)
    # Docker Worker 读取宿主 CPU、内存和网络统计的只读 proc 根目录；原生部署保持空值。
    host_proc_root: Path | None = None
    log_root: Path = Path("data/logs")
    # 海康 SSH coredump 的 NFS 总挂载目录；Worker 部署时应配置为宿主机绝对路径。
    nfs_root: Path = Path("/srv/camera-logs/nfs-coredump")
    # 设备可达的宿主机 IP，不能使用 Docker 服务名或容器内回环地址。
    nfs_server_ip: str = ""
    # NFS coredump 扫描/冻结独立于采集会话；接收源文件默认不自动删除。
    # 十秒轮询及时发现设备子目录文件；稳定判定独立于设备端 mtime 时钟。
    coredump_scan_interval_seconds: int = Field(default=5, ge=1, le=3600)
    coredump_scan_max_files: int = Field(default=500, ge=1, le=10000)
    coredump_snapshot_max_bytes: int = Field(default=20_000_000_000, ge=1)
    coredump_snapshot_quota_bytes: int = Field(default=100_000_000_000, ge=1)
    coredump_export_max_bytes: int = Field(default=20_000_000_000, ge=1)
    coredump_export_quota_bytes: int = Field(default=100_000_000_000, ge=1)
    # 仅冻结快照和导出产物的保留时间；绝不按此删除设备直接写入的 NFS 原文件。
    coredump_retention_hours: int = Field(default=24, ge=1, le=168)
    known_hosts: str = ""
    ssh_verify_host_key: bool = False
    node_capacity: int = Field(default=DEFAULT_NODE_CAPACITY, ge=1, le=MAX_NODE_CAPACITY, strict=True)
    # 0 保留为停用调度的开发/维护开关；平台持久化配置仍要求正整数。
    cluster_capacity: int = Field(default=DEFAULT_CLUSTER_CAPACITY, ge=0, le=MAX_CLUSTER_CAPACITY, strict=True)
    retention_days: int = Field(default=7, ge=1, le=3650, strict=True)
    # 增长型 MongoDB 记录默认永久保留（0）。管理员显式设置正数后，维护作业
    # 才可按记录完成时间清理；这些开关不影响设备日志正文和 coredump 源文件。
    authentication_record_retention_days: int = Field(default=90, ge=0, le=3650, strict=True)
    audit_record_retention_days: int = Field(default=0, ge=0, le=3650, strict=True)
    runtime_event_retention_days: int = Field(default=0, ge=0, le=3650, strict=True)
    command_history_retention_days: int = Field(default=0, ge=0, le=3650, strict=True)
    operation_history_retention_days: int = Field(default=0, ge=0, le=3650, strict=True)
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
