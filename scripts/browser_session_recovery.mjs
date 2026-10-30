// 会话恢复浏览器验收：慢身份与慢初始数据不得展示可提交登录；暂态失败必须可重试。
import assert from "node:assert/strict";
import { mkdir } from "node:fs/promises";
import { dashboardFixture } from "./dashboard_fixture.mjs";

const { chromium } = (await import(process.env.PLAYWRIGHT_MODULE || "playwright")).default || await import(process.env.PLAYWRIGHT_MODULE || "playwright");
const baseUrl = process.env.BASE_URL || "http://127.0.0.1:5173";
const output = "output/playwright";
const user = { id: "recovery", username: "recovery", displayName: "恢复验收", isAdmin: true, enabled: true, mustChangePassword: false, scopes: ["*"] };
const browser = await chromium.launch({ headless: true, channel: "chrome" });
try {
  await mkdir(output, { recursive: true });
  for (const width of [1440, 390]) {
    let authCalls = 0, releaseTasks;
    const tasksBlocked = new Promise(resolve => { releaseTasks = resolve; });
    const context = await browser.newContext({ viewport: { width, height: 900 } });
    await context.route("**/api/v1/**", async route => {
      const request = route.request(), path = new URL(request.url()).pathname;
      const json = body => route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(body) });
      if (path === "/api/v1/auth/me") {
        authCalls += 1;
        if (authCalls === 1) { await new Promise(resolve => setTimeout(resolve, 500)); return json({ user }); }
        if (authCalls === 2) return route.fulfill({ status: 503, contentType: "application/json", body: JSON.stringify({ message: "临时网络故障" }) });
        return json({ user });
      }
      if (path === "/api/v1/tasks") { await tasksBlocked; return json({ items: [], total: 0, page: 1, pageSize: 20 }); }
      if (["/api/v1/command-templates", "/api/v1/nodes"].includes(path)) return json({ items: [], total: 0, page: 1, pageSize: 20 });
      if (path === "/api/v1/dashboard") {
        const url = new URL(request.url());
        return json(dashboardFixture(url.searchParams.get("date") || undefined, Number(url.searchParams.get("days") || 7)));
      }
      if (path === "/api/v1/resources") return json({ items: [], total: 0, page: 1, pageSize: 20 });
      throw new Error(`未模拟：${request.method()} ${path}`);
    });
    const page = await context.newPage();
    await page.goto(`${baseUrl}/`, { waitUntil: "domcontentloaded" });
    const login = page.getByRole("button", { name: "登录", exact: true });
    await login.waitFor();
    assert.equal(await login.isDisabled(), true, `${width}px 慢auth/me时登录不得可提交`);
    await page.getByText("正在恢复登录会话…", { exact: true }).waitFor();
    await page.locator(".shell.is-authenticated").waitFor();
    assert.equal(await login.count(), 0, `${width}px auth/me成功后不得在慢初始加载期间显示登录表单`);
    await page.screenshot({ path: `${output}/session-recovery-${width}-loading.png`, fullPage: true });
    releaseTasks();
    await page.waitForTimeout(80);
    await page.reload({ waitUntil: "domcontentloaded" });
    await page.getByText(/会话恢复失败：临时网络故障/).waitFor();
    assert.equal(await login.isDisabled(), false, `${width}px 恢复失败后允许明确登录`);
    await page.getByRole("button", { name: "重试恢复会话", exact: true }).click();
    await page.locator(".shell.is-authenticated").waitFor();
    await page.screenshot({ path: `${output}/session-recovery-${width}-retry.png`, fullPage: true });
    await context.close();
  }
  console.log(JSON.stringify({ passed: true, screenshots: ["session-recovery-1440-loading.png", "session-recovery-390-loading.png", "session-recovery-1440-retry.png", "session-recovery-390-retry.png"] }));
} finally { await browser.close(); }
