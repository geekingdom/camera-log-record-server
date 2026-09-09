"""定义少量需要跨客户端稳定识别的业务错误码，不改变普通 HTTP 错误合同。"""

from fastapi import HTTPException


class StableCodeHTTPException(HTTPException):
    """将机器可读错误码保留在服务端异常属性，由统一异常处理器输出。"""

    def __init__(self, status_code: int, code: str, detail: str):
        super().__init__(status_code, detail)
        self.code = code
