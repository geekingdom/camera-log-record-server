"""coredump 平台接口的输入模型；路径始终由节点扫描生成，客户端不能提交。"""

from pydantic import BaseModel, Field


class CoredumpExportCreate(BaseModel):
    """请求将一个或多个已登记 coredump 冻结并导出。"""

    fileIds: list[str] = Field(min_length=1, max_length=100)
