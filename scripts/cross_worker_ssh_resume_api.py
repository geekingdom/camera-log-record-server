"""跨 Worker SSH 验证的隔离 API 启动器。

当 ``ISAPI_TEST_PORT`` 非空时，仅在此测试子进程覆盖认证 URL 的端口；认证协议、
HTTP 客户端、凭据校验和生产配置均不改动。CI/root 环境保持默认80，不使用该适配。
"""

import os

import uvicorn


def main() -> None:
    """按需安装仅限本进程的回环 ISAPI URL 适配后启动正式 FastAPI 应用。"""
    test_port = os.environ.get("ISAPI_TEST_PORT", "")
    if test_port:
        from camera_logs.resources import authentication

        original = authentication._device_info_url

        def test_url(ip: str) -> str:
            """保留生产 IPv4/IPv6 URL 规则，只为非特权本机加入测试端口。"""
            return original(ip).replace("/ISAPI/", f":{int(test_port)}/ISAPI/")

        authentication._device_info_url = test_url
    from camera_logs.common.config import Settings
    from camera_logs.main import app

    settings = Settings()
    uvicorn.run(app, host="127.0.0.1", port=settings.node_port)


if __name__ == "__main__":
    main()
