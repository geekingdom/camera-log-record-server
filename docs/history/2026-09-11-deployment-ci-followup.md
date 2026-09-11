# 部署CI后续核对

代码起点9124548；运行[34568213239](https://github.com/geekingdom/camera-log-record-server/actions/runs/34568213239)已完成。

## 实际结果

backend、frontend、native-smoke、component-smoke、nfs-smoke、commit-messages六项成功。container-smoke的一键部署及重复部署步骤成功，说明此前Settings拒绝整数字符串的启动问题已通过真实Linux部署验证。

container-smoke随后在verify_node_registration.py第22行失败：reportedUrl与脚本默认http://worker:8001不一致。源码核对确认Compose实际默认节点端口18081，且支持.env覆盖。该断言前的在线、未登记及空闲节点断言均通过。不能将脚本旧预期解释为节点离线，也不能跳过尚未执行的后续容器业务链路。容器与卷清理步骤成功。

## 节点预期配置修复

verify_node_registration.py不再硬编码节点ID和URL。缺少显式参数时，读取docker compose config --format json中的worker最终NODE_ID/NODE_URL，确保.env和Compose端口展开是验证依据，而非从API响应自证。保留显式参数覆盖，并允许指定项目、Compose文件和环境文件；配置命令有界等待，失败不打印含凭据的完整配置。

主代理复查默认18081、自定义19081、显式覆盖及坏JSON/缺服务路径；相关测试与部署测试通过。新提交完整容器链路仍需远端执行，不仅凭配置解析测试宣称端到端成功。

最终节点配置7项及部署20项合计27项通过，Ruff/diff通过；CodeGraph影响限于节点登记验证器。真实Docker Compose以/dev/null环境文件和合成凭据展开，默认返回compose-worker-1/http://worker:18081，自定义NODE_PORT=19081及节点ID返回custom-collector/http://worker:19081；没有启动容器或生成数据卷。

## 持续验收补充

手动设备认证审计的真实Mongo验证器加入container-smoke，与既有事务验证采用相同stdin执行方式。本机以stdin执行六种故障组合通过，随机数据库与临时文件已删除。部署测试20项、工作流结构解析及diff检查通过。该步骤尚待新提交远端运行，不把本机结果写成CI结果。

## 修复提交的远端结果

10579f3对应[CI34569446838](https://github.com/geekingdom/camera-log-record-server/actions/runs/34569446838)七项全部成功。节点登记、手动认证审计及多路真实协议验收步骤成功；日志中8路分别verified=true，最终cleanupVerified=true、passed=true。合成采集参数为8路、每路1200行/秒、60秒，运行中并发实时订阅和搜索。隔离容器与测试卷清理步骤成功。原生、独立组件、NFS及前端浏览器作业同时通过。此前上文的“待新提交”已在此运行完成，不再作为当前阻塞；物理多机及500路全天验收仍未完成。
