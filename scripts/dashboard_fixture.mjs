// 资源看板的隔离浏览器数据：固定测试IP与计数，绝不访问实际采集设备。
export function dashboardFixture(date = "2026-10-14", days = 7, admin = true, logs = true) {
  const end = new Date(`${date}T00:00:00+08:00`);
  const daily = Array.from({ length: days }, (_, i) => {
    const day = new Date(end.getTime() - (days - 1 - i) * 86400000);
    const key = new Intl.DateTimeFormat("en-CA", { timeZone: "Asia/Shanghai" }).format(day);
    return { date: key, bytes: (i + 1) * 4 * 1024 ** 3, fileCount: (i + 1) * 24 };
  });
  return {
    date, days, range: { startDate: daily[0].date, endDate: date }, generatedAt: new Date().toISOString(),
    overview: { resources: 128, onlineResources: 116, collectingTasks: 86, tasks: 104, nodes: admin ? 3 : null },
    availability: { logs, nodes: admin },
    daily: logs ? daily : null,
    topResourcesTruncated: logs ? false : null,
    topResources: logs ? [
      { resourceId: "dash-r1", name: "视频分析主从设备", ip: "192.0.2.35", bytes: 14 * 1024 ** 3, fileCount: 124 },
      { resourceId: "dash-r2", name: "边缘网关", ip: "192.0.2.36", bytes: 8 * 1024 ** 3, fileCount: 96 },
    ] : null,
    coredumps: logs ? { resourceCount: 1, fileCount: 3, bytes: 128 * 1024 ** 2, truncated: false, items: [
      { resourceId: "dash-r1", name: "视频分析主从设备", ip: "192.0.2.35", fileCount: 3, bytes: 128 * 1024 ** 2, lastReceivedAt: `${date}T02:10:00Z` },
    ] } : null,
    nodes: admin ? [
      { id: "worker-a", name: "采集节点 A", status: "ONLINE", healthLevel: "HEALTHY", accepting: true, activeTasks: 36, capacity: 150, inputBytesPerSecond: 1048576, reasons: [] },
      { id: "worker-b", name: "采集节点 B", status: "ONLINE", healthLevel: "WARNING", accepting: false, activeTasks: 50, capacity: 150, inputBytesPerSecond: 524288, reasons: ["写入延迟接近配置限制"] },
      { id: "worker-c", name: "采集节点 C", status: "OFFLINE", healthLevel: "OFFLINE", accepting: false, activeTasks: 0, capacity: 150, inputBytesPerSecond: 0, reasons: ["超过心跳接收期限"] },
    ] : null,
    notes: ["原始日志量按已登记分卷统计，受目录更新与保留期影响。"],
  };
}
