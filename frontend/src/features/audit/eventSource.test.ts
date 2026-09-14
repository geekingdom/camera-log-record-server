// 事件来源展示保留真实主体，兼容旧API但不把任务创建人推断成操作人。
import { describe, expect, it } from "vitest";
import { eventSource } from "./eventSource";

describe("事件操作者与来源", () => {
  it("优先显示服务端明确的主体及节点来源", () => {
    expect(eventSource({ sourceName: "采集节点", sourceDetail: "collector-01" })).toEqual({ name: "采集节点", detail: "collector-01" });
  });
  it("兼容旧接口已有的用户与客户端IP", () => {
    expect(eventSource({ actor: "user-1", actorName: "运维", clientIp: "192.0.2.8" })).toEqual({ name: "运维", detail: "192.0.2.8" });
  });
  it("没有来源时明确缺失身份，不使用任务创建人", () => {
    expect(eventSource({ createdBy: "someone", taskName: "设备日志" })).toEqual({ name: "历史记录缺少身份", detail: "来源地址未保存" });
  });
});
