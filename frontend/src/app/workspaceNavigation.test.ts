import { expect, it } from "vitest";
import { DEFAULT_WORKSPACE } from "./useWorkspaceNavigation";
import { workspaceNavigation } from "./workspaceNavigation";

it("资源看板位于第一导航并作为默认工作空间", () => {
  expect(workspaceNavigation[0]?.key).toBe("dashboard");
  expect(DEFAULT_WORKSPACE).toBe("dashboard");
});
