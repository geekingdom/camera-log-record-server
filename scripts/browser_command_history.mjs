// 命令记录浏览器验收：同正文不同配置 ID 必须独立筛选，所有接口均为页面 mock。
import assert from "node:assert/strict";
import { mkdir } from "node:fs/promises";

const imported = await import(process.env.PLAYWRIGHT_MODULE || "playwright");
const { chromium } = imported.default || imported;
const output = process.env.BROWSER_SCREENSHOTS || "output/playwright";
const tasks = ["甲任务", "乙任务"].map((name, index) => ({
  id: `task-${index + 1}`,
  name,
  protocol: "SSH",
  ip: `192.0.2.${index + 1}`,
  port: 22,
  resourceId: "camera",
  status: "COLLECTING",
  desiredState: "RUNNING",
  initialCommands: [],
  scheduledCommands: [],
}));
const filters = [];

const browser = await chromium.launch({ headless: true, channel: "chrome" });
try {
  await mkdir(output, { recursive: true });
  const context = await browser.newContext({ viewport: { width: 1440, height: 900 } });
  await context.routeWebSocket("**/api/v1/tasks/*/logs**", socket => socket.close());
  await context.addInitScript(() => sessionStorage.setItem("camera-log-record-token", "history"));
  await context.route("**/api/v1/**", async route => {
    const request = route.request();
    const url = new URL(request.url());
    const path = url.pathname;
    const json = (body, status = 200) => route.fulfill({
      status,
      contentType: "application/json",
      body: JSON.stringify(body),
    });
    if (path === "/api/v1/auth/me") {
      return json({ user: {
        id: "admin", username: "admin", displayName: "管理员", isAdmin: true,
        enabled: true, mustChangePassword: false, scopes: ["*"],
      } });
    }
    if (path === "/api/v1/display-settings") return json({ liveLogBufferMiB: 10 });
    if (path === "/api/v1/tasks") return json({ items: tasks, total: 2, page: 1, pageSize: 20 });
    if (["/api/v1/resources", "/api/v1/command-templates", "/api/v1/nodes", "/api/v1/users/creators"].includes(path)) {
      const items = path === "/api/v1/resources"
        ? [{ id: "camera", name: "模拟相机", kind: "HIKVISION_NETWORK", ip: "192.0.2.1" }]
        : [];
      return json({ items, total: items.length, page: 1, pageSize: 20 });
    }
    if (path.startsWith("/api/v1/tasks/") && path.endsWith("/command-executions")) {
      const id = path.split("/")[4];
      const commandId = url.searchParams.get("commandId");
      const kind = url.searchParams.get("kind");
      filters.push({ id, commandId, kind });
      const scheduledCommands = [
        { id: "same-a", command: "logread", totalExecutions: 5, intervalSeconds: 60, attempts: 2 },
        { id: "same-b", command: "logread", totalExecutions: 8, intervalSeconds: 30, attempts: 6 },
      ];
      const items = id === "task-2"
        ? [{ id: "old", kind: "SCHEDULED", commandId: "gone", commandSource: "UNAVAILABLE", status: "UNKNOWN" }]
        : commandId === "same-b"
          ? [{ id: "b", kind: "SCHEDULED", commandId: "same-b", command: "logread", commandSource: "SNAPSHOT", attempts: 6, totalExecutions: 8, intervalSeconds: 30, status: "SUCCEEDED" }]
          : [{ id: "a", kind: "SCHEDULED", commandId: "same-a", command: "logread", commandSource: "SNAPSHOT", attempts: 2, totalExecutions: 5, intervalSeconds: 60, status: "SUCCEEDED" }];
      return json({
        items,
        total: items.length,
        page: 1,
        pageSize: 50,
        runId: id === "task-1" ? "run-current" : null,
        scheduledCommands: id === "task-1" ? scheduledCommands : [],
      });
    }
    if (path.startsWith("/api/v1/tasks/")) return json(tasks.find(task => path.endsWith(task.id)) || tasks[0]);
    throw new Error(`unmocked ${request.method()} ${path}`);
  });

  const page = await context.newPage();
  page.setDefaultTimeout(10000);
  const errors = [];
  page.on("pageerror", error => errors.push(error.message));
  await page.goto(process.env.BASE_URL || "http://127.0.0.1:5173", { waitUntil: "networkidle" });
  await page.getByRole("tab", { name: "日志工作台", exact: true }).click();
  await page.getByRole("button", { name: "选择任务", exact: true }).click();
  await page.getByLabel("选择任务 甲任务").click();
  await page.getByRole("button", { name: "确认切换", exact: true }).click();
  await page.getByRole("tab", { name: "命令记录", exact: true }).click();
  await page.getByText("当前运行：run-current", { exact: true }).waitFor();
  assert.equal(await page.locator(".command-progress-item").count(), 2);

  await page.locator(".command-progress-item").filter({ hasText: "定时命令 2 · logread" }).click();
  await page.getByText("配置 ID: same-b", { exact: true }).waitFor();
  assert.deepEqual(filters.at(-1), { id: "task-1", commandId: "same-b", kind: "SCHEDULED" });

  for (const width of [1440, 390]) {
    await page.setViewportSize({ width, height: 844 });
    assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1));
    await page.screenshot({ path: `${output}/command-history-${width}.png`, fullPage: true });
  }

  await page.getByRole("button", { name: "切换任务", exact: true }).click();
  await page.getByLabel("选择任务 乙任务").click();
  await page.getByRole("button", { name: "确认切换", exact: true }).click();
  await page.getByRole("tab", { name: "命令记录", exact: true }).click();
  await page.getByText("历史记录未保存命令内容", { exact: true }).waitFor();
  assert.equal(await page.getByText("配置 ID: same-b", { exact: true }).count(), 0);
  assert.deepEqual(errors, []);
  await context.close();
  console.log(JSON.stringify({ passed: true, filters, screenshots: `${output}/command-history-{1440,390}.png` }));
} finally {
  await browser.close();
}
