// 用户删除处置弹窗验收：使用 API 替身检查分页目标、取消与迟到响应，不触及真实账号。
import assert from "node:assert/strict";
import { mkdir } from "node:fs/promises";

const imported = await import(process.env.PLAYWRIGHT_MODULE || "playwright");
const { chromium } = imported.default || imported;
const browser = await chromium.launch({ headless: true, channel: "chrome" });
const output = process.env.BROWSER_SCREENSHOTS || "output/playwright";
const baseUrl = process.env.BASE_URL || "http://127.0.0.1:5173";
const pageOf = (items, page, pageSize, total = items.length) => ({ items, page, pageSize, total });
const subjectA = { id: "subject-a", username: "subject-a", displayName: "迟到用户", isAdmin: false, scopes: [], enabled: true, version: 6 };
const subjectB = { id: "subject-b", username: "subject-b", displayName: "当前用户", isAdmin: false, scopes: [], enabled: true, version: 7 };
const targets = Array.from({ length: 150 }, (_, index) => ({
  id: `recipient-${index}`, username: `receiver-${index}`, displayName: `接收用户 ${index}`,
}));
const deleted = [];
let shareTargetCalls = 0;
let users = [subjectA, subjectB];
const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } });

// 下拉层采用 Teleport 渲染，截图前必须等待其关闭，避免旧视口坐标参与全页尺寸计算。
async function closeSelectAndAssertLayout(page, dialog) {
  await page.keyboard.press("Escape");
  await page.locator(".el-select__popper").waitFor({ state: "hidden" });
  await page.waitForTimeout(300);
  const layout = await page.evaluate(() => ({
    viewport: window.innerWidth,
    root: document.documentElement.scrollWidth,
    body: document.body.scrollWidth,
  }));
  assert.ok(
    layout.root <= layout.viewport && layout.body <= layout.viewport,
    `页面存在横向溢出：${JSON.stringify(layout)}`,
  );
  const box = await dialog.boundingBox();
  assert.ok(box, "删除用户弹窗未渲染");
  assert.ok(
    box.x >= 0 && box.x + box.width <= layout.viewport,
    `删除用户弹窗超出视口：${JSON.stringify({ layout, box })}`,
  );
}

async function json(route, body, status = 200) {
  await route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });
}

await context.route("**/api/v1/**", async route => {
  const request = route.request();
  const url = new URL(request.url());
  const { pathname } = url;
  const method = request.method();
  if (method !== "GET")
    assert.equal(request.headers()["x-requested-with"], "XMLHttpRequest", `${method} ${pathname} 缺少 CSRF 请求头`);
  if (pathname === "/api/v1/auth/me") return json(route, { user: {
    id: "admin", username: "admin", displayName: "验收管理员", isAdmin: true, enabled: true,
    mustChangePassword: false, scopes: ["*"], version: 1,
  } });
  if (pathname === "/api/v1/users/permissions") return json(route, { scopes: [] });
  if (pathname === "/api/v1/users" && method === "GET")
    return json(route, pageOf(users, Number(url.searchParams.get("page") || 1), Number(url.searchParams.get("pageSize") || 20), users.length));
  if (pathname === "/api/v1/users/share-targets") {
    const page = Number(url.searchParams.get("page"));
    const pageSize = Number(url.searchParams.get("pageSize"));
    assert.equal(pageSize, 100, "删除弹窗必须按 100 条分页读取接收用户");
    shareTargetCalls += 1;
    if (shareTargetCalls === 1) await new Promise(resolve => setTimeout(resolve, 900));
    return json(route, pageOf(targets.slice((page - 1) * pageSize, page * pageSize), page, pageSize, targets.length));
  }
  if (/^\/api\/v1\/users\/subject-b$/.test(pathname) && method === "DELETE") {
    deleted.push(Object.fromEntries(url.searchParams));
    users = [subjectA];
    return route.fulfill({ status: 204 });
  }
  if (pathname === "/api/v1/resources" || pathname === "/api/v1/tasks" || pathname === "/api/v1/command-templates" || pathname === "/api/v1/service-tokens" || pathname === "/api/v1/nodes")
    return json(route, pageOf([], 1, 20, 0));
  if (pathname === "/api/v1/dashboard") return json(route, { resources: [], tasks: [], nodes: [] });
  if (pathname === "/api/v1/admin/ip-policy") return json(route, { version: 1, enabled: false, rules: [] });
  throw new Error(`未模拟 API：${method} ${pathname}`);
});

try {
  await mkdir(output, { recursive: true });
  const page = await context.newPage();
  page.setDefaultTimeout(10_000);
  const errors = [];
  page.on("pageerror", error => errors.push(error.message));
  await page.goto(`${baseUrl}/#resources`, { waitUntil: "networkidle" });
  await page.getByRole("tab", { name: "账号管理", exact: true }).click();
  await page.getByRole("tab", { name: "用户账号", exact: true }).click();
  const row = name => page.locator(".user-manager .el-table__row").filter({ hasText: name });
  const dialog = page.getByRole("dialog", { name: "删除用户", exact: true });

  // 第一行读取故意迟到；第二行完成两页读取后，迟到结果不得重新打开错误弹窗。
  await row("迟到用户").getByRole("button", { name: "删除用户" }).click();
  await row("当前用户").getByRole("button", { name: "删除用户" }).click();
  await dialog.waitFor();
  await page.getByText("删除“当前用户”后", { exact: false }).waitFor();
  await page.waitForTimeout(1000);
  assert.equal(await page.getByText("删除“迟到用户”后", { exact: false }).count(), 0, "迟到目标列表覆盖了当前删除用户");
  assert.equal(shareTargetCalls, 4, "快速切行的迟到分页请求必须被丢弃且不能覆盖当前弹窗");

  await dialog.getByRole("button", { name: "删除用户", exact: true }).click();
  await page.getByText("请选择资源处置方式", { exact: true }).waitFor();
  assert.equal(deleted.length, 0, "未选处置方式不应发送 DELETE");
  await dialog.getByText("转移资源和本人创建的关联任务", { exact: true }).click();
  await dialog.getByRole("button", { name: "删除用户", exact: true }).click();
  await page.getByText("请选择资源接收用户", { exact: true }).waitFor();
  assert.equal(deleted.length, 0, "TRANSFER 未选目标不应发送 DELETE");

  await dialog.getByRole("combobox").click();
  await page.getByRole("option", { name: "接收用户 124（receiver-124）", exact: true }).click();
  await closeSelectAndAssertLayout(page, dialog);
  await page.screenshot({ path: `${output}/user-deletion-transfer-1440.png`, fullPage: true });
  await dialog.getByRole("button", { name: "取消", exact: true }).click();
  await dialog.waitFor({ state: "hidden" });
  assert.equal(deleted.length, 0, "取消删除不应发送 DELETE");

  await row("当前用户").getByRole("button", { name: "删除用户" }).click();
  await dialog.waitFor();
  await dialog.getByText("转移资源和本人创建的关联任务", { exact: true }).click();
  await dialog.getByRole("combobox").click();
  await page.getByRole("option", { name: "接收用户 124（receiver-124）", exact: true }).click();
  await closeSelectAndAssertLayout(page, dialog);
  await page.setViewportSize({ width: 390, height: 844 });
  await page.waitForTimeout(300);
  await closeSelectAndAssertLayout(page, dialog);
  await page.screenshot({ path: `${output}/user-deletion-transfer-390.png`, fullPage: true });
  await dialog.getByRole("button", { name: "删除用户", exact: true }).click();
  await dialog.waitFor({ state: "hidden" });
  assert.deepEqual(deleted, [{ version: "7", resourceAction: "TRANSFER", targetUserId: "recipient-124" }]);
  await page.getByText("用户已删除", { exact: true }).waitFor();
  assert.equal(await row("当前用户").count(), 0, "成功删除后列表未刷新");
  assert.deepEqual(errors, []);
} finally {
  await context.close();
  await browser.close();
}
