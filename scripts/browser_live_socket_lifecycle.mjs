// 实时日志关闭语义浏览器验收：本地 transport error 的默认 1000 必须重连，服务端 1000 必须停止订阅。
import assert from "node:assert/strict";
import { mkdir } from "node:fs/promises";

const imported = await import(process.env.PLAYWRIGHT_MODULE || "playwright");
const { chromium } = imported.default || imported;
const baseUrl = process.env.BASE_URL || "http://127.0.0.1:5173";
const output = "output/playwright";
const task = {
  id: "lifecycle-task", name: "实时日志连接生命周期验收", protocol: "TELNET_SERIAL",
  resourceId: "lifecycle-resource", ip: "192.0.2.102", port: 23,
  status: "RUNNING", desiredState: "RUNNING", version: 1,
  initialCommands: [], scheduledCommands: [], updatedAt: "2026-10-30T00:00:00.000Z",
};

const browser = await chromium.launch({ headless: true, channel: "chrome" });
try {
  await mkdir(output, { recursive: true });
  for (const width of [1440, 390]) {
    const context = await browser.newContext({ viewport: { width, height: 900 } });
    await context.addInitScript(() => {
      sessionStorage.setItem("camera-log-record-token", "lifecycle-token");
      localStorage.setItem("camera-log-sidebar-collapsed", "false");
      window.__liveLogSocketInstances = [];
      class MockWebSocket {
        constructor(url) {
          this.url = url;
          this.readyState = 0;
          this.sent = [];
          if (url.includes("/api/v1/tasks/")) window.__liveLogSocketInstances.push(this);
          queueMicrotask(() => {
            if (this.readyState !== 0) return;
            this.readyState = 1;
            this.onopen?.(new Event("open"));
          });
        }
        send(data) { this.sent.push(data); }
        close(code = 1000, reason = "") {
          if (this.readyState >= 2) return;
          this.readyState = 3;
          this.onclose?.(new CloseEvent("close", { code, reason, wasClean: true }));
        }
        triggerTransportError() { this.onerror?.(new Event("error")); }
      }
      MockWebSocket.CONNECTING = 0;
      MockWebSocket.OPEN = 1;
      MockWebSocket.CLOSING = 2;
      MockWebSocket.CLOSED = 3;
      window.WebSocket = MockWebSocket;
    });
    await context.route("**/api/v1/**", async route => {
      const request = route.request();
      const path = new URL(request.url()).pathname;
      const json = body => route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(body) });
      if (request.method() === "GET" && path === "/api/v1/auth/me")
        return json({ user: { id: "lifecycle-user", username: "lifecycle", displayName: "连接验收", isAdmin: true, enabled: true, mustChangePassword: false, scopes: ["*"] } });
      if (request.method() === "GET" && path === "/api/v1/tasks") return json({ items: [task], total: 1, page: 1, pageSize: 100 });
      if (request.method() === "GET" && path === "/api/v1/resources") return json({ items: [{ id: task.resourceId, name: "连接生命周期资源", kind: "SERIAL_SERVER", ip: task.ip, version: 1 }], total: 1, page: 1, pageSize: 20 });
      if (request.method() === "GET" && path === `/api/v1/tasks/${task.id}`) return json(task);
      if (request.method() === "GET" && path === `/api/v1/tasks/${task.id}/log-hours`) return json({ items: [], total: 0, page: 1, pageSize: 24 });
      if (request.method() === "GET" && ["/api/v1/command-templates", "/api/v1/nodes", "/api/v1/admin/nodes"].includes(path)) return json({ items: [], total: 0, page: 1, pageSize: 100 });
      if (request.method() === "GET" && path === "/api/v1/platform-settings") return json({ retentionDays: 7, version: 1, updatedAt: task.updatedAt });
      if (request.method() === "GET" && path === "/api/v1/display-settings") return json({ liveLogBufferMiB: 10 });
      if (request.method() === "GET" && path === "/api/v1/common-commands") return json({ items: [], total: 0, page: 1, pageSize: 100 });
      if (request.method() === "GET" && path === "/api/v1/service-tokens") return json({ items: [], total: 0, page: 1, pageSize: 20 });
      if (request.method() === "GET" && ["/api/v1/audit-events", "/api/v1/runtime-events"].includes(path)) return json({ items: [], total: 0, page: 1, pageSize: 50 });
      throw new Error(`未模拟的请求：${request.method()} ${path}`);
    });
    const page = await context.newPage();
    page.setDefaultTimeout(10_000);
    const errors = [];
    page.on("pageerror", error => errors.push(error.message));
    page.on("console", message => { if (message.type() === "error") errors.push(message.text()); });
    await page.goto(`${baseUrl}/#logs`, { waitUntil: "networkidle" });
    await page.getByRole("button", { name: "选择任务", exact: true }).click();
    const picker = page.getByRole("dialog", { name: "选择日志任务", exact: true });
    await picker.getByText(task.name, { exact: true }).click();
    await picker.getByRole("button", { name: "确认切换", exact: true }).click();
    await page.waitForFunction(() => window.__liveLogSocketInstances.length === 1);
    await page.getByText("实时连接中", { exact: true }).waitFor();

    // 浏览器在 error 后由客户端 close() 产生的默认码是 1000，不能被误判为服务端的终态关闭。
    await page.evaluate(() => window.__liveLogSocketInstances[0].triggerTransportError());
    await page.waitForFunction(() => window.__liveLogSocketInstances.length === 2);
    await page.getByText("实时连接中", { exact: true }).waitFor();
    const localFailureState = await page.evaluate(() => ({
      sockets: window.__liveLogSocketInstances.length,
      firstCode: window.__liveLogSocketInstances[0].readyState,
      secondHello: window.__liveLogSocketInstances[1].sent.length,
    }));
    assert.deepEqual(localFailureState, { sockets: 2, firstCode: 3, secondHello: 1 }, `${width}px 本地 error 后必须重新订阅`);
    await page.screenshot({ path: `${output}/live-socket-lifecycle-${width}-reconnected.png`, fullPage: true, animations: "disabled" });

    // 未出现本地 error 的 1000 来自服务端终态，页面应明确停止且不再建立第三条连接。
    await page.evaluate(() => window.__liveLogSocketInstances[1].close(1000, "任务已停止"));
    await page.getByText("任务已停止，实时日志已关闭", { exact: true }).waitFor();
    await page.waitForTimeout(1200);
    const serverCloseState = await page.evaluate(() => window.__liveLogSocketInstances.length);
    assert.equal(serverCloseState, 2, `${width}px 服务端 1000 后不得重新订阅`);
    await page.screenshot({ path: `${output}/live-socket-lifecycle-${width}-stopped.png`, fullPage: true, animations: "disabled" });
    assert.deepEqual(errors, [], `${width}px 浏览器错误：${JSON.stringify(errors)}`);
    await context.close();
  }
  console.log(JSON.stringify({ passed: true, screenshots: [1440, 390].flatMap(width => [
    `live-socket-lifecycle-${width}-reconnected.png`, `live-socket-lifecycle-${width}-stopped.png`,
  ]) }));
} finally {
  await browser.close();
}
