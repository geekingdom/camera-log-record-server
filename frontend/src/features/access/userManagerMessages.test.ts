import { describe, expect, it } from "vitest";
import { ApiError } from "../../shared/api";
import { userSaveFailureMessage } from "./userManagerMessages";

describe("用户账号保存提示", () => {
  it("创建重名只提示用户名冲突，不误报并发修改", () => {
    expect(userSaveFailureMessage(new ApiError(409, "用户名已存在", undefined, "CONFLICT"), false))
      .toBe("用户名已存在，请使用其他用户名");
  });

  it("编辑版本冲突提示刷新后重试", () => {
    expect(userSaveFailureMessage(new ApiError(409, "账号版本已变化", undefined, "CONFLICT"), true))
      .toBe("用户已被其他管理员修改，请刷新后重试");
  });
});
