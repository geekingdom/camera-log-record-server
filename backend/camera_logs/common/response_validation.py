"""输出有界响应校验诊断，仅保留合同字段路径和标准错误码，不记录业务值。"""

from collections.abc import Mapping
from functools import lru_cache
from typing import Any

from fastapi.exceptions import ResponseValidationError
from pydantic import TypeAdapter
from pydantic_core import ErrorType

_ERROR_TYPES = frozenset(TypeAdapter(ErrorType).json_schema()["enum"])
_HIDDEN_LOCATION = "[未声明字段或动态键]"


@lru_cache(maxsize=256)
def _response_schema(model: Any) -> dict[str, Any]:
    """缓存同一响应模型的 JSON Schema，避免失败请求重复编译合同。"""
    return TypeAdapter(model).json_schema()


def _resolved(schema: Mapping[str, Any], definitions: Mapping[str, Any]) -> Mapping[str, Any] | None:
    """只解析本地定义引用；异常引用不猜测其字段路径。"""
    seen = set()
    while isinstance(schema.get("$ref"), str):
        reference = schema["$ref"]
        if not reference.startswith("#/$defs/") or reference in seen:
            return None
        seen.add(reference)
        schema = definitions.get(reference.removeprefix("#/$defs/"))
        if not isinstance(schema, Mapping):
            return None
    return schema


def _branches(schema: Mapping[str, Any], definitions: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    """展开联合分支；后续仅在每个候选分支均证明字段静态时才显示。"""
    resolved = _resolved(schema, definitions)
    if resolved is None:
        return []
    choices = resolved.get("anyOf", resolved.get("oneOf"))
    if not isinstance(choices, list):
        return [resolved]
    return [branch for item in choices if isinstance(item, Mapping)
            for branch in _branches(item, definitions)]


def _location(error_location: Any, schema: Mapping[str, Any]) -> list[str]:
    """仅展示可由当前合同分支证明的字段和数组下标，其余路径严格隐藏。"""
    if not isinstance(error_location, (tuple, list)):
        return []
    definitions = schema.get("$defs", {})
    if not isinstance(definitions, Mapping):
        definitions = {}
    location: list[str] = []
    schemas: list[Mapping[str, Any]] = [schema]
    parts = list(error_location[:12])
    if parts and parts[0] == "response":
        location.append("response")
        parts.pop(0)
    for part in parts:
        branches = [branch for candidate in schemas for branch in _branches(candidate, definitions)]
        if isinstance(part, int) and not isinstance(part, bool):
            items = [branch.get("items") for branch in branches if branch.get("type") == "array"]
            if branches and len(items) == len(branches) and all(isinstance(item, Mapping) for item in items):
                location.append(str(part))
                schemas = list(items)
                continue
        elif isinstance(part, str):
            properties = [branch.get("properties") for branch in branches]
            if (branches and all(isinstance(values, Mapping) and part in values for values in properties)
                    and all(isinstance(values[part], Mapping) for values in properties)):
                location.append(part)
                schemas = [values[part] for values in properties]
                continue
        location.append(_HIDDEN_LOCATION)
        schemas = []
    return location


def response_validation_details(error: BaseException | None, request: Any) -> dict[str, Any] | None:
    """提取最多20项、每项最多12层路径；未知键隐藏，禁止复制 input、ctx、msg。"""
    if not isinstance(error, ResponseValidationError):
        return None
    try:
        model = getattr(request.scope.get("route"), "response_model", None)
        schema = _response_schema(model) if model is not None else {}
        errors = error.errors()
        issues = []
        truncated = len(errors) > 20
        for item in errors[:20]:
            path = item.get("loc", ())
            truncated = truncated or not isinstance(path, (tuple, list)) or len(path) > 12
            kind = item.get("type")
            issues.append({
                "location": _location(path, schema),
                "type": kind if kind in _ERROR_TYPES else "custom_error",
            })
        return {"count": len(errors), "truncated": truncated, "issues": issues}
    except Exception:  # noqa: BLE001 - 诊断辅助不得影响原始请求与请求事件。
        # 排障诊断不可覆盖原始500响应，更不能让请求事件完全丢失。
        return None
