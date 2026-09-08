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
  // 首次启动三个成员健康后才执行初始化。
  print("Initializing replica set rs0.");
  rs.initiate(config);
}
