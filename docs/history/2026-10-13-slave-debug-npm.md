# 2026-10-13 公司npm构建与从机debug确认

## 范围与定位

公司前端Docker构建需在npm ci前设置内网Artifactory registry及strict-ssl=false；完整部署配置基于用户本机env.txt，不复制任何真实配置或凭据入库。

标准从机PSH横幅下，初始化、手动和定时debug原已共用CommandDispatcher及PshPasswordProvider。模拟复现的边界是缓存/横幅提示ASH但当前从机实际为PSH时，原先直接提前返回ALREADY_ASH，初始化和定时入口未调用解密。持续普通输出也可能干扰初始ls探测。本轮针对从机显式debug直接验证当前响应，不将这个合成边界冒充公司现场唯一根因；没有真实设备或现场失败会话供核实。

## 行为

- 公司npm源与strict-ssl=false作为Dockerfile和两种Compose的默认构建参数；.env可通过NPM_REGISTRY/NPM_STRICT_SSL覆盖。CI显式设置公网registry与strict-ssl=true，不依赖公司网络。
- 原公司.env不加参数也可生效。完整配置副本已在仓库外生成，逐字节保留原文件，再追加两个非敏感npm参数，权限0600；原env.txt未改动。
- 从机必须完成dbclient目标登录，然后唯一接收器确认交接横幅后才执行初始化或开放手动/定时命令；最多等待10秒，失败关闭当前连接并由运行实例重连。
- 从机1至3的显式debug不因缓存ASH而提前成功，也不以普通日志中的目录词代替当前响应。PSH挑战交由既有公共解密服务，返回密码只发送给当前从机。已在ASH的debug-not-found响应表示无需解密，不发送口令。
- 全平台只使用debug，不再发送旧备用切换命令。明确不支持debug时记录DEBUG_UNSUPPORTED；仍可按既有Ctrl-C/ls恢复策略确认已退出密码输入状态。command_safe表示可安全发送命令，不代表必须处于ASH；不改变PSH下已有手动命令恢复语义。
- 保持唯一接收器及命令发送队列；SSH从机和Telnet串口仍不启用资源CPU/内存及Coredump监控。

## 验证与边界

- 从机1至3 × 初始化/手动/定时 × 标准PSH/陈旧ASH横幅；分包挑战、后续初始化顺序、单接收器、已是ASH不调用解密、延迟横幅交接、旧切换命令禁用回归。
- 相关72项通过，前端190项和生产构建通过，部署61项及Compose默认值/覆盖展开通过；最终后端全量1811项及Ruff通过，完整CI按提交SHA核验。
- 本机Docker Hub基础镜像manifest拉取超时，尚未验证公司镜像源的真实npm下载；公网Docker构建交由CI验证。
- 公司实机与公司HTTP解密服务端到端验收仍待现场部署；此轮使用受控模拟会话，解密HTTP协议沿用既有回归。
