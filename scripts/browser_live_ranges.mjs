// 实时缺口浏览器回归：以 mock API/WebSocket 驱动真实组件，不访问设备、数据库或服务端日志。
import assert from "node:assert/strict";
import { mkdir } from "node:fs/promises";

const imported = await import(process.env.PLAYWRIGHT_MODULE || "playwright");
const { chromium } = imported.default || imported;
const output = "output/playwright";
const token = "synthetic-live-ranges-token";
const timestamp = "2026-09-09T08:00:00.000Z";
const primaryTask = {
  id: "ranges-task-a", name: "实时缺口模拟任务 A", protocol: "TELNET_SERIAL",
  resourceId: "ranges-serial", ip: "192.0.2.141", port: 10003,
  status: "COLLECTING", desiredState: "RUNNING", version: 1,
  initialCommands: [], scheduledCommands: [], updatedAt: timestamp,
};
const secondaryTask = {
  ...primaryTask, id: "ranges-task-b", name: "实时缺口模拟任务 B", version: 2,
};
const tasks = [primaryTask, secondaryTask];
const administrator = {
  id: "ranges-admin", username: "admin", displayName: "范围模拟管理员", isAdmin: true,
  scopes: ["*"], resourceIds: null, enabled: true, mustChangePassword: false, builtin: true, version: 1,
};
const encoder = new TextEncoder();
const byteLength = (text) => encoder.encode(text).byteLength;
const encoded = (text) => Buffer.from(text).toString("base64");
const line = (index) => `省略范围-${String(index).padStart(3, "0")}-${"数据".repeat(900)}\n`;
const completeText = Array.from({ length: 250 }, (_, index) => line(index)).join("");
const retainedText = Array.from({ length: 200 }, (_, index) => line(index)).join("");
const omittedText = Array.from({ length: 50 }, (_, index) => line(index + 200)).join("");
const omittedBytes = encoder.encode(omittedText);
const omittedStart = byteLength(retainedText);
const omittedEnd = byteLength(completeText);
const requestedContent = [];
const transportFaultRequests = [];
const catalogRequests = [];
const catalogIssue = { reason: "FILE_UNAVAILABLE", message: "中间目录文件已删除，无法补读。" };
const sockets = new Map();
let transportReadAttempt = 0;
let staleReply;
let staleRequestStarted;
const staleRequestStartedPromise = new Promise(resolve => { staleRequestStarted = resolve; });

async function json(route, body, status = 200) {
  await route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });
}

const browser = await chromium.launch({ headless: true, channel: "chrome" });
const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
await context.addInitScript((currentToken) => {
  sessionStorage.setItem("camera-log-record-token", currentToken);
  localStorage.setItem("camera-log-sidebar-collapsed", "false");
}, token);

await context.route("**/api/v1/**", async route => {
  const request = route.request();
  const url = new URL(request.url());
  const { pathname } = url;
  if (request.method() !== "GET") throw new Error(`不应发出写请求：${request.method()} ${pathname}`);
  if (pathname === "/api/v1/auth/me") return json(route, { user: administrator });
  if (pathname === "/api/v1/tasks") return json(route, { items: tasks, total: tasks.length, page: 1, pageSize: 100 });
  if (pathname === "/api/v1/resources") return json(route, {
    items: [{ id: "ranges-serial", name: "实时范围串口服务器", kind: "SERIAL_SERVER", ip: primaryTask.ip, version: 1 }],
    total: 1, page: 1, pageSize: 20,
  });
  if (pathname === "/api/v1/command-templates") return json(route, { items: [], total: 0, page: 1, pageSize: 100 });
  if (pathname === "/api/v1/nodes" || pathname === "/api/v1/admin/nodes") return json(route, { items: [], total: 0, page: 1, pageSize: 100 });
  if (pathname === "/api/v1/platform-settings") return json(route, { retentionDays: 7, version: 1, updatedAt: timestamp });
  if (["/api/v1/service-tokens", "/api/v1/audit-events", "/api/v1/runtime-events"].includes(pathname))
    return json(route, { items: [], total: 0, page: 1, pageSize: 100 });
  if (pathname === `/api/v1/tasks/${primaryTask.id}`) return json(route, primaryTask);
  if (pathname === `/api/v1/tasks/${secondaryTask.id}`) return json(route, secondaryTask);
  if (pathname.endsWith("/log-hours")) return json(route, { items: [], total: 0, page: 1, pageSize: 24 });
  if (pathname === `/api/v1/tasks/${primaryTask.id}/log-gap-catalog`) {
    catalogRequests.push(Object.fromEntries(url.searchParams));
    assert.equal(url.searchParams.get("beforeFileId"), "gap-node-a-file");
    assert.equal(url.searchParams.get("afterFileId"), "gap-node-b-file");
    if (!url.searchParams.get("cursor"))
      return json(route, { items: [{ fileId: "gap-node-a-file", sessionId: "range-session", start: 0, end: 11 }], nextCursor: "next-page", unrecoverable: [catalogIssue] });
    assert.equal(url.searchParams.get("cursor"), "next-page");
    return json(route, { items: [{ fileId: "gap-node-b-file", sessionId: "range-session", start: 0, end: 11 }], nextCursor: null, unrecoverable: [catalogIssue] });
  }
  if (pathname === "/api/v1/log-files/gap-node-a-file/content")
    return json(route, { fileId: "gap-node-a-file", sessionId: "range-session", data: encoded("NODE_A_GAP\n"), nextOffset: 11 });
  if (pathname === "/api/v1/log-files/gap-node-b-file/content")
    return json(route, { fileId: "gap-node-b-file", sessionId: "range-session", data: encoded("NODE_B_GAP\n"), nextOffset: 11 });
  if (pathname === "/api/v1/log-files/range-file/content") {
    const offset = Number(url.searchParams.get("offset"));
    const limit = Number(url.searchParams.get("limit"));
    if (offset === omittedEnd) {
      transportReadAttempt += 1;
      transportFaultRequests.push({ offset, limit, attempt: transportReadAttempt });
      assert.equal(limit, 16, "传输缺口补读必须限制在精确的 16 字节区间内");
      if (transportReadAttempt === 1)
        return json(route, { fileId: "wrong-file", sessionId: "range-session", data: encoded("x"), nextOffset: offset + 1 });
      if (transportReadAttempt === 2)
        return json(route, { fileId: "range-file", sessionId: "wrong-session", data: encoded("x"), nextOffset: offset + 1 });
      if (transportReadAttempt === 3)
        return json(route, { fileId: "range-file", sessionId: "range-session", data: encoded("x"), nextOffset: offset + 2 });
      if (transportReadAttempt === 4)
        return json(route, { fileId: "range-file", sessionId: "range-session", data: encoded("x".repeat(17)), nextOffset: offset + 17 });
      if (transportReadAttempt === 5)
        return json(route, { fileId: "range-file", sessionId: "range-session", data: "", nextOffset: offset });
      if (transportReadAttempt === 6) {
        const validText = "TRANSPORT_OK_16!";
        return json(route, { fileId: "range-file", sessionId: "range-session", data: encoded(validText), nextOffset: offset + byteLength(validText) });
      }
      staleRequestStarted();
      await new Promise(resolve => { staleReply = resolve; });
      const staleText = "STALE_RANGE_TEST";
      return json(route, { fileId: "range-file", sessionId: "range-session", data: encoded(staleText), nextOffset: offset + byteLength(staleText) });
    }
    requestedContent.push({ offset, limit });
    const expectedOffset = requestedContent.length === 1 ? omittedStart : requestedContent.at(-2).offset + requestedContent.at(-2).responseBytes;
    assert.equal(offset, expectedOffset, "读取必须从精确缺口起点连续推进");
    assert.equal(limit, Math.min(65536, omittedEnd - offset), "读取请求不得超出缺口终点");
    const start = offset - omittedStart;
    const bytes = omittedBytes.slice(start, start + limit);
    requestedContent.at(-1).responseBytes = bytes.byteLength;
    return json(route, { fileId: "range-file", sessionId: "range-session", data: Buffer.from(bytes).toString("base64"), nextOffset: offset + bytes.byteLength });
  }
  throw new Error(`未模拟的 API 请求：${request.method()} ${pathname}`);
});

await context.routeWebSocket("**/api/v1/tasks/*/logs", socket => {
  const taskId = new URL(socket.url()).pathname.split("/").at(-2);
  sockets.set(taskId, socket);
  socket.onMessage(message => {
    const hello = JSON.parse(String(message));
    assert.deepEqual(hello, { token, cursor: null }, "新任务订阅必须使用空 cursor");
    if (taskId === primaryTask.id) socket.send(JSON.stringify({
      type: "data", data: encoded(completeText), fileId: "range-file", sessionId: "range-session", offset: 0, cursor: "range-cursor-250",
    }));
  });
});

const page = await context.newPage();
page.setDefaultTimeout(15000);
const errors = [];
page.on("pageerror", error => errors.push(error.message));
page.on("console", message => { if (message.type() === "error") errors.push(message.text()); });

async function openPrimaryLiveLogs() {
  await page.getByRole("tab", { name: "日志工作台", exact: true }).click();
  await page.getByRole("heading", { name: "日志工作台", exact: true }).waitFor();
  await chooseTask(primaryTask.name, "选择任务");
}

async function chooseTask(name, button) {
  await page.getByRole("button", { name: button, exact: true }).click();
  const picker = page.getByRole("dialog", { name: "选择日志任务", exact: true });
  await picker.getByText(name, { exact: true }).click();
  await picker.getByRole("button", { name: "确认切换", exact: true }).click();
  await picker.waitFor({ state: "hidden" });
}

async function assertNoHorizontalOverflow(width, label) {
  const result = await page.evaluate(() => ({ documentWidth: document.documentElement.scrollWidth, viewport: window.innerWidth }));
  assert.ok(result.documentWidth <= width, `${label} 页面横向溢出：${JSON.stringify(result)}`);
}

async function assertDialogFooterReachable(dialog) {
  const close = dialog.getByRole("button", { name: "关闭", exact: true });
  const scroll = await close.evaluate(element => {
    let current = element.parentElement;
    while (current) {
      const style = getComputedStyle(current);
      if (/(auto|scroll)/.test(style.overflowY) && current.scrollHeight > current.clientHeight) {
        const before = current.scrollTop;
        current.scrollTop = current.scrollHeight;
        return { scrollable: true, moved: current.scrollTop > before };
      }
      current = current.parentElement;
    }
    return { scrollable: false, moved: false };
  });
  await close.scrollIntoViewIfNeeded();
  await close.waitFor({ state: "visible" });
  assert.ok(!scroll.scrollable || scroll.moved, "窄屏缺口对话框的纵向滚动层必须能到达页脚关闭按钮");
}

try {
  await mkdir(output, { recursive: true });
  await page.goto(process.env.BASE_URL || "http://127.0.0.1:5173", { waitUntil: "networkidle" });
  await openPrimaryLiveLogs();
  const consoleOutput = page.locator(".log-console");
  await consoleOutput.getByText(line(199).trim(), { exact: true }).waitFor();
  const renderedLineCount = await consoleOutput.evaluate(element => Math.round(element.scrollHeight / 24));
  assert.equal(renderedLineCount, 200, "单帧 250 行仅应保留 200 行可见日志；虚拟列表只挂载当前视口行");
  await page.getByText("本地已省略 50 行高频日志。", { exact: true }).waitFor();

  // 暂停后接收器继续推进 cursor，并把偏移跳跃和服务端未知缺口写入范围列表。
  await page.getByRole("button", { name: "暂停视图", exact: true }).click();
  const socket = sockets.get(primaryTask.id);
  assert.ok(socket, "主任务必须建立模拟 WebSocket");
  socket.send(JSON.stringify({
    type: "data", data: encoded("暂停期间收到的新行\n"), fileId: "range-file", sessionId: "range-session",
    offset: omittedEnd + 16, cursor: "paused-cursor-forward-gap",
  }));
  socket.send(JSON.stringify({ type: "gap", message: "服务端未提供精确字节区间", cursor: "paused-cursor-server-gap" }));
  socket.send(JSON.stringify({ type: "data", data: encoded("NODE_A_GAP\n"), fileId: "gap-node-a-file", sessionId: "range-session", offset: 0, cursor: "gap-before" }));
  socket.send(JSON.stringify({ type: "gap", message: "跨节点目录补读", cursor: "gap-catalog" }));
  socket.send(JSON.stringify({ type: "data", data: encoded("NODE_B_GAP\n"), fileId: "gap-node-b-file", sessionId: "range-session", offset: 0, cursor: "gap-after" }));
  await page.waitForTimeout(180);
  assert.equal(await consoleOutput.getByText("暂停期间收到的新行", { exact: true }).count(), 0, "暂停视图不得直接追加新行");

  await page.getByRole("button", { name: "查看省略范围", exact: true }).click();
  const dialog = page.getByRole("dialog", { name: "实时日志缺口", exact: true });
  await dialog.waitFor();
  await dialog.getByText("50 行", { exact: false }).waitFor();
  await dialog.getByText("传输", { exact: false }).waitFor();
  await dialog.getByText("服务端未提供精确字节区间", { exact: true }).waitFor();
  await dialog.screenshot({ path: `${output}/live-ranges-1440.png` });
  await assertNoHorizontalOverflow(1440, "桌面缺口对话框");

  const rateRow = dialog.locator(".range-item").filter({ hasText: "涉及 50 行" }).first();
  const rateRead = rateRow.getByRole("button", { name: /读取.*范围/ });
  await rateRead.click();
  await dialog.getByText(new RegExp(`字节范围 \\[${omittedStart.toLocaleString("zh-CN")}, ${omittedEnd.toLocaleString("zh-CN")}\\)`)).waitFor();
  await dialog.getByText(omittedText.slice(0, 30), { exact: false }).waitFor();
  const continueRead = dialog.getByRole("button", { name: "继续读取范围", exact: true });
  for (let pageCount = 1; await continueRead.isEnabled(); pageCount += 1) {
    assert.ok(pageCount < 16, "缺口分页读取不应无限循环");
    await continueRead.click();
    await page.waitForTimeout(80);
  }
  await dialog.getByText(omittedText.slice(-30).trim(), { exact: false }).waitFor();
  const rangeBytes = requestedContent.reduce((total, item) => total + item.responseBytes, 0);
  assert.equal(rangeBytes, omittedEnd - omittedStart, "分页读取必须覆盖精确省略范围的所有字节");
  assert.equal(requestedContent[0].offset, omittedStart, "首个分页请求必须从省略范围起点读取");
  assert.equal(requestedContent.at(-1).offset + requestedContent.at(-1).responseBytes, omittedEnd, "最后分页请求不得越过省略范围终点");

  // 同一精确传输缺口在每次失败后从未推进的 cursor 重试，直至有效页完成。
  await dialog.getByRole("button", { name: "返回缺口列表", exact: true }).click();
  const transportRow = dialog.locator(".range-item").filter({ hasText: "传输中断" }).first();
  await transportRow.getByRole("button", { name: /读取.*范围/ }).click();
  const transportCursor = `已读取至 ${omittedEnd.toLocaleString("zh-CN")} / ${(omittedEnd + 16).toLocaleString("zh-CN")}`;
  for (const message of ["返回文件与缺口记录不一致", "返回会话与缺口记录不一致", "返回字节边界无效", "返回字节边界无效"]) {
    await dialog.getByText(message, { exact: true }).waitFor();
    await dialog.getByText(transportCursor, { exact: true }).waitFor();
    await dialog.getByRole("button", { name: "重试读取", exact: true }).click();
  }
  await dialog.getByText("文件内容尚不可用，请稍后重试。", { exact: true }).waitFor();
  await dialog.getByText(transportCursor, { exact: true }).waitFor();
  await dialog.getByRole("button", { name: "继续读取范围", exact: true }).click();
  await dialog.getByText("TRANSPORT_OK_16!", { exact: true }).waitFor();
  assert.deepEqual(
    transportFaultRequests.slice(0, 6).map(({ offset, limit }) => ({ offset, limit })),
    Array.from({ length: 6 }, () => ({ offset: omittedEnd, limit: 16 })),
    "错误和空页重试不得推进传输缺口的读取 cursor",
  );

  // 未知 gap 以两端帧锚点查询分页目录，再按各 fileId 读取；节点位置不暴露给页面。
  await dialog.getByRole("button", { name: "返回缺口列表", exact: true }).click();
  const catalogRow = dialog.locator(".range-item").filter({ hasText: "跨节点目录补读" }).first();
  await catalogRow.getByRole("button", { name: /读取.*范围/ }).click();
  await dialog.getByText("NODE_A_GAP", { exact: true }).waitFor();
  await dialog.getByText("目录片段 2 / 2", { exact: true }).waitFor();
  assert.equal(await dialog.getByText(catalogIssue.message, { exact: true }).count(), 1,
    "跨页返回相同目录问题时只能显示一条警告");
  await dialog.getByRole("button", { name: "继续读取范围", exact: true }).click();
  await dialog.getByText("NODE_B_GAP", { exact: false }).waitFor();
  assert.equal(catalogRequests.length, 2, "跨节点目录必须按服务端 nextCursor 续页");
  await dialog.getByRole("button", { name: "返回缺口列表", exact: true }).click();

  await page.setViewportSize({ width: 390, height: 844 });
  await dialog.screenshot({ path: `${output}/live-ranges-390.png` });
  await assertNoHorizontalOverflow(390, "390 像素缺口对话框");
  await page.setViewportSize({ width: 320, height: 844 });
  await dialog.screenshot({ path: `${output}/live-ranges-320.png` });
  await assertNoHorizontalOverflow(320, "320 像素缺口对话框");
  await assertDialogFooterReachable(dialog);
  await page.setViewportSize({ width: 1440, height: 1000 });

  // 未知服务端缺口没有伪造文件范围，只允许跳到小时归档。
  await dialog.getByRole("button", { name: "查看小时归档", exact: true }).click();
  await page.getByRole("tab", { name: "小时归档与检索", exact: true }).waitFor();
  await page.getByRole("tab", { name: "实时打印", exact: true }).click();
  await consoleOutput.getByText(line(199).trim(), { exact: true }).waitFor();
  const reopenedSocket = sockets.get(primaryTask.id);
  assert.ok(reopenedSocket, "返回实时打印后必须重建当前任务的模拟 WebSocket");
  reopenedSocket.send(JSON.stringify({
    type: "data", data: encoded("关闭后迟到读取\n"), fileId: "range-file", sessionId: "range-session",
    offset: omittedEnd + 16, cursor: "stale-cursor-forward-gap",
  }));
  await page.waitForTimeout(180);
  await page.getByRole("button", { name: "查看省略范围", exact: true }).click();
  const reopened = page.getByRole("dialog", { name: "实时日志缺口", exact: true });
  await reopened.waitFor();

  // 关闭读取窗口后才返回的响应不得覆盖已关闭的状态。
  const staleRow = reopened.locator(".range-item").filter({ hasText: "传输中断" }).first();
  await staleRow.getByRole("button", { name: /读取.*范围/ }).click();
  await staleRequestStartedPromise;
  await reopened.getByRole("button", { name: /关闭/ }).click().catch(async () => {
    await reopened.locator(".el-dialog__headerbtn").click();
  });
  await reopened.waitFor({ state: "hidden" });
  staleReply();
  await page.waitForTimeout(100);
  assert.equal(await page.getByText("STALE_RANGE_TEST", { exact: true }).count(), 0, "关闭后的陈旧读取不得写入页面");

  // 任务切换会卸载旧范围；主任务的迟到 socket 与读取均不能污染任务 B。
  await chooseTask(secondaryTask.name, "切换任务");
  await page.getByText(/实时缺口模拟任务 B/).first().waitFor();
  assert.equal(await page.getByRole("button", { name: "查看省略范围", exact: true }).count(), 0, "切换任务后不得保留旧任务缺口入口");
  await page.getByRole("button", { name: "继续视图", exact: true }).count().catch(() => 0);
  assert.deepEqual(errors, [], "浏览器不应产生控制台或页面错误");
  console.log(JSON.stringify({
    passed: true, visibleLines: 200, omittedLines: 50, omittedRange: [omittedStart, omittedEnd],
    contentRequests: requestedContent, transportFaultRequests, consoleErrors: 0,
    screenshots: [`${output}/live-ranges-1440.png`, `${output}/live-ranges-390.png`, `${output}/live-ranges-320.png`],
  }));
} finally {
  staleReply?.();
  await context.close();
  await browser.close();
}
