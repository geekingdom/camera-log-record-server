# 设备 Coredump NFS 部署

本说明覆盖 Ubuntu/Debian 采集节点上的海康 coredump NFS 服务。NFS 配置以实际运行 Worker 的节点部署环境文件为准；平台设置接口不提供 NFS 根目录修改，避免保存配置与主机实际导出不一致。

## 配置

完整 Docker 部署编辑 `.env`，独立 Docker Worker 编辑 `.env.worker`；原生部署编辑 `/etc/camera-logs/native.env`。以下两项配置必须属于同一个采集节点：

| 配置项 | 示例 | 含义 |
| --- | --- | --- |
| `NFS_ROOT` | `/srv/camera-logs/nfs-coredump` | 宿主机 coredump 总目录。必须是非根绝对路径；Worker 在此创建设备 IP 子目录。Docker 容器以相同绝对路径 bind mount。 |
| `NFS_SERVER_IP` | `192.0.2.30` | 设备能到达的 Worker 宿主机 IPv4 或 IPv6 地址。留空时禁用 NFS；重复部署会撤销本项目专属导出并刷新，不停止其他服务使用的 NFS 服务。不能填写容器服务名、`0.0.0.0` 或回环地址。 |

完整部署执行 `sudo ./deploy-all.sh`，独立 Docker 节点执行 `sudo ./deploy-worker.sh`，原生完整或节点部署执行对应 `deploy-native*.sh`。NFS 启用后，部署器会安装 `nfs-kernel-server`、启用 `nfs-server` 的 systemd 自启动、创建 `NFS_ROOT`，并原子写入 `/etc/exports.d/camera-logs-coredump.exports` 后运行 `exportfs -ra`。

该专属文件固定将 `NFS_ROOT` 导出至 `*`，使用 `rw,sync,no_subtree_check,all_squash,anonuid=<worker-uid>,anongid=<worker-gid>`。Docker Worker 的默认 UID/GID 为 `10001:10001`；原生部署从实际 `SERVICE_USER` 读取 UID/GID。无论设备固件以哪个身份发起写入，文件都会映射到 Worker 账号，使 Worker 能读取并归档，同时不暴露设备 root 身份。所有网络可达的设备均可挂载该路径；不要将本项目文件用于其它共享，也不要编辑或覆盖 `/etc/exports` 与其它 `/etc/exports.d/*.exports` 文件。旧环境文件中的 `NFS_DEVICE_NETWORK` 会被忽略，重新部署会以星号导出覆盖本项目专属 exports 文件。

## 存储与排障

`NFS_ROOT` 只保存 coredump，建议位于独立容量受控的磁盘；`HOST_LOG_ROOT`/原生 `LOG_ROOT` 保存采集日志、分卷和小时归档，`API_DATA_ROOT`/原生 `API_LOG_ROOT` 保存 API 运行日志和下载临时文件。三个根目录可以同属一个受管数据盘，但不要填 `/`、数据库目录或其它服务目录。

部署后在节点主机检查：

```sh
sudo systemctl status nfs-server
sudo exportfs -v
find /srv/camera-logs/nfs-coredump -maxdepth 2 -type d
```

部署导出的是父目录 `NFS_ROOT`；Worker 运行时创建 `NFS_ROOT/<设备IP>/`，并将 `NFS_SERVER_IP:NFS_ROOT/<设备IP>` 作为 `gdbcfg --nfsmount` 的目标。手工核对设备挂载时也应使用完整设备子目录，不需要把容器内路径换算成其它路径。隔离Ubuntu已通过真实内核NFS父目录挂载和双路写入；设备子目录直接挂载的补充验证见下文。实际海康设备网络路由和固件写入仍需上线前验证，再确认目录属主、容量与文件保留策略。

## 下载期间的副本保留

固定副本与设备写入的NFS源文件分开保存。下载和跨节点导出使用同一固定版本的读者租约；开始清理后拒绝新增读者，已有读者完成后才删除副本。文件页中的`RETIRING`表示等待读取结束，`DELETING`表示正在清理副本，完成后回到`RECEIVING`。源文件仍在时可再次导出。

删除副本前保留路径、版本令牌和配额信息；文件删除及配额收尾完成后才清除这些记录。维护过程中失败会保留待处理状态，下一轮按相同令牌恢复。该机制不删除NFS源文件，也不替代跨节点网络中断或主机故障的实机验收。

旧版本中断若留下无目录引用的副本，维护会检查过期的快照声明，按当前节点及文件ID/版本/令牌精确回收。异常声明会以`RECLAIMING`保留并写入运行日志；错误标识、符号链接及未登记文件不会被猜测删除。已登记版本的声明直接检查对应文件，旧版缺少版本时流式扫描私有快照目录。不要手动删除配额声明来释放额度，否则会丢失定位和恢复依据。

## 独立 Linux 验证

CI的`nfs-smoke`作业在临时Ubuntu主机安装真实`nfs-kernel-server`及`nfs-common`，运行`scripts/verify_nfs_service.py`。脚本使用独立临时目录和专属exports文件，验证NFSv3/v4双挂载点并行写入、客户端和服务端SHA-256、UID/GID映射、重复配置后的重新挂载以及测试文件清理。默认每路64MiB，结果保存为JSONL构件。

仅在隔离的Linux测试主机手动运行：

```sh
sudo apt-get install -y nfs-kernel-server nfs-common
sudo python3 scripts/verify_nfs_service.py
```

该验证会启用并启动主机NFS服务；结束时清理自己的挂载、导出及文件，不停止主机NFS服务。挂载或清理失败会保留相应路径并报告失败。两个客户端挂载点位于同一主机，此验证不代表真实海康设备、跨服务器网络或长时间大文件并发容量已验收。
