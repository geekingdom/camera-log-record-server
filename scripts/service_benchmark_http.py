"""以固定小连接池测量正式 API，避免压测客户端的大池分配等待遮蔽服务耗时。"""

from contextlib import AsyncExitStack

import httpx


class BenchmarkTransport(httpx.AsyncBaseTransport):
    """八池轮询分发完整请求，保持原 max(32, 路数 + 8) 总连接预算。

    原 Limits 未限制空闲数，实际空闲上限等于总连接数；各池采用同样规则。
    仅拆分连接分配范围，不重试、不改变超时、正文、认证或观测计时。
    调用方须在 AsyncClient 上使用异步上下文，统一回收流式下载与普通请求。
    """

    def __init__(self, routes: int):
        self._limit = max(32, routes + 8)
        self._stack = AsyncExitStack()
        self._transports = []
        self._next = 0
        self._started = False
        self._closed = False

    async def __aenter__(self):
        """登记每个已进入的传输层，部分初始化失败仍关闭已创建的连接池。"""
        if self._started or self._closed:
            raise RuntimeError("压测 HTTP 连接池不能重复启动")
        self._started = True
        try:
            for index in range(8):
                count = self._limit // 8 + (index < self._limit % 8)
                limits = httpx.Limits(max_connections=count, max_keepalive_connections=count)
                transport = await self._stack.enter_async_context(httpx.AsyncHTTPTransport(limits=limits))
                self._transports.append(transport)
        except BaseException:
            await self.aclose()
            raise
        return self

    async def handle_async_request(self, request):
        """首次 await 前分配请求；直接返回响应流，由 HTTPX 按原语义读取和关闭。"""
        if not self._started or self._closed or len(self._transports) != 8:
            raise RuntimeError("压测 HTTP 连接池未运行")
        transport = self._transports[self._next]
        self._next = (self._next + 1) % len(self._transports)
        return await transport.handle_async_request(request)

    async def aclose(self):
        """关闭开始即拒绝借用；某个关闭失败也由 ExitStack 继续释放其余池。"""
        self._closed = True
        await self._stack.aclose()

    async def __aexit__(self, *exc):
        """将异常上下文传给所有传输层，并在正常、失败或取消后关闭连接。"""
        self._closed = True
        return await self._stack.__aexit__(*exc)
