"""API 目录自身的返回合同，递归模型通过命名引用表达而不截断嵌套含义。"""

from camera_logs.common.api_contracts.response_schema import array, error_schema, field, obj

JSON_VALUE = field("any", "任意 JSON 值（对象、数组、字符串、数值、布尔或 null）；业务字段含义参照该操作的请求或响应合同。", nullable=True)
SCHEMA_REF = {"$ref": "#/components/schemas/ResponseSchema", "description": "递归的 JSON Schema 定义，字段释义与 ResponseSchema 相同。"}
SCHEMA_PROPERTIES = {
    "$ref": field("string", "被引用模型路径，末段是 schemas 字典中的模型名称。"),
    "type": field("string", "JSON 数据类型；any 表示依具体操作而变化的 JSON 值。"),
    "title": field("string", "模型或字段的标题。"),
    "description": field("string", "中文业务含义、单位或适用条件。"),
    "format": field("string", "值格式，如 date-time、date。"),
    "nullable": field("boolean", "是否允许 JSON null。"),
    "properties": field("object", "对象的字段名到字段定义的映射。", additionalProperties=SCHEMA_REF),
    "required": array("对象中必须存在的字段名。", field("string", "当前对象的必需字段名。")),
    "items": SCHEMA_REF,
    "anyOf": array("满足任一分支即可的备选类型，例如可空联合类型。", SCHEMA_REF),
    "oneOf": array("应满足恰好一种形态的备选响应，如不同 WebSocket 消息。", SCHEMA_REF),
    "allOf": array("必须同时满足的组合定义。", SCHEMA_REF),
    "additionalProperties": {"description": "动态字典值的递归模型，或布尔值表示是否允许额外字段。", "anyOf": [
        SCHEMA_REF, field("boolean", "true 允许额外字段，false 拒绝。"),
    ]},
    "enum": array("允许的固定值集合。", JSON_VALUE),
    "const": JSON_VALUE | {"description": "唯一允许的固定 JSON 值。"},
    "default": JSON_VALUE | {"description": "缺省值；不表示响应一定返回此值。"},
    "examples": array("模型的虚拟示例值。", JSON_VALUE),
    "example": JSON_VALUE | {"description": "字段虚拟示例。"},
    "pattern": field("string", "字符串必须匹配的正则表达式。"),
    "deprecated": field("boolean", "模型或字段是否已废弃。"),
    "readOnly": field("boolean", "只用于响应，不允许作为写入参数。"),
    "writeOnly": field("boolean", "只用于请求，不应出现在响应。"),
    "$defs": field("object", "当前模型的局部命名定义。", additionalProperties=SCHEMA_REF),
}
for name, meaning in {
    "minimum": "数值下界（包含）。", "maximum": "数值上界（包含）。",
    "exclusiveMinimum": "数值下界（不包含）。", "exclusiveMaximum": "数值上界（不包含）。",
    "multipleOf": "数值必须为此数的倍数。", "minLength": "字符串最少字符数。",
    "maxLength": "字符串最多字符数。", "minItems": "数组最少元素数。", "maxItems": "数组最多元素数。",
    "minProperties": "对象最少字段数。", "maxProperties": "对象最多字段数。",
}.items():
    SCHEMA_PROPERTIES[name] = field("number", meaning)
SCHEMA_PROPERTIES["uniqueItems"] = field("boolean", "数组元素是否必须唯一。")
RESPONSE_SCHEMA = obj("递归 JSON Schema 元数据；任意深度子模型沿用此表。", SCHEMA_PROPERTIES, ())
RESPONSE_CONTRACT = obj("单个操作的完整响应协议。", {
    "kind": field("string", "json、empty、binary、text 或 websocket。"),
    "description": field("string", "响应协议总说明。"),
    "schema": SCHEMA_REF | {"nullable": True, "description": "成功响应字段定义；empty 无正文时为 null。"},
    "conditional": array("状态相关字段、错误及协议边界说明。", field("string", "一项适用条件。")),
    "errors": array("此操作可能出现的错误响应。", obj("一种错误状态。", {
        "status": field("integer", "HTTP 错误状态码。"),
        "when": field("string", "该错误出现的条件。"),
        "schema": SCHEMA_REF,
    }, ("status", "when", "schema"))),
    "errorSchema": SCHEMA_REF | {"nullable": True, "description": "共享错误报文结构；无适用错误时可为 null。"},
}, ("kind", "description", "schema", "errors", "errorSchema"))
PARAMETER = obj("路径、查询或请求头参数。", {
    "name": field("string", "参数名称。"),
    "in": field("string", "参数位置：path、query、header 或 cookie。"),
    "required": field("boolean", "请求中是否必须提供。"),
    "schema": SCHEMA_REF,
    "description": field("string", "参数中文含义。"),
    "example": JSON_VALUE,
    "deprecated": field("boolean", "参数是否已废弃。"),
}, ("name", "in", "schema", "description", "example"))
REFERENCE = obj("当前版本全部公开接口的机器可读文档。", {
    "version": field("string", "文档目录结构版本。"),
    "guides": array("调用指南。", obj("指南条目。", {
        "title": field("string", "指南标题。"), "text": field("string", "完整中文调用约定。"),
    }, ("title", "text"))),
    "operations": array("HTTP 接口和 WebSocket 操作。", obj("公开操作定义。", {
        "id": field("string", "方法加路由构成的唯一标识。"),
        "method": field("string", "HTTP 方法或 WS。"),
        "path": field("string", "包含路径参数占位符的路由。"),
        "title": field("string", "接口中文名称。"),
        "group": field("string", "接口业务分组。"),
        "description": field("string", "行为、前置条件和状态变化说明。"),
        "permission": field("string", "权限作用域及所有权要求；公开下载说明无需认证。"),
        "headers": field("object", "请求头名称到示例值的映射。", additionalProperties=field("string", "该请求头示例值，不包含实际凭据。")),
        "parameters": array("路径、查询和头参数。", PARAMETER),
        "requestSchema": SCHEMA_REF | {"nullable": True, "description": "请求 JSON 的模型；无 JSON 请求时 null。"},
        "requestExample": JSON_VALUE | {"description": "本接口虚拟请求示例；具体字段见 requestSchema，可能为 null。"},
        "pythonExample": field("string", "可复制的 Python 调用代码；页面不会执行。"),
        "responseStatus": field("integer", "主要成功状态码，WebSocket 为 101。"),
        "responseExample": JSON_VALUE | {"description": "本接口虚拟返回示例；全部字段含义见 responseContract。"},
        "responseContract": RESPONSE_CONTRACT,
    }, ())),
    "schemas": field("object", "模型名到 JSON Schema 的映射，包括递归模型 ResponseSchema。", additionalProperties=SCHEMA_REF),
    "errorExample": error_schema(),
}, ("version", "guides", "operations", "schemas", "errorExample"))
