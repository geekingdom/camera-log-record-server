---
name: camera-log-record-server-development
description: Use when any conversation concerns CameraLogRecordServer or geekingdom/camera-log-record-server, including planning, implementation, debugging, review, testing, documentation, delivery, status questions, and context-compaction recovery. Applies by default within this project; does not apply to unrelated projects.
---

# CameraLogRecordServer 工程开发规范

本工程后续所有相关对话默认使用本Skill，包括新对话、状态询问和压缩后恢复。先读取[完整开发规范](references/development-rules.md)，同一未压缩上下文已读取且文件未变化时无需重复读取。完整规范十六节均有效；本入口提供执行顺序，不替代完整规范。用户本轮明确范围与后续更正优先，不能把业务功能参数或一次性授权加入本Skill。

## 开始或恢复

1. 确定当前工程根目录；本机为`/Users/geekingdommac/VSCodeProject/CameraLogRecordServer`。在工程外讨论本工程时，仍应用本规范；读取工程文件使用其真实根路径，不能在当前无关目录执行Git或CodeGraph。
2. 核对最新用户请求/更正、工程`AGENTS.md`、`docs/implementation-status.md`、`git status --short`、最新提交和相关源码/验证产物。区分本轮所有权与其他并行工作的未提交改动。
3. 每次上下文压缩后，先读`/Users/geekingdommac/.codex/skills/post-compaction-continuity/SKILL.md`并核对目标、约束、验收条件、已完成/待完成、证据及下一步；该文件在其他机器缺失时，仍按完整规范第二节执行全部恢复步骤并说明缺失。
4. 从第一个未完成步骤继续。摘要推测重新验证；状态询问和生成摘要不替代业务目标。保留简短连续性记录，不虚构缺失历史。
5. 纯咨询、审计或状态询问按其范围回答；不因此改代码、提交、推送、部署或启动与请求无关的完整测试。

## 实现与协作

- 修改前在工程根执行`codegraph sync .`，对涉及的关键代码符号执行`codegraph impact <符号>`，再用源码/测试核实。纯文档变更无可分析符号时记录不适用，不能虚构impact结果；工具不可用时说明替代检查。
- 独立多步骤工程任务使用并行subagent，派工明确范围、产物和验收，避免重叠修改。每个subagent指定`model=gpt-5.6-terra`、`reasoning_effort=medium`；按实际运行时额度派工和复用，不写死槽位数。
- 指定模型未提供时报告限制，不擅自替换、不虚构派工；主代理继续可独立完成的工作，不以模型限制取消用户已授权任务。
- 主代理负责设计、协调、审核、集成、状态和最终交付；子代理遵守相关规范，默认只交付分配范围的产物/证据，不自行提交、推送或修改总表，除非主代理明确委派。
- 复用工程已有虚拟环境与依赖；先定位解释器和工具，不能将PATH缺失当作依赖未安装。
- 保持Python/FastAPI、Vue3/TypeScript、MongoDB及既有模块边界；中文模块/关键逻辑说明；单文件建议≤500行。不得靠压缩代码规避模块化。
- 资源生命周期、异常、并发与不确定结果有明确处理。日志/审计可关联、适度降噪且秘密脱敏。接口模型、动态文档、Python示例与实际响应同步；数据库增长、保留、引用和索引同步评估。

## 验证与收尾

按完整规范第十三节完成专项及工程基线：工程虚拟环境中的`pytest -q`、`ruff check backend tests scripts`，前端`npm test`及`npm run build`。UI/实时/下载变更增加实际浏览器与桌面移动截图；数据库事务/索引用真实Mongo；性能结论注明环境、负载、持续时间、口径。只改Skill/规范文档时执行技能结构、引用、安装入口和内容范围验证，其他工程检查由适用CI验证，不为无行为变更增加镜像式测试。

更新本轮状态和相关文档；只清理已证明属于本轮且无活动引用的模拟数据、下载副本和专属进程。保护其他并行工作；共享状态表有正在进行的编辑时，协调合并本轮条目或先写独立历史记录说明入口，不能覆盖别人的未提交进度。

## 提交与交付完成条件

工程变更默认遵守完整规范第十五节：验证、更新文档、中文提交、推送，然后等待**最终交付提交**的全部CI完成成功；失败定位修复后重新提交/等待。文档记录提交也不能用前一功能提交的CI替代。外部阻塞无法解决则报告具体阻塞、已完成与剩余项，不宣称全部完成。

仓库：`git@github.com:geekingdom/camera-log-record-server.git`。核验`scripts/install-git-hooks.sh`的钩子/模板配置。提交标题中文，正文四段均有实质中文说明并通过现有脚本：

```text
固化工程开发规范与默认Skill入口

背景：说明本次问题及约束。
变更：说明最终修改内容。
测试：说明实际执行和结果。
影响：说明兼容性、范围及剩余验证。
```

只提交本轮文件；不捎带其它工作、秘密或测试产物。不以旧提交、部分CI、本机测试或推送成功作为完成依据。最终汇报改动、验证、提交、CI、未完成项和部署边界。仅用户明确限制范围/暂停时按其指令收尾；一次CI上传例外不扩大成永久豁免。

## 安装与后续维护

工程内本目录为唯一源：`.agents/skills/camera-log-record-server-development/`，随仓库维护；工程`AGENTS.md`要求每个相关对话先读取。用户级`~/.codex/skills/camera-log-record-server-development`可链接到本目录供本机发现；`agents/openai.yaml`允许隐式调用。搬迁工程时更新链接，不能维护第二份会漂移的规范副本。加载入口是指令机制，不承诺替代运行时限制或恢复不可获得的历史。
