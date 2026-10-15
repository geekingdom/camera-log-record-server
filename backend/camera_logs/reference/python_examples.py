"""由接口目录最终合同生成可运行的 Python 调用示例，不读取实际配置或账号。"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

_SENSITIVE_ENV = {
    "password": "CAMERA_LOGS_PASSWORD",
    "currentpassword": "CAMERA_LOGS_CURRENT_PASSWORD",
    "newpassword": "CAMERA_LOGS_NEW_PASSWORD",
    "token": "CAMERA_LOGS_TOKEN",
}
_SESSION_PATHS = {"/api/v1/auth/login", "/api/v1/auth/logout", "/api/v1/auth/password", "/api/v1/auth/me"}


def _secret_environment(name: str) -> str | None:
    """将请求示例中的口令字段映射到运行环境，文档正文不保留任何口令样例。"""
    return _SENSITIVE_ENV.get(name.lower())


def _expression(value: Any, *, key: str = "", indent: int = 4) -> str:
    """生成 Python 字面量；口令和值为 token 的字段固定读取环境变量。"""
    environment = _secret_environment(key)
    if environment:
        return f'os.environ["{environment}"]'
    if isinstance(value, Mapping):
        if not value:
            return "{}"
        nested = indent + 4
        return "{\n" + ",\n".join(
            f"{' ' * nested}{field!r}: {_expression(item, key=field, indent=nested)}" for field, item in value.items()
        ) + f",\n{' ' * indent}}}"
    if isinstance(value, list):
        if not value:
            return "[]"
        nested = indent + 4
        return "[\n" + ",\n".join(
            f"{' ' * nested}{_expression(item, indent=nested)}" for item in value
        ) + f",\n{' ' * indent}]"
    return repr(value)


def _path_values(operation: Mapping[str, Any]) -> dict[str, Any]:
    """取目录路径参数示例，缺失示例仍给出可替换的安全占位符。"""
    return {
        str(parameter["name"]): parameter.get("example", f"<{parameter['name']}>")
        for parameter in operation.get("parameters", [])
        if parameter.get("in") == "path"
    }


def _query_values(operation: Mapping[str, Any]) -> dict[str, Any]:
    """仅传必填或默认查询项，避免将分页 cursor 与 page 等互斥条件同时拼入。"""
    return {
        str(parameter["name"]): _parameter_value(parameter)
        for parameter in operation.get("parameters", [])
        if parameter.get("in") == "query" and parameter.get("example") is not None
        and (parameter.get("required") or "default" in parameter.get("schema", {}))
    }


def _parameter_value(parameter: Mapping[str, Any]) -> Any:
    """将目录参数示例修正为其 OpenAPI 正则或日期格式允许的最小值。"""
    schema = parameter.get("schema", {})
    pattern = schema.get("pattern")
    if pattern == r"^\d{4}-\d{2}$":
        return "2026-09"
    if pattern == r"^\d{4}-\d{2}-\d{2}$":
        return "2026-09-09"
    if schema.get("format") == "date-time":
        return "2026-09-09T10:00:00+00:00"
    if schema.get("format") == "date":
        return "2026-09-09"
    return parameter.get("example")


def _request_headers(operation: Mapping[str, Any], *, session: bool) -> dict[str, str]:
    """把展示型目录头转换为真实 requests 头，Cookie 由 Session 自己维护。"""
    headers = operation.get("headers", {})
    result = {str(key): str(value) for key, value in headers.items()
              if key not in {"Authorization", "Cookie", "Content-Type", "Idempotency-Key"}}
    if session and operation["method"] in {"POST", "PATCH", "PUT", "DELETE"}:
        result["X-Requested-With"] = "XMLHttpRequest"
    return result


def _is_binary(operation: Mapping[str, Any]) -> bool:
    """二进制合同使用流式落盘，目录响应示例明确标记其非 JSON 内容。"""
    return isinstance(operation.get("responseExample"), str) and "二进制" in operation["responseExample"]


def _is_prometheus_text(operation: Mapping[str, Any]) -> bool:
    """Prometheus 指标端点返回文本，不可按 JSON 解码。"""
    return operation["path"] == "/metrics"


def _environment_variables(operation: Mapping[str, Any], *, session: bool) -> list[tuple[str, str]]:
    """列出当前片段真实读取的环境变量，便于复制前一次性完成最小配置。"""
    values = [("CAMERA_LOGS_BASE_URL", "http://127.0.0.1:8000")]
    if session:
        values.extend([("CAMERA_LOGS_USERNAME", "your-username"), ("CAMERA_LOGS_PASSWORD", "your-password")])
    elif "Authorization" in operation.get("headers", {}):
        values.append(("CAMERA_LOGS_TOKEN", "your-service-token"))
    payload = operation.get("requestExample") or {}
    serialized = repr(payload).lower()
    for field, environment in _SENSITIVE_ENV.items():
        if field in serialized and environment not in {name for name, _ in values}:
            values.append((environment, "your-password" if "PASSWORD" in environment else "your-service-token"))
    if "Idempotency-Key" in operation.get("headers", {}):
        values.append(("CAMERA_LOGS_IDEMPOTENCY_KEY", "stable-request-key"))
    return list(dict.fromkeys(values))


def _preamble(operation: Mapping[str, Any], *, session: bool, websocket: bool = False) -> list[str]:
    """在每个独立代码块首部写明版本、依赖和本例所需环境变量。"""
    dependency = "websockets" if websocket else "requests"
    lines = ["# Python 3.11+", f"# Install: python -m pip install {dependency}", "# Required environment variables:"]
    lines.extend(f"# export {name}={value}" for name, value in _environment_variables(operation, session=session))
    return lines + [""]


def _optional_query_comment(operation: Mapping[str, Any]) -> str | None:
    """提醒读者可选筛选必须按接口组合规则手动开启，避免目录示例误拼互斥参数。"""
    optional = [str(parameter["name"]) for parameter in operation.get("parameters", [])
                if parameter.get("in") == "query" and not parameter.get("required")
                and "default" not in parameter.get("schema", {})]
    if optional:
        return "    # 可选查询参数请按接口约束增补，不能与 cursor/page 等互斥项盲目同传：" + ", ".join(optional)
    return None


def _http_source(operation: Mapping[str, Any]) -> str:
    """生成 requests 示例，覆盖 Bearer、登录会话、参数、JSON 和二进制流。"""
    session = operation["path"] in _SESSION_PATHS
    path_values = _path_values(operation)
    query = _query_values(operation)
    payload = operation.get("requestExample")
    documented_headers = operation.get("headers", {})
    headers = _request_headers(operation, session=session)
    header_expression = _expression(headers)
    lines = _preamble(operation, session=session) + [
        "import os",
        "from pathlib import Path",
        "from urllib.parse import quote",
        "",
        "",
        "def main():",
        "    import requests",
        '    base_url = os.environ.get("CAMERA_LOGS_BASE_URL", "http://127.0.0.1:8000").rstrip("/")',
        f"    path = {operation['path']!r}",
        f"    path_values = {_expression(path_values)}",
        "    for name, value in path_values.items():",
        '        path = path.replace("{" + name + "}", quote(str(value), safe=""))',
        "    url = base_url + path",
        f"    headers = {header_expression}",
        f"    params = {_expression(query)}",
    ]
    optional_query = _optional_query_comment(operation)
    if optional_query:
        lines.append(optional_query)
    if not session and "Authorization" in documented_headers:
        lines.append('    headers["Authorization"] = "Bearer " + os.environ["CAMERA_LOGS_TOKEN"]')
    if "Idempotency-Key" in documented_headers:
        lines.extend([
            '    # 结果未知时以同一个键重试；修改业务请求时改用新键。',
            '    headers["Idempotency-Key"] = os.environ["CAMERA_LOGS_IDEMPOTENCY_KEY"]',
        ])
    if payload is not None:
        if operation["path"] == "/api/v1/auth/login":
            lines.extend([
                "    payload = {",
                '        "username": os.environ["CAMERA_LOGS_USERNAME"],',
                '        "password": os.environ["CAMERA_LOGS_PASSWORD"],',
                "    }",
            ])
        else:
            lines.append(f"    payload = {_expression(payload)}")
    lines.append("    session = requests.Session()")
    lines.append("    login_response = response = None")
    lines.append("    try:")
    request_lines: list[str] = []
    if session and operation["path"] != "/api/v1/auth/login":
        request_lines.extend([
            'login_response = session.request(method="POST", url=base_url + "/api/v1/auth/login",',
            '                                 headers={"X-Requested-With": "XMLHttpRequest"},',
            '                                 json={"username": os.environ["CAMERA_LOGS_USERNAME"],',
            '                                       "password": os.environ["CAMERA_LOGS_PASSWORD"]}, timeout=30)',
            "login_response.raise_for_status()",
            "login_response.close()",
            "login_response = None",
        ])
    request_lines.append("response = session.request(")
    request_lines.extend([
        f"    method={operation['method']!r},",
        "    url=url,",
        "    headers=headers,",
        "    params=params,",
        "    json=" + ("payload," if payload is not None else "None,"),
        "    timeout=30,",
    ])
    if _is_binary(operation):
        request_lines.append("    stream=True,")
    request_lines.extend([")", "response.raise_for_status()"]) 
    if _is_binary(operation):
        request_lines.extend([
            'output = Path("download.bin")',
            'partial = output.with_suffix(output.suffix + ".partial")',
            'with partial.open("wb") as target:',
            "    for chunk in response.iter_content(chunk_size=1024 * 1024):",
            "        if chunk:",
            "            target.write(chunk)",
            "partial.replace(output)",
            "print(output)",
        ])
    elif operation.get("responseStatus") == 204:
        request_lines.append("print({\"status\": response.status_code})")
    elif _is_prometheus_text(operation):
        request_lines.append("print(response.text)")
    else:
        request_lines.append("print(response.json())")
    lines.extend("        " + line for line in request_lines)
    lines.extend([
        "    finally:",
        "        if response is not None:",
        "            response.close()",
        "        if login_response is not None:",
        "            login_response.close()",
        "        session.close()",
    ])
    lines.extend(["", "", 'if __name__ == "__main__":', "    main()", ""])
    return "\n".join(lines)


def _websocket_source(operation: Mapping[str, Any]) -> str:
    """生成实时日志订阅示例，认证 token 只在 JSON 首帧发送而不置入 URL。"""
    request = operation.get("requestExample") or {}
    cursor = request.get("cursor", "")
    path_values = _path_values(operation)
    return "\n".join(_preamble(operation, session=False, websocket=True) + [
        "import asyncio",
        "import base64",
        "import json",
        "import os",
        "import sys",
        "from urllib.parse import quote",
        "",
        "",
        "async def main():",
        "    import websockets",
        '    base_url = os.environ.get("CAMERA_LOGS_BASE_URL", "http://127.0.0.1:8000").rstrip("/")',
        '    ws_base_url = ("wss://" + base_url.removeprefix("https://")) if base_url.startswith("https://") else ("ws://" + base_url.removeprefix("http://"))',
        f"    path = {operation['path']!r}",
        f"    path_values = {_expression(path_values)}",
        "    for name, value in path_values.items():",
        '        path = path.replace("{" + name + "}", quote(str(value), safe=""))',
        "    async with websockets.connect(ws_base_url + path, open_timeout=10) as websocket:",
        f"        cursor = {_expression(cursor)}",
        "        await websocket.send(json.dumps({'token': os.environ['CAMERA_LOGS_TOKEN'], 'cursor': cursor}))",
        "        try:",
        "            while True:",
        "                event = json.loads(await websocket.recv())",
        "                cursor = event.get('cursor', cursor)",
        "                if event.get('type') == 'data':",
        "                    sys.stdout.buffer.write(base64.b64decode(event['data']))",
        "                    sys.stdout.buffer.flush()",
        "                elif event.get('type') == 'gap':",
        "                    print('实时缓冲存在缺口，请按 fileId/offset 从日志接口补读。', file=sys.stderr)",
        "                else:",
        "                    print(event, file=sys.stderr)",
        "        except websockets.ConnectionClosed:",
        "            print('连接已关闭；重连时将最后 cursor 放入首帧：' + str(cursor), file=sys.stderr)",
        "",
        "",
        'if __name__ == "__main__":',
        "    asyncio.run(main())",
        "",
    ])


def build_python_example(operation: Mapping[str, Any]) -> str:
    """从目录最终操作生成一段完整可运行的 Python 调用示例。"""
    if operation["method"] == "WS":
        return _websocket_source(operation)
    return _http_source(operation)
