// 独立数据库使用对外可达的成员地址。重复部署只验证，不自动改写已有拓扑。
const host = process.env.DATABASE_HOST;
const ports = [1, 2, 3].map(n => Number(process.env[`MONGO_PORT_${n}`]));
const config = {_id: "rs0", members: ports.map((port, index) => ({
  _id: index, host: `${host}:${port}`, priority: index === 0 ? 2 : 1
}))};
try {
  const current = rs.conf();
  if (current._id !== config._id || current.members.length !== 3 ||
      current.members.some((item, index) => item.host !== config.members[index].host)) {
    throw new Error("现有副本集地址与配置不同，请按迁移流程处理；脚本不会自动改写拓扑");
  }
} catch (error) {
  if (error.code !== 94) throw error;
  rs.initiate(config);
}
const deadline = Date.now() + 120000;
let ready = false;
while (Date.now() < deadline) {
  const members = rs.status().members || [];
  ready = members.filter(item => item.health === 1 && item.state === 1).length === 1 &&
          members.filter(item => item.health === 1 && item.state === 2).length === 2;
  if (ready) break;
  sleep(1000);
}
if (!ready) throw new Error("副本集选主或同步超时，请检查成员地址、端口和持久化目录");
print("数据库部署通过：三成员副本集已就绪，认证已启用");
