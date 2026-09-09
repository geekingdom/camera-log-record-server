// 所有者写权限只补充功能 scope，不替代后端鉴权或平台来源 IP 策略。
export function canManageOwnedRecord(
  userId: string | undefined,
  isAdmin: boolean | undefined,
  createdBy: string | undefined,
) {
  return Boolean(isAdmin || (userId && createdBy === userId));
}
