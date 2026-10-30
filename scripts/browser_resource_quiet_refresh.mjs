// 资源列表慢刷新验收：静默轮询不遮罩，筛选变更中止旧读取且最终快照不可被旧响应覆盖。
import assert from "node:assert/strict";
import { mkdir } from "node:fs/promises";

const mod = await import(process.env.PLAYWRIGHT_MODULE || "playwright");
const { chromium } = mod.default || mod;
const baseUrl = process.env.BASE_URL || "http://127.0.0.1:5173";
const browser = await chromium.launch({ headless: true, channel: "chrome" });
const user = { id: "resource-admin", username: "resource-admin", displayName: "资源验收", isAdmin: true, enabled: true, mustChangePassword: false, scopes: ["*"] };
try {
  await mkdir("output/playwright", { recursive: true });
  for (const width of [1440, 390]) {
    let resourceReads = 0, quietStarted;
    const quietRequest = new Promise(resolve => { quietStarted = resolve; });
    const context = await browser.newContext({ viewport: { width, height: 900 } });
    await context.route("**/api/v1/**", async route => {
      const request = route.request(), url = new URL(request.url()), path = url.pathname;
      const json = body => route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(body) });
      if (path === "/api/v1/auth/me") return json({ user });
      if (["/api/v1/tasks", "/api/v1/command-templates", "/api/v1/nodes"].includes(path)) return json({ items: [], total: 0, page: 1, pageSize: 20 });
      if (path === "/api/v1/resources") {
        resourceReads += 1;
        const search = url.searchParams.get("search") || "";
        if (resourceReads === 2) { quietStarted(); await new Promise(resolve => setTimeout(resolve, 1400)); }
        if (search === "新筛选") { await new Promise(resolve => setTimeout(resolve, 60)); return json({ items: [{ id: "new", name: "新筛选设备", kind: "SERIAL_SERVER", ip: "192.0.2.88", version: 1 }], total: 1, page: 1, pageSize: 20 }); }
        return json({ items: [{ id: "old", name: "旧快照设备", kind: "SERIAL_SERVER", ip: "192.0.2.87", version: 1 }], total: 1, page: 1, pageSize: 20 });
      }
      if (path === "/api/v1/dashboard") return json({ totals: {}, taskStatus: [], recentLogs: [], resourceHealth: [] });
      throw new Error(`未模拟：${request.method()} ${path}`);
    });
    const page = await context.newPage();
    await page.goto(`${baseUrl}/#resources`, { waitUntil: "networkidle" });
    await page.getByText("旧快照设备", { exact: true }).waitFor();
    await quietRequest;
    assert.equal(await page.locator(".resource-table .el-loading-mask").count(), 0, `${width}px quiet刷新不得遮罩已有行`);
    await page.getByLabel("搜索资源", { exact: true }).fill("新筛选");
    await page.getByRole("button", { name: "筛选", exact: true }).click();
    await page.getByText("新筛选设备", { exact: true }).waitFor();
    await page.waitForTimeout(1500);
    assert.equal(await page.getByText("旧快照设备", { exact: true }).count(), 0, `${width}px 旧慢响应不得覆盖新筛选`);
    assert.ok(resourceReads <= 3, `${width}px 同一静默查询不得重复发起：${resourceReads}`);
    await page.screenshot({ path: `output/playwright/resource-quiet-refresh-${width}.png`, fullPage: true });
    await context.close();
  }
  console.log(JSON.stringify({ passed: true, screenshots: ["resource-quiet-refresh-1440.png", "resource-quiet-refresh-390.png"] }));
} finally { await browser.close(); }
