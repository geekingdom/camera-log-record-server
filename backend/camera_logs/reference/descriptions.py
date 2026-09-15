"""维护开放接口目录的中文字段语义，并在生成目录时校验完整性。

Pydantic 的 ``Field`` 约束负责接口校验；这里集中维护面向调用方的业务说明，
避免把前端展示文案分散到页面字典中。新公开字段若未登记会在目录生成时失败，
从而强制同时补齐文档语义与测试，而不是回退为泛化提示。
"""

from copy import deepcopy
from typing import Any

FIELD_DESCRIPTIONS = {
    "accepting": "节点是否接受调度器分配新的采集任务。",
    "active": "资源当前是否存在有效的 Coredump 监控负责人。",
    "allowPartial": "小时归档存在缺失片段时，是否仍允许生成部分下载包。",
    "authType": "访问海康 ISAPI 设备信息接口时使用的 HTTP 认证方式。",
    "autoStart": "创建采集任务后是否立即提交启动操作。",
    "capacity": "该 Worker 节点允许同时承载的采集任务数量。",
    "clearPassword": "编辑任务时是否明确清除已保存的设备登录密码。",
    "clusterCapacity": "平台允许同时运行的全部采集任务总上限。",
    "command": "发送给设备的一条单行命令正文。",
    "confirmIsolation": "是否确认旧运行实例已完成外部隔离，仅管理员可设为 true。",
    "confirmation": "执行节点隔离确认时必须填写的固定确认词。",
    "ctx": "校验失败的补充上下文，仅在错误响应中出现。",
    "currentPassword": "当前登录账户的原密码，用于验证本人改密请求。",
    "delaySeconds": "命令写入连接后额外等待的秒数。",
    "description": "资源、任务或命令模板的补充业务说明。",
    "detail": "错误响应中的具体错误详情或详情列表。",
    "displayName": "平台账户在界面和审计记录中显示的名称。",
    "enableCoredumpMonitor": "海康设备资源是否启用 Coredump NFS 监控；仅有正在采集的 SSH 或 Telnet 设备任务时执行。",
    "enableResourceMonitor": "海康设备资源是否启用每分钟 CPU 与内存采样；复用正在采集的设备连接。",
    "resourceMonitor": "管理员维护的设备 CPU 与内存采样配置，保存后下一轮采样生效。",
    "recordRetention": "审计、运行事件及已结束运行明细的保留天数；0禁用对应维护，活动及不确定引用继续保护。",
    "items": "系统指标采样规则列表，每项指定命令、数值正则、名称和单位。",
    "pattern": "用于匹配设备响应的正则表达式；数值规则使用第一个捕获组。",
    "unit": "指标的显示单位，KB 表示内存，% 表示百分比。",
    "processDiscoveryCommand": "获取设备进程列表的单行命令，默认 ps。",
    "processRules": "从进程命令列识别目标进程的规则列表。",
    "processStatusCommand": "读取进程状态的命令模板，唯一的 {pid} 将替换为已验证的正整数进程 ID。",
    "processValuePattern": "从进程状态响应中提取内存值的正则，第一个捕获组为 KB 数值。",
    "nameGroup": "可选的进程正则捕获组编号，用于生成动态进程名称；为空时使用规则名称。",
    "enabled": "账户或来源 IP 白名单策略是否启用。",
    "encoding": "接收设备输出时采用的字符编码名称。",
    "end": "时间范围结束时间，采用带时区 ISO 8601，且不包含该时刻。",
    "evidence": "确认旧实例隔离所依据的工单、操作记录或其他可核验证据。",
    "expiresInDays": "服务账号从创建或更新起可使用的天数；null 表示永久有效。",
    "fileIds": "需要冻结并导出的 Coredump 文件标识列表。",
    "hourIds": "需要加入日志下载作业的逻辑小时归档标识列表。",
    "id": "对象、命令配置或节点的稳定唯一标识。",
    "initialCommands": "连接登录成功后按数组顺序执行的初始化命令配置。",
    "input": "校验失败时服务端接收到的输入片段，仅在错误详情中出现。",
    "intervalSeconds": "同一条定时命令两次发送之间等待的秒数。",
    "ip": "设备资源、串口服务器或采集连接目标的 IPv4/IPv6 地址。",
    "isAdmin": "账户是否为管理员；创建普通子账户时只能为 false。",
    "isGeneralNode": "是否为可接收全部设备资源的通用节点，默认 true；false 时按资源地址规则准入并优先分配。",
    "resourceNetworks": "非通用节点允许接入的资源 IP 或 CIDR 列表，匹配所属资源地址而非串口连接地址。",
    "keyword": "日志检索的字面关键词；空字符串按时间范围读取原始日志片段。",
    "kind": "设备资源类别：海康网络设备或串口服务器。",
    "label": "来源 IP 白名单规则的可读名称。",
    "latestAt": "连续相同成功认证聚合区间的最近一次观测时间；旧单次记录回退为 createdAt。",
    "loc": "校验失败字段在请求中的位置路径，仅在错误详情中出现。",
    "loginPrompt": "Telnet 登录阶段识别用户名输入提示符的文本。",
    "mountStatus": "Coredump NFS 在设备侧最后一次观测到的挂载状态。",
    "msg": "校验或错误详情中的人类可读消息。",
    "name": "资源、任务、模板、节点或服务账号的用户可识别名称。",
    "network": "允许访问平台的单个 IP 地址或 CIDR 网段。",
    "newPassword": "要设置给当前登录账户的新密码。",
    "newline": "发送命令时自动附加的换行符类型。",
    "ownerTask": "当前负责资源级 Coredump 监控的采集任务摘要。",
    "occurrenceCount": "连续相同成功认证状态合并后的观测次数；旧单次记录为 1。",
    "password": "用于登录设备、认证资源或创建账户的密码；响应永不回传。",
    "passwordPrompt": "Telnet 登录阶段识别密码输入提示符的文本。",
    "port": "SSH、Telnet 设备或串口服务器的连接端口。",
    "prompt": "命令发送后需要等待设备输出匹配的可选提示符。",
    "protocol": "采集连接协议：SSH、Telnet 设备或 Telnet 串口。",
    "resourceId": "所属设备资源的稳定唯一标识。",
    "retentionDays": "日志与归档元数据在平台中保留的天数。",
    "rules": "来源 IP 白名单规则列表，启用策略时至少包含一项。",
    "scheduledCommands": "按各自次数和间隔独立执行的定时命令配置列表。",
    "scopes": "账户或 IP 规则被授予的平台权限标识列表。",
    "serialServerResourceId": "Telnet 串口任务可选关联的已登记串口服务器资源标识。",
    "sshTarget": "SSH 日志采集任务类型：HOST 为主机，SLAVE_1、SLAVE_2、SLAVE_3 分别为从机 1、2、3；仅 SSH 可选择从机。",
    "sharedWith": "可读取此命令模板的指定有效用户标识列表。",
    "sharedWithAll": "是否将模板共享给全部有效平台用户，仅管理员可启用。",
    "sourceTemplateId": "创建或编辑任务时复制命令配置的来源模板标识。",
    "sourceTemplateVersion": "复制命令配置时记录的来源模板版本号。",
    "start": "时间范围开始时间，采用带时区 ISO 8601，且包含该时刻。",
    "taskId": "目标日志采集任务的稳定唯一标识。",
    "timeoutSeconds": "启用提示符等待时允许等待设备响应的最长秒数。",
    "totalExecutions": "该定时命令在一次任务运行中最多发起的发送次数。",
    "type": "事件类型或校验错误类型标识，具体含义由当前接口上下文决定。",
    "url": "Worker 节点供平台回调和代理访问的 HTTP(S) 根地址。",
    "userId": "服务账号绑定并实时继承权限的既有平台用户标识。",
    "username": "登录设备或平台账户使用的用户名。",
    "version": "当前配置版本号，用于乐观锁并发修改校验。",
}

# 少数接口的请求体是否存在本身承载业务状态转换，需覆盖路由简短 docstring 的目录说明。
OPERATION_DESCRIPTIONS = {
    ("POST", "/api/v1/resources/{identifier}/authenticate"): (
        "空请求体仅使用服务端已保存的密文凭据认证，并刷新资源健康状态；认证失败会记录状态并可能停止关联任务，"
        "但不推进周期认证失败计数。该操作要求 resources:write、tasks:control 及资源所有权；非管理员不能影响其他用户关联的任务。"
        "携带完整 ResourceInput 时仅用于编辑器凭据预览，不改变保存资源的健康状态或任务，因此不要求 tasks:control。"
    ),
}

PARAMETER_DESCRIPTIONS = {
    "beforeFileId": "缺口前最后一个可靠帧所在的日志文件标识。",
    "beforeOffset": "缺口前可靠帧的结束字节偏移，也是待补读的起点。",
    "beforeSessionId": "缺口前可靠帧所属采集会话标识，必须与文件目录一致。",
    "afterFileId": "缺口后第一个可靠帧所在的日志文件标识。",
    "afterOffset": "缺口后可靠帧的起始字节偏移，不包含该偏移后的已展示内容。",
    "afterSessionId": "缺口后可靠帧所属采集会话标识，允许与缺口前不同。",
    "action": "按审计操作类型筛选，例如 create_task 或 control:PAUSED。",
    "actor": "按执行该审计操作的平台用户标识筛选。",
    "clientIp": "按实际访问平台的调用方来源 IP 筛选请求记录。",
    "cursor": "上页返回的稳定分页游标；管理事件传空字符串开启首屏游标查询，续页使用nextCursor并保持原筛选条件。不能与大于1的page同用。",
    "commandId": "按某一条定时命令配置标识筛选执行记录。",
    "createdBy": "按资源、任务或模板的创建用户标识筛选。",
    "date": "按北京时间自然日筛选任务小时归档。",
    "end": "查询范围结束时间，带时区 ISO 8601 且不包含该时刻。",
    "identifier": "路径所指资源、作业、文件、命令或操作的唯一标识。",
    "identityChanged": "仅返回身份信息是否发生变化的认证记录。",
    "includeDeleted": "是否在列表中包含已经软删除的资源或模板。",
    "includeTotal": "管理事件游标查询是否额外统计精确总数，默认false并返回total:null；统计会增加数据库扫描成本。",
    "ip": "按设备资源或节点的精确网络地址筛选。",
    "kind": "按设备资源类别或命令执行类别筛选。",
    "level": "按事件告警级别筛选：INFO、WARNING 或 ERROR。",
    "limit": "单次读取日志内容的最大字节数。",
    "method": "按 HTTP 请求方法筛选请求记录。",
    "model": "按海康设备型号的字面子串筛选资源。",
    "name": "按资源名称或 Coredump 文件名的字面子串筛选。",
    "nodeId": "按 Worker 节点标识筛选运行事件。",
    "node_id": "路径中指定要管理或隔离的 Worker 节点标识。",
    "offset": "从日志文件开头开始读取的字节偏移量。",
    "outcome": "按事件处理结果筛选：成功、失败、处理中、取消或未知。",
    "page": "从 1 开始的分页页码。",
    "pageSize": "每页返回的记录数，受接口允许的最大值限制。",
    "receivedFrom": "仅返回服务器首次接收时间不早于该时刻的 Coredump 文件。",
    "receivedTo": "仅返回服务器首次接收时间早于该时刻的 Coredump 文件。",
    "requestId": "按平台分配的请求追踪编号筛选审计或请求记录。",
    "resourceId": "按所属设备资源标识筛选采集任务或日志任务选择结果。",
    "resource_id": "路径中指定要查询的设备资源标识。",
    "result": "按海康设备认证结果筛选认证历史。",
    "route": "按 API 路由模板筛选请求记录。",
    "search": "按资源名称、地址、型号、序列号或软件版本进行综合检索。",
    "softwareVersion": "按设备软件版本的字面子串筛选资源。",
    "start": "查询范围开始时间，带时区 ISO 8601 且包含该时刻。",
    "status": "按 HTTP 响应状态码筛选请求记录。",
    "subSerialNumber": "按设备短序列号的字面子串筛选资源。",
    "taskId": "按日志采集任务标识筛选相关事件或列表。",
    "taskLimit": "资源列表中每个资源内嵌任务摘要的最大返回数量。",
    "task_id": "路径中指定要操作、读取或订阅的日志采集任务标识。",
    "type": "按运行事件类型筛选，例如 CONNECTION_GAP。",
    "version": "删除或更新对象时必须提供的当前版本号。",
}


def _annotate_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """递归为 OpenAPI 模型属性补齐说明，未登记字段立即暴露为开发错误。"""
    result = deepcopy(schema)
    for name, field in result.get("properties", {}).items():
        if not field.get("description"):
            try:
                field["description"] = FIELD_DESCRIPTIONS[name]
            except KeyError as exc:
                raise RuntimeError(f"开放接口字段缺少中文说明: {name}") from exc
        result["properties"][name] = _annotate_schema(field)
    if isinstance(result.get("items"), dict):
        result["items"] = _annotate_schema(result["items"])
    for union in ("anyOf", "oneOf", "allOf"):
        if isinstance(result.get(union), list):
            result[union] = [_annotate_schema(item) for item in result[union]]
    return result


def described_schemas(schemas: dict[str, Any]) -> dict[str, Any]:
    """返回不修改 FastAPI 原始 OpenAPI 缓存的、已补齐说明的组件副本。"""
    return {name: _annotate_schema(schema) for name, schema in schemas.items()}


def describe_parameter(parameter: dict[str, Any]) -> str:
    """为每个路径或查询参数取得正式中文语义，禁止页面生成泛化兜底。"""
    if parameter.get("description"):
        return parameter["description"]
    name = parameter.get("name", "")
    try:
        return PARAMETER_DESCRIPTIONS[name]
    except KeyError as exc:
        raise RuntimeError(f"开放接口参数缺少中文说明: {name}") from exc


def describe_operation(method: str, path: str, fallback: str) -> str:
    """返回需要额外表达请求体语义的正式操作说明，其余路径沿用 FastAPI 描述。"""
    return OPERATION_DESCRIPTIONS.get((method, path), fallback)
