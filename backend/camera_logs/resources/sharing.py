"""显式设备共享的对象操作资格；平台作用域始终由调用入口独立校验。"""

from fastapi import HTTPException

from camera_logs.common.security import authorize_owner


def resource_is_shared(identity, resource):
    """仅匹配不可复用的用户 ID；未配置、空列表及已删除资源均不授予共享资格。"""
    return resource.get('deletedAt') is None and identity.get('id') in resource.get('sharedUserIds', [])


def authorize_resource_operation(identity, resource):
    """允许所有者、管理员及显式共享用户操作资源；删除入口不得使用本函数。"""
    if not resource_is_shared(identity, resource):
        authorize_owner(identity, resource)


async def validate_share_users(repo, identifiers, *, session=None):
    """共享只能指定有效平台用户；事务中声明用户以串行化用户删除和禁用。"""
    for identifier in sorted(set(identifiers)):
        query = {'id': identifier, 'enabled': True, 'deletedAt': None}
        if session is None:
            found = await repo.db.users.find_one(query)
        else:
            found = await repo.db.users.find_one_and_update(query, {'$inc': {'resourceShareClaimVersion': 1}}, session=session)
        if found is None:
            raise HTTPException(422, '共享用户不存在、已禁用或已删除')


async def authorize_task_operation(repo, identity, task, *, session=None):
    """保留任务所有权；资源共享只沿主资源授予资格，串口服务器共享不扩散到其他设备。"""
    try:
        authorize_owner(identity, task)
        return
    except HTTPException:
        pass
    query = {'id': task.get('resourceId'), 'deletedAt': None, 'sharedUserIds': identity['id']}
    if session is None:
        resource = await repo.db.resources.find_one(query)
    else:
        # 与共享撤销/资源删除写入同一资源，使事务重试时重新判断当前授权。
        resource = await repo.db.resources.find_one_and_update(query, {'$inc': {'controlClaimVersion': 1}}, session=session)
    if resource is None:
        raise HTTPException(403, '仅任务创建者、管理员或设备资源指定共享用户可以执行此操作')


async def add_task_sharing(repo, tasks):
    """一次批量读取主资源共享名单供前端判断操作资格，避免逐任务查询。"""
    ids = list({task.get('resourceId') for task in tasks if task.get('resourceId')})
    resources = {r['id']: r.get('sharedUserIds', []) async for r in repo.db.resources.find(
        {'id': {'$in': ids}, 'deletedAt': None}, {'id': 1, 'sharedUserIds': 1})}
    for task in tasks:
        task['resourceSharedUserIds'] = resources.get(task.get('resourceId'), [])
