/** 用户账号管理的错误提示按操作类型区分，避免把用户名冲突误报为版本冲突。 */

import { ApiError } from "../../shared/api";

export function userSaveFailureMessage(error: unknown, editing: boolean): string {
  // 创建重名不触发无关的列表刷新，避免覆盖用户仍在填写的表单。
  if (error instanceof ApiError && error.status === 409)
    return editing ? "用户已被其他管理员修改，请刷新后重试" : "用户名已存在，请使用其他用户名";
  return error instanceof Error ? error.message : "保存用户失败";
}
