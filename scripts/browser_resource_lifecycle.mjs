// 资源生命周期浏览器验收：全量 API 在浏览器层 mock，覆盖编辑确认、软删除与日志保留入口。
import assert from "node:assert/strict";
import { mkdir } from "node:fs/promises";

const imported = await import(process.env.PLAYWRIGHT_MODULE || "playwright");
const { chromium } = imported.default || imported;
const browser = await chromium.launch({ headless: true, channel: "chrome" });
const context = await browser.newContext({ viewport: { width: 1440, height: 900 } });
await context.addInitScript(() => sessionStorage.setItem("camera-log-record-token", "resource-lifecycle-token"));
const active = {
  id: "network-resource", name: "待维护网络设备", kind: "HIKVISION_NETWORK", ip: "192.0.2.81",
  username: "operator", authType: "DIGEST", model: "DS-2CD-fixture", subSerialNumber: "SN-maintenance",
  softwareVersion: "V5.7", version: 3, taskCount: 3, activeTaskCount: 2,
};
const deleted = {
  id: "deleted-resource", name: "已删除串口服务器", kind: "SERIAL_SERVER", ip: "192.0.2.82",
  version: 5, taskCount: 1, activeTaskCount: 0, deletedAt: "2026-09-08T12:00:00.000Z",
};
const changes = [];
async function json(route, body, status = 200) {
  await route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });
}
function task(resourceId, resourceDeleted = false) {
  return {
    id: `${resourceId}-task`, name: resourceDeleted ? "已删除资源的历史任务" : "网络资源任务",
    resourceId, resourceDeleted, protocol: resourceDeleted ? "TELNET_SERIAL" : "SSH",
    ip: resourceDeleted ? deleted.ip : active.ip, port: resourceDeleted ? 10002 : 22,
    status: "STOPPED", desiredState: "STOPPED", initialCommands: [], scheduledCommands: [], version: 1,
  };
}
await context.route("**/api/v1/**", async route => {
  const request = route.request(); const url = new URL(request.url()); const path = url.pathname; const method = request.method();
  const payload = request.postDataJSON?.() ?? {};
  if (method === "GET" && path === "/api/v1/resources") {
    const items = url.searchParams.get("includeDeleted") === "true" ? [active, deleted] : [active];
    return json(route, { items, total: items.length, page: 1, pageSize: 20 });
  }
  if (method === "GET" && path === `/api/v1/resources/${active.id}`) return json(route, active);
  if (method === "GET" && path === `/api/v1/resources/${deleted.id}`) return json(route, deleted);
  if (method === "PATCH" && path === `/api/v1/resources/${active.id}`) {
    changes.push({ method, path, payload }); Object.assign(active, payload, { version: active.version + 1 });
    delete active.password; return json(route, active);
  }
  if (method === "DELETE" && path === `/api/v1/resources/${active.id}`) {
    changes.push({ method, path, version: url.searchParams.get("version") });
    Object.assign(active, { deletedAt: "2026-09-08T13:00:00.000Z", activeTaskCount: 0 }); return json(route, active, 202);
  }
  if (method === "GET" && path === "/api/v1/tasks") {
    const resourceId = url.searchParams.get("resourceId");
    const items = resourceId === deleted.id ? [task(deleted.id, true)] : resourceId === active.id ? [task(active.id)] : [];
    return json(route, { items, total: items.length, page: 1, pageSize: 20 });
  }
  if (method === "GET" && path === `/api/v1/tasks/${deleted.id}-task`) return json(route, task(deleted.id, true));
  if (method === "GET" && path === `/api/v1/tasks/${deleted.id}-task/log-hours`)
    return json(route, { items: [], total: 0, page: 1, pageSize: 24 });
  if (method === "GET" && path === "/api/v1/command-templates") return json(route, { items: [], total: 0, page: 1, pageSize: 20 });
  if (method === "GET" && path === "/api/v1/nodes") return json(route, { items: [], total: 0, page: 1, pageSize: 20 });
  throw new Error(`未模拟的 API 请求：${method} ${path}`);
});
const page = await context.newPage(); page.setDefaultTimeout(10000);
const errors = []; page.on("pageerror", error => errors.push(error.message)); page.on("console", message => { if (message.type() === "error") errors.push(message.text()); });
const activeRow = () => page.getByRole("row").filter({ hasText: active.name });
async function assertDialogFits(label) {
  const dialog = page.locator(".el-message-box"); await dialog.waitFor();
  const result = await dialog.evaluate(element => ({ width: element.getBoundingClientRect().width, scrollWidth: element.scrollWidth, viewport: window.innerWidth }));
  assert.ok(result.width <= result.viewport, `${label}: 确认框越过视口`);
  assert.ok(result.scrollWidth <= result.width + 1, `${label}: 确认文字横向溢出`);
  return dialog;
}
async function cancelDialog() { await page.locator(".el-message-box").getByRole("button", { name: "取消", exact: true }).click(); }
async function openEditConfirmation() {
  await activeRow().getByRole("button", { name: "编辑资源" }).click();
  await page.getByRole("dialog", { name: "编辑设备资源" }).waitFor();
  await page.getByLabel("资源名称", { exact: true }).fill("已修改但待确认的名称");
  await page.getByRole("button", { name: "保存资源", exact: true }).click();
}
async function openDeleteConfirmation() {
  await activeRow().getByRole("button", { name: "删除资源" }).click();
  await page.getByText("关联 3 个采集任务，其中 2 个尚未停止", { exact: false }).waitFor();
}
try {
  await mkdir("output/playwright", { recursive: true });
  await page.goto("http://127.0.0.1:5173", { waitUntil: "networkidle" });
  await page.getByRole("heading", { name: "设备资源", exact: true }).waitFor();
  for (const width of [1440, 390, 320]) {
    await page.setViewportSize({ width, height: 900 });
    await openEditConfirmation();
    const editDialog = await assertDialogFits(`${width}px 编辑确认`);
    await page.screenshot({ path: `output/playwright/resource-lifecycle-edit-${width}.png`, fullPage: true });
    await editDialog.screenshot({ path: `output/playwright/resource-lifecycle-edit-dialog-${width}.png` });
    await editDialog.getByRole("button", { name: "取消", exact: true }).click();
    await page.getByRole("button", { name: "关闭", exact: true }).click();
    await openDeleteConfirmation();
    const deleteDialog = await assertDialogFits(`${width}px 删除确认`);
    await page.screenshot({ path: `output/playwright/resource-lifecycle-delete-${width}.png`, fullPage: true });
    await deleteDialog.screenshot({ path: `output/playwright/resource-lifecycle-delete-dialog-${width}.png` });
    await deleteDialog.getByRole("button", { name: "取消", exact: true }).click();
  }
  assert.equal(changes.filter(change => change.method === "PATCH").length, 0, "取消编辑不得发送 PATCH");
  assert.equal(changes.filter(change => change.method === "DELETE").length, 0, "取消删除不得发送 DELETE");
  await page.setViewportSize({ width: 1440, height: 900 });
  await openEditConfirmation();
  await assertDialogFits("确认编辑");
  await page.locator(".el-message-box").getByRole("button", { name: "确认", exact: true }).click();
  await page.getByText("资源已保存", { exact: true }).waitFor();
  assert.equal(changes.filter(change => change.method === "PATCH").length, 1, "确认编辑必须发送一次 PATCH");
  await openDeleteConfirmation();
  await assertDialogFits("确认删除");
  await page.locator(".el-message-box").getByRole("button", { name: "确认", exact: true }).click();
  await page.getByText("资源已标记删除，已有日志保留", { exact: true }).waitFor();
  assert.equal(changes.filter(change => change.method === "DELETE").length, 1, "确认删除必须发送一次 DELETE");
  await page.getByText("包含已删除资源", { exact: true }).click();
  await page.getByText("已删除 · 日志保留", { exact: true }).waitFor();
  const deletedRow = page.getByRole("row").filter({ hasText: deleted.name });
  await deletedRow.getByRole("button", { name: "查看历史日志", exact: true }).click();
  await page.getByText("已删除资源的历史任务", { exact: true }).waitFor();
  const historyTask = page.getByRole("row").filter({ hasText: "已删除资源的历史任务" });
  assert.equal(await historyTask.getByRole("button", { name: "编辑任务" }).count(), 0, "已删除资源任务不得显示编辑入口");
  assert.equal(await historyTask.getByRole("button", { name: "启动任务" }).count(), 0, "已删除资源任务不得显示启动入口");
  await historyTask.getByRole("button", { name: /查看实时打印/ }).click();
  await page.getByRole("heading", { name: "日志工作台", exact: true }).waitFor();
  assert.deepEqual(errors, []);
  console.log(JSON.stringify({ passed: true, patchRequests: 1, deleteRequests: 1, screenshots: "output/playwright/resource-lifecycle-*.png", consoleErrors: 0 }));
} finally { await browser.close(); }
