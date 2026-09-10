// 防止把未冻结的完整文件永久显示成正在传输。
import { describe, expect, it } from "vitest";
import type { CoredumpFile } from "../../shared/types";
import { coredumpFileStatus } from "./coredumpStatus";

describe("coredump 文件状态", () => {
  const file = { status: "RECEIVING" } as CoredumpFile;
  it("旧记录不武断宣称正在传输", () => {
    expect(coredumpFileStatus(file)).toBe("等待稳定确认");
  });
  it("稳定文件不依赖用户先创建下载快照", () => {
    expect(coredumpFileStatus({ ...file, sourceState: "STABLE" })).toBe("文件已稳定");
    expect(coredumpFileStatus({ ...file, sourceState: "CHANGING" })).toBe("文件更新中");
  });
  it("正在创建和已完成副本保持独立状态", () => {
    expect(coredumpFileStatus({ ...file, status: "FREEZING", sourceState: "STABLE" })).toBe("正在准备副本");
    expect(coredumpFileStatus({ ...file, status: "FROZEN" })).toBe("副本可下载");
  });
});
