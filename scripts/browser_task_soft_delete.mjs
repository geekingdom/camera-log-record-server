// 软删除界面验收只使用模拟API，确认默认隐藏、历史筛选、创建人和保留日志提示。
import assert from "node:assert/strict";
import { mkdir } from "node:fs/promises";
const imported = await import(process.env.PLAYWRIGHT_MODULE || "playwright");
const { chromium } = imported.default || imported;
const browser = await chromium.launch({ headless: true, channel: "chrome" });
try {
  await mkdir("output/playwright", { recursive: true });
  for (const width of [1440, 390]) {
    const context = await browser.newContext({ viewport: { width, height: 1000 } });
    let deleted = false, writes = 0;
    const queries = [];
    const task = { id: "task-soft", name: "保留历史任务", createdBy: "admin", createdByName: "采集管理员",
      version: 7, protocol: "SSH", ip: "192.0.2.1", port: 22, status: "COLLECTING", desiredState: "RUNNING",
      resourceId: "resource", initialCommands: [], scheduledCommands: [] };
    await context.route("**/api/v1/**", async route => {
      const request = route.request(), url = new URL(request.url());
      if (url.pathname === "/api/v1/auth/me") return route.fulfill({ json: { user: {
        id: "admin", isAdmin: true, scopes: ["*"], username: "admin", displayName: "采集管理员", mustChangePassword: false,
      } } });
      if (url.pathname === "/api/v1/tasks/task-soft" && request.method() === "DELETE") {
        assert.equal(url.searchParams.get("version"), "7");
        writes++; deleted = true;
        return route.fulfill({ status: 202, json: { ...task, deletedAt: "2026-10-20T10:00:00+08:00", desiredState: "STOPPED" } });
      }
      if (url.pathname === "/api/v1/tasks") {
        queries.push(url.searchParams.get("includeDeleted"));
        const items = !deleted ? [task] : url.searchParams.get("includeDeleted") === "true"
          ? [{ ...task, deletedAt: "2026-10-20T10:00:00+08:00", desiredState: "STOPPED" }] : [];
        return route.fulfill({ json: { items, total: items.length, page: 1, pageSize: 20 } });
      }
      assert.equal(request.method(), "GET", "只有指定任务可执行删除请求");
      return route.fulfill({ json: { items: [], total: 0, page: 1, pageSize: 20 } });
    });
    const page = await context.newPage(), errors = [];
    page.on("pageerror", error => errors.push(error.message));
    await page.goto(`${process.env.BASE_URL || "http://127.0.0.1:5179"}/#tasks`);
    const row = () => page.locator(".el-table__body tr").filter({ hasText: "保留历史任务" });
    await row().waitFor();
    await page.getByRole("columnheader", { name: "创建人", exact: true }).waitFor();
    assert.match(await row().textContent(), /采集管理员/);
    await row().getByRole("button", { name: "删除采集任务", exact: true }).click();
    const confirm = page.getByRole("dialog", { name: "确认删除采集任务" });
    await confirm.waitFor();
    assert.match(await confirm.textContent(), /下载链接不因删除任务而失效/);
    await confirm.getByRole("button", { name: "取消", exact: true }).click();
    assert.equal(writes, 0);
    await row().getByRole("button", { name: "删除采集任务", exact: true }).click();
    await confirm.getByRole("button", { name: "确认", exact: true }).click();
    await row().waitFor({ state: "detached" });
    assert.equal(writes, 1);
    await page.locator("label.el-checkbox").filter({ hasText: "包含已删除任务" }).click();
    await row().waitFor();
    assert.match(await row().textContent(), /已删除/);
    for (const name of ["删除采集任务", "编辑任务", "启动任务", "停止任务", "继续任务"]) {
      assert.equal(await row().getByRole("button", { name, exact: true }).count(), 0);
    }
    assert.ok(queries.includes("true"));
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
    await page.screenshot({ path: `output/playwright/task-soft-delete-${width}.png`, fullPage: true });
    assert.deepEqual(errors, []);
    await context.close();
  }
  console.log(JSON.stringify({ passed: true, viewports: [1440, 390], softDeleteAndCreator: true }));
} finally { await browser.close(); }
