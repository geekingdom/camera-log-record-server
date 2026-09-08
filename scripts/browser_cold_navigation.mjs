// 使用全新 Vite 缓存验证懒加载导航；模拟接口避免访问真实设备与凭据。
import assert from "node:assert/strict";
import { mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { createServer } from "../frontend/node_modules/vite/dist/node/index.js";

const { chromium } = await import(process.env.PLAYWRIGHT_MODULE || "playwright");
const cacheDir = await mkdtemp(join(tmpdir(), "camera-vite-cold-"));
const server = await createServer({
  root: resolve("frontend"), cacheDir, logLevel: "warn",
  server: { host: "127.0.0.1", port: 15189, strictPort: false, open: false },
});
let browser;
const reloads = [], errors = [], navigations = [];
try {
  await server.listen();
  browser = await chromium.launch({ headless: true, channel: "chrome" });
  const context = await browser.newContext();
  await context.addInitScript(() => sessionStorage.setItem("camera-log-record-token", "synthetic-token"));
  await context.route("**/api/v1/**", async route => {
    const path = new URL(route.request().url()).pathname;
    const allowed = ["tasks", "resources", "command-templates", "nodes", "admin/nodes", "service-tokens", "audit-events", "runtime-events"];
    let body;
    if (route.request().method() !== "GET") {
      errors.push(`非预期写请求 ${path}`);
      return route.abort();
    }
    if (path === "/api/v1/platform-settings") body = { retentionDays: 7, version: 1 };
    else if (allowed.some(name => path === `/api/v1/${name}`)) body = { items: [], total: 0, page: 1, pageSize: 20 };
    else { errors.push(`未模拟接口 ${path}`); return route.abort(); }
    await route.fulfill({ contentType: "application/json", body: JSON.stringify(body) });
  });
  const page = await context.newPage();
  page.on("pageerror", error => errors.push(error.message));
  page.on("framenavigated", frame => { if (frame === page.mainFrame()) navigations.push(frame.url()); });
  page.on("websocket", socket => socket.on("framereceived", ({ payload }) => {
    try { const message = JSON.parse(String(payload)); if (message.type === "full-reload") reloads.push(message); } catch { /* 忽略非 JSON 帧。 */ }
  }));
  await page.goto(`http://127.0.0.1:${server.httpServer.address().port}/`, { waitUntil: "networkidle" });
  for (const [name, selector] of [
    ["服务节点", ".workspace .data-table"], ["服务账号", ".access-manager"],
    ["审计与事件", ".audit-workspace"], ["后台配置", ".settings-manager"],
  ]) {
    await page.getByRole("tab", { name, exact: true }).click();
    await page.locator(selector).waitFor({ state: "visible", timeout: 10000 });
    await page.waitForLoadState("networkidle");
    assert.equal(await page.getByRole("tab", { name, exact: true }).getAttribute("aria-selected"), "true");
  }
  assert.deepEqual(reloads, [], "冷导航不能触发 Vite 整页刷新");
  assert.equal(navigations.length, 1, "只能发生初始页面导航");
  assert.deepEqual(errors, []);
  console.log(JSON.stringify({ passed: true, pages: 4, reloads: reloads.length }));
} finally {
  console.log(JSON.stringify({ reloads, errors, navigations }));
  if (browser) await browser.close();
  await server.close();
  await rm(cacheDir, { recursive: true, force: true });
}
