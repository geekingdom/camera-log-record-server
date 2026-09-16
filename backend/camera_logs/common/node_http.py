"""固定分池复用节点 HTTP 连接，限制单次连接分配的扫描范围。"""

from contextlib import AsyncExitStack

import httpx

POOL_COUNT = 8
MAX_CONNECTIONS = 500
MAX_KEEPALIVE_CONNECTIONS = 100


class NodeHttpPool:
    """轮询分配只读请求；八个池合计保持原有 500/100 连接预算。

    HTTP 请求彼此独立，文件身份、字节偏移及实时游标仍随每个请求传递。
    单个请求只使用一个客户端，不在分池层重试；只读重试仍由调用方控制。
    """

    def __init__(self):
        self._stack = AsyncExitStack()
        self._clients = []
        self._next = 0
        self._started = False
        self._closed = False

    @property
    def is_closed(self):
        """关闭开始后拒绝新请求，调用方不应继续借用正在回收的连接池。"""
        return self._closed

    async def __aenter__(self):
        """逐一登记客户端；初始化失败也释放之前已经进入生命周期的池。"""
        if self._started or self._closed:
            raise RuntimeError("节点 HTTP 连接池不能重复启动")
        self._started = True
        try:
            for index in range(POOL_COUNT):
                limits = httpx.Limits(
                    max_connections=MAX_CONNECTIONS // POOL_COUNT + (index < MAX_CONNECTIONS % POOL_COUNT),
                    max_keepalive_connections=MAX_KEEPALIVE_CONNECTIONS // POOL_COUNT
                    + (index < MAX_KEEPALIVE_CONNECTIONS % POOL_COUNT),
                )
                client = await self._stack.enter_async_context(httpx.AsyncClient(timeout=30, limits=limits))
                self._clients.append(client)
        except BaseException:
            self._closed = True
            await self._stack.aclose()
            raise
        return self

    async def __aexit__(self, *exc):
        """即使某个客户端退出失败，ExitStack 仍继续回收其余客户端。"""
        self._closed = True
        return await self._stack.__aexit__(*exc)

    async def get(self, url, **kwargs):
        """在首次让出事件循环前选择客户端，均匀分配并发请求且不改写参数。"""
        if not self._started or self._closed or len(self._clients) != POOL_COUNT:
            raise RuntimeError("节点 HTTP 连接池未运行")
        client = self._clients[self._next]
        self._next = (self._next + 1) % POOL_COUNT
        return await client.get(url, **kwargs)

    async def post(self, url, **kwargs):
        """复用同一受生命周期管理的连接池发送内部节点控制请求。"""
        if not self._started or self._closed or len(self._clients) != POOL_COUNT:
            raise RuntimeError("节点 HTTP 连接池未运行")
        client = self._clients[self._next]
        self._next = (self._next + 1) % POOL_COUNT
        return await client.post(url, **kwargs)
