// 三成员副本集配置；各成员名称与 Compose 服务名保持一致。
const config = {
  _id: "rs0",
  members: [
    { _id: 0, host: "mongo1:27017", priority: 2 },
    { _id: 1, host: "mongo2:27017", priority: 1 },
    { _id: 2, host: "mongo3:27017", priority: 1 }
  ]
};

try {
  // 已初始化时保留现有拓扑，避免部署重启覆盖副本集配置。
  rs.status();
  print("Replica set already initialized.");
} catch (error) {
  if (error.code !== 94) throw error;
  // 首次启动三个成员健康后才执行初始化。
  print("Initializing replica set rs0.");
  rs.initiate(config);
}

// ping 成功不代表已选主；只在一主两从均就绪后放行依赖多数派写入的服务。
const deadline = Date.now() + 120000;
let ready = false;
while (Date.now() < deadline) {
  const status = rs.status();
  const members = status.members || [];
  ready = status.set === config._id && members.length === config.members.length
    && members.filter(member => member.state === 1 && member.health === 1).length === 1
    && members.filter(member => member.state === 2 && member.health === 1).length === 2;
  if (ready) break;
  sleep(1000);
}
if (!ready) throw new Error("Replica set did not become ready within 120 seconds.");
print("Replica set ready: one PRIMARY and two SECONDARY members.");
