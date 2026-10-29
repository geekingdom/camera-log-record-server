/** 用户账号页面的纯权限规则，固定权限始终以服务端权限目录为准。 */

import type { SessionUser, UserRole } from "../../shared/api";

export interface PermissionDirectoryItem {
  value: string;
  label: string;
  fixed?: boolean;
}

export function permissionGroups(items: PermissionDirectoryItem[]) {
  return {
    fixed: items.filter((item) => item.fixed === true),
    additional: items.filter((item) => item.fixed !== true),
  };
}

/** 历史账号仍可能带有已调整为固定的 scope，编辑时不允许将它作为额外权限回写。 */
export function formPermissionScopes(scopes: string[], items: PermissionDirectoryItem[]) {
  const fixed = new Set(permissionGroups(items).fixed.map((item) => item.value));
  return scopes.filter((scope) => !fixed.has(scope));
}

/** 管理员只能管理普通用户或自己；内建账号仍保留原有的受限保护。 */
export function canManageUser(currentUserId: string | undefined, currentRole: UserRole | undefined, target: Pick<SessionUser, "id" | "role" | "builtin">) {
  if (target.builtin) return false;
  if (currentRole === "SUPER_ADMIN") return true;
  return currentRole === "ADMIN" && (target.id === currentUserId || (target.role ?? "USER") === "USER");
}

export function roleLabel(role: UserRole | undefined) {
  return ({ SUPER_ADMIN: "超级管理员", ADMIN: "管理员", USER: "普通用户" } as const)[role ?? "USER"];
}
