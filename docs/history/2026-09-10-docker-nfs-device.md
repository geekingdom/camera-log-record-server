# macOS Docker NFS 设备实测记录

## 范围与结论

本记录是 2026-09-10 在 macOS Docker Desktop 上，为 `10.41.203.35` 进行的一次临时真实设备验证，不替代 Linux 原生部署文档或生产 NFS 容量验收。设备经 `en7` 可到达本机 `10.41.203.10`；`192.168.1.103` 是另一张家庭网络接口，不能填入本次设备的 `NFS_SERVER_IP`。

设备已成功挂载：

```text
10.41.203.10:/exports/10.41.203.35
```

挂载协议为 NFSv3、TCP、`nolock`。设备触发的真实 core 已进入 Docker named volume，平台独立扫描 Worker 已通过正式 API 发现该文件。扫描 catalog 的文件大小为 6,724,543 字节，最初状态为 `RECEIVING`、源观测状态为 `STABLE`；页面显示“文件已稳定”。真实页面导出完成后为 `FROZEN`，显示“副本可下载”，不表示可以删除设备源文件。

## 实机与下载证据

- 35已默认ASH。通过现有任务 `39b00799af9144208c63b3f11b772db1` 的正式命令API和实时WS核对mount、ps；重新识别到唯一 `{Dsp_Main} /home/hikdsp` PID 2127，仅发送一次kill -6，命令ID `753344035b7645e28ca7632cdb12c011`。任务继续COLLECTING，没有额外设备SSH连接。
- 文件：`(none)/10.41.203.35-(none)-core-6-Dsp_Main-2127-1789058427.gz`。源SHA-256：`a0b4d638e0a2fb851d6272b0c5946c1b3e599238c27129638166e937c63be2e1`。
- Catalog ID `f4968cf1c7916b7bc2ddc5ef72035291`；首次发现16:49:36，源mtime16:41:00。此处差异来自扫描Worker晚于文件落盘启动，证明首次发现不能称为设备生成时间或传输结束时间。
- 主代理在真实前端资源页确认自动出现文件、状态“文件已稳定”、总数1且没有flag。页面选择文件并二次确认导出，作业 `86344e14b82647fda060d4792282ba09` 成功，页面显示“导出完成”和“副本可下载”。
- 正式API按文件名及首次发现范围查询命中1条；flag关键词和不相交时间查询均0条。
- 两路并发单文件下载均200、6,724,543字节，SHA-256均与源一致；导出内容接口也得到相同长度/摘要。Range `bytes=1048576-2097151` 返回206、1,048,576字节及正确Content-Range，其摘要 `3b46e592318cc02577d19f2bc6652f371ec47ce8e270229259fbd53a84e1fea4` 与源对应区间一致。
- API校验流式消费数据，没有写入本机下载副本；临时只读验证账户已正式API删除。真实源文件、已冻结副本和导出按既有保留策略处理，不删除NFS源。

本次gdbcfg通过正式手动命令队列执行。原local-dev节点没有NFS配置，本实验没有把其现有采集任务迁移到Docker扫描节点，因此不将本次结果作为“自动一分钟重挂”已实机验收的证据。

## 预先接入扫描后的重启实测

第二轮先确认扫描 Worker 已挂接同一 NFS 数据卷，实际 Settings 扫描间隔为默认 10 秒，再重启设备。17:03:24 认证 OFFLINE、17:04:24 认证 ONLINE，约 10.8 秒后自动恢复 COLLECTING。专用重启测试任务已停止，后续命令复用用户原采集任务。

确认实际 mount 输出为目标 NFS 挂载行后，重新执行 ps 得到 PID 2125。2026-09-10 17:11:52.274（上海时间）仅发送一次 `kill -6 2125`，命令 ID `5877571245a842319ded393190c19824`。该轮没有执行 debug，也没有建立额外 SSH 连接。

| 上海时间 | 观测事实 |
| --- | --- |
| 17:12:24.097 | 新文件首次发现，2,215,936 字节，OBSERVING |
| 17:12:34.303 | 增长至 6,725,382 字节，CHANGING |
| 17:12:44.556 | 观测窗口内未再变化，STABLE |

新文件为 `(none)/10.41.203.35-(none)-core-6-Dsp_Main-2125-1789060315.gz`，fileId `252468bdf5287ac4b8c91a4f848daa8e`。从命令提交时刻至首次发现约 31.8 秒，包含设备生成和开始写入耗时，不等同于扫描延迟；首次发现发生在文件仍增长时。源最后修改时间为 17:12:28.467，稳定确认晚约 16.1 秒；STABLE 仅代表观测结果，不是设备完成通知。

导出 `a366566ede8443a89a5147e48ae9f262` 已成功，主代理独立复核完整下载 HTTP 200、6,725,382 字节，SHA-256 与 NFS 源一致：`285c9c8c8f4f4cbc9b4a301ec102c20baefc4d37b001a458a740368a27dd3091`。Range `bytes=1048576-1114111` 返回 HTTP 206、65,536 字节及正确 Content-Range，摘要为 `8562a5a034325e3f7f7a911e9d2deb171d87265e60e47e5053d8708fdbd0a5be`，与对应源内容一致。原日志任务持续 COLLECTING，真实源与导出保留，校验未落地下载副本。

观察器曾只按 catalog 新 ID 识别新文件，误把旧文件的新版本识别为本轮结果；以上结论已按 PID 对应的精确文件名重新查询验证。旧文件 inode、大小、mtime 未变，但 ctime 改变；扫描器保留冻结版本并建立 version 2，因此旧文件出现新 ID。该情况不证明产生了第二份新 core，验证器须同时排除基线文件名并匹配本轮 PID。

新增 `scripts/verify_device_coredump.py` 的15项回归覆盖旧文件新ID、精确PID名段、跨文件状态不可混用、中文缓冲、重置、完整ps及提示符确认。该脚本尚未实机执行；以上设备证据来自主代理独立API/WS流程。若一次传输在两次扫描之间已完成，脚本要求观测CHANGING的严格模式可能超时，不能据此认定服务未收到文件。第二轮页面复核遇到Mac锁屏，未冒充浏览器验收成功；正式API查询、导出及下载均已实际通过。

## 当前运行资源

| 资源 | 当前名称/值 | 用途与保留要求 |
| --- | --- | --- |
| NFS 镜像 | `camera-nfs-kernel:probe` | 由本机现有 Debian/Python 镜像在隔离容器内安装 `nfs-kernel-server`、`nfs-common`、`rpcbind` 后固化；仅供本次 macOS 实验。 |
| NFS 容器 | `camera-nfs-server-35` | 特权内核 NFS 服务，restart policy 为 `unless-stopped`。 |
| NFS 数据卷 | `camera-nfs-probe-data` | 实际导出根挂载为容器内 `/exports`；其中 `/exports/10.41.203.35` 含真实设备文件，禁止作为开发临时数据删除。 |
| 扫描镜像 | `camera-logs-scanner:validation` | 从当前工作区源码安装依赖而成的本地验证镜像。 |
| 扫描容器 | `camera-nfs-scanner-35` | 以只读方式共享 NFS 数据卷，restart policy 为 `unless-stopped`，节点内部 API 仅发布在 `127.0.0.1:18001`。 |
| Worker 运行卷 | `camera-nfs-scanner-runtime` | 独立的 Worker 运行日志与私有快照空间，不能与 NFS 源目录混用。 |
| 节点配置 | `nfs-docker-validation` | 经正式 API 登记为 `url=http://127.0.0.1:18001`、`capacity=1`、`accepting=false`；在线但不会被调度器领取采集任务。 |
| 依赖缓存卷 | `camera-nfs-pip-cache` | 仅保存本次构建下载的 Python 包；不含设备数据。 |

NFS 容器只绑定设备可达网卡，不占用家庭网络接口：

```text
10.41.203.10:111/tcp,111/udp
10.41.203.10:2049/tcp,2049/udp
10.41.203.10:20048/tcp,20048/udp
```

容器内 `rpcinfo -p` 已确认 portmapper 为 111、NFSv3 TCP 为 2049、mountd 为 20048。`20048` 必须固定，不能让 RPC 服务随机分配后只映射 2049。

## macOS 专用限制

Docker Desktop 的 macOS bind mount 使用 VirtioFS。尽管 Worker 可读取该 bind mount，内核 `exportfs` 会拒绝导出它并报告 `does not support NFS export`。因此本实验用 LinuxKit 内的 named volume 承载 NFS 源文件，不能把宿主 `.local/logs` 或任意 macOS 路径直接作为 Docker 内核 NFS 导出。

Docker Desktop 的端口代理会把设备连接转入容器时改为高位源端口。Linux NFS 导出的默认 `secure` 限制会拒绝该请求；本次专属导出必须显式包含 `insecure`：

```text
/exports *(rw,sync,no_subtree_check,insecure,all_squash,anonuid=10001,anongid=10001)
```

`all_squash` 将设备写入映射为 `10001:10001`，供扫描 Worker 读取。该 `insecure` 例外只说明 Docker Desktop 代理下的本机实验条件；项目的 Linux 原生导出仍遵循 [NFS 部署说明](../nfs-coredump-deployment.md)，不能将此结论直接外推为生产配置变更。

## 可重复启动

下列命令只描述环境变量名称，绝不在文档、Shell 历史或 Git 中写入密钥。执行者须在受控终端中已有 `ENCRYPTION_KEY`、`BOOTSTRAP_TOKEN`、`INTERNAL_TOKEN`，并在启动前确认 `10.41.203.10` 仍是到设备的实际路由源地址。

先创建数据卷和设备目录。仅在目录不存在时执行；已有真实文件时不可重新初始化、清空或改属主：

```sh
docker volume create camera-nfs-probe-data
docker run --rm -v camera-nfs-probe-data:/exports --entrypoint sh camera-nfs-kernel:probe -ec \
  'mkdir -p /exports/10.41.203.35 && chown 10001:10001 /exports /exports/10.41.203.35 && chmod 755 /exports /exports/10.41.203.35'
```

启动 NFS 服务。启动脚本中的 `rpc.mountd` 固定在 20048，端口映射只绑定 `en7` 地址：

```sh
docker run -d --name camera-nfs-server-35 --restart unless-stopped --privileged \
  -v camera-nfs-probe-data:/exports \
  -p 10.41.203.10:111:111/tcp -p 10.41.203.10:111:111/udp \
  -p 10.41.203.10:2049:2049/tcp -p 10.41.203.10:2049:2049/udp \
  -p 10.41.203.10:20048:20048/tcp -p 10.41.203.10:20048:20048/udp \
  --entrypoint sh camera-nfs-kernel:probe -ec '
    set -e
    mount -t nfsd nfsd /proc/fs/nfsd
    printf "/exports *(rw,sync,no_subtree_check,insecure,all_squash,anonuid=10001,anongid=10001)\\n" >/etc/exports
    rpcbind -w
    rpc.nfsd 8
    exportfs -rv
    rpc.mountd -F -p 20048 >/tmp/mountd.log 2>&1 &
    trap "exportfs -u /exports || true; rpc.nfsd 0 || true; umount /proc/fs/nfsd || true; exit 0" TERM INT
    sleep infinity
  '
```

节点必须先由正式 API 登记为 `accepting=false`，不允许直接写 Mongo 的 `nodes` 或 `node_configs` 集合。登记请求使用受控环境中的 `BOOTSTRAP_TOKEN`：

```sh
curl --fail --show-error -X POST http://127.0.0.1:8000/api/v1/admin/nodes \
  -H "Authorization: Bearer ${BOOTSTRAP_TOKEN}" -H 'Content-Type: application/json' \
  --data '{"id":"nfs-docker-validation","url":"http://127.0.0.1:18001","capacity":1,"accepting":false}'
```

启动只读扫描 Worker。`MONGO_URI` 只用于本机实验的单机 Mongo 直连；不要复制到生产配置：

```sh
docker run -d --name camera-nfs-scanner-35 --restart unless-stopped \
  --entrypoint python3 -p 127.0.0.1:18001:8001/tcp \
  -v camera-nfs-probe-data:/exports:ro \
  -v camera-nfs-scanner-runtime:/var/lib/camera-logs \
  -e MONGO_URI='mongodb://host.docker.internal:27019/?directConnection=true' \
  -e DATABASE_NAME=camera_logs -e ENCRYPTION_KEY -e BOOTSTRAP_TOKEN -e INTERNAL_TOKEN \
  -e NODE_ID=nfs-docker-validation -e NODE_URL=http://127.0.0.1:18001 \
  -e NODE_PORT=8001 -e NODE_BIND_IP=0.0.0.0 -e LOG_ROOT=/var/lib/camera-logs/data \
  -e NFS_ROOT=/exports -e NFS_SERVER_IP=10.41.203.10 \
  camera-logs-scanner:validation -m camera_logs.worker
```

验证应同时覆盖 NFS 导出、扫描节点和正式 API；设备正文不应打印到终端或提交到仓库：

```sh
docker exec camera-nfs-server-35 exportfs -v
docker exec camera-nfs-server-35 rpcinfo -p
curl --fail http://127.0.0.1:18001/health
```

首轮接通验证曾为独立扫描 Worker 显式设置 2 秒扫描间隔，以尽快确认第一份真实 core 能从 named volume 进入 catalog；该结果不能作为项目默认扫描周期的证据。第二轮重启/kill 验收前，已在没有 `QUEUED`、`RUNNING` 或有效 `WRITING` coredump 导出作业时重建同一容器，移除了该环境变量覆盖。当前容器使用 `Settings` 的默认 `coredump_scan_interval_seconds=10`，保留同一节点 ID、只读 NFS volume、运行卷和 `accepting=false` 配置。第二轮发现延迟必须从设备 kill 事件开始计时，并与这份 10 秒基线对应。

## 停止与清理顺序

1. 先由设备控制流程关闭 NFS 写入并确认设备端不再挂载 `10.41.203.10:/exports/10.41.203.35`；不要在设备仍写入时停止容器或删除导出。
2. 确认平台没有进行中的 coredump 下载、冻结或导出，并保留需要交付的验证报告、文件元数据和摘要。源 core 与标志文件均不是普通开发产物。
3. 停止扫描 Worker，再停止 NFS 服务：`docker stop camera-nfs-scanner-35 camera-nfs-server-35`。确认节点心跳离线后，通过正式 API 处理节点登记；不能直接修改数据库。
4. 仅在明确批准丢弃真实源数据后，才可删除容器、`camera-nfs-probe-data` 或 `camera-nfs-scanner-runtime` 卷。默认保留这些卷。
5. 可以在确认不再重启验证后清理不含设备数据的构建缓存卷 `camera-nfs-pip-cache`、本地验证镜像和已停止容器。清理前先逐项 `docker inspect` 确认路径归属。

早期 `.local-nfs-35-export` bind-export 探针及其合成文件已清除；它从未承载真实设备数据。当前运行的两个容器均已设为 `unless-stopped`，Docker Desktop 正常启动后会尝试自动恢复，设备端实际重连仍须按设备状态另行确认。
