// 所有者权限规则：管理员全权，普通用户只能操作自己创建的对象。
import { describe, expect, it } from "vitest";
import { canManageOwnedRecord, canManageSharedResourceRecord } from "./ownership";

describe("canManageOwnedRecord", () => {
  it("允许管理员管理任意对象", () => {
    expect(canManageOwnedRecord("admin", true, "other-user")).toBe(true);
  });

  it("只允许普通用户管理自己创建的对象", () => {
    expect(canManageOwnedRecord("user-a", false, "user-a")).toBe(true);
    expect(canManageOwnedRecord("user-a", false, "user-b")).toBe(false);
  });

  it("缺失当前用户或创建者时拒绝普通用户写入", () => {
    expect(canManageOwnedRecord(undefined, false, "user-a")).toBe(false);
    expect(canManageOwnedRecord("user-a", false, undefined)).toBe(false);
  });
});

describe("canManageSharedResourceRecord", () => {
  it("允许资源所有者、管理员和被显式共享的用户编辑或操作资源", () => {
    expect(canManageSharedResourceRecord("owner", false, "owner", [])).toBe(true);
    expect(canManageSharedResourceRecord("admin", true, "owner", [])).toBe(true);
    expect(canManageSharedResourceRecord("member", false, "owner", ["member"])).toBe(true);
  });

  it("不因共享而授予未列名用户或无身份用户资源操作资格", () => {
    expect(canManageSharedResourceRecord("other", false, "owner", ["member"])).toBe(false);
    expect(canManageSharedResourceRecord(undefined, false, "owner", ["member"])).toBe(false);
  });
});
