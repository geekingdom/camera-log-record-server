// 归档请求竞态浏览器验收：延迟日期 A 的响应，确认日期 B 始终保留在当前表格中。
import assert from "node:assert/strict";

const imported = await import(process.env.PLAYWRIGHT_MODULE || "playwright");
const { chromium } = imported.default || imported;
const task = {
  id: "log-race-fixture", name: "归档竞态验收任务", protocol: "TELNET_SERIAL",
  resourceId: "serial-resource-fixture",
  ip: "192.0.2.20", port: 10003, status: "COLLECTING", desiredState: "RUNNING",
  initialCommands: [], scheduledCommands: [], updatedAt: "2030-01-01T00:00:00.000Z",
};
const dateA = "2030-01-01";
const dateB = "2030-01-02";
let releaseA;
let dateAStarted;
let oldResponseFinished;
const dateAStartedPromise = new Promise((resolve) => { dateAStarted = resolve; });
const oldResponseFinishedPromise = new Promise((resolve) => { oldResponseFinished = resolve; });

function hour(hourId, timestamp) {
  return {
    hourId, hour: timestamp, status: "READY", integrity: "VERIFIED", bytes: 1024,
    archiveBytes: 512, fragmentCount: 1, files: [],
  };
}
async function json(route, body) {
  await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(body) });
}

const browser = await chromium.launch({ headless: true, channel: "chrome" });
const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
await context.addInitScript(() => sessionStorage.setItem("camera-log-record-token", "synthetic-ui-token"));
await context.route("**/api/v1/**", async (route) => {
  const request = route.request();
  const url = new URL(request.url());
  if (request.method() !== "GET") throw new Error(`不应发出写请求：${request.method()} ${url.pathname}`);
  if (url.pathname === "/api/v1/tasks")
    return json(route, { items: [task], total: 1, page: 1, pageSize: 100 });
  if (url.pathname === "/api/v1/resources")
    return json(route, { items: [{ id: "serial-resource-fixture", name: "竞态串口服务器", kind: "SERIAL_SERVER", ip: task.ip, version: 1 }], total: 1, page: 1, pageSize: 20 });
  if (url.pathname === `/api/v1/tasks/${task.id}`) return json(route, task);
  if (url.pathname === "/api/v1/command-templates")
    return json(route, { items: [], total: 0, page: 1, pageSize: 100 });
  if (url.pathname === "/api/v1/nodes" || url.pathname === "/api/v1/admin/nodes")
    return json(route, { items: [], total: 0, page: 1, pageSize: 100 });
  if (url.pathname === "/api/v1/platform-settings")
    return json(route, { retentionDays: 7, version: 1, updatedAt: task.updatedAt });
  if (url.pathname === "/api/v1/display-settings")
    return json(route, { liveLogBufferMiB: 10 });
  if (url.pathname === "/api/v1/service-tokens" || url.pathname === "/api/v1/audit-events" || url.pathname === "/api/v1/runtime-events")
    return json(route, { items: [], total: 0, page: 1, pageSize: 100 });
  if (url.pathname === `/api/v1/tasks/${task.id}/log-hours`) {
    const date = url.searchParams.get("date");
    if (date === dateA) {
      dateAStarted();
      await new Promise((resolve) => { releaseA = resolve; });
      await json(route, { items: [hour("old-a", "2030-01-01T00:00:00.000Z")], total: 1, page: 1, pageSize: 24 });
      oldResponseFinished();
      return;
    }
    if (date === dateB)
      return json(route, { items: [hour("current-b", "2030-01-02T00:00:00.000Z")], total: 1, page: 1, pageSize: 24 });
    return json(route, { items: [], total: 0, page: 1, pageSize: 24 });
  }
  throw new Error(`未模拟的 API 请求：${request.method()} ${url.pathname}`);
});

const page = await context.newPage();
page.setDefaultTimeout(10000);
const errors = [];
page.on("pageerror", (error) => errors.push(error.message));
page.on("console", (message) => { if (message.type() === "error") errors.push(message.text()); });

try {
  await page.goto("http://127.0.0.1:5173", { waitUntil: "networkidle" });
  await page.getByRole("tab", { name: "采集任务", exact: true }).click();
  await page.getByText(task.name, { exact: true }).waitFor();
  await page.getByRole("button", { name: "编辑任务", exact: true }).first().click();
  await page.getByRole("tab", { name: "小时归档", exact: true }).click();
  const dateInput = page.getByLabel("归档日期", { exact: true });
  await dateInput.fill(dateA);
  await dateInput.press("Enter");
  await dateAStartedPromise;
  await dateInput.fill(dateB);
  await dateInput.press("Enter");
  const currentRow = page.locator(".archive-table .el-table__row").filter({ hasText: /2030\/1\/2/ });
  await currentRow.waitFor();
  releaseA();
  await oldResponseFinishedPromise;
  assert.equal(await currentRow.count(), 1, "当前列表必须保留日期 B");
  assert.equal(await page.locator(".archive-table .el-table__row").filter({ hasText: /2030\/1\/1/ }).count(), 0, "延迟的日期 A 响应不得覆盖日期 B");
  assert.deepEqual(errors, []);
  console.log(JSON.stringify({ passed: true, delayedDate: dateA, currentDate: dateB, consoleErrors: 0 }));
} finally {
  await browser.close();
}
