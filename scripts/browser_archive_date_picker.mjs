// 归档日期选择器验收：使用模拟 API 验证日期标记、选中、翻月、切任务和失败重试。
import assert from "node:assert/strict";
import { mkdir } from "node:fs/promises";
import { resolve } from "node:path";

const imported = await import(process.env.PLAYWRIGHT_MODULE || "playwright");
const { chromium } = imported.default || imported;

const taskA = {
  id: "calendar-task-a", name: "日期验收任务 A", resourceId: "calendar-resource-a",
  protocol: "SSH", ip: "192.0.2.31", port: 22, status: "COLLECTING", desiredState: "RUNNING",
  initialCommands: [], scheduledCommands: [],
};
const taskB = {
  id: "calendar-task-b", name: "日期验收任务 B", resourceId: "calendar-resource-b",
  protocol: "TELNET_SERIAL", ip: "192.0.2.32", port: 23, status: "STOPPED", desiredState: "STOPPED",
  initialCommands: [], scheduledCommands: [],
};
const logDays = {
  [taskA.id]: {
    "2030-04": ["2030-04-30"],
    "2030-05": ["2030-05-12", "2030-05-15"],
    "2030-06": ["2030-06-01", "2030-06-04"],
  },
  [taskB.id]: {
    "2030-04": [],
    "2030-05": ["2030-05-20"],
    "2030-06": ["2030-06-18"],
    "2030-07": [],
  },
};
const logDayRequests = [];
let failNextTaskBLogDays = false;

function page(items, pageSize = 100) {
  return { items, total: items.length, page: 1, pageSize };
}

async function json(route, body, status = 200) {
  await route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });
}

async function selectTask(ui, task) {
  const choose = ui.getByRole("button", { name: "选择任务", exact: true });
  const change = ui.getByRole("button", { name: "切换任务", exact: true });
  await Promise.race([
    choose.waitFor(),
    change.waitFor(),
  ]);
  const trigger = await choose.isVisible() ? choose : change;
  await trigger.click();
  const dialog = ui.getByRole("dialog", { name: "选择日志任务", exact: true });
  await dialog.getByText(task.name, { exact: true }).click();
  await dialog.getByRole("button", { name: "确认切换", exact: true }).click();
  await dialog.waitFor({ state: "hidden" });
  await ui.getByText(task.name, { exact: true }).waitFor();
}

async function openCalendar(ui) {
  const input = ui.getByLabel("归档日期（上海时区）", { exact: true });
  await input.click();
  const panel = ui.locator(".el-picker-panel").filter({ visible: true }).last();
  await panel.locator(".el-date-table").waitFor();
  return { input, panel };
}

async function assertNoPageOverflow(ui, screenshotName) {
  // 视口改变后 Element Plus 的 Popper 会异步重排；等待布局稳定，避免在旧宽度仍挂在文档上时误报。
  const overflow = await ui.evaluate(async () => {
    const deadline = performance.now() + 1500;
    let width = document.documentElement.scrollWidth;
    while (performance.now() < deadline) {
      if (width <= innerWidth + 1) return false;
      await new Promise(resolve => requestAnimationFrame(resolve));
      width = document.documentElement.scrollWidth;
    }
    return width > innerWidth + 1;
  });
  assert.equal(overflow, false, `${screenshotName} 不得出现页面横向溢出`);
  await ui.screenshot({ path: resolve("output/playwright", screenshotName), fullPage: true, animations: "disabled" });
}

const browser = await chromium.launch({ headless: true, channel: "chrome" });
const context = await browser.newContext({ viewport: { width: 1440, height: 900 } });
await context.addInitScript(() => {
  sessionStorage.setItem("camera-log-record-token", "calendar-date-picker-fixture");
  const RealDate = Date;
  const fixed = RealDate.parse("2030-05-15T04:00:00.000Z");
  // 固定当前时刻，使归档日期和月份断言独立于验收机器日期。
  // @ts-ignore 测试环境覆盖无参 Date 构造器。
  window.Date = class extends RealDate {
    constructor(...args) { super(...(args.length ? args : [fixed])); }
    static now() { return fixed; }
  };
});
await context.route("**/api/v1/**", async route => {
  const request = route.request();
  const url = new URL(request.url());
  const path = url.pathname;
  if (request.method() !== "GET") throw new Error(`日期验收不应发出写请求：${request.method()} ${path}`);
  if (path === "/api/v1/auth/me") return json(route, { user: { id: "admin", username: "fixture", scopes: ["*"], isAdmin: true } });
  if (path === "/api/v1/tasks") return json(route, page([taskA, taskB], 10));
  if (path === "/api/v1/resources") return json(route, page([
    { id: taskA.resourceId, name: "日期模拟资源 A", kind: "HIKVISION_NETWORK", ip: taskA.ip, version: 1 },
    { id: taskB.resourceId, name: "日期模拟资源 B", kind: "SERIAL_SERVER", ip: taskB.ip, version: 1 },
  ], 20));
  if (path === `/api/v1/tasks/${taskA.id}`) return json(route, taskA);
  if (path === `/api/v1/tasks/${taskB.id}`) return json(route, taskB);
  const logDaysMatch = path.match(/^\/api\/v1\/tasks\/(calendar-task-[ab])\/log-days$/);
  if (logDaysMatch) {
    const taskId = logDaysMatch[1];
    const month = url.searchParams.get("month");
    logDayRequests.push({ taskId, month });
    if (taskId === taskB.id && month === "2030-07" && failNextTaskBLogDays) {
      failNextTaskBLogDays = false;
      return json(route, { detail: "模拟日期索引暂时不可用" }, 503);
    }
    return json(route, { days: logDays[taskId][month] || [] });
  }
  if (path === `/api/v1/tasks/${taskA.id}/log-hours` || path === `/api/v1/tasks/${taskB.id}/log-hours`)
    return json(route, page([], 24));
  if (["/api/v1/command-templates", "/api/v1/nodes", "/api/v1/admin/nodes", "/api/v1/service-tokens", "/api/v1/audit-events", "/api/v1/runtime-events"].includes(path))
    return json(route, page([]));
  if (path === "/api/v1/platform-settings") return json(route, { retentionDays: 7, version: 1 });
  if (path === "/api/v1/display-settings") return json(route, { liveLogBufferMiB: 10 });
  throw new Error(`未模拟请求：${request.method()} ${path}`);
});
await context.routeWebSocket("**/api/v1/tasks/*/logs*", socket => socket.close());

const ui = await context.newPage();
ui.setDefaultTimeout(10_000);
const errors = [];
ui.on("pageerror", error => errors.push(error.message));
ui.on("console", message => {
  if (message.type() !== "error") return;
  // 此专项主动注入一次 log-days 503；其它控制台错误仍为失败。
  if (message.location().url.includes(`/tasks/${taskB.id}/log-days`) && message.text().includes("503")) return;
  errors.push(message.text());
});

try {
  await mkdir(resolve("output/playwright"), { recursive: true });
  await ui.goto(`${process.env.BASE_URL || "http://127.0.0.1:5173"}/#resources`, { waitUntil: "networkidle" });
  await ui.getByRole("tab", { name: "日志工作台", exact: true }).click();
  await selectTask(ui, taskA);
  await ui.getByRole("tab", { name: "小时归档与检索", exact: true }).click();

  const first = await openCalendar(ui);
  await first.panel.locator('[title="2030-05-12 有日志"] .archive-calendar-dot').waitFor();
  assert.equal(await first.input.inputValue(), "2030-05-15", "默认归档日期必须使用上海当天");
  assert.equal(await first.panel.locator('[title="2030-05-13 有日志"]').count(), 0, "无日志日期不得显示标记");
  assert.ok(logDayRequests.some(item => item.taskId === taskA.id && item.month === "2030-04"));
  assert.ok(logDayRequests.some(item => item.taskId === taskA.id && item.month === "2030-05"));
  assert.ok(logDayRequests.some(item => item.taskId === taskA.id && item.month === "2030-06"));
  await first.panel.locator('[title="2030-05-12 有日志"]').click();
  await first.panel.waitFor({ state: "hidden" });
  assert.equal(await first.input.inputValue(), "2030-05-12", "点击已标记日期必须更新归档日期");

  const second = await openCalendar(ui);
  await second.panel.locator(".el-picker-panel__icon-btn.arrow-right").click();
  await second.panel.locator('[title="2030-06-04 有日志"] .archive-calendar-dot').waitFor();
  assert.ok(logDayRequests.some(item => item.taskId === taskA.id && item.month === "2030-07"), "翻月必须预取新的后续月份");
  await assertNoPageOverflow(ui, "archive-date-picker-1440.png");
  await second.input.press("Escape");
  await second.panel.waitFor({ state: "hidden" });

  await selectTask(ui, taskB);
  await ui.getByRole("tab", { name: "小时归档与检索", exact: true }).click();
  const switched = await openCalendar(ui);
  await switched.panel.locator('[title="2030-05-20 有日志"] .archive-calendar-dot').waitFor();
  assert.equal(await switched.panel.locator('[title="2030-05-12 有日志"]').count(), 0, "切换任务后不得保留旧任务日期标记");
  assert.ok(logDayRequests.some(item => item.taskId === taskB.id && item.month === "2030-05"), "切任务必须按新任务查询日期");
  failNextTaskBLogDays = true;
  await switched.panel.locator(".el-picker-panel__icon-btn.arrow-right").click();
  await ui.getByText("日期标记加载失败，重新打开日历可重试", { exact: true }).waitFor();
  await assertNoPageOverflow(ui, "archive-date-picker-failure-1440.png");
  await switched.input.press("Escape");
  await switched.panel.waitFor({ state: "hidden" });

  const retry = await openCalendar(ui);
  await ui.getByText("日期标记加载失败，重新打开日历可重试", { exact: true }).waitFor({ state: "hidden" });
  await retry.input.fill("2030-06-18");
  await retry.input.press("Enter");
  const june = await openCalendar(ui);
  await june.panel.locator('[title="2030-06-18 有日志"] .archive-calendar-dot').waitFor();
  await ui.setViewportSize({ width: 390, height: 844 });
  await assertNoPageOverflow(ui, "archive-date-picker-390.png");

  assert.deepEqual(errors, [], `浏览器出现非预期错误：${errors.join("\n")}`);
  console.log(JSON.stringify({
    passed: true,
    assertions: ["date-dots", "date-selection", "month-change", "task-change", "failure-retry"],
    screenshots: ["archive-date-picker-1440.png", "archive-date-picker-failure-1440.png", "archive-date-picker-390.png"],
    logDayRequests,
    consoleErrors: 0,
  }));
} finally {
  await context.close();
  await browser.close();
}
