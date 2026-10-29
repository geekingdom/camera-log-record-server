// 用户管理权限规则：固定权限来自服务端目录，角色保护必须使用当前生效角色。
import { describe, expect, it } from "vitest";

import {
  canManageUser,
  formPermissionScopes,
  permissionGroups,
  roleLabel,
} from "./userManagerPermissions";

describe("用户管理权限规则", () => {
  const permissions = [
    { value: "tasks:read", label: "查看设备与任务", fixed: true },
    { value: "commands:send", label: "发送命令", fixed: true },
    { value: "tasks:write", label: "编辑任务", fixed: false },
  ];

  it("按服务端 fixed 标记区分固定与额外权限，固定 commands:send 不进入可编辑选择", () => {
    expect(permissionGroups(permissions)).toEqual({
      fixed: permissions.slice(0, 2),
      additional: [permissions[2]],
    });
    expect(formPermissionScopes(["commands:send", "tasks:write"], permissions)).toEqual(["tasks:write"]);
  });

  it("超级管理员可管理非内建账号，管理员只能管理自己或普通用户", () => {
    expect(canManageUser("admin-self", "ADMIN", { id: "other-admin", role: "ADMIN", builtin: false })).toBe(false);
    expect(canManageUser("admin-self", "ADMIN", { id: "admin-self", role: "ADMIN", builtin: false })).toBe(true);
    expect(canManageUser("admin-self", "ADMIN", { id: "operator", role: "USER", builtin: false })).toBe(true);
    expect(canManageUser("super", "SUPER_ADMIN", { id: "other-admin", role: "ADMIN", builtin: false })).toBe(true);
    expect(canManageUser("super", "SUPER_ADMIN", { id: "super", role: "SUPER_ADMIN", builtin: true })).toBe(false);
  });

  it("将配置角色和到期后生效角色分别显示", () => {
    expect(roleLabel("ADMIN")).toBe("管理员");
    expect(roleLabel("USER")).toBe("普通用户");
    expect(roleLabel("SUPER_ADMIN")).toBe("超级管理员");
  });
});
