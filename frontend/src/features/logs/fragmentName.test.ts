import { describe, expect, it } from "vitest";
import { displayLogFragmentName } from "./fragmentName";

describe("日志片段展示名", () => {
  it("优先使用归档成员名，不把小时包名误作片段名", () => {
    expect(displayLogFragmentName({
      id: "ready", archiveName: "任务-192.0.2.9-20261019120000-20261019130000.tar.gz",
      archiveMember: "任务-192.0.2.9-20261019121841-20261019151008-part-000001.log",
    })).toBe("任务-192.0.2.9-20261019121841-20261019151008-part-000001.log");
  });

  it("写入中的旧文件按上海服务器首末时间生成当前名称", () => {
    expect(displayLogFragmentName({
      id: "open", rawFileName: "任务-192.0.2.9-20261019120000-20261019130000-part-000001.log",
      firstReceivedAt: "2026-10-19T04:18:41+00:00", lastReceivedAt: "2026-10-19T07:10:08+00:00",
    })).toBe("任务-192.0.2.9-20261019121841-20261019151008-part-000001.log");
  });

  it("历史目录缺少完整或带时区时间时保留原始名并最终回退文件标识", () => {
    expect(displayLogFragmentName({ id: "legacy", rawFileName: "legacy-part-000001.log", firstReceivedAt: "bad" }))
      .toBe("legacy-part-000001.log");
    expect(displayLogFragmentName({
      id: "naive", rawFileName: "任务-192.0.2.9-20261019120000-20261019130000-part-000001.log",
      firstReceivedAt: "2026-10-19T12:18:41", lastReceivedAt: "2026-10-19T15:10:08",
    })).toBe("任务-192.0.2.9-20261019120000-20261019130000-part-000001.log");
    expect(displayLogFragmentName({ id: "unknown" })).toBe("unknown");
  });
});
