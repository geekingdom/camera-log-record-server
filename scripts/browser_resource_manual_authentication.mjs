// 资源行即时认证浏览器验收：全部 API 路由模拟，不读取真实资源或设备凭据。
import assert from "node:assert/strict";
import { mkdir } from "node:fs/promises";

const imported = await import(process.env.PLAYWRIGHT_MODULE || "playwright");
const { chromium } = imported.default || imported;
const browser = await chromium.launch({ headless: true, channel: "chrome" });
const context = await browser.newContext({ viewport: { width: 1440, height: 900 } });
const baseUrl = process.env.BASE_URL || "http://127.0.0.1:5173";
let authCalls = 0;
let failedOnce = false;
let allowTaskControl = true;
const resources = [
  { id: "owned-network", name: "可认证海康", kind: "HIKVISION_NETWORK", ip: "192.0.2.41", createdBy: "operator", createdByName: "模拟操作员", healthStatus: "OFFLINE", version: 1 },
  { id: "foreign-network", name: "他人海康", kind: "HIKVISION_NETWORK", ip: "192.0.2.42", createdBy: "other", createdByName: "其他用户", healthStatus: "ONLINE", version: 1 },
  { id: "deleted-network", name: "已删除海康", kind: "HIKVISION_NETWORK", ip: "192.0.2.43", createdBy: "operator", deletedAt: "2026-09-14T08:00:00Z", healthStatus: "OFFLINE", version: 1 },
  { id: "serial", name: "串口服务器", kind: "SERIAL_SERVER", ip: "192.0.2.44", createdBy: "operator", version: 1 },
];

async function json(route, body, status = 200) {
  await route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });
}
await context.route("**/api/v1/**", async route => {
  const request = route.request(); const path = new URL(request.url()).pathname;
  if (request.method() === "GET" && path === "/api/v1/auth/me") return json(route, { user: {
    id: "operator", username: "operator", displayName: "模拟操作员", isAdmin: false, enabled: true, mustChangePassword: false,
    scopes: ["tasks:read", "resources:write", "resources:create", "logs:read", "templates:read", ...(allowTaskControl ? ["tasks:control"] : [])],
  } });
  if (request.method() === "GET" && path === "/api/v1/resources") return json(route, { items: resources, total: resources.length, page: 1, pageSize: 20 });
  if (request.method() === "POST" && path === "/api/v1/resources/owned-network/authenticate") {
    assert.equal(request.postData(), null, "资源行认证不得发送或读取明文凭据");
    authCalls += 1;
    if (failedOnce) {
      resources[0].healthStatus = "OFFLINE";
      return json(route, { error: { message: "模拟设备离线" } }, 503);
    }
    resources[0].healthStatus = "ONLINE";
    return json(route, { model: "DS-2CD", subSerialNumber: "AUTH-41", softwareVersion: "V5.8" });
  }
  if (request.method() === "GET" && ["/api/v1/tasks", "/api/v1/command-templates", "/api/v1/nodes", "/api/v1/users/creators"].includes(path))
    return json(route, { items: [], total: 0, page: 1, pageSize: 20 });
  if (request.method() === "GET" && path === "/api/v1/platform-settings") return json(route, { retentionDays: 7, version: 1 });
  throw new Error(`未模拟接口：${request.method()} ${path}`);
});

const page = await context.newPage();
page.setDefaultTimeout(12_000);
const errors = [];
page.on("pageerror", error => errors.push(error.message));
page.on("console", message => {
  // 503 是本脚本主动注入并已断言呈现错误提示的分支，其余控制台错误仍须失败。
  if (message.type() === "error" && !message.text().includes("status of 503")) errors.push(message.text());
});
try {
  await mkdir("output/playwright", { recursive: true });
  await page.goto(baseUrl, { waitUntil: "networkidle" });
  const owned = page.getByRole("row").filter({ hasText: "可认证海康" });
  const foreign = page.getByRole("row").filter({ hasText: "他人海康" });
  const deleted = page.getByRole("row").filter({ hasText: "已删除海康" });
  const serial = page.getByRole("row").filter({ hasText: "串口服务器" });
  await owned.getByRole("button", { name: "立即认证设备" }).waitFor();
  assert.equal(await foreign.getByRole("button", { name: "立即认证设备" }).count(), 0, "非所有者不得触发认证");
  assert.equal(await deleted.getByRole("button", { name: "立即认证设备" }).count(), 0, "已删除资源不得触发认证");
  assert.equal(await serial.getByRole("button", { name: "立即认证设备" }).count(), 0, "串口资源不得触发认证");

  await owned.getByRole("button", { name: "立即认证设备" }).click();
  const dialog = page.getByRole("dialog", { name: "确认设备认证", exact: true });
  await dialog.getByText("已保存的 HTTP 凭据", { exact: false }).waitFor();
  await dialog.getByText("失败可能停止关联采集任务", { exact: false }).waitFor();
  await dialog.getByRole("button", { name: "确认", exact: true }).click();
  await owned.getByText("当前连通", { exact: true }).waitFor();
  await page.getByText("设备资源“可认证海康”认证成功", { exact: true }).waitFor();
  assert.equal(authCalls, 1, "确认后只发送一次空请求认证");

  failedOnce = true;
  await owned.getByRole("button", { name: "立即认证设备" }).click();
  await page.getByRole("dialog", { name: "确认设备认证", exact: true }).getByRole("button", { name: "确认", exact: true }).click();
  await page.getByText("模拟设备离线", { exact: true }).waitFor();
  await owned.getByText("设备离线", { exact: true }).waitFor();
  assert.equal(authCalls, 2, "失败提示后仍只保留一次请求，没有重试风暴");

  allowTaskControl = false;
  const readOnlyControlPage = await context.newPage();
  await readOnlyControlPage.goto(baseUrl, { waitUntil: "networkidle" });
  const readOnlyOwned = readOnlyControlPage.getByRole("row").filter({ hasText: "可认证海康" });
  assert.equal(await readOnlyOwned.getByRole("button", { name: "立即认证设备" }).count(), 0,
    "缺少任务控制权限时不得展示可停止关联任务的认证入口");
  await readOnlyControlPage.close();
  allowTaskControl = true;
  for (const width of [1440, 390]) {
    await page.setViewportSize({ width, height: 844 });
    await page.waitForTimeout(150);
    // 操作列在资源宽表的末端；截图移至该列，直接保留认证入口的视觉证据。
    await page.locator(".resource-table .el-scrollbar__wrap").evaluate(element => { element.scrollLeft = element.scrollWidth; });
    await owned.getByRole("button", { name: "立即认证设备" }).hover();
    await page.getByText("立即认证设备", { exact: true }).last().waitFor();
    const geometry = await page.evaluate(() => ({ scrollWidth: document.documentElement.scrollWidth, clientWidth: window.innerWidth }));
    assert.ok(geometry.scrollWidth <= geometry.clientWidth + 1, `${width}px 不得横向溢出：${JSON.stringify(geometry)}`);
    await page.screenshot({ path: `output/playwright/resource-manual-authentication-${width}.png`, fullPage: true, animations: "disabled" });
  }
  assert.deepEqual(errors, []);
  console.log(JSON.stringify({ passed: true, authCalls, screenshots: "output/playwright/resource-manual-authentication-{1440,390}.png" }));
} finally {
  await context.close();
  await browser.close();
}
