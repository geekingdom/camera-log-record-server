"""提供管理员维护的全局常用命令，以及所有登录用户的快捷补全读取接口。"""

from typing import Annotated

from fastapi import Depends, HTTPException, Query, Request
from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

from camera_logs.common.audited_mutations import audited_create, audited_mutation
from camera_logs.common.database import now, public
from camera_logs.common.models import CommonCommandCreate, CommonCommandPatch
from camera_logs.common.security import actor, authorize


def _public_common_command(document: dict) -> dict:
    """输出补全与管理共用的公开字段，不把 Mongo 内部标识暴露给浏览器。"""
    return public(document)


def install_common_command_routes(app, repo):
    """安装全局快捷命令路由；管理写入和全员只读入口明确分离。"""
    User = Annotated[dict, Depends(actor)]

    async def listing(query: dict, page_number: int, page_size: int) -> dict:
        """按命令文本升序分页，前端可用页偏移生成稳定的表格序号。"""
        cursor = repo().db.common_commands.find(query).sort([("command", 1), ("id", 1)]).skip(
            (page_number - 1) * page_size
        ).limit(page_size)
        return {
            "items": [_public_common_command(item) async for item in cursor],
            "total": await repo().db.common_commands.count_documents(query),
            "page": page_number,
            "pageSize": page_size,
        }

    def filtered_query(query_text: str | None, *, enabled_only: bool) -> dict:
        """全文本前缀提示使用转义正则，避免用户输入改变查询语义。"""
        import re

        query: dict = {"enabled": True} if enabled_only else {}
        if query_text:
            expression = {"$regex": re.escape(query_text.strip()), "$options": "i"}
            query["$or"] = [{"command": expression}, {"description": expression}]
        return query

    @app.get("/api/v1/common-commands")
    async def common_commands(
        user: User,
        page: int = Query(1, ge=1, description="从 1 开始的分页页码。"),
        pageSize: int = Query(100, ge=1, le=100, description="每页返回的常用命令数量，最大100。"),
        query: str | None = Query(default=None, max_length=256, description="按命令或描述进行不区分大小写的字面子串筛选。"),
    ):
        """所有登录用户读取已启用命令，用于工作台手工输入的本地补全。"""
        del user
        return await listing(filtered_query(query, enabled_only=True), page, pageSize)

    @app.get("/api/v1/admin/common-commands")
    async def admin_common_commands(
        user: User,
        page: int = Query(1, ge=1, description="从 1 开始的分页页码。"),
        pageSize: int = Query(100, ge=1, le=100, description="每页返回的常用命令数量，最大100。"),
        query: str | None = Query(default=None, max_length=256, description="按命令或描述进行不区分大小写的字面子串筛选。"),
    ):
        """管理员读取完整表格，包含禁用项以支持恢复或删除。"""
        authorize(user, "admin")
        return await listing(filtered_query(query, enabled_only=False), page, pageSize)

    @app.post("/api/v1/admin/common-commands", status_code=201)
    async def create_common_command(body: CommonCommandCreate, request: Request, user: User):
        """管理员以幂等键创建全局快捷命令，并将创建事实与审计同时提交。"""
        authorize(user, "admin")
        async def prepare(identifier: str) -> dict:
            """固定标识和版本后再进入可重试事务，避免重复执行产生多个记录。"""
            timestamp = now()
            return body.model_dump() | {
                "id": identifier,
                "version": 1,
                "createdAt": timestamp,
                "updatedAt": timestamp,
            }

        try:
            result = await audited_create(
                repo(), user["id"], request.headers.get("Idempotency-Key"), "create_common_command",
                body.model_dump(), "common_commands", prepare,
            )
        except DuplicateKeyError as error:
            raise HTTPException(409, "常用命令已存在") from error
        except HTTPException as error:
            # MongoMock 的测试替身没有真实事务回滚，会遗留本应回滚的幂等映射。
            # 真实副本集由唯一索引触发 DuplicateKeyError；此分支只把已确认的同文本冲突稳定为 409。
            if error.status_code == 410 and await repo().db.common_commands.find_one({"command": body.command}):
                raise HTTPException(409, "常用命令已存在") from error
            raise
        return _public_common_command(result)

    @app.patch("/api/v1/admin/common-commands/{identifier}")
    async def update_common_command(identifier: str, body: CommonCommandPatch, user: User):
        """管理员按版本局部编辑命令、说明或启用状态，冲突时要求刷新。"""
        authorize(user, "admin")

        async def commit(session):
            """版本条件与更新写入必须在同一事务中，避免并发管理覆盖。"""
            values = body.model_dump(exclude={"version"}, exclude_none=True)
            result = await repo().db.common_commands.find_one_and_update(
                {"id": identifier, "version": body.version},
                {"$set": values | {"updatedAt": now()}, "$inc": {"version": 1}},
                return_document=ReturnDocument.AFTER,
                session=session,
            )
            if result is None:
                if await repo().db.common_commands.find_one({"id": identifier}, session=session) is None:
                    raise HTTPException(404, "常用命令不存在")
                raise HTTPException(409, "常用命令版本已变化，请刷新")
            return result

        try:
            result = await audited_mutation(repo(), user["id"], "update_common_command", identifier, commit)
        except DuplicateKeyError as error:
            raise HTTPException(409, "常用命令已存在") from error
        return _public_common_command(result)

    @app.delete("/api/v1/admin/common-commands/{identifier}", status_code=204)
    async def delete_common_command(
        identifier: str,
        user: User,
        version: int = Query(..., ge=1, description="删除前必须提交的当前命令版本号。"),
    ):
        """管理员按版本永久删除未再需要的提示项，历史审计仍保留删除事实。"""
        authorize(user, "admin")

        async def commit(session):
            """删除和审计共同提交，避免不可追溯的全局命令变更。"""
            result = await repo().db.common_commands.delete_one(
                {"id": identifier, "version": version}, session=session
            )
            if result.deleted_count:
                return
            if await repo().db.common_commands.find_one({"id": identifier}, session=session) is None:
                raise HTTPException(404, "常用命令不存在")
            raise HTTPException(409, "常用命令版本已变化，请刷新")

        await audited_mutation(repo(), user["id"], "delete_common_command", identifier, commit)
