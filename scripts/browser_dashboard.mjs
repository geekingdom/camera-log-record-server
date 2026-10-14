// 看板浏览器验收只模拟API：默认导航、统计口径、刷新失败保留快照与窄屏均不连接真实设备。
import assert from "node:assert/strict";
import { mkdir } from "node:fs/promises";
import { dashboardFixture } from "./dashboard_fixture.mjs";
const imported = await import(process.env.PLAYWRIGHT_MODULE || "playwright");
const { chromium } = imported.default || imported;
const browser = await chromium.launch({ headless: true, channel: "chrome" });
const context = await browser.newContext({ viewport: { width: 1440, height: 1080 } });
let fail = false;
let empty = false;
let restricted = false;
let holdSeven = false;
let releaseSeven;
let requests = 0;
const errors = [];
await context.route("**/api/v1/**", async route => {
  const url = new URL(route.request().url());
  const send = (body, status = 200) => route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });
  if (url.pathname === "/api/v1/auth/me") return send({ user: { id: "dashboard-admin", username: "dashboard-admin", displayName: "看板验收", scopes: restricted ? ["tasks:read"] : ["*"], isAdmin: !restricted, enabled: true, mustChangePassword: false } });
  if (url.pathname === "/api/v1/dashboard") {
    requests++;
    if (fail) return send({ error: { message: "模拟统计服务暂不可用" } }, 503);
    const data = dashboardFixture(url.searchParams.get("date") || undefined, Number(url.searchParams.get("days") || 7), !restricted, !restricted);
    if (holdSeven && data.days === 7) await new Promise(resolve => { releaseSeven = resolve; });
    if (empty) {
      data.overview = { resources: 0, onlineResources: 0, collectingTasks: 0, tasks: 0, nodes: 0 };
      data.daily.forEach(item => { item.bytes = 0; item.fileCount = 0; });
      data.topResources = []; data.coredumps = { resourceCount: 0, fileCount: 0, bytes: 0, truncated: false, items: [] }; data.nodes = [];
    }
    return send(data);
  }
  if (url.pathname === "/api/v1/platform-settings") return send({ retentionDays: 7, version: 1 });
  if (["/api/v1/tasks", "/api/v1/resources", "/api/v1/command-templates", "/api/v1/nodes"].includes(url.pathname)) return send({ items: [], total: 0, page: 1, pageSize: 20 });
  throw new Error(`未模拟接口 ${url.pathname}`);
});
const page = await context.newPage(); page.setDefaultTimeout(12000);
page.on("pageerror", error => errors.push(error.message));
async function assertHeaderClear() {
  await page.evaluate(() => window.scrollTo(0, 0));
  const header = await page.locator('.topbar').boundingBox();
  const dashboard = await page.locator('.dashboard').boundingBox();
  assert.ok(header && dashboard && dashboard.y >= header.y + header.height - 1, '看板不得覆盖顶部面包屑');
}
try {
  await mkdir("output/playwright", { recursive: true });
  await page.goto(process.env.BASE_URL || "http://127.0.0.1:5175", { waitUntil: "networkidle" });
  await page.getByRole("heading", { name: "资源看板", exact: true }).waitFor();
  assert.equal(new URL(page.url()).hash, "#dashboard");
  assert.equal(await page.getByRole("tab").first().getAttribute("aria-label"), "资源看板");
  await page.getByText("128", { exact: true }).waitFor();
  await page.locator(".dashboard-chart canvas").waitFor();
  await page.getByText("采集节点 A", { exact: true }).waitFor();
  await page.getByRole("button", { name: "暂停自动刷新", exact: true }).click();
  await Promise.all([page.waitForResponse(response => response.url().includes("/api/v1/dashboard?") && new URL(response.url()).searchParams.get("days") === "14"), page.getByText("14 天", { exact: true }).click()]);
  await page.waitForLoadState("networkidle");
  assert.ok(requests >= 2);
  await assertHeaderClear();
  await page.screenshot({ path: "output/playwright/dashboard-1440.png", fullPage: true, animations: "disabled" });
  for (const width of [768, 390, 320]) {
    await page.setViewportSize({ width, height: 900 });
    await page.waitForTimeout(180);
    await assertHeaderClear();
    assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth + 1), `${width}看板横向溢出`);
    await page.screenshot({ path: `output/playwright/dashboard-${width}.png`, fullPage: true, animations: "disabled" });
  }
  fail = true;
  await page.getByRole("button", { name: "刷新资源看板" }).click();
  await page.getByText(/数据可能过期/).waitFor();
  assert.equal(await page.getByText("128", { exact: true }).count(), 1, "刷新失败须保留原统计");
  fail = false; empty = true;
  await page.getByRole("button", { name: "刷新资源看板" }).click();
  await page.getByText("该日暂无设备日志", { exact: true }).waitFor();
  empty = false;
  await page.setViewportSize({ width: 1440, height: 1080 });
  holdSeven = true;
  await Promise.all([page.waitForRequest(request => request.url().includes("/api/v1/dashboard?") && new URL(request.url()).searchParams.get("days") === "7"), page.getByText("7 天", { exact: true }).click()]);
  await page.getByText("30 天", { exact: true }).click();
  await page.getByText("30 日窗口", { exact: true }).waitFor();
  holdSeven = false;
  releaseSeven();
  await page.waitForLoadState("networkidle");
  assert.equal(await page.getByText("30 日窗口", { exact: true }).count(), 1, "较早响应不可覆盖最新窗口");
  restricted = true;
  await page.reload({ waitUntil: "networkidle" });
  await page.getByText("当前账号无节点健康详情权限", { exact: true }).waitFor();
  assert.equal(await page.getByText("当前账号无日志统计权限", { exact: true }).count(), 3);
  assert.equal(await page.getByText("采集节点 A", { exact: true }).count(), 0);
  await assertHeaderClear();
  assert.deepEqual(errors, []);
  console.log(JSON.stringify({ passed: true, requests, widths: [1440, 768, 390, 320], stalePreserved: true, emptyState: true }));
} finally { await browser.close(); }
