"""提供命令模板的分页读取、幂等创建、乐观锁更新和删除接口。"""

from typing import Annotated

from fastapi import Depends, HTTPException, Query, Request
from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

from camera_logs.common.database import now, public
from camera_logs.common.models import TemplateCreate, TemplatePatch
from camera_logs.common.security import actor, authorize


def install_template_routes(app, repo, listing):
    """安装模板路由，并通过版本字段避免并发编辑覆盖。"""
    User = Annotated[dict, Depends(actor)]

    @app.get("/api/v1/command-templates")
    async def templates(user: User, page: int = Query(1, ge=1), pageSize: int = Query(100, ge=1, le=100)):
        """分页列出当前主体可读取的命令模板。"""
        authorize(user, "templates:read")
        return await listing("templates", {}, page, pageSize)

    @app.post("/api/v1/command-templates", status_code=201)
    async def create_template(body: TemplateCreate, request: Request, user: User):
        """用幂等键创建唯一名称模板，重复名称转换为 409。"""
        authorize(user, "templates:write")
        async def build(identifier):
            doc = body.model_dump() | {"id": identifier, "version": 1, "createdAt": now(), "updatedAt": now()}
            try:
                await repo().db.templates.insert_one(doc)
            except DuplicateKeyError as exc:
                raise HTTPException(409, "模板名称已存在") from exc
            return doc
        return public(await repo().idem(user["id"], request.headers.get("Idempotency-Key"), "create_template",
                                        body.model_dump(), "templates", build))

    @app.get("/api/v1/command-templates/{identifier}")
    async def get_template(identifier: str, user: User):
        """读取单个模板及其命令快照。"""
        authorize(user, "templates:read")
        return public(await repo().get("templates", identifier))

    @app.patch("/api/v1/command-templates/{identifier}")
    async def edit_template(identifier: str, body: TemplatePatch, user: User):
        """以 version 条件原子更新模板，陈旧版本拒绝覆盖新内容。"""
        authorize(user, "templates:write")
        try:
            result = await repo().db.templates.find_one_and_update({"id": identifier, "version": body.version},
                {"$set": body.model_dump(exclude={"version"}) | {"updatedAt": now()}, "$inc": {"version": 1}},
                return_document=ReturnDocument.AFTER)
        except DuplicateKeyError as exc:
            raise HTTPException(409, "模板名称已存在") from exc
        if not result:
            raise HTTPException(409, "模板版本已变化，请刷新")
        await repo().audit(user["id"], "edit_template", identifier)
        return public(result)

    @app.delete("/api/v1/command-templates/{identifier}", status_code=204)
    async def delete_template(identifier: str, user: User, version: int = Query(..., ge=1)):
        """仅删除指定版本的模板，防止并发编辑后误删。"""
        authorize(user, "templates:write")
        result = await repo().db.templates.delete_one({"id": identifier, "version": version})
        if not result.deleted_count:
            raise HTTPException(409, "模板版本已变化，请刷新")
        await repo().audit(user["id"], "delete_template", identifier)
