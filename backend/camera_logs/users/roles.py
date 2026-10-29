"""集中计算配置角色、北京时间权限有效期及管理员之间的账户保护。"""

from datetime import date

from fastapi import HTTPException

from camera_logs.common.presentation_time import SHANGHAI, utc_instant, utc_now

FIXED_SCOPES = frozenset({
    'tasks:read', 'logs:read', 'logs:download', 'templates:read',
    'templates:write', 'service-tokens:read', 'commands:send',
})


def configured_role(user):
    """旧账户按内置身份和 isAdmin 兼容读取；超级管理员只能是初始化账户。"""
    if user.get('builtin') and user.get('id') == 'builtin-admin':
        return 'SUPER_ADMIN'
    return 'ADMIN' if user.get('role') == 'ADMIN' or ('role' not in user and user.get('isAdmin')) else 'USER'


def permission_state(user, *, instant=None):
    """截止日当天完整有效，次日上海零点失效；缺少截止日表示长期有效。"""
    role = configured_role(user)
    expires = user.get('permissionsExpiresOn')
    today = utc_instant(instant or utc_now()).astimezone(SHANGHAI).date()
    expired = role != 'SUPER_ADMIN' and expires is not None and today > date.fromisoformat(expires)
    effective = 'USER' if expired else role
    return {'role': role, 'effectiveRole': effective, 'isAdmin': effective != 'USER',
            'permissionsExpiresOn': expires, 'permissionsExpired': expired}


def effective_scopes(user):
    """额外权限实时计算，不依赖定时任务改库，过期后服务令牌同步收权。"""
    state = permission_state(user)
    if state['isAdmin']:
        return ['*']
    return sorted(FIXED_SCOPES | (set() if state['permissionsExpired'] else set(user.get('scopes', []))))


def authorize_user_target(identity, target):
    """保护内置账户和其他管理员；到期管理员的配置身份仍受保护，避免横向接管。"""
    if target.get('builtin'):
        raise HTTPException(403, '内置超级管理员仅能自行修改密码')
    if identity.get('id') == 'bootstrap' or configured_role(identity) == 'SUPER_ADMIN':
        return
    if configured_role(target) == 'ADMIN' and target['id'] != identity['id']:
        raise HTTPException(403, '管理员不能编辑、删除或重置其他管理员的账户')


async def claim_account_manager(repo, identity, *, session=None, target=None):
    """事务内声明管理者，和并发降级、到期配置修改及删除串行化后重新授权。"""
    if identity['id'] == 'bootstrap':
        current = identity
    else:
        current = await repo.db.users.find_one_and_update(
            {'id': identity['id'], 'enabled': True, 'deletedAt': None},
            {'$inc': {'accountManagementVersion': 1}}, session=session,
        )
        if current is None or not permission_state(current)['isAdmin']:
            raise HTTPException(403, '管理权限已失效，请刷新账户状态')
    if target is not None:
        authorize_user_target(current, target)


async def protect_service_account(repo, identity, user_id, *, session=None):
    """管理员不能通过其他管理员的服务凭据绕过账户编辑保护；超级管理员及本人允许。"""
    if identity is None or identity.get('id') == 'bootstrap':
        return
    if session is not None:
        await claim_account_manager(repo, identity, session=session)
        target = await repo.db.users.find_one_and_update(
            {'id': user_id}, {'$inc': {'serviceAccountClaimVersion': 1}}, session=session,
        )
    else:
        target = await repo.db.users.find_one({'id': user_id})
    if (target and configured_role(identity) != 'SUPER_ADMIN'
            and configured_role(target) in {'ADMIN', 'SUPER_ADMIN'} and identity['id'] != user_id):
        raise HTTPException(403, '管理员不能操作其他管理员的服务凭据')
