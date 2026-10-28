// 请求排障入口与响应字段诊断的生产构建验收，仅使用合成API。
import assert from "node:assert/strict";
import { mkdir } from "node:fs/promises";
const imported = await import(process.env.PLAYWRIGHT_MODULE || "playwright");
const { chromium } = imported.default || imported;
const browser = await chromium.launch({ headless: true, channel: "chrome" });
const errors = [];
try {
  for (const width of [1440, 390, 320]) {
    const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
    await context.addInitScript(() => sessionStorage.setItem("camera-log-record-token", "mock-token"));
    await context.route("**/api/v1/**", async route => {
      const url = new URL(route.request().url());
      if (url.pathname === "/api/v1/auth/me") return route.fulfill({ json: { user: {
        id: "admin", username: "admin", displayName: "模拟管理员", isAdmin: true, scopes: ["*"], mustChangePassword: false,
      } } });
      const items = url.pathname === "/api/v1/request-events" ? [{
        id: "diagnostic", createdAt: new Date().toISOString(), summary: "查询小时归档失败", level: "ERROR", outcome: "FAILED",
        requestId: "diagnostic-request", method: "GET", route: "/api/v1/tasks/{task_id}/log-hours", httpStatus: 500,
        reason: "服务内部异常", errorType: "ResponseValidationError",
        responseValidation: { count: 1, truncated: false, issues: [{ location: ["response", "items", "0", "files", "0", "[未声明字段或动态键]"], type: "extra_forbidden" }] },
      }] : url.pathname === "/api/v1/audit-events" ? [{
        id: "login-failed", createdAt: new Date().toISOString(), summary: "用户登录失败", level: "ERROR", outcome: "FAILED",
        actor: "anonymous", action: "login_failed", sourceKind: "ANONYMOUS", sourceName: "未认证身份",
        metadata: { username: "audit.operator" },
      }] : [];
      return route.fulfill({ json: { items, total: items.length, page: 1, pageSize: 20, hasMore: false, nextCursor: null } });
    });
    const page = await context.newPage();
    page.on("pageerror", error => errors.push(error.message));
    await page.goto(process.env.BASE_URL || "http://127.0.0.1:5173");
    await page.getByRole("tab", { name: "审计与事件", exact: true }).click();
    await page.getByRole("tab", { name: "请求记录", exact: true }).click();
    await page.setViewportSize({ width, height: 1000 });
    await page.getByLabel("按请求编号筛选", { exact: true }).fill("diagnostic-request");
    await page.getByLabel("按请求编号筛选", { exact: true }).press("Enter");
    await page.getByText("查询小时归档失败", { exact: true }).click();
    await page.getByRole("heading", { name: "响应字段校验" }).waitFor();
    assert.match(await page.locator(".audit-event-drawer").innerText(), /extra_forbidden/);
    assert.match(await page.locator(".audit-event-drawer").innerText(), /未声明字段或动态键/);
    await page.locator(".audit-event-drawer .el-drawer__close-btn").click();
    await page.getByRole("tab", { name: "审计记录", exact: true }).click();
    await page.getByText("用户登录失败", { exact: true }).click();
    const auditDrawer = page.locator(".audit-event-drawer");
    await auditDrawer.getByText("填写用户名", { exact: true }).waitFor();
    assert.match(await auditDrawer.innerText(), /填写用户名\s*audit\.operator/);
    assert.match(await auditDrawer.innerText(), /anonymous/);
    assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1));
    await mkdir("output/playwright", { recursive: true });
    // 等待抽屉过渡进入视口，截图必须包含诊断正文而非动画起始帧。
    await page.waitForFunction(() => {
      const drawer = document.querySelector('.audit-event-drawer');
      if (!drawer) return false;
      const bounds = drawer.getBoundingClientRect();
      return bounds.left >= 0 && bounds.right <= innerWidth + 1;
    });
    await page.screenshot({ path: `output/playwright/request-diagnostics-${width}.png`, fullPage: true });
    await context.close();
  }
  assert.deepEqual(errors, []);
  console.log("请求记录入口与安全响应诊断：1440/390/320通过");
} finally {
  await browser.close();
}
