/** 任务列表生命周期操作的状态矩阵测试。 */
import { describe, expect, it } from "vitest";

import type { Task } from "../../shared/types";
import { availableTaskActions } from "./taskActions";

function task(overrides: Partial<Task>): Task {
  return {
    id: "task-1",
    resourceId: "resource-1",
    name: "测试任务",
    protocol: "SSH",
    initialCommands: [],
    scheduledCommands: [],
    ...overrides,
  };
}

describe("availableTaskActions", () => {
  it("采集中的串口任务只显示停止", () => {
    expect(availableTaskActions(task({
      protocol: "TELNET_SERIAL", status: "COLLECTING", desiredState: "RUNNING",
    }))).toEqual(["stop"]);
  });

  it("采集中的 SSH 任务显示暂停和停止，不显示重复启动", () => {
    expect(availableTaskActions(task({ status: "COLLECTING", desiredState: "RUNNING" })))
      .toEqual(["pause", "stop"]);
  });

  it("已暂停的 SSH 任务显示继续和停止", () => {
    expect(availableTaskActions(task({ status: "PAUSED", desiredState: "PAUSED" })))
      .toEqual(["resume", "stop"]);
  });

  it("暂停意图已改变时不重复显示继续或停止", () => {
    expect(availableTaskActions(task({ status: "PAUSED", desiredState: "RUNNING" }))).toEqual([]);
    expect(availableTaskActions(task({ status: "PAUSED", desiredState: "STOPPED" }))).toEqual([]);
  });

  it("已停止任务显示启动，过渡状态不显示重复操作", () => {
    expect(availableTaskActions(task({ status: "STOPPED", desiredState: "STOPPED" })))
      .toEqual(["start"]);
    expect(availableTaskActions(task({ status: "PAUSING", desiredState: "PAUSED" })))
      .toEqual([]);
    expect(availableTaskActions(task({ status: "STOPPING", desiredState: "STOPPED" })))
      .toEqual([]);
  });

  it("等待调度和连接中的任务只允许撤销运行意图", () => {
    expect(availableTaskActions(task({ status: "PENDING", desiredState: "RUNNING" })))
      .toEqual(["stop"]);
    expect(availableTaskActions(task({ status: "CONNECTING", desiredState: "RUNNING" })))
      .toEqual(["stop"]);
    expect(availableTaskActions(task({ status: "RECONNECTING", desiredState: "RUNNING" })))
      .toEqual(["stop"]);
  });

  it("Telnet 设备采集中不显示 SSH 暂停，未归属的错误任务可以启动", () => {
    expect(availableTaskActions(task({
      protocol: "TELNET_DEVICE", status: "COLLECTING", desiredState: "RUNNING",
    }))).toEqual(["stop"]);
    expect(availableTaskActions(task({ status: "ERROR", desiredState: "STOPPED", nodeId: undefined })))
      .toEqual(["start"]);
    expect(availableTaskActions(task({ status: "ERROR", desiredState: "STOPPED", nodeId: "node-a" })))
      .toEqual([]);
  });

  it("隔离等待任务不显示无法完成的控制操作", () => {
    expect(availableTaskActions(task({ status: "BLOCKED", desiredState: "RUNNING" }))).toEqual([]);
  });
});
