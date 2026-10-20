"""检查接口声明中的中文说明；文档不再按字段名拼接独立业务释义。"""

import re
from copy import deepcopy
from typing import Any

# FastAPI 自带的校验模型没有中文元数据；业务模型必须直接声明 Field(description=...)。
_FRAMEWORK_FIELDS = {
    "detail": "框架输入校验问题列表；平台实际错误正文以各接口错误响应表为准。",
    "loc": "无效参数在路径、查询或请求体中的位置。",
    "msg": "输入值不符合模型约束的原因。",
    "type": "Pydantic 输入校验错误类型代码。",
    "input": "触发输入校验失败的原始值。",
    "ctx": "输入校验器附带的约束上下文。",
}


def _has_chinese_description(value: Any) -> bool:
    """拒绝空白或仅英文占位说明；业务语义是否准确仍由源码审查和测试核对。"""
    return isinstance(value, str) and bool(re.search(r"[\u4e00-\u9fff]", value))


def _validate(schema: dict[str, Any], location: str, schemas, *, framework=False) -> dict[str, Any]:
    """递归验证字段说明，漏写时拒绝生成文档，避免泛化文字掩盖遗漏。"""
    value = deepcopy(schema)
    for name, child in value.get("properties", {}).items():
        if not child.get("description") and child.get("$ref"):
            child["description"] = schemas.get(child["$ref"].rsplit("/", 1)[-1], {}).get("description")
        if not _has_chinese_description(child.get("description")):
            if framework and name in _FRAMEWORK_FIELDS:
                child["description"] = _FRAMEWORK_FIELDS[name]
            else:
                raise RuntimeError(f"API 模型字段缺少中文说明：{location}.{name}")
        value["properties"][name] = _validate(child, f"{location}.{name}", schemas, framework=framework)
    if isinstance(value.get("items"), dict):
        value["items"] = _validate(value["items"], location + "[]", schemas, framework=framework)
    if isinstance(value.get("additionalProperties"), dict):
        value["additionalProperties"] = _validate(value["additionalProperties"], location + ".{键}", schemas, framework=framework)
    for union in ("anyOf", "oneOf", "allOf"):
        if isinstance(value.get(union), list):
            value[union] = [_validate(item, location, schemas, framework=framework) for item in value[union]]
    return value


def described_schemas(schemas: dict[str, Any]) -> dict[str, Any]:
    """保留接口模型自身说明，仅翻译框架内置校验模型。"""
    return {name: _validate(schema, name, schemas, framework=name in {"HTTPValidationError", "ValidationError"})
            for name, schema in schemas.items()}


def describe_parameter(parameter: dict[str, Any]) -> str:
    """参数说明来自实际 Query/Path 声明，未声明中文说明直接失败。"""
    if not _has_chinese_description(parameter.get("description")):
        raise RuntimeError(f"API 参数缺少中文说明：{parameter.get('name')}")
    return parameter["description"]
