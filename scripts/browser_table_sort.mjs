// 资源和任务表三态排序浏览器验收：请求必须传 asc、desc，并在清除后移除排序参数。
import assert from "node:assert/strict";
import { mkdir } from "node:fs/promises";

const imported = await import(process.env.PLAYWRIGHT_MODULE || "playwright");
const { chromium } = imported.default || imported;
const browser = await chromium.launch({ headless: true, channel: "chrome" });
const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
await context.addInitScript(() => sessionStorage.setItem("camera-log-record-token", "table-sort-token"));
const calls = { resources: [], tasks: [] };
const resource = { id: "resource", name: "排序资源", kind: "HIKVISION_NETWORK", ip: "192.0.2.10", version: 1, createdAt: "2026-09-10T00:00:00Z", taskCount: 0, activeTaskCount: 0 };
const task = { id: "task", name: "排序任务", protocol: "SSH", ip: "192.0.2.10", port: 22, status: "STOPPED", desiredState: "STOPPED", resourceId: "resource", initialCommands: [], scheduledCommands: [] };
await context.route("**/api/v1/**", async route => {
  const request = route.request(); const url = new URL(request.url());
  if (request.method() !== "GET") return route.fulfill({ status: 204 });
  if (url.pathname === "/api/v1/auth/me") return route.fulfill({ json: { user: { id: "admin", username: "admin", displayName: "管理员", isAdmin: true, enabled: true, mustChangePassword: false, scopes: ["*"] } } });
  if (url.pathname === "/api/v1/resources") { calls.resources.push(Object.fromEntries(url.searchParams)); return route.fulfill({ json: { items: [resource], total: 1, page: 1, pageSize: 20 } }); }
  if (url.pathname === "/api/v1/tasks") { calls.tasks.push(Object.fromEntries(url.searchParams)); return route.fulfill({ json: { items: [task], total: 1, page: 1, pageSize: 20 } }); }
  return route.fulfill({ json: { items: [], total: 0, page: 1, pageSize: 20 } });
});
const page = await context.newPage(); page.setDefaultTimeout(10_000);
const output = "output/playwright";
try {
  await mkdir(output, { recursive: true });
  await page.goto("http://127.0.0.1:5173", { waitUntil: "networkidle" });
  await page.getByRole("heading", { name: "设备资源", exact: true }).waitFor();
  async function clickHeader(name, callsForTable) {
    const before = callsForTable.length;
    await page.locator(".el-table__header th").filter({ hasText: name }).first().click();
    await page.waitForTimeout(80);
  }
  await clickHeader("资源身份", calls.resources); await clickHeader("资源身份", calls.resources); await clickHeader("资源身份", calls.resources);
  assert.deepEqual(calls.resources.slice(-3).map(item => [item.sortBy, item.sortOrder]), [["identity", "asc"], ["identity", "desc"], [undefined, undefined]]);
  await page.setViewportSize({ width: 390, height: 900 });
  await page.locator(".el-table__header th").filter({ hasText: "网络地址" }).first().click(); await page.waitForTimeout(80);
  assert.deepEqual(calls.resources.at(-1).sortBy, "ip");
  await page.screenshot({ path: `${output}/table-sort-resource-mobile.png`, fullPage: true });
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.getByRole("tab", { name: "采集任务", exact: true }).click();
  await page.locator(".el-table__header th").filter({ hasText: "任务" }).first().click(); await page.waitForTimeout(80);
  await page.locator(".el-table__header th").filter({ hasText: "任务" }).first().click(); await page.waitForTimeout(80);
  await page.locator(".el-table__header th").filter({ hasText: "任务" }).first().click(); await page.waitForTimeout(80);
  assert.deepEqual(calls.tasks.slice(-3).map(item => [item.sortBy, item.sortOrder]), [["name", "asc"], ["name", "desc"], [undefined, undefined]]);
  await page.setViewportSize({ width: 390, height: 900 });
  await page.screenshot({ path: `${output}/table-sort-task-mobile.png`, fullPage: true });
  console.log(JSON.stringify({ passed: true, resourceCalls: calls.resources.length, taskCalls: calls.tasks.length }));
} finally { await browser.close(); }
