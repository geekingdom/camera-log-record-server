// 在隔离 mongosh 模型中验证独立数据库地址、重复初始化和异常路径。
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import vm from "node:vm";

const script = readFileSync(new URL("../../deploy/mongo-standalone-init.js", import.meta.url), "utf8");
function scenario({ existing = false, mismatch = false, healthy = true, unauthorized = false } = {}) {
  let initiated;
  let time = 0;
  const members = [27117, 27118, 27119].map((port, index) => ({ _id: index, host: `192.0.2.10:${port}` }));
  const context = {
    process: { env: { DATABASE_HOST: "192.0.2.10", MONGO_PORT_1: "27117", MONGO_PORT_2: "27118", MONGO_PORT_3: "27119" } },
    rs: {
      conf() {
        if (unauthorized) throw Object.assign(new Error("Unauthorized"), { code: 13 });
        if (!existing) throw Object.assign(new Error("NotYetInitialized"), { code: 94 });
        return { _id: "rs0", members: mismatch ? [{ host: "old:27017" }] : members };
      },
      initiate(config) { initiated = config; },
      status() { return { members: [1, 2, 2].map((state) => ({ state, health: healthy ? 1 : 0 })) }; },
    },
    print() {}, sleep(ms) { time += ms; }, Date: { now: () => time },
  };
  return { run: () => vm.runInNewContext(script, context), get initiated() { return initiated; } };
}
test("独立初始化使用配置的可达成员地址", () => {
  const fixture = scenario(); fixture.run();
  assert.equal(fixture.initiated._id, "rs0");
  assert.equal(fixture.initiated.members[2].host, "192.0.2.10:27119");
});
test("重复部署不重写原副本集", () => {
  const fixture = scenario({ existing: true }); fixture.run();
  assert.equal(fixture.initiated, undefined);
});
test("拓扑变更和认证失败不被当作首次部署", () => {
  assert.throws(scenario({ existing: true, mismatch: true }).run, /迁移/);
  assert.throws(scenario({ unauthorized: true }).run, /Unauthorized/);
});
test("缺少主从就绪不能报告部署成功", () => {
  assert.throws(scenario({ healthy: false }).run, /超时/);
});
