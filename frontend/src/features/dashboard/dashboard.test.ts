import { describe, expect, it } from "vitest";
import { formatDashboardBytes, nodeHealthLabel, shanghaiDate, trendPoints } from "./dashboard";

describe("资源看板展示边界", () => {
  it("按上海日期查询，字节只在界面换算", () => {
    expect(shanghaiDate(new Date("2026-10-13T16:30:00.000Z"))).toBe("2026-10-14");
    expect(formatDashboardBytes(1_572_864)).toBe("1.5 MiB");
    expect(formatDashboardBytes(undefined)).toBe("暂无数据");
  });
  it("趋势缺失不补零，节点状态不推测未返回的原因", () => {
    expect(trendPoints([{ date: "2026-10-14", bytes: Number.NaN, fileCount: 1 }])).toEqual([{ date: "2026-10-14", bytes: null }]);
    expect(nodeHealthLabel("WARNING")).toBe("需关注");
    expect(nodeHealthLabel("CUSTOM")).toBe("CUSTOM");
  });
});
