// ANSI 显示验收：仅使用 mock API/WebSocket 驱动真实 LiveLogs 组件，不连接真实服务或设备。
import assert from "node:assert/strict";
import { mkdir } from "node:fs/promises";

const imported = await import(process.env.PLAYWRIGHT_MODULE || "playwright");
const { chromium } = imported.default || imported;
const output = "output/playwright";
const token = "synthetic-ansi-token";
const task = {
  id: "ansi-task", name: "ANSI 显示验证任务", protocol: "TELNET_SERIAL",
  resourceId: "serial-resource-fixture",
  ip: "192.0.2.101", port: 23, status: "RUNNING", desiredState: "RUNNING", version: 1,
  initialCommands: [], scheduledCommands: [], updatedAt: "2026-09-08T08:00:00.000Z",
};
const rawChunks = [
  "\u001B[31m完整红色\u001B[0m\n",
  "^[[31m插入符红色^[[0m\n",
  "跨帧 \u001B[1;",
  "31m红色\u001B[0m\n",
  '<img src=x onerror="window.__ansiExecuted = true">字面 HTML\n',
];
const expected = ["完整红色", "插入符红色", "跨帧 红色", '<img src=x onerror="window.__ansiExecuted = true">字面 HTML'];
let offset = 0;
const frames = rawChunks.map((chunk, index) => {
  const frame = {
    type: "data", data: Buffer.from(chunk).toString("base64"), cursor: `cursor-${index + 1}`,
    fileId: "ansi-file", sessionId: "ansi-session", offset,
  };
  offset += Buffer.byteLength(chunk);
  return frame;
});

const browser = await chromium.launch({ headless: true, channel: "chrome" });
const context = await browser.newContext({ viewport: { width: 1440, height: 800 } });
await context.addInitScript((currentToken) => {
  sessionStorage.setItem("camera-log-record-token", currentToken);
  localStorage.setItem("camera-log-sidebar-collapsed", "false");
  window.__ansiExecuted = false;
}, token);

async function json(route, body, status = 200) {
  await route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });
}

await context.route("**/api/v1/**", async (route) => {
  const request = route.request();
  const path = new URL(request.url()).pathname;
  if (request.method() === "GET" && path === "/api/v1/tasks")
    return json(route, { items: [task], total: 1, page: 1, pageSize: 100 });
  if (request.method() === "GET" && path === "/api/v1/resources")
    return json(route, { items: [{ id: "serial-resource-fixture", name: "ANSI 串口服务器", kind: "SERIAL_SERVER", ip: task.ip, version: 1 }], total: 1, page: 1, pageSize: 20 });
  if (request.method() === "GET" && path === `/api/v1/tasks/${task.id}`) return json(route, task);
  if (request.method() === "GET" && path === `/api/v1/tasks/${task.id}/log-hours`)
    return json(route, { items: [], total: 0, page: 1, pageSize: 24 });
  if (request.method() === "GET" && path === "/api/v1/command-templates")
    return json(route, { items: [], total: 0, page: 1, pageSize: 20 });
  if (request.method() === "GET" && ["/api/v1/nodes", "/api/v1/admin/nodes"].includes(path))
    return json(route, { items: [], total: 0, page: 1, pageSize: 100 });
  if (request.method() === "GET" && path === "/api/v1/platform-settings")
    return json(route, { retentionDays: 7, version: 1, updatedAt: task.updatedAt });
  if (request.method() === "GET" && path === "/api/v1/display-settings")
    return json(route, { liveLogBufferMiB: 10 });
  if (request.method() === "GET" && path === "/api/v1/service-tokens")
    return json(route, { items: [], total: 0, page: 1, pageSize: 20 });
  if (request.method() === "GET" && ["/api/v1/audit-events", "/api/v1/runtime-events"].includes(path))
    return json(route, { items: [], total: 0, page: 1, pageSize: 50 });
  throw new Error(`未模拟的请求：${request.method()} ${path}`);
});

let socketHello;
let socketConnections = 0;
await context.routeWebSocket(`**/api/v1/tasks/${task.id}/logs`, (socket) => {
  socketConnections += 1;
  socket.onMessage((message) => {
    socketHello = JSON.parse(String(message));
    for (const frame of frames) socket.send(JSON.stringify(frame));
  });
});

const page = await context.newPage();
page.setDefaultTimeout(10000);
const issues = [];
try {
  await mkdir(output, { recursive: true });
  await page.goto("http://127.0.0.1:5173", { waitUntil: "networkidle" });
  await page.getByRole("tab", { name: "日志工作台", exact: true }).click();
  await page.getByRole("heading", { name: "日志工作台", exact: true }).waitFor();
  await page.getByRole("combobox", { name: "选择日志任务", exact: true }).click();
  await page.getByRole("option", { name: /ANSI 显示验证任务/ }).click();
  await page.keyboard.press("Escape");

  const consoleOutput = page.locator(".log-console");
  await consoleOutput.getByText(expected[3], { exact: true }).waitFor();
  const [result, lines] = await Promise.all([
    consoleOutput.evaluate((element) => ({
    text: element.textContent ?? "",
    htmlNodes: element.querySelectorAll("img").length,
    executed: window.__ansiExecuted === true,
    })),
    consoleOutput.locator(".log-line").allTextContents(),
  ]);
  const expectedOrder = expected.every((line, index) => lines[index] === line);
  const controlsAbsent = !/[\u001B]|\^\[\[\d*;?\d*m/.test(result.text);
  if (JSON.stringify(socketHello) !== JSON.stringify({ token, cursor: null }))
    issues.push(`WebSocket 首帧鉴权不符合预期：${JSON.stringify(socketHello)}`);
  if (!expectedOrder) issues.push(`显示正文不符合预期：${JSON.stringify(lines)}`);
  if (!controlsAbsent) issues.push(`显示文本仍包含 ANSI 控制码：${JSON.stringify(result.text)}`);
  if (result.htmlNodes || result.executed)
    issues.push(`HTML 字面正文被解释：nodes=${result.htmlNodes}, executed=${result.executed}`);

  const workspace = page.locator(".workspace");
  const size = async () => Promise.all([workspace.boundingBox(), consoleOutput.boundingBox()]);
  const [expandedWorkspace, expandedConsole] = await size();
  await page.screenshot({ path: `${output}/ansi-live-1440x800-expanded.png`, fullPage: true });
  await page.getByRole("button", { name: "折叠导航栏", exact: true }).click();
  await page.waitForTimeout(100);
  const [collapsedWorkspace, collapsedConsole] = await size();
  await page.screenshot({ path: `${output}/ansi-live-1440x800-collapsed.png`, fullPage: true });
  await page.getByRole("button", { name: "展开导航栏", exact: true }).click();
  await page.waitForTimeout(100);
  const [restoredWorkspace, restoredConsole] = await size();
  const preservedLines = await consoleOutput.locator(".log-line").allTextContents();
  const workspaceExpanded = Boolean(expandedWorkspace && collapsedWorkspace
    && collapsedWorkspace.width > expandedWorkspace.width);
  const consoleExpanded = Boolean(expandedConsole && collapsedConsole
    && collapsedConsole.width > expandedConsole.width);
  const workspaceRestored = Boolean(expandedWorkspace && restoredWorkspace
    && Math.abs(restoredWorkspace.width - expandedWorkspace.width) < 1);
  const consoleRestored = Boolean(expandedConsole && restoredConsole
    && Math.abs(restoredConsole.width - expandedConsole.width) < 1);
  if (!workspaceExpanded || !consoleExpanded)
    issues.push(`折叠后工作区或实时日志未扩宽：${JSON.stringify({ expandedWorkspace, collapsedWorkspace, expandedConsole, collapsedConsole })}`);
  if (!workspaceRestored || !consoleRestored)
    issues.push(`展开后工作区或实时日志未恢复：${JSON.stringify({ expandedWorkspace, restoredWorkspace, expandedConsole, restoredConsole })}`);
  if (!expected.every((line, index) => preservedLines[index] === line))
    issues.push(`侧栏切换后实时日志丢失：${JSON.stringify(preservedLines)}`);
  if (socketConnections !== 1)
    issues.push(`侧栏切换创建了额外 WebSocket 连接：${socketConnections}`);

  console.log(JSON.stringify({
    passed: issues.length === 0, issues, socketHello, socketConnections, lines, preservedLines,
    htmlNodes: result.htmlNodes, executed: result.executed,
    widths: { expandedWorkspace, collapsedWorkspace, restoredWorkspace, expandedConsole, collapsedConsole, restoredConsole },
    screenshots: [`${output}/ansi-live-1440x800-expanded.png`, `${output}/ansi-live-1440x800-collapsed.png`],
  }));
  assert.deepEqual(issues, [], "发现 ANSI 实时显示问题");
} finally {
  await context.close();
  await browser.close();
}
