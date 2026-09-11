# 35设备SSH名额生命周期实测

## 范围和来源

在`35b269c`后收到持续总目标，继续已授权35设备验证。经正式API认证并新建专用资源`fc460b8b67a643a9a065146ebf24c719`、任务`966922849b9d4f9f9e9e1ded1966c72a`，不复用已软删除旧资源。设备默认ASH，未发送debug、reboot、kill，不启用NFS。

初始化按四条独立命令配置：outputClose、outputOpen、setDebug -m all -l 7 -d 111、prtHardInfo，每条延时0.3秒。Worker为local-dev，观测PID95057；PID只用于本次，不是部署配置。

## 验证结果

运行`verify_ssh_lifecycle.py --task-id 966922849b9d4f9f9e9e1ded1966c72a --worker-pid 95057 --cycles 1 --verify-idle-reconnect`，退出0且passed=true。

- 启动后正式API分配到本机节点，当前运行Mongo名额1、目标TCP FD1。
- 暂停后FD和名额0，12秒后仍为0；恢复保持runId，生成新sessionId，FD和名额恢复为1。
- 手动命令outputClose必须明确为SENT才能继续。随后11秒内42次目标TCP FD采样未超过1，再次观察到同运行新会话COLLECTING，FD与名额1。
- finally通过正式stop API收尾成功，任务STOPPED且nodeId为空。停止后按taskId检查所有run/generation的名额，不依赖可能被清除的任务运行字段。

运行ID为`0ecd9585eba949278942cd12f09166d6`。三个会话ID为`6bdfd8dd-2906-4cea-91b2-3b3487dbfef6`、`ea77eeb8-e854-4451-9efb-5a7e062cd40e`、`55ab79ee-ebe3-428d-8eaa-9b61de775a95`。

FD采样不测量设备输出，不能单独证明10秒无输出或排除其它断线原因，也不排除采样间的短暂重叠。单独运行生产默认10秒阈值的受控runtime测试，确认关闭时间不早于10秒、close-1先于factory-2且两次发送初始化；这不是实体设备内部执行结果证明。

## 自动化和审核

验证器18项、引用同一socket观察函数的重启验证器3项、连接/名额/runtime27项、默认空闲检测及重连2项，共50项通过；Ruff与diff检查通过。未修改产品运行逻辑或前端，因此不重复声称本轮全量或浏览器验收。CodeGraph同步并检查connection_counts调用影响，独立子代理审核后补齐全部TCP FD及节点校验，修正FD采样不等同无输出的证据表述。

## 归档和清理

三个会话统一为1个小时tar.gz，大小171216字节，包内仅3个普通.log文件，解压字节数分别642969、644434、642427；逐成员读至末尾验证gzip可读性和成员长度。该检查没有源端序号，不宣称源端零丢失。

确认任务停止、无SSH名额和活动日志作业后，经正式API软删除专用资源；核对deletionState=DONE及resourceDeleted后，显式传入本次3个文件ID到apply_retention的开发清理入口。归档、索引和元数据共248382字节，清理removed=3、failures=0；skipped=2来自同组成员已经批量删除后的剩余游标项。

最终数据库文件登记0、跟踪文件0、专用任务目录内文件0；Worker无设备TCP FD，RUNNING任务0，存在占用的SSH名额IP数0。保留资源软删除与操作审计，不清理其它设备文件，不改变全局保留期。
