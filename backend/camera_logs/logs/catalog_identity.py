"""日志目录身份：节点本地相对路径必须带节点命名空间，旧记录保持可寻址。"""

import hashlib
from pathlib import Path


def file_identity(root: Path, path: Path, node_id: str | None = None) -> str:
    """生成稳定分卷ID；省略节点仅用于识别升级前的旧目录记录。"""
    relative = str(path.relative_to(root)).removesuffix(".tar.gz").removesuffix(".log")
    value = relative if node_id is None else f"node:{node_id}\0{relative}"
    return hashlib.sha256(value.encode()).hexdigest()[:32]
