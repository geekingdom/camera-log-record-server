"""限定可直接分享的附件内容路由，目录与管理接口继续执行访问策略。"""
import re

_CONTENT_PATH = re.compile(r"/api/v1/(?:downloads|coredumps|coredump-exports)/[^/]+/content")


def is_public_download(method: str, path: str) -> bool:
    """仅精确匹配已生成附件的GET路径，避免相似前缀扩大策略豁免范围。"""
    return method == "GET" and _CONTENT_PATH.fullmatch(path) is not None
