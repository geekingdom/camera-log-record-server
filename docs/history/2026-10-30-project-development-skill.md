# 工程开发规范Skill安装记录

## 范围与唯一源

用户确认将十六类非功能性开发规范制作成工程Skill，并要求后续所有相关对话默认使用。唯一源位于`.agents/skills/camera-log-record-server-development/`，完整规范在`references/development-rules.md`；`SKILL.md`提供开始/恢复、CodeGraph、子代理、验证和完整CI交付的执行入口。未混入角色、日志分片等具体功能要求。

工程`AGENTS.md`明确要求所有相关新对话、状态询问及压缩恢复先读取Skill和完整规范；`agents/openai.yaml`配置`allow_implicit_invocation: true`。本机`~/.codex/skills/camera-log-record-server-development`链接到工程唯一源，不维护漂移副本。其它机器可通过工程AGENTS的相对路径读取；需要技能目录发现时再建立本机链接。

## 已执行验证

- 修改前执行`codegraph sync .`成功；纯Skill/Markdown/YAML变更没有适用代码符号，impact不适用。
- 使用工程`.venv/bin/python`运行Skill Creator的`quick_validate.py`，结果`Skill is valid!`。
- 对照上一轮用户要求制作的正文，十六节完整保留；仅将状态表绝对路径改成工程相对路径说明。
- Skill引用可解析，YAML允许隐式调用，本机链接与工程源指向同一目录；已检查已知凭据字面量未混入文件。
- `git diff --check`通过；核验Git钩子及中文提交模板已配置。
- 本轮未修改并行工程工作的源码、测试或共享状态总表。该总表在其他任务中编辑，本轮规范安装状态单独记录于此，避免覆盖进行中进度。

## 行为验证与限制

历史原始用户反馈已证明缺少统一入口时发生过跨对话漏掉提交/等待CI和摘要目标漂移。已针对这些失败加入必读入口与明确完成条件。

本次未执行新的独立subagent压力测试：用户指定`gpt-5.6-terra / medium`，当前会话不提供该模型选项，未擅自替换。结构及路由检查不代表实测了新会话行为；当前技能清单也不会因写入文件而自动改写。新对话依赖工程AGENTS必读入口及技能目录发现，已有对话须显式重读入口；必要时重开对话/应用以刷新目录。

Skill是持久指令与发现入口，不能突破运行时并发/模型限制，也不能恢复已不存在的历史。GitHub完整CI以本次最终提交实际运行结果及最终交付消息为准，不能引用旧功能提交作为本次验收。
