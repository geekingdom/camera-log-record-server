// 普通HTTP内网、TLS域名和IPv6访问均使用相匹配的WebSocket协议。
import { expect, it } from "vitest";
import { websocketUrl } from "./websocketUrl";
it.each([
  ["http:", "192.0.2.1:5175", "ws://192.0.2.1:5175"],
  ["https:", "logs.example.com", "wss://logs.example.com"],
  ["http:", "[2001:db8::1]:5175", "ws://[2001:db8::1]:5175"],
])("使用%s访问时匹配实时订阅协议", (protocol, host, base) => {
  expect(websocketUrl("/api/v1/tasks/task/logs", { protocol, host })).toBe(`${base}/api/v1/tasks/task/logs`);
});
