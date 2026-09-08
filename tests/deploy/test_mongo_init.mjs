// 在隔离的 mongosh 上下文验证初始化门槛，不需要真实数据库或等待时钟。
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import vm from "node:vm";

const script = readFileSync(new URL("../../deploy/mongo-init.js", import.meta.url), "utf8");
const ready = { set: "rs0", members: [
  { name: "mongo1:27017", state: 1, health: 1 },
  { name: "mongo2:27017", state: 2, health: 1 },
  { name: "mongo3:27017", state: 2, health: 1 },
] };

function scenario(statuses) {
  let clock = 0;
  let calls = 0;
  let initializations = 0;
  const context = {
    rs: {
      status() {
        const result = statuses[Math.min(calls++, statuses.length - 1)];
        if (result instanceof Error) throw result;
        return result;
      },
      initiate() { initializations++; },
    },
    print() {},
    sleep(milliseconds) { clock += milliseconds; },
    Date: { now: () => clock },
  };
  return { run: () => vm.runInNewContext(script, context),
    snapshot: () => ({ clock, calls, initializations }) };
}

test("首次初始化等待一主两从全部就绪", () => {
  const missing = Object.assign(new Error("NotYetInitialized"), { code: 94 });
  const pending = { set: "rs0", members: ready.members.map(member => ({ ...member, state: 2 })) };
  const fixture = scenario([missing, pending, ready]);
  fixture.run();
  assert.equal(fixture.snapshot().initializations, 1);
  assert.ok(fixture.snapshot().clock >= 1000);
});

test("已初始化的拓扑不重复初始化", () => {
  const fixture = scenario([ready]);
  fixture.run();
  assert.equal(fixture.snapshot().initializations, 0);
});

test("鉴权或其他异常不能当成未初始化", () => {
  const fixture = scenario([Object.assign(new Error("Unauthorized"), { code: 13 })]);
  assert.throws(fixture.run, /Unauthorized/);
  assert.equal(fixture.snapshot().initializations, 0);
});

test("缺少健康副本时就绪检查必须超时失败", () => {
  const fixture = scenario([{ ...ready, members: ready.members.slice(0, 2) }]);
  assert.throws(fixture.run, /ready|就绪/i);
  assert.ok(fixture.snapshot().clock >= 120000);
});
