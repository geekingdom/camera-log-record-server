// 所有者写权限只补充功能 scope，不替代后端鉴权或平台来源 IP 策略。
export function canManageOwnedRecord(
  userId: string | undefined,
  isAdmin: boolean | undefined,
  createdBy: string | undefined,
) {
  return Boolean(isAdmin || (userId && createdBy === userId));
}

/** 资源显式共享只扩大编辑、认证和关联任务操作资格，删除资源仍由调用处校验所有者资格。 */
export function canManageSharedResourceRecord(
  userId: string | undefined,
  isAdmin: boolean | undefined,
  createdBy: string | undefined,
  sharedUserIds: string[] | undefined,
) {
  return Boolean(
    canManageOwnedRecord(userId, isAdmin, createdBy)
    || (userId && sharedUserIds?.includes(userId)),
  );
}
