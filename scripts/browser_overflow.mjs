// 溢出验收：以超长 mock 数据测量实际滚动容器，绝不访问真实 API、凭据或设备。
import assert from "node:assert/strict";
import { mkdir } from "node:fs/promises";

const imported = await import(process.env.PLAYWRIGHT_MODULE || "playwright");
const { chromium } = imported.default || imported;
const output = "output/playwright";
const timestamp = "2026-09-08T08:00:00.000Z";
const long = "超长字段内容-".repeat(50);
const initialCommands = Array.from({ length: 14 }, (_, index) => ({
  command: `${index + 1}-${long}`, newline: "\n", delaySeconds: 0, prompt: null, timeoutSeconds: 30,
}));
const scheduledCommands = Array.from({ length: 12 }, (_, index) => ({
  id: `scheduled-${index}`, command: `${index + 1}-${long}`, totalExecutions: 999999, intervalSeconds: 3600,
}));
const task = {
  id: "overflow-task-" + "x".repeat(64), name: "任务名称-" + "标题".repeat(40), protocol: "TELNET_SERIAL",
  resourceId: "serial-resource-fixture",
  ip: "192.0.2.100", port: 65535, status: "ERROR", desiredState: "STOPPED", version: 1,
  runId: "run-" + "r".repeat(128), sessionId: "session-" + "s".repeat(128), shellMode: "PSH",
  debugPhase: "FAILED", error: `${long}\n${long}\n${long}`, updatedAt: timestamp, initialCommands, scheduledCommands,
};
const node = {
  id: "node-" + "n".repeat(80), url: "https://node.example.test:8443", reportedUrl: "https://node.example.test:8443",
  capacity: 100, accepting: true, version: 1, registered: true, online: true, reportedAt: timestamp,
};
const hour = {
  hourId: "hour-fixture", hour: timestamp, status: "READY", integrity: "VERIFIED", fragmentCount: 1,
  bytes: 123456789, archiveBytes: 98765432,
  files: [{ id: "file-fixture", nodeId: node.id, sessionId: task.sessionId, status: "READY", bytes: 123456789, archiveName: `${long}.tar.gz` }],
};
const browser = await chromium.launch({ headless: true, channel: "chrome" });
const context = await browser.newContext({ viewport: { width: 1440, height: 800 } });
await context.addInitScript(() => sessionStorage.setItem("camera-log-record-token", "synthetic-overflow-token"));
await context.routeWebSocket("**/api/v1/tasks/*/logs", (socket) => socket.close());

async function json(route, body, status = 200) {
  await route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });
}

await context.route("**/api/v1/**", async (route) => {
  const request = route.request();
  const url = new URL(request.url());
  const path = url.pathname;
  if (request.method() === "GET" && path === "/api/v1/tasks")
    return json(route, { items: [task], total: 1, page: 1, pageSize: 100 });
  if (request.method() === "GET" && path === "/api/v1/resources")
    return json(route, { items: [{ id: "serial-resource-fixture", name: "溢出串口服务器", kind: "SERIAL_SERVER", ip: task.ip, version: 1 }], total: 1, page: 1, pageSize: 20 });
  if (request.method() === "GET" && path === `/api/v1/tasks/${task.id}`) return json(route, task);
  if (request.method() === "GET" && path === "/api/v1/command-templates")
    return json(route, { items: [], total: 0, page: 1, pageSize: 20 });
  if (request.method() === "GET" && path === "/api/v1/nodes")
    return json(route, { items: [], total: 0, page: 1, pageSize: 100 });
  if (request.method() === "GET" && path === "/api/v1/platform-settings")
    return json(route, { retentionDays: 7, version: 1, updatedAt: timestamp });
  if (request.method() === "GET" && path === "/api/v1/admin/nodes") return json(route, { items: [node] });
  if (request.method() === "GET" && path === "/api/v1/service-tokens") return json(route, {
    items: [{ id: "service-token", name: long, scopes: ["tasks:read", "logs:read"], taskIds: [task.id], expiresAt: "2030-01-01T00:00:00Z", createdAt: timestamp, revoked: false }],
    total: 1, page: 1, pageSize: 20,
  });
  if (request.method() === "GET" && path === `/api/v1/tasks/${task.id}/log-hours`)
    return json(route, { items: [hour], total: 1, page: 1, pageSize: 24 });
  if (request.method() === "GET" && path === "/api/v1/audit-events")
    return json(route, { items: [], total: 0, page: 1, pageSize: 50 });
  if (request.method() === "GET" && path === "/api/v1/runtime-events")
    return json(route, { items: [], total: 0, page: 1, pageSize: 50 });
  throw new Error(`未模拟的请求：${request.method()} ${path}`);
});

const page = await context.newPage();
page.setDefaultTimeout(10000);
const issues = [];
const measurements = [];

async function inspectOverflow(locator, label, axis) {
  const result = await locator.evaluate((element, direction) => {
    const vertical = direction === "y";
    const scrollSize = vertical ? element.scrollHeight : element.scrollWidth;
    const clientSize = vertical ? element.clientHeight : element.clientWidth;
    const overflow = scrollSize > clientSize + 1;
    const key = vertical ? "overflowY" : "overflowX";
    const scrollKey = vertical ? "scrollHeight" : "scrollWidth";
    const clientKey = vertical ? "clientHeight" : "clientWidth";
    const positionKey = vertical ? "scrollTop" : "scrollLeft";
    const ancestors = [];
    let current = element;
    while (current && current !== document.documentElement) {
      const style = getComputedStyle(current);
      ancestors.push({ tag: current.tagName, className: current.className, overflow: style[key], scroll: current[scrollKey], client: current[clientKey] });
      if (/(auto|scroll)/.test(style[key]) && current[scrollKey] > current[clientKey] + 1) {
        const before = current[positionKey];
        current[positionKey] = current[scrollKey];
        return {
          overflow, handled: true, scroll: scrollSize, client: clientSize, ancestors,
          scrollContainer: current.className, before, after: current[positionKey],
        };
      }
      current = current.parentElement;
    }
    return { overflow, handled: !overflow, scroll: scrollSize, client: clientSize, ancestors };
  }, axis);
  measurements.push({ label, axis, ...result });
  if (result.overflow && !result.handled) issues.push(`${label}: ${axis} 向内容 ${result.scroll}px 超出容器 ${result.client}px，但没有可滚动祖先`);
  if (result.overflow && result.after <= result.before)
    issues.push(`${label}: ${axis} 向溢出但实际滚动容器无法移动`);
}

async function assertActionReachable(container, action, label) {
  const scroll = await container.evaluate((element) => {
    element.scrollTop = 0;
    const before = element.scrollTop;
    element.scrollTop = element.scrollHeight;
    return { before, after: element.scrollTop, scroll: element.scrollHeight, client: element.clientHeight };
  });
  measurements.push({ label, axis: "scroll-top", ...scroll });
  if (scroll.scroll > scroll.client + 1 && scroll.after <= scroll.before)
    issues.push(`${label}: 垂直溢出但 scrollTop 无法移动`);
  const box = await action.boundingBox();
  const viewport = page.viewportSize();
  const reachable = box && viewport && box.y >= 0 && box.y + box.height <= viewport.height;
  measurements.push({ label, action: "bottom", box, viewport, reachable });
  if (!reachable) issues.push(`${label}: 底部操作不可达`);
}

async function assertHorizontalScroll(container, label) {
  const scroll = await container.evaluate((element) => {
    element.scrollLeft = 0;
    const before = element.scrollLeft;
    element.scrollLeft = element.scrollWidth;
    return { before, after: element.scrollLeft, scroll: element.scrollWidth, client: element.clientWidth };
  });
  measurements.push({ label, axis: "scroll-left", ...scroll });
  if (scroll.scroll > scroll.client + 1 && scroll.after <= scroll.before)
    issues.push(`${label}: 水平溢出但 scrollLeft 无法移动`);
}

async function assertDrawerCommands(tabContent, lastScheduledCommand, addScheduledCommand, drawer, label) {
  const scroll = await tabContent.evaluate((element) => {
    element.scrollTop = 0;
    const before = element.scrollTop;
    element.scrollTop = element.scrollHeight;
    return { before, after: element.scrollTop, scroll: element.scrollHeight, client: element.clientHeight };
  });
  measurements.push({ label, axis: "tab-content-scroll-top", ...scroll });
  if (scroll.scroll > scroll.client + 1 && scroll.after <= scroll.before)
    issues.push(`${label}: 标签内容溢出但 scrollTop 无法移动`);

  await Promise.all([lastScheduledCommand.waitFor(), addScheduledCommand.waitFor()]);
  const [addBox, bottomContentBox, drawerBox] = await Promise.all([
    addScheduledCommand.boundingBox(), tabContent.boundingBox(), drawer.boundingBox(),
  ]);
  const viewport = page.viewportSize();
  const addVisible = Boolean(addBox && bottomContentBox && viewport
    && addBox.y >= bottomContentBox.y && addBox.y + addBox.height <= bottomContentBox.y + bottomContentBox.height
    && addBox.y >= 0 && addBox.y + addBox.height <= viewport.height);
  const drawerWithinViewport = Boolean(drawerBox && viewport
    && drawerBox.y >= 0 && drawerBox.y + drawerBox.height <= viewport.height);
  measurements.push({ label, command: "滚动到底后的添加定时命令", addBox, contentBox: bottomContentBox, addVisible, drawerBox, viewport, drawerWithinViewport });
  if (!addVisible) issues.push(`${label}: 滚动到底后添加定时命令不可见`);
  if (!drawerWithinViewport) issues.push(`${label}: 抽屉边界超出视口`);

  await lastScheduledCommand.evaluate((element) => element.scrollIntoView({ block: "nearest", inline: "nearest" }));
  const scheduledInput = lastScheduledCommand.locator("input").first();
  await scheduledInput.focus();
  const [commandBox, contentBox, inputState] = await Promise.all([
    lastScheduledCommand.boundingBox(), tabContent.boundingBox(), scheduledInput.evaluate((element) => ({
      focused: document.activeElement === element,
      disabled: element.disabled,
      readOnly: element.readOnly,
    })),
  ]);
  const commandVisible = Boolean(commandBox && contentBox && viewport
    && commandBox.y >= contentBox.y && commandBox.y + commandBox.height <= contentBox.y + contentBox.height
    && commandBox.y >= 0 && commandBox.y + commandBox.height <= viewport.height);
  measurements.push({ label, command: "定位后的最后一条定时命令", commandBox, contentBox, commandVisible, inputState });
  if (!commandVisible) issues.push(`${label}: 定位最后一条定时命令后仍不可见`);
  if (!inputState.focused || inputState.disabled || inputState.readOnly)
    issues.push(`${label}: 最后一条定时命令输入框不可聚焦编辑`);
}

async function assertPageWidth(label) {
  const result = await page.evaluate(() => {
    const viewport = window.innerWidth;
    const offenders = [...document.querySelectorAll("*")]
      .map((element) => {
        const box = element.getBoundingClientRect();
        const style = getComputedStyle(element);
        return { tag: element.tagName, className: element.className, left: box.left, right: box.right, width: box.width, visible: style.display !== "none" && style.visibility !== "hidden" && style.opacity !== "0" };
      })
      .filter((item) => item.visible && (item.right > viewport + 1 || item.left < -1))
      .sort((left, right) => Math.max(right.right - viewport, -right.left) - Math.max(left.right - viewport, -left.left))
      .slice(0, 6);
    return { scrollWidth: document.documentElement.scrollWidth, viewport, offenders };
  });
  measurements.push({ label, axis: "page-x", ...result });
  if (result.scrollWidth > result.viewport + 1)
    issues.push(`${label}: 页面存在水平溢出 scrollWidth=${result.scrollWidth}, viewport=${result.viewport}, offenders=${JSON.stringify(result.offenders)}`);
}

async function nav(label) {
  await page.getByRole("tab", { name: label, exact: true }).click();
  await page.locator("h1").filter({ hasText: label }).waitFor();
}

async function capture(label) {
  await page.screenshot({ path: `${output}/overflow-${label}.png`, fullPage: true });
}

async function pagePosition() {
  return page.evaluate(() => ({
    scrollY: window.scrollY,
    scrollHeight: document.documentElement.scrollHeight,
    clientHeight: document.documentElement.clientHeight,
  }));
}

async function taskDrawer(label) {
  await nav("采集任务");
  await page.getByRole("button", { name: "编辑任务", exact: true }).click();
  const drawer = page.locator(".el-drawer.open");
  const body = drawer.locator(".el-drawer__body");
  const tabContent = body.locator(".el-tabs > .el-tabs__content");
  const lastScheduledCommand = tabContent.locator(".schedule-edit-row").last();
  const addScheduledCommand = tabContent.getByRole("button", { name: "添加定时命令", exact: true });
  await drawer.getByLabel("初始化命令 14", { exact: true }).waitFor();
  await page.waitForTimeout(350);
  measurements.push({ label: `${label} 任务抽屉`, action: "打开后页面位置", page: await pagePosition() });
  await inspectOverflow(tabContent, `${label} 任务抽屉标签内容`, "y");
  await assertDrawerCommands(tabContent, lastScheduledCommand, addScheduledCommand, drawer, `${label} 任务抽屉`);
  measurements.push({ label: `${label} 任务抽屉`, action: "标签滚动后页面位置", page: await pagePosition() });
  await assertActionReachable(tabContent, drawer.getByRole("button", { name: "保存任务", exact: true }), `${label} 任务抽屉`);
  const drawerAfterAction = await drawer.boundingBox();
  const pageAfterAction = await pagePosition();
  measurements.push({ label: `${label} 任务抽屉`, action: "保存任务后页面状态", drawerAfterAction, pageAfterAction });
  await capture(`${label}-task-drawer`);
  await drawer.getByRole("button", { name: "关闭", exact: true }).click();

  await page.getByRole("button", { name: "查看任务状态", exact: true }).click();
  const dialog = page.getByRole("dialog", { name: "任务运行状态" });
  const dialogBody = dialog.locator(".el-dialog__body");
  await dialog.getByText("最近错误记录", { exact: true }).waitFor();
  await inspectOverflow(dialogBody, `${label} 任务诊断`, "y");
  await assertActionReachable(dialogBody, dialog.getByRole("button", { name: "关闭", exact: true }), `${label} 任务诊断`);
  await capture(`${label}-task-diagnostics`);
  await dialog.getByRole("button", { name: "关闭", exact: true }).click();
}

async function dialogs(label) {
  await nav("后台配置");
  await page.getByRole("button", { name: "编辑节点配置", exact: true }).click();
  const nodeDialog = page.getByRole("dialog", { name: "编辑节点配置" });
  const nodeBody = nodeDialog.locator(".el-dialog__body");
  await inspectOverflow(nodeBody, `${label} 节点配置对话框`, "y");
  await assertActionReachable(nodeBody, nodeDialog.getByRole("button", { name: "保存配置", exact: true }), `${label} 节点配置对话框`);
  await capture(`${label}-node-dialog`);
  await nodeDialog.getByRole("button", { name: "取消", exact: true }).click();

  await nav("服务账号");
  await page.getByRole("button", { name: "新建服务账号", exact: true }).click();
  const accountDialog = page.getByRole("dialog", { name: "新建第三方服务账号" });
  const accountBody = accountDialog.locator(".el-dialog__body");
  await inspectOverflow(accountBody, `${label} 服务账号对话框`, "y");
  await assertActionReachable(accountBody, accountDialog.getByRole("button", { name: "创建并显示口令", exact: true }), `${label} 服务账号对话框`);
  await capture(`${label}-account-dialog`);
  await accountDialog.getByRole("button", { name: "取消", exact: true }).click();
}

async function logs(label) {
  await nav("日志工作台");
  await page.getByRole("combobox", { name: "选择日志任务", exact: true }).click();
  await page.getByRole("option", { name: new RegExp("任务名称") }).click();
  await page.keyboard.press("Escape");
  await page.waitForTimeout(350);
  await page.getByRole("tab", { name: "小时归档与检索", exact: true }).click();
  const table = page.locator(".archive-table");
  await table.locator(".el-table__row").first().waitFor();
  const tableScroll = table.locator(".el-scrollbar__wrap").first();
  await inspectOverflow(tableScroll, `${label} 日志宽表格`, "x");
  await assertHorizontalScroll(tableScroll, `${label} 日志宽表格`);
  const horizontal = await tableScroll.evaluate((element) => element.scrollWidth > element.clientWidth + 1);
  if (horizontal && !await table.locator(".el-scrollbar__bar.is-horizontal").isVisible())
    issues.push(`${label} 日志宽表格: 横向溢出但水平滚动条不可见`);
  await capture(`${label}-log-table`);
}

try {
  await mkdir(output, { recursive: true });
  await page.goto("http://127.0.0.1:5173", { waitUntil: "networkidle" });
  await page.getByRole("tab", { name: "采集任务", exact: true }).click();
  await page.getByRole("heading", { name: "采集任务", exact: true }).waitFor();
  for (const [width, height] of [[1440, 800], [390, 650], [320, 568]]) {
    const label = `${width}x${height}`;
    await page.setViewportSize({ width, height });
    await taskDrawer(label);
    await dialogs(label);
    await logs(label);
    await assertPageWidth(label);
  }
  console.log(JSON.stringify({ passed: issues.length === 0, issues, measurements, screenshots: output }));
  assert.deepEqual(issues, [], "发现未处理的组件溢出");
} finally {
  await browser.close();
}
