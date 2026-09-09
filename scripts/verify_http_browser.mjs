// 非localhost普通HTTP浏览器验证；仅模拟API响应，不连接真实设备或写真实平台数据。
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { createServer } from "node:http";
import { createRequire } from "node:module";
import { networkInterfaces } from "node:os";
const require = createRequire(new URL("../frontend/package.json", import.meta.url));
const ts = require("typescript");
const { chromium } = await import(process.env.PLAYWRIGHT_MODULE || "playwright");
const source = await readFile(new URL("../frontend/src/shared/api.ts", import.meta.url), "utf8");
const javascript = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 } }).outputText;
const requests = [];
const server = createServer((request, response) => {
  if (request.url === "/api.js") {
    response.writeHead(200, { "Content-Type": "text/javascript" }); response.end(javascript);
  } else if (request.url.startsWith("/api/v1/")) {
    requests.push({ path: request.url, key: request.headers["idempotency-key"] });
    response.writeHead(200, { "Content-Type": "application/json" }); response.end("{}");
  } else {
    response.writeHead(200, { "Content-Type": "text/html" }); response.end("<!doctype html><title>HTTP browser verification</title>");
  }
});
await new Promise(resolve => server.listen(0, "0.0.0.0", resolve));
let browser;
try {
  browser = await chromium.launch({ headless: true, channel: "chrome", args: [
    "--no-proxy-server", "--host-resolver-rules=MAP camera-http.test 127.0.0.1",
  ] });
  const page = await browser.newPage();
  const address = Object.values(networkInterfaces()).flat().find(item => item?.family === "IPv4" && !item.internal)?.address;
  assert.ok(address, "验证需要非loopback的服务器IPv4地址");
  await page.goto(`http://${address}:${server.address().port}`);
  const result = await page.evaluate(async () => {
    const module = await import("/api.js");
    await module.api.operation("synthetic-task", "start");
    await module.api.command("synthetic-task", { command: "ls" });
    await module.api.download("synthetic-task", ["hour-1"], false);
    return { secure: isSecureContext, randomUUID: typeof crypto.randomUUID, key: module.idempotencyKey() };
  });
  assert.equal(result.secure, false);
  assert.equal(result.randomUUID, "undefined");
  assert.equal(requests.length, 3);
  for (const request of requests) assert.match(request.key, /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/);
  assert.equal(new Set(requests.map(item => item.key)).size, 3);
  console.log(JSON.stringify({ passed: true, serverIpAccess: true, insecureHttp: true, nativeRandomUuidUnavailable: true, writeRequests: requests.length }));
} finally {
  await browser?.close();
  await new Promise(resolve => server.close(resolve));
}
