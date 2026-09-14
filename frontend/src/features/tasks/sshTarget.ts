// 统一 SSH 主从采集目标的传输值与控制台标签，避免列表和选择器产生歧义。
import type { Protocol, SshTarget } from "../../shared/types";

export const sshTargetOptions: ReadonlyArray<{ value: SshTarget; label: string }> = [
  { value: "HOST", label: "主机" },
  { value: "SLAVE_1", label: "从机 1" },
  { value: "SLAVE_2", label: "从机 2" },
  { value: "SLAVE_3", label: "从机 3" },
];

/** 非 SSH 协议没有从机通道，协议切换时必须恢复主机避免提交无效配置。 */
export function normalizeSshTarget(protocol: Protocol, target?: SshTarget): SshTarget {
  return protocol === "SSH" ? target ?? "HOST" : "HOST";
}

/** 历史任务没有该字段时按创建接口默认值展示为主机。 */
export function sshTargetLabel(target?: SshTarget): string {
  return sshTargetOptions.find(option => option.value === (target ?? "HOST"))?.label ?? "主机";
}
