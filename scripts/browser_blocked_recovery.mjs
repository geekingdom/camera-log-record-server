// BLOCKED 任务浏览器验收：全部 API 由页面路由模拟，覆盖确认、隔离证据和移动端布局。
import assert from "node:assert/strict";
import { mkdir } from "node:fs/promises";

const imported = await import(process.env.PLAYWRIGHT_MODULE || "playwright");
const { chromium } = imported.default || imported;
const screenshots = process.env.BROWSER_SCREENSHOTS || "output/playwright";
const browser = await chromium.launch({ headless: true, channel: "chrome" });
const task = {
  id: "blocked-task", name: "等待隔离的采集", protocol: "SSH", ip: "192.0.2.70", port: 22,
  resourceId: "camera-resource", status: "BLOCKED", desiredState: "RUNNING", nodeId: "lost-worker",
  createdBy: "operator", createdByName: "操作员", initialCommands: [], scheduledCommands: [],
};
const requests = [];
let admin = false;

const pageOf = items => ({ items, total: items.length, page: 1, pageSize: 20 });
async function json(route, body, status = 200) {
  await route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });
}
function user() {
  return admin
    ? { id: "admin", username: "admin", displayName: "管理员", isAdmin: true, enabled: true, mustChangePassword: false, scopes: ["*"] }
    : { id: "operator", username: "operator", displayName: "操作员", isAdmin: false, enabled: true, mustChangePassword: false, scopes: ["tasks:read", "tasks:control"] };
}
async function assertViewport(page, width, label) {
  await page.setViewportSize({ width, height: 844 });
  await page.waitForTimeout(100);
  const layout = await page.evaluate(() => ({ scrollWidth: document.documentElement.scrollWidth, clientWidth: document.documentElement.clientWidth }));
  assert.ok(layout.scrollWidth <= layout.clientWidth + 1, `${label} 页面不得横向溢出`);
  const dialog = page.getByRole("dialog").last();
  const box = await dialog.boundingBox();
  assert.ok(box && box.x >= 0 && box.x + box.width <= width, `${label} 对话框必须完整可见`);
  await page.screenshot({ path: `${screenshots}/blocked-recovery-${width}.png`, fullPage: true, animations: "disabled" });
}

try {
  await mkdir(screenshots, { recursive: true });
  const context = await browser.newContext({ viewport: { width: 1440, height: 900 } });
  await context.addInitScript(() => sessionStorage.setItem("camera-log-record-token", "blocked-recovery-token"));
  await context.route("**/api/v1/**", async route => {
    const request = route.request();
    const url = new URL(request.url());
    const path = url.pathname;
    if (request.method() === "GET" && path === "/api/v1/auth/me") return json(route, { user: user() });
    if (request.method() === "GET" && path === "/api/v1/tasks") return json(route, pageOf([task]));
    if (request.method() === "GET" && path.startsWith("/api/v1/operations/")) return json(route, { id: path.split("/").at(-1), status: "PENDING" });
    if (request.method() === "GET" && path === "/api/v1/resources") return json(route, pageOf([
      { id: "camera-resource", name: "模拟相机", kind: "HIKVISION_NETWORK", ip: "192.0.2.70", version: 1 },
    ]));
    if (request.method() === "GET" && ["/api/v1/command-templates", "/api/v1/nodes", "/api/v1/users/creators"].includes(path)) return json(route, pageOf([]));
    if (request.method() === "POST" && path === "/api/v1/tasks/blocked-task/restart") {
      const body = request.postDataJSON();
      requests.push(body);
      if (!body.confirmIsolation && admin) {
        return json(route, { id: "restart-isolation", action: "restart-blocked", status: "PENDING", phase: "ISOLATION_REQUIRED" }, 202);
      }
      return json(route, { id: `restart-${requests.length}`, action: "restart-blocked", desiredState: "RUNNING", status: "PENDING" }, 202);
    }
    throw new Error(`未模拟的 API 请求：${request.method()} ${path}`);
  });
  const page = await context.newPage();
  page.setDefaultTimeout(10_000);
  const errors = [];
  page.on("pageerror", error => errors.push(error.message));
  page.on("console", message => {
    if (message.type() === "error" && !message.text().includes("status of 409")) errors.push(message.text());
  });
  const row = () => page.getByRole("row").filter({ hasText: task.name });
  const restart = () => row().getByLabel("重新启动任务");

  await page.goto(process.env.BASE_URL || "http://127.0.0.1:5173", { waitUntil: "networkidle" });
  await page.getByRole("tab", { name: "采集任务", exact: true }).click();
  await row().waitFor();
  assert.equal(await restart().count(), 1, "BLOCKED 任务必须显示重新启动");
  assert.equal(await row().getByLabel("停止任务").count(), 1, "BLOCKED 任务必须显示停止");

  await restart().click();
  const memberDialog = page.getByRole("dialog", { name: "确认重新启动任务" });
  await memberDialog.getByText("旧运行收尾后创建新的采集运行", { exact: false }).waitFor();
  await memberDialog.getByRole("button", { name: "取消", exact: true }).click();
  assert.deepEqual(requests, [], "取消二次确认不得发送重新启动请求");
  await restart().click();
  await memberDialog.getByRole("button", { name: "确认重新启动", exact: true }).click();
  await page.getByText("重新启动请求已提交", { exact: true }).waitFor();
  assert.deepEqual(requests, [{}], "普通用户确认后只提交常规重新启动请求");
  assert.equal(await page.getByText("隔离证据", { exact: true }).count(), 0, "普通用户不得看到隔离证据输入");

  admin = true;
  await page.reload({ waitUntil: "networkidle" });
  await page.getByRole("tab", { name: "采集任务", exact: true }).click();
  await row().waitFor();
  await restart().click();
  await page.getByRole("dialog", { name: "确认重新启动任务" }).getByRole("button", { name: "确认重新启动", exact: true }).click();
  const isolationDialog = page.getByRole("dialog", { name: "确认旧任务已隔离" });
  await isolationDialog.getByLabel("隔离证据", { exact: true }).fill("值班人员已关闭旧 worker 进程并确认会话断开");
  assert.deepEqual(requests, [{}, {}], "管理员先收到隔离要求，尚未提交隔离确认");
  await assertViewport(page, 1440, "管理员隔离确认");
  await assertViewport(page, 390, "管理员隔离确认");
  await isolationDialog.getByRole("button", { name: "确认隔离并重新启动", exact: true }).click();
  await page.getByText("隔离确认已提交，正在等待新运行采集", { exact: true }).waitFor();
  assert.deepEqual(requests.at(-1), { confirmIsolation: true, evidence: "值班人员已关闭旧 worker 进程并确认会话断开" });
  assert.deepEqual(errors, []);
  await context.close();
  console.log(JSON.stringify({ passed: true, restartRequests: requests.length, screenshots: `${screenshots}/blocked-recovery-{1440,390}.png`, consoleErrors: 0 }));
} finally {
  await browser.close();
}
