// 任务批量控制浏览器验收：所有 API 均由路由模拟，绝不连接真实设备或受控服务。
import assert from "node:assert/strict";
import { mkdir } from "node:fs/promises";

const imported = await import(process.env.PLAYWRIGHT_MODULE || "playwright");
const { chromium } = imported.default || imported;
const browser = await chromium.launch({ headless: true, channel: "chrome" });
const task = (id, name, status, desiredState, extra = {}) => ({
  id, name, protocol: "SSH", ip: `192.0.2.${id.length + 10}`, port: 22, status, desiredState,
  resourceId: "fixture-resource", version: 1, createdAt: "2026-09-09T10:00:00Z",
  initialCommands: [], scheduledCommands: [], ...extra,
});
const tasks = [
  task("stopped", "可启动任务", "STOPPED", "STOPPED", { createdBy: "admin", createdByName: "管理员" }),
  task("collecting", "可停止任务", "COLLECTING", "RUNNING", { createdBy: "admin", createdByName: "管理员" }),
  task("paused", "可继续任务", "PAUSED", "PAUSED", { createdBy: "admin", createdByName: "管理员" }),
  task("deleted", "资源已删除任务", "STOPPED", "STOPPED", { createdBy: "admin", resourceDeleted: true, resourceDeletedAt: "2026-09-09T11:00:00Z" }),
  task("other", "他人任务", "STOPPED", "STOPPED", { createdBy: "other", createdByName: "其他用户" }),
  task("mine", "本人任务", "STOPPED", "STOPPED", { createdBy: "user", createdByName: "普通用户" }),
];
const operations = [];
const errors = [];

async function newPage(isAdmin) {
  const context = await browser.newContext({ viewport: { width: 1440, height: 900 } });
  await context.addInitScript(() => sessionStorage.setItem("camera-log-record-token", "mock-token"));
  let taskReads = 0;
  await context.route("**/api/v1/**", async route => {
    const request = route.request();
    const url = new URL(request.url());
    const path = url.pathname;
    if (path === "/api/v1/auth/me") return route.fulfill({ json: { user: { id: isAdmin ? "admin" : "user", username: "fixture", displayName: "模拟用户", isAdmin, scopes: ["*"], mustChangePassword: false } } });
    if (path === "/api/v1/tasks" && request.method() === "GET") {
      taskReads += 1;
      const createdBy = url.searchParams.get("createdBy");
      const items = tasks.filter(item => !createdBy || item.createdBy === createdBy).map(item => ({ ...item }));
      return route.fulfill({ json: { items, total: items.length, page: 1, pageSize: 20 } });
    }
    const match = path.match(/^\/api\/v1\/tasks\/([^/]+)\/(start|stop|pause|resume)$/);
    if (match && request.method() === "POST") {
      operations.push(`${match[1]}:${match[2]}`);
      if (match[1] === "collecting") return route.fulfill({ status: 500, json: { message: "模拟失败" } });
      return route.fulfill({ status: 202, json: { id: `operation-${match[1]}` } });
    }
    if (path === "/api/v1/users/creators") return route.fulfill({ json: { items: [{ id: "admin", username: "admin", displayName: "管理员" }], total: 1, page: 1, pageSize: 20 } });
    return route.fulfill({ json: { items: [], total: 0, page: 1, pageSize: 20 } });
  });
  const page = await context.newPage();
  page.setDefaultTimeout(10000);
  page.on("pageerror", error => errors.push(error.message));
  page.on("console", message => {
    // collecting:stop 的 500 是本用例刻意注入的 API 失败；它不代表应用抛出了控制台异常。
    if (message.type() === "error" && !message.text().startsWith("Failed to load resource:")) errors.push(message.text());
  });
  await page.goto(process.env.BASE_URL || "http://127.0.0.1:5173", { waitUntil: "networkidle" });
  await page.getByRole("tab", { name: "采集任务", exact: true }).click();
  await page.getByText(isAdmin ? "可启动任务" : "本人任务", { exact: true }).waitFor();
  return { context, page, get taskReads() { return taskReads; } };
}
function row(page, name) { return page.locator(".el-table__body tr").filter({ hasText: name }); }
async function select(page, name) { await row(page, name).locator(".el-checkbox").click(); }

try {
  await mkdir("output/playwright", { recursive: true });
  const admin = await newPage(true);
  assert.equal(await admin.page.locator(".table-actions .el-checkbox").locator("input").isChecked(), true, "管理员默认查看全部");
  await admin.page.getByRole("combobox", { name: "按创建用户筛选" }).click();
  await admin.page.getByRole("option", { name: /管理员/ }).waitFor();
  await admin.page.keyboard.press("Escape");
  for (const name of ["可启动任务", "可停止任务", "资源已删除任务"]) {
    await select(admin.page, name);
  }
  await admin.page.getByRole("button", { name: /批量启动 \(1\/3\)/ }).click();
  await admin.page.getByText("适用 1 项，跳过 2 项", { exact: false }).waitFor();
  await admin.page.getByRole("button", { name: "取消", exact: true }).click();
  assert.deepEqual(operations, [], "取消确认不得发送控制请求");
  await admin.page.getByRole("button", { name: /批量启动 \(1\/3\)/ }).click();
  await admin.page.getByRole("button", { name: "确认", exact: true }).click();
  await admin.page.getByText("已提交 1 项控制请求", { exact: false }).waitFor();
  assert.deepEqual(operations, ["stopped:start"], "启动按冻结顺序发送");
  await admin.page.reload({ waitUntil: "networkidle" });
  await admin.page.getByRole("tab", { name: "采集任务", exact: true }).click();
  await admin.page.getByText("可停止任务", { exact: true }).waitFor();
  await select(admin.page, "可停止任务");
  await select(admin.page, "可继续任务");
  assert.equal(await row(admin.page, "可停止任务").locator(".el-checkbox input").isChecked(), true, "批量操作前应保留当前选择");
  await admin.page.waitForTimeout(5200);
  assert.ok(admin.taskReads >= 2, "任务列表应约每五秒轮询");
  assert.equal(await row(admin.page, "可停止任务").locator(".el-checkbox input").isChecked(), true, "同页轮询必须保留选择");
  if (!await row(admin.page, "可继续任务").locator(".el-checkbox input").isChecked()) await select(admin.page, "可继续任务");
  const stopButton = admin.page.getByRole("button", { name: /批量停止/ });
  assert.equal(await stopButton.textContent(), "批量停止 (2/2)");
  await stopButton.click();
  await admin.page.getByRole("button", { name: "确认", exact: true }).click();
  await admin.page.getByText("失败 1 项", { exact: false }).waitFor();
  assert.deepEqual(operations.slice(-2), ["collecting:stop", "paused:stop"], "失败项不能阻断后续任务");
  assert.ok((await admin.page.locator(".bulk-operation-result").textContent()).includes("可停止任务：失败（模拟失败）"));
  for (const width of [390, 320]) {
    await admin.page.setViewportSize({ width, height: 844 });
    await admin.page.waitForTimeout(150);
    assert.equal(await admin.page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth), false, "小屏页面不得横向溢出");
    await admin.page.screenshot({ path: `output/playwright/task-bulk-${width}.png`, fullPage: true });
  }
  await admin.context.close();

  const member = await newPage(false);
  assert.equal(await member.page.locator(".table-actions .el-checkbox").locator("input").isChecked(), false, "普通用户默认只查看本人");
  assert.ok(!operations.some(item => item.startsWith("other:")), "普通用户查询不应包含其他创建者任务");
  await member.context.close();
  assert.deepEqual(errors, [], `浏览器控制台异常：${errors.join("；")}`);
  console.log(JSON.stringify({ passed: true, operations, viewports: [390, 320], consoleErrors: 0 }));
} finally {
  await browser.close();
}
