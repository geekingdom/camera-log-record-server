// 编辑器独立检查权限；缺少根会话提供者时默认拒绝，避免异步打开绕过入口判断。
import { inject, type InjectionKey } from "vue";

export interface Permissions {
  can(scope: string): boolean;
  resource(id?: string): boolean;
  allResources(): boolean;
}
export const permissionKey: InjectionKey<Permissions> = Symbol("platform-permissions");
export function usePermissions(): Permissions {
  return inject(permissionKey, { can: () => false, resource: () => false, allResources: () => false });
}
