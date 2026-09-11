# SSH连接名额持续验收

代码起点10579f3。对照用户每设备最多五个SSH连接、主动关闭及暂停释放要求，独立审查确认运行实现与单测已存在，但真实Mongo跨进程和实际SSH socket验证尚未纳入CI。

## 本轮变更

container-smoke增加verify_ssh_admission.py及verify_ssh_socket_admission.py。前者复制到API容器/tmp后以真实文件执行，以便multiprocessing spawn加载子进程入口；后者沿用stdin方式。两个验证器只使用随机数据库，socket验证器仅监听回环随机端口，不访问34/35设备，不写采集日志。

## 本机验证

- 跨进程真实Mongo验证通过：同IP不同端口共享五名额，第六路拒绝，陈旧未知占位不按时间释放，旧运行精确释放不影响后继。
- 真实回环SSH与Mongo验证通过：acceptedShells=5、rejectedSixth=true、reopenedAfterClose=true，serverConnections=0、finalClaims=0。
- 两个脚本finally均删除并确认临时数据库；CI工作流结构解析及diff检查通过。

这些检查为持续防回归补充，不代表真实物理多机故障隔离或500路全天容量验收。

后续已核实7d5e383对应CI34570026428七项全部成功，包含新增的真实Mongo跨进程名额和真实SSH socket步骤。这是该提交的远端执行证据，不借用前一提交的CI结果。
