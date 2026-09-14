// SSH 主从任务编辑器浏览器验收：页面 API 全部模拟，不读取或修改真实任务与设备。
import assert from "node:assert/strict";
import { mkdir } from "node:fs/promises";

const imported = await import(process.env.PLAYWRIGHT_MODULE || "playwright");
const { chromium } = imported.default || imported;
const browser = await chromium.launch({ headless: true, channel: "chrome" });
const context = await browser.newContext({ viewport: { width: 1440, height: 900 } });
const baseUrl = process.env.BASE_URL || "http://127.0.0.1:5173";
const resource = {
  id: "ssh-resource", name: "模拟 SSH 设备", kind: "HIKVISION_NETWORK", ip: "192.0.2.30",
  model: "DS-2CD", subSerialNumber: "SSH-EXAMPLE", authenticatedAt: "2026-09-14T08:00:00Z", version: 1,
};
const task = {
  id: "ssh-host-task", name: "模拟主机日志", description: "隔离浏览器验收", protocol: "SSH", sshTarget: "HOST",
  ip: resource.ip, port: 22, username: "operator", resourceId: resource.id, initialCommands: [], scheduledCommands: [],
  status: "STOPPED", desiredState: "STOPPED", createdBy: "operator", createdByName: "模拟操作员", version: 1,
};
const updateBodies = [];

async function json(route, body, status = 200) {
  await route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });
}

await context.route("**/api/v1/**", async route => {
  const request = route.request();
  const path = new URL(request.url()).pathname;
  if (request.method() === "GET" && path === "/api/v1/auth/me") {
    return json(route, { user: {
      id: "operator", username: "operator", displayName: "模拟操作员", isAdmin: false, enabled: true,
      mustChangePassword: false, scopes: ["tasks:read", "tasks:write", "tasks:create", "resources:create", "logs:read", "templates:read"],
    } });
  }
  if (request.method() === "GET" && path === "/api/v1/tasks")
    return json(route, { items: [task], total: 1, page: 1, pageSize: 20 });
  if (request.method() === "GET" && path === `/api/v1/tasks/${task.id}`) return json(route, task);
  if (request.method() === "PATCH" && path === `/api/v1/tasks/${task.id}`) {
    updateBodies.push(request.postDataJSON());
    return json(route, { ...task, ...updateBodies.at(-1), version: 2 });
  }
  if (request.method() === "GET" && path === "/api/v1/resources")
    return json(route, { items: [resource], total: 1, page: 1, pageSize: 20 });
  if (request.method() === "GET" && path === "/api/v1/command-templates")
    return json(route, { items: [], total: 0, page: 1, pageSize: 20 });
  if (request.method() === "GET" && path === "/api/v1/platform-settings") return json(route, { retentionDays: 7, version: 1 });
  throw new Error(`未模拟的 API 请求：${request.method()} ${path}`);
});

const page = await context.newPage();
page.setDefaultTimeout(12_000);
const errors = [];
page.on("pageerror", error => errors.push(error.message));
page.on("console", message => { if (message.type() === "error") errors.push(message.text()); });

try {
  await mkdir("output/playwright", { recursive: true });
  await page.goto(baseUrl, { waitUntil: "networkidle" });
  await page.getByRole("tab", { name: "采集任务", exact: true }).click();
  await page.getByRole("button", { name: "编辑任务", exact: true }).click();
  await page.getByRole("heading", { name: "基本连接", exact: true }).waitFor();

  const targetField = page.locator(".el-form-item").filter({ hasText: "日志采集任务类型" });
  assert.match(await targetField.innerText(), /主机/, "编辑既有主机任务必须保留 HOST 选择");
  await targetField.locator(".el-select").click();
  for (const label of ["主机", "从机 1", "从机 2", "从机 3"])
    await page.getByText(label, { exact: true }).last().waitFor();
  await page.getByText("从机 2", { exact: true }).last().click();
  await page.waitForTimeout(100);
  assert.match(await targetField.innerText(), /从机 2/, "选择器必须把从机值写回编辑表单");

  for (const width of [1440, 390]) {
    await page.setViewportSize({ width, height: 844 });
    await page.waitForTimeout(200);
    const metrics = await page.evaluate(() => ({ scrollWidth: document.documentElement.scrollWidth, clientWidth: window.innerWidth }));
    assert.ok(metrics.scrollWidth <= metrics.clientWidth + 1, `${width}px 编辑器不得横向溢出：${JSON.stringify(metrics)}`);
    await page.screenshot({ path: `output/playwright/ssh-target-editor-${width}.png`, fullPage: true, animations: "disabled" });
  }
  assert.deepEqual(updateBodies, [], "下拉验收不得提交或修改模拟任务");
  assert.deepEqual(errors, []);
  console.log(JSON.stringify({ passed: true, screenshots: "output/playwright/ssh-target-editor-{1440,390}.png", apiWrites: 0 }));
} finally {
  await context.close();
  await browser.close();
}
