"""节点间全文索引候选接口，只返回固定水位索引证据。"""

from __future__ import annotations

import hmac
import inspect
from typing import Annotated, Any

from fastapi import Depends, Header, HTTPException
from pydantic import BaseModel, ConfigDict, Field

FileIdentifier = Annotated[str, Field(min_length=1, max_length=128)]


class SearchIndexCandidatesRequest(BaseModel):
    """限制协调节点一次请求的目录范围，避免内部接口成为任意目录查询入口。"""

    model_config = ConfigDict(extra="forbid")
    fileIds: list[FileIdentifier] = Field(min_length=1, max_length=256)
    keyword: str = Field(min_length=1, max_length=1024)


def _item(file_id: str, evidence: Any) -> dict[str, Any] | None:
    """将可信的本地索引证据收敛为固定公开字段，异常证据交由协调端保守回退。"""
    if not isinstance(evidence, dict):
        return None
    size = evidence.get("bytes")
    has_newline = evidence.get("hasNewline")
    may_match = evidence.get("mayMatch")
    if isinstance(size, bool) or not isinstance(size, int) or size < 0:
        return None
    if not isinstance(has_newline, bool) or not isinstance(may_match, bool):
        return None
    try:
        head = bytes(evidence["head"])[:16]
        tail = bytes(evidence["tail"])[:16]
    except (KeyError, TypeError, ValueError):
        return None
    return {"fileId": file_id, "bytes": size, "headHex": head.hex(), "tailHex": tail.hex(),
            "hasNewline": has_newline, "mayMatch": may_match}


def install_search_index_routes(app: Any, repo: Any) -> None:
    """注册仅由协调节点调用的 READY 文件索引证据接口。"""

    async def internal(authorization: str | None = Header(default=None)) -> None:
        expected = "Bearer " + repo.settings.internal_token
        if not repo.settings.internal_token or not authorization or not hmac.compare_digest(authorization, expected):
            raise HTTPException(401, "内部节点认证失败")

    @app.post("/internal/search-index/candidates")
    async def candidates(body: SearchIndexCandidatesRequest, _: None = Depends(internal)):
        """返回本节点已封存文件的有限索引证据，未知或不合格标识符不透露原因。"""
        files = [file async for file in repo.db.files.find({
            "id": {"$in": body.fileIds}, "nodeId": repo.settings.node_id, "status": "READY",
        })]
        from camera_logs.logs.search_candidates import local_index_evidence
        evidence = local_index_evidence(repo, files, body.keyword)
        if inspect.isawaitable(evidence):
            evidence = await evidence
        if not isinstance(evidence, dict):
            return {"items": []}
        items = []
        eligible = {file.get("id") for file in files}
        returned = set()
        for file_id in body.fileIds:
            if file_id not in eligible or file_id in returned:
                continue
            item = _item(file_id, evidence.get(file_id))
            if item is not None:
                items.append(item)
                returned.add(file_id)
        return {"items": items}
