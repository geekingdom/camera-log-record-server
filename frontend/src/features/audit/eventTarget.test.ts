import { describe, expect, it } from "vitest";
import { requestTargetDetail, requestTargetName } from "./eventTarget";

describe("请求对象展示", () => {
  it("节点列表没有单个ID，显示集合查询", () => {
    const row = { route: "/api/v1/nodes", targetLabel: "服务节点", targetScope: "COLLECTION", method: "GET" };
    expect(requestTargetName(row)).toBe("服务节点");
    expect(requestTargetDetail(row)).toBe("集合查询（无单个对象）");
  });
  it("实体显示名称及IP，平台设置显示平台范围", () => {
    const row = { targetName: "测试设备", deviceIp: "10.41.203.35", targetId: "resource-35" };
    expect(requestTargetName(row)).toBe("测试设备");
    expect(requestTargetDetail(row)).toBe("10.41.203.35");
    expect(requestTargetDetail({ targetScope: "PLATFORM" })).toBe("平台级接口");
    expect(requestTargetDetail({ targetScope: "OBJECT" })).toBe("该记录未保存对象标识");
  });
});
