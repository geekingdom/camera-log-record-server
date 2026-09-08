// 状态详情浏览器验收：隔离 API 响应，不修改真实设备任务，也不使用真实访问令牌。
import assert from "node:assert/strict";
import { mkdir } from "node:fs/promises";

const imported = await import(process.env.PLAYWRIGHT_MODULE || "playwright");
const { chromium } = imported.default || imported;
const browser = await chromium.launch({ headless: true, channel: "chrome" });
const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
await context.addInitScript(() => sessionStorage.setItem("camera-log-record-token", "synthetic-ui-token"));
const originalError = "未收到完整调试密文，疑似锁定或响应超时；已停止任务，不自动重试";
const task = {
  id: "status-fixture", name: "串口任务状态验收-" + "长名称".repeat(15),
  protocol: "TELNET_SERIAL", ip: "192.0.2.1", port: 10003,
  status: "ERROR", desiredState: "STOPPED", error: originalError,
  shellMode: "PSH", debugPhase: "FAILED", runId: "r".repeat(64), sessionId: "s".repeat(64),
  initialCommands: [], scheduledCommands: [], updatedAt: "2026-09-08T06:00:00Z",
};
const seenFilters = [];
await context.route("**/api/v1/**", async route => {
  const request = route.request();
  assert.equal(request.method(), "GET", "状态查看不得发出修改请求");
  const url = new URL(request.url());
  let items = [];
  if (url.pathname === "/api/v1/tasks") {
    seenFilters.push(url.searchParams.get("status"));
    items = [task];
  }
  await route.fulfill({ json: { items, total: items.length, page: 1, pageSize: 20 } });
});
const page = await context.newPage();
page.setDefaultTimeout(10000);
const errors = [];
page.on("pageerror", error => errors.push(error.message));
page.on("console", message => { if (message.type() === "error") errors.push(message.text()); });
const output = "output/playwright";

try {
  await mkdir(output, { recursive: true });
  await page.goto("http://127.0.0.1:5173", { waitUntil: "networkidle" });
  await page.getByRole("cell", { name: "采集失败", exact: true }).waitFor();
  await page.locator(".task-filters .el-select").click();
  const filtered = page.waitForResponse(response => response.url().includes("status=BLOCKED"));
  await page.getByRole("option", { name: "等待隔离", exact: true }).click();
  await filtered;
  assert.ok(seenFilters.includes("BLOCKED"));
  await page.getByRole("button", { name: "查看任务状态", exact: true }).click();
  const dialog = page.getByRole("dialog", { name: "任务运行状态" });
  await dialog.getByText(originalError, { exact: true }).waitFor();
  await dialog.getByText("PSH", { exact: true }).waitFor();
  await dialog.getByText("切换失败", { exact: true }).waitFor();
  // 弹窗保持打开，轮询新快照必须更新原来的详情；错误按文本渲染，不解释为 HTML。
  task.error = "更新后的记录 <img src=x onerror=alert(1)> " + "x".repeat(180);
  await dialog.getByText(task.error, { exact: true }).waitFor();
  assert.equal(await dialog.locator("img").count(), 0);
  for (const width of [1440, 390, 320]) {
    await page.setViewportSize({ width, height: 1000 });
    await page.waitForTimeout(200);
    const bounds = await dialog.boundingBox();
    assert.ok(bounds.x >= 0 && bounds.x + bounds.width <= width, "弹窗越过视口");
    const overflow = await dialog.evaluate(element => element.scrollWidth > element.clientWidth);
    assert.equal(overflow, false, "弹窗内容横向溢出");
    await page.screenshot({ path: `${output}/task-status-${width}.png`, fullPage: true });
  }
  await dialog.getByRole("button", { name: "关闭", exact: true }).click();
  await dialog.waitFor({ state: "hidden" });
  assert.deepEqual(errors, []);
  console.log(JSON.stringify({ passed: true, viewports: [1440, 390, 320], consoleErrors: 0, taskMutations: 0 }));
} finally {
  await browser.close();
}
