// 通用批量操作必须串行、保留逐项失败，并在调用方失效后显式标记未执行项目。
import { describe, expect, it, vi } from "vitest";
import { runSequentially } from "./bulkOperations";

describe("runSequentially", () => {
  it("按选择顺序执行，失败后继续并保留每项结果", async () => {
    const calls: string[] = [];
    const operation = vi.fn(async (id: string) => {
      calls.push(id);
      if (id === "second") throw new Error("版本已变化");
    });

    await expect(runSequentially([
      { id: "first", name: "第一个", version: 2 },
      { id: "second", name: "第二个", version: 3 },
      { id: "third", name: "第三个" },
    ], operation)).resolves.toEqual([
      { id: "first", name: "第一个", status: "success" },
      { id: "second", name: "第二个", status: "error", error: "版本已变化" },
      { id: "third", name: "第三个", status: "success" },
    ]);
    expect(calls).toEqual(["first", "second", "third"]);
    expect(operation).toHaveBeenNthCalledWith(3, "third", 1);
  });

  it("调用方停止后不发送其余请求，并将其标记为跳过", async () => {
    const operation = vi.fn(async (_id: string, _version: number) => undefined);
    let allowed = true;
    const result = await runSequentially([
      { id: "first", name: "第一个" },
      { id: "second", name: "第二个" },
    ], async (id, version) => {
      await operation(id, version);
      allowed = false;
    }, { shouldContinue: () => allowed });

    expect(operation).toHaveBeenCalledTimes(1);
    expect(result).toEqual([
      { id: "first", name: "第一个", status: "success" },
      { id: "second", name: "第二个", status: "skipped" },
    ]);
  });
});
