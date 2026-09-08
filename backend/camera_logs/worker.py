"""采集节点启动入口。保留 python -m camera_logs.worker 的部署命令，运行逻辑位于 node 模块。"""

import uvicorn

from camera_logs.node.worker import create_worker_app

if __name__ == "__main__":
    uvicorn.run(create_worker_app(), host="0.0.0.0", port=8001)
