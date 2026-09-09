"""提供命令模板的分页读取、幂等创建、乐观锁更新和删除接口。"""

from typing import Annotated

from fastapi import Depends, HTTPException, Query, Request
from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

from camera_logs.common.audited_mutations import audited_create, audited_mutation
from camera_logs.common.database import now, public
from camera_logs.common.models import TemplateCreate, TemplatePatch
from camera_logs.common.security import actor, authorize, authorize_owner


def public_template(document):
    """输出完整的模板读模型；历史模板缺少共享字段时按未共享兼容。"""
    return public(document) | {
        "sharedWith": document.get("sharedWith", []),
        "sharedWithAll": bool(document.get("sharedWithAll", False)),
    }


def template_read_query(user, include_deleted=False):
    """构造模板可见范围；已删除模板仅管理员或创建者通过显式参数查询。"""
    if user.get("isAdmin") or "*" in user.get("scopes", []):
        return {} if include_deleted else {"deletedAt": None}
    visible = {"$or": [
        {"createdBy": user["id"]},
        {"sharedWith": user["id"]},
        {"sharedWithAll": True},
    ]}
    if include_deleted:
        return {"$or": [
            {"createdBy": user["id"]},
            {"$and": [visible, {"deletedAt": None}]},
        ]}
    return {"$and": [visible, {"deletedAt": None}]}


async def readable_template(repo, user, identifier):
    """读取当前主体可使用的模板；不可见模板按不存在处理，避免泄露其名称和配置。"""
    template = await repo.db.templates.find_one({"id": identifier} | template_read_query(user))
    if template is None:
        raise HTTPException(404, "命令模板不存在或无权使用")
    return template


async def validate_shared_users(repo, identifiers):
    """确认每个共享对象均为有效用户；被禁用或删除的账号不能获得模板读取能力。"""
    if not identifiers:
        return
    users = [user async for user in repo.db.users.find({
        "id": {"$in": identifiers}, "enabled": True, "deletedAt": None,
    })]
    if {user["id"] for user in users} != set(identifiers):
        raise HTTPException(422, "共享用户不存在、已禁用或已删除")


def install_template_routes(app, repo, listing):
    """安装模板路由，并通过版本字段避免并发编辑覆盖。"""
    User = Annotated[dict, Depends(actor)]

    @app.get("/api/v1/command-templates")
    async def templates(user: User, page: int = Query(1, ge=1), pageSize: int = Query(100, ge=1, le=100),
                        createdBy: str | None = None, includeDeleted: bool = False):
        """分页列出可读取模板；历史项只向管理员或创建者开放。"""
        authorize(user, "templates:read")
        query = template_read_query(user, includeDeleted)
        if createdBy:
            query = {"$and": [query, {"createdBy": createdBy}]}
        result = await listing("templates", query, page, pageSize)
        result["items"] = [public_template(item) for item in result["items"]]
        return result

    @app.post("/api/v1/command-templates", status_code=201)
    async def create_template(body: TemplateCreate, request: Request, user: User):
        """用幂等键创建唯一名称模板，重复名称转换为 409。"""
        authorize(user, "templates:write")
        if body.sharedWithAll and not user.get("isAdmin"):
            raise HTTPException(403, "仅管理员可以公开共享模板")
        await validate_shared_users(repo(), body.sharedWith)
        async def prepare(identifier):
            """固定配置和标识后进入数据库事务，创建事实与审计不可分离。"""
            return body.model_dump() | {
                "id": identifier, "version": 1, "createdAt": now(), "updatedAt": now(),
                "createdBy": user["id"],
                "createdByName": user.get("displayName") or user.get("username") or user["id"],
            }

        return public_template(await audited_create(repo(), user["id"], request.headers.get("Idempotency-Key"),
                                                    "create_template", body.model_dump(), "templates", prepare))

    @app.get("/api/v1/command-templates/{identifier}")
    async def get_template(identifier: str, user: User):
        """读取单个模板及其命令快照。"""
        authorize(user, "templates:read")
        return public_template(await readable_template(repo(), user, identifier))

    @app.patch("/api/v1/command-templates/{identifier}")
    async def edit_template(identifier: str, body: TemplatePatch, user: User):
        """以 version 条件原子更新模板，陈旧版本拒绝覆盖新内容。"""
        authorize(user, "templates:write")
        existing = await repo().get("templates", identifier)
        authorize_owner(user, existing)
        if existing.get("deletedAt") is not None:
            raise HTTPException(404, "命令模板不存在或已删除")
        if body.sharedWithAll and not user.get("isAdmin"):
            raise HTTPException(403, "仅管理员可以公开共享模板")
        await validate_shared_users(repo(), body.sharedWith)
        async def commit(session):
            """过期版本抛出异常回滚，不能为未发生的修改记录成功审计。"""
            result = await repo().db.templates.find_one_and_update({"id": identifier, "version": body.version, "createdBy": existing.get("createdBy"), "deletedAt": None},
                {"$set": body.model_dump(exclude={"version"}) | {"updatedAt": now()}, "$inc": {"version": 1}},
                return_document=ReturnDocument.AFTER, session=session)
            if not result:
                raise HTTPException(409, "模板版本已变化，请刷新")
            return result

        try:
            result = await audited_mutation(repo(), user["id"], "edit_template", identifier, commit)
        except DuplicateKeyError as exc:
            raise HTTPException(409, "模板名称已存在") from exc
        return public_template(result)

    @app.delete("/api/v1/command-templates/{identifier}", status_code=204)
    async def delete_template(identifier: str, user: User, version: int = Query(..., ge=1)):
        """仅删除指定版本的模板，防止并发编辑后误删。"""
        authorize(user, "templates:write")
        existing = await repo().get("templates", identifier)
        authorize_owner(user, existing)
        if existing.get("deletedAt") is not None:
            raise HTTPException(404, "命令模板不存在或已删除")
        async def commit(session):
            """模板删除与审计一起提交，已复制到任务的命令快照不参与此事务。"""
            result = await repo().db.templates.find_one_and_update(
                {"id": identifier, "version": version, "createdBy": existing.get("createdBy"), "deletedAt": None},
                {"$set": {"deletedAt": now(), "updatedAt": now()}, "$inc": {"version": 1}},
                return_document=ReturnDocument.AFTER,
                session=session,
            )
            if not result:
                raise HTTPException(409, "模板版本已变化，请刷新")

        await audited_mutation(repo(), user["id"], "delete_template", identifier, commit)
