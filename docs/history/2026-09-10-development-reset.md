# 本机开发环境重建

2026-09-10用户明确说明工程仍在开发调试阶段，不需要兼容本机旧服务和旧数据，并授权直接删除。此次授权仅用于本工程本机环境；正式环境与其它项目不在范围内。

## 清理前确认

- 仓库：`/Users/geekingdommac/VSCodeProject/CameraLogRecordServer`。
- `camera-log-record-api`、`camera-log-record-worker`为本仓库动态launchctl任务，无持久plist。
- Mongo进程使用本仓库`.local/runtime`二进制与`.local/mongodb`目录，仅绑定`127.0.0.1:27019`，业务库为`camera_logs`；任务1961条，活动任务和日志作业均为0。
- 两个NFS容器及三个卷的本工程创建记录见[设备实测记录](2026-09-10-docker-nfs-device.md)。其`filemanageplatform`标签来自固化的基础镜像，不能据此判断当前容器属于其它项目；已结合镜像标签、命令记录和卷挂载关系复核归属。

## 已执行

- 注销旧API/Worker，正常关闭Mongo，确认对应进程与8000/8001/27019监听均结束。
- 删除`.local`内旧采集日志、Mongo数据、服务日志与实验产物；保留运行依赖`runtime`和开发配置`secrets`。
- 删除`camera-nfs-scanner-35`、`camera-nfs-server-35`及专属卷`camera-nfs-probe-data`、`camera-nfs-scanner-runtime`、`camera-nfs-pip-cache`。其中旧设备core也在本次明确授权的删除范围内。
- 保留真正的`filemanageplatform-*`和`camera-test-platform-*`容器/卷；未按宽泛标签或名称前缀清理Docker。
- 重建本地单成员`rs0`，生成新的数据库管理员随机密码及成员密钥，开启认证，仍只绑定27019回环端口。更新权限0600的开发`.env`，未输出或提交凭据。
- 重建API/Worker动态服务；新管理员为`admin`，初始密码`asdf!234`。旧用户、资源、任务、模板和历史记录不再保留。

## 验证与当前状态

- API与Worker健康检查200，5173前端200；带浏览器请求校验头的真实管理员登录经5173代理返回200，随后退出测试会话。
- 当前节点仅`local-dev`，`HEALTHY`、`HOST/OK`，CPU、内存、网络上传和下载速率均非空。旧NFS验证节点不再展示；重新测试NFS需重新部署对应实验服务。
- `.local`由约3016.8MiB缩减至约424MiB，主要为保留的Mongo运行时；净减少约2.5GiB，不含已删除Docker卷。
- 原生Mongo关闭后pid文件仍可能留存，本次通过实际PID退出与端口释放确认关闭，没有把pid文件是否存在当作进程存活证明。
- 临时重建脚本已删除；没有发送设备debug、reboot或kill命令。

此记录仅证明本机开发环境重建，不替代Linux完整平台与跨机Worker部署验收。后续状态以[当前状态总表](../implementation-status.md)为准。
