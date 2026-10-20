"""从路由的实际 OpenAPI 输出模型生成响应释义，不另维护字段清单。"""

from copy import deepcopy

from camera_logs.common.api_contracts.response_schema import error_schema


def expand_schema(source, schemas, ancestors=()):
    """展开命名对象以供页面逐层展示；递归引用保留原名，避免无限展开。"""
    value = deepcopy(source)
    reference = value.get("$ref")
    if reference:
        name = reference.rsplit("/", 1)[-1]
        if name in ancestors:
            return value
        if name not in schemas:
            raise RuntimeError(f"OpenAPI 响应引用缺少定义：{name}")
        resolved = expand_schema(schemas[name], schemas, (*ancestors, name))
        resolved.update({key: item for key, item in value.items() if key != "$ref"})
        return resolved
    choices = value.get("anyOf", value.get("oneOf"))
    if choices:
        concrete = [item for item in choices if item.get("type") != "null"]
        if len(concrete) == 1 and len(concrete) < len(choices):
            resolved = expand_schema(concrete[0], schemas, ancestors)
            resolved.update({key: item for key, item in value.items() if key not in {"anyOf", "oneOf"}})
            return resolved | {"nullable": True}
    for name in ("properties", "$defs"):
        if name in value:
            value[name] = {key: expand_schema(item, schemas, ancestors) for key, item in value[name].items()}
    for name in ("items", "additionalProperties"):
        if isinstance(value.get(name), dict):
            value[name] = expand_schema(value[name], schemas, ancestors)
    for name in ("anyOf", "oneOf", "allOf"):
        if name in value:
            value[name] = [expand_schema(item, schemas, ancestors) for item in value[name]]
    return value


def openapi_response_contract(operation, status, schemas):
    """成功字段只读取 FastAPI response_model 生成的 Schema；协议备注取路由扩展。"""
    metadata = operation.get("x-response-contract", {})
    response = operation["responses"][str(status)]
    content = response.get("content", {})
    kind = metadata.get("kind", "json" if "application/json" in content else "empty")
    if kind == "json":
        source = content.get("application/json", {}).get("schema")
        if not source:
            raise RuntimeError("JSON 接口缺少真实响应模型")
        schema = expand_schema(source, schemas)
        schema.setdefault("description", response.get("description", "接口成功响应。"))
    else:
        schema = deepcopy(metadata.get("schema"))
    errors = []
    for code, specification in operation["responses"].items():
        if not code.isdecimal() or int(code) < 400:
            continue
        source = specification.get("content", {}).get("application/json", {}).get("schema")
        if source:
            errors.append({"status": int(code), "when": specification["description"],
                           "schema": expand_schema(source, schemas)})
    result = {"kind": kind, "description": metadata.get("description", "由接口响应模型自动生成的 JSON 正文。"),
              "schema": schema, "errors": errors, "errorSchema": error_schema()}
    if metadata.get("conditional"):
        result["conditional"] = metadata["conditional"]
    return result


def response_schema_example(schema, schemas, seed=None, *, depth=0, name=""):
    """按当前响应模型投影虚拟示例；旧示例仅提供值提示，不能添加模型外字段。"""
    from camera_logs.reference.examples import STAMP

    if depth > 6:
        return None
    if "$ref" in schema:
        schema = schemas[schema["$ref"].rsplit("/", 1)[-1]]
    choices = schema.get("anyOf", schema.get("oneOf"))
    if choices:
        concrete = next((item for item in choices if item.get("type") != "null"), choices[0])
        return response_schema_example(concrete, schemas, seed, depth=depth + 1, name=name)
    kind = schema.get("type")
    if kind == "object" or "properties" in schema:
        hints = seed if isinstance(seed, dict) else {}
        return {key: response_schema_example(child, schemas, hints.get(key), depth=depth + 1, name=key)
                for key, child in schema.get("properties", {}).items()
                if key in schema.get("required", []) or key in hints}
    if kind == "array":
        hints = seed if isinstance(seed, list) else []
        return [response_schema_example(schema.get("items", {}), schemas, item, depth=depth + 1)
                for item in hints]
    if seed is not None:
        return seed
    if "const" in schema:
        return schema["const"]
    if schema.get("enum"):
        return schema["enum"][0]
    if schema.get("nullable") or kind == "null":
        return None
    if kind == "boolean":
        return False
    if kind in {"integer", "number"}:
        return 1
    if name.endswith(("At", "Until", "Since")) or schema.get("format") == "date-time":
        return STAMP
    return "example"
