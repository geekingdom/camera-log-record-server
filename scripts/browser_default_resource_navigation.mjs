// 工作区导航回归：用隔离Vite和模拟接口验证默认资源页、哈希恢复、权限回退与资源分页刷新。
import assert from "node:assert/strict";
import { mkdir, mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { createServer } from "../frontend/node_modules/vite/dist/node/index.js";

const imported = await import(process.env.PLAYWRIGHT_MODULE || "playwright");
const { chromium } = imported.default || imported;
const screenshots = process.env.BROWSER_SCREENSHOTS || "output/playwright";
const resources = Array.from({ length: 41 }, (_, index) => ({
  id: `resource-${index + 1}`, name: `分页资源 ${index + 1}`, kind: "SERIAL_SERVER",
  ip: `192.0.2.${index + 1}`, version: 1,
}));
const administrator = {
  id: "navigation-admin", username: "navigation-admin", displayName: "导航管理员",
  isAdmin: true, scopes: ["*"], enabled: true, mustChangePassword: false, version: 1,
};
const operator = {
  id: "navigation-operator", username: "navigation-operator", displayName: "导航操作员",
  isAdmin: false, scopes: ["tasks:read"], enabled: true, mustChangePassword: false, version: 1,
};
let cacheDir;
let server;
let browser;
const errors = [];
const resourcePages = [];

async function json(route, body) {
  await route.fulfill({ contentType: "application/json", body: JSON.stringify(body) });
}

function pageOf(items, page = 1, pageSize = 20) {
  return { items, total: items.length, page, pageSize };
}

try {
  await mkdir(screenshots, { recursive: true });
  if (!process.env.BASE_URL) {
    cacheDir = await mkdtemp(join(tmpdir(), "camera-vite-navigation-"));
    server = await createServer({
      root: resolve("frontend"), cacheDir, logLevel: "warn",
      server: { host: "127.0.0.1", port: 15191, strictPort: false, open: false },
    });
    await server.listen();
  }
  browser = await chromium.launch({ headless: true, channel: "chrome" });
  const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
  await context.addInitScript(() => {
    if (!sessionStorage.getItem("camera-log-record-token"))
      sessionStorage.setItem("camera-log-record-token", "navigation-admin");
  });
  await context.route("**/api/v1/**", async route => {
    const request = route.request();
    const url = new URL(request.url());
    const path = url.pathname;
    const account = request.headers().authorization?.endsWith("navigation-operator") ? operator : administrator;
    if (request.method() !== "GET") throw new Error(`不应发出写请求：${request.method()} ${path}`);
    if (path === "/api/v1/auth/me") return json(route, { user: account });
    if (path === "/api/v1/resources") {
      const page = Number(url.searchParams.get("page") || "1");
      const pageSize = Number(url.searchParams.get("pageSize") || "20");
      resourcePages.push({ account: account.id, page });
      return json(route, { ...pageOf(resources.slice((page - 1) * pageSize, page * pageSize), page, pageSize), total: resources.length });
    }
    if (path === "/api/v1/tasks") return json(route, pageOf([]));
    if (["/api/v1/command-templates", "/api/v1/nodes", "/api/v1/admin/nodes"].includes(path)) return json(route, pageOf([]));
    if (path === "/api/v1/platform-settings") return json(route, { retentionDays: 7, version: 1 });
    throw new Error(`未模拟的读取接口：${path}`);
  });
  const page = await context.newPage();
  page.setDefaultTimeout(10_000);
  page.on("pageerror", error => errors.push(error.message));
  page.on("console", message => { if (message.type() === "error") errors.push(message.text()); });
  const baseUrl = process.env.BASE_URL || `http://127.0.0.1:${server.httpServer.address().port}`;

  await page.goto(baseUrl, { waitUntil: "networkidle" });
  await page.getByRole("heading", { name: "设备资源", exact: true }).waitFor();
  assert.equal(new URL(page.url()).hash, "#resources", "首次登录必须进入设备资源页");
  await page.screenshot({ path: `${screenshots}/navigation-desktop.png`, fullPage: true, animations: "disabled" });

  await page.getByRole("tab", { name: "采集任务", exact: true }).click();
  await page.getByRole("heading", { name: "采集任务", exact: true }).waitFor();
  assert.equal(new URL(page.url()).hash, "#tasks");
  await page.reload({ waitUntil: "networkidle" });
  await page.getByRole("heading", { name: "采集任务", exact: true }).waitFor();

  await page.getByRole("tab", { name: "设备资源", exact: true }).click();
  await page.getByText("分页资源 1", { exact: true }).waitFor();
  await page.locator(".el-pagination .btn-next:not([disabled])").click();
  await page.getByText("分页资源 21", { exact: true }).waitFor();
  const refreshRequest = page.waitForRequest(request => {
    const url = new URL(request.url());
    return url.pathname === "/api/v1/resources" && url.searchParams.get("page") === "2";
  });
  await page.getByRole("button", { name: "刷新列表", exact: true }).click();
  await refreshRequest;
  assert.equal(resourcePages.at(-1)?.page, 2, "资源页刷新必须继续请求当前第2页");

  await page.setViewportSize({ width: 390, height: 844 });
  await page.waitForTimeout(100);
  const layout = await page.evaluate(() => ({ scrollWidth: document.documentElement.scrollWidth, clientWidth: document.documentElement.clientWidth }));
  assert.ok(layout.scrollWidth <= layout.clientWidth + 1, `手机资源页不得横向溢出：${JSON.stringify(layout)}`);
  await page.screenshot({ path: `${screenshots}/navigation-mobile.png`, fullPage: true, animations: "disabled" });

  await page.evaluate(() => sessionStorage.setItem("camera-log-record-token", "navigation-operator"));
  await page.goto(`${baseUrl}/?session=operator#settings`, { waitUntil: "networkidle" });
  await page.getByRole("heading", { name: "设备资源", exact: true }).waitFor();
  assert.equal(new URL(page.url()).hash, "#resources", "无权限页必须优先回退设备资源");
  assert.deepEqual(errors, []);
  console.log(JSON.stringify({ passed: true, resourceRefreshPage: 2, screenshots, consoleErrors: 0 }));
} finally {
  if (browser) await browser.close();
  if (server) await server.close();
  if (cacheDir) await rm(cacheDir, { recursive: true, force: true });
}
