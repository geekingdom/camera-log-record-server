// Coredump 配置迁移验收：资源保存开关，任务编辑器不再显示或提交该配置。
import assert from "node:assert/strict";
import { mkdir } from "node:fs/promises";
const imported = await import(process.env.PLAYWRIGHT_MODULE || "playwright");
const { chromium } = imported.default || imported;
const browser = await chromium.launch({ headless: true, channel: "chrome" });
const context = await browser.newContext({ viewport: { width: 1440, height: 900 } });
await context.addInitScript(() => sessionStorage.setItem("camera-log-record-token", "resource-coredump-test-token"));
const resources = [{ id: "camera-a", name: "模拟海康 A", kind: "HIKVISION_NETWORK", ip: "192.0.2.10", username: "http", authType: "DIGEST", version: 1, enableCoredumpMonitor: false, enableResourceMonitor: false }];
const task = { id: "task-a", name: "模拟采集任务", resourceId: "camera-a", protocol: "SSH", ip: "192.0.2.10", port: 22, username: "ssh", status: "STOPPED", desiredState: "STOPPED", version: 1, initialCommands: [], scheduledCommands: [] };
const mutations = [];
async function json(route, body, status = 200) { await route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) }); }
await context.route("**/api/v1/**", async route => {
  const request = route.request(), url = new URL(request.url()), path = url.pathname, method = request.method();
  if (path === "/api/v1/auth/me") return json(route, { user: { id: "admin", username: "admin", displayName: "验收管理员", isAdmin: true, enabled: true, mustChangePassword: false, scopes: ["*"] } });
  if (method === "GET" && path === "/api/v1/resources") return json(route, { items: resources, total: 1, page: 1, pageSize: 20 });
  if (method === "GET" && path === "/api/v1/resources/camera-a") return json(route, resources[0]);
  if (method === "POST" && path === "/api/v1/resources/camera-a/authenticate") return json(route, { model: "DS-2CD", subSerialNumber: "fixture", softwareVersion: "V5" });
  if (method === "PATCH" && path === "/api/v1/resources/camera-a") { const payload = request.postDataJSON(); mutations.push(payload); Object.assign(resources[0], payload); return json(route, resources[0]); }
  if (method === "GET" && path === "/api/v1/tasks") return json(route, { items: [task], total: 1, page: 1, pageSize: 20 });
  if (method === "GET" && path === "/api/v1/tasks/task-a") return json(route, task);
  if (method === "GET" && path === "/api/v1/platform-settings") return json(route, { retentionDays: 7, version: 1 });
  if (method === "GET" && ["/api/v1/command-templates", "/api/v1/nodes"].includes(path)) return json(route, { items: [], total: 0, page: 1, pageSize: 20 });
  throw new Error(`未模拟请求：${method} ${path}`);
});
const page = await context.newPage(); page.setDefaultTimeout(12_000);
const errors = []; page.on("pageerror", error => errors.push(error.message));
async function confirm() { const dialog = page.locator(".el-message-box"); if (await dialog.isVisible().catch(() => false)) await dialog.getByRole("button", { name: "确认", exact: true }).click(); }
try {
  await mkdir("output/playwright", { recursive: true });
  await page.goto(process.env.BASE_URL || "http://127.0.0.1:5173", { waitUntil: "networkidle" });
  await page.getByRole("heading", { name: "设备资源", exact: true }).waitFor();
  await page.getByRole("button", { name: "编辑资源", exact: true }).click();
  const editor = page.getByRole("dialog", { name: "编辑设备资源", exact: true });
  await editor.getByText("资源监控", { exact: true }).waitFor();
  const switches = editor.locator(".resource-monitor-options .el-switch");
  await switches.nth(0).scrollIntoViewIfNeeded(); await switches.nth(0).click();
  await switches.nth(1).scrollIntoViewIfNeeded(); await switches.nth(1).click();
  await editor.getByLabel("密码（留空保持原值）", { exact: true }).fill("http-password");
  await editor.getByRole("button", { name: "点击认证", exact: true }).click();
  await editor.getByRole("button", { name: "保存资源", exact: true }).click(); await confirm();
  await editor.waitFor({ state: "hidden" });
  assert.equal(mutations.at(-1).enableCoredumpMonitor, true, "Coredump 开关必须通过资源 PATCH 保存");
  assert.equal(mutations.at(-1).enableResourceMonitor, true, "资源监控开关必须通过资源 PATCH 保存");
  await page.getByRole("tab", { name: "采集任务", exact: true }).click();
  await page.getByRole("row").filter({ hasText: task.name }).getByRole("button", { name: "编辑任务" }).click();
  const taskEditor = page.getByRole("dialog", { name: /模拟采集任务/ }); await taskEditor.waitFor();
  assert.equal(await taskEditor.getByText("Coredump 监控", { exact: true }).count(), 0, "任务编辑器不得保留 Coredump 配置");
  await taskEditor.getByRole("button", { name: "关闭", exact: true }).click();
  for (const width of [1440, 390]) {
    await page.setViewportSize({ width, height: 844 }); await page.getByRole("tab", { name: "设备资源", exact: true }).click();
    await page.getByRole("button", { name: "编辑资源", exact: true }).click();
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth), false, `${width}px 资源编辑器不得横向溢出`);
    await page.screenshot({ path: `output/playwright/resource-coredump-monitor-${width}.png`, fullPage: true }); await page.getByRole("button", { name: "关闭", exact: true }).click();
  }
  assert.deepEqual(errors, []); console.log(JSON.stringify({ passed: true, screenshots: "output/playwright/resource-coredump-monitor-{1440,390}.png" }));
} finally { await context.close(); await browser.close(); }
