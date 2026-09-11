import { describe, expect, it } from "vitest";
import { appendCommandHistory, commandHistoryKey, commandSuggestions } from "./liveCommandHistory";

describe("实时命令历史", () => {
  it("以用户和任务共同隔离存储键", () => {
    expect(commandHistoryKey("user-a", "task-a")).not.toBe(commandHistoryKey("user-b", "task-a"));
    expect(commandHistoryKey("user-a", "task-a")).not.toBe(commandHistoryKey("user-a", "task-b"));
  });

  it("成功命令去重并将最新项置顶且最多保存一百条", () => {
    const history = Array.from({ length: 100 }, (_, index) => `command-${index}`);
    const updated = appendCommandHistory(history, "latest-command");
    expect(updated).toHaveLength(100);
    expect(updated[0]).toBe("latest-command");
    expect(updated.filter(item => item === "latest-command")).toHaveLength(1);
    expect(updated).not.toContain("command-99");
  });

  it("按输入内容给出有界的字面候选", () => {
    expect(commandSuggestions(["outputOpen", "outputClose", "prtHardInfo"], "out")).toEqual([
      "outputOpen", "outputClose",
    ]);
  });
});
