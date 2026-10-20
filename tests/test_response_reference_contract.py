"""对 API 目录自身进行递归合同覆盖，包含每个操作和 JSON Schema 元数据。"""
# ruff: noqa: F811
from camera_logs.common.api_contracts.response_reference import REFERENCE
from test_api import client  # noqa: F401


def check(value, schema, definitions, path="catalog"):
    """校验实际 JSON 的对象键、联合类型及递归定义，任意示例值显式标为 any。"""
    if "$ref" in schema:
        schema = definitions[schema["$ref"].rsplit("/", 1)[-1]] | {k: v for k, v in schema.items() if k != "$ref"}
    assert schema.get("description"), path
    if value is None:
        assert schema.get("nullable") or schema.get("type") in {"null", "any"}, path
        return
    if schema.get("type") == "any":
        return
    alternatives = schema.get("anyOf") or schema.get("oneOf")
    if alternatives:
        errors = []
        for variant in alternatives:
            try:
                check(value, variant, definitions, path)
                return
            except AssertionError as error:
                errors.append(str(error))
        raise AssertionError(f"{path}: 不满足任何分支 {errors}")
    if isinstance(value, dict):
        assert schema.get("type") == "object", path
        assert set(schema.get("required", [])) <= set(value), path
        for key, child in value.items():
            definition = schema.get("properties", {}).get(key, schema.get("additionalProperties"))
            assert isinstance(definition, dict), f"{path}.{key} 未解释"
            check(child, definition, definitions, f"{path}.{key}")
    elif isinstance(value, list):
        assert schema.get("type") == "array", path
        for index, child in enumerate(value):
            check(child, schema["items"], definitions, f"{path}[{index}]")
    elif isinstance(value, bool):
        assert schema.get("type") == "boolean", path
    elif isinstance(value, (float, int)):
        assert schema.get("type") in {"integer", "number"}, path
    else:
        assert schema.get("type") == "string", path


def test_complete_catalog_response_matches_its_recursive_contract(client):
    response = client.get("/api/v1/api-reference")
    assert response.status_code == 200
    body = response.json()
    check(body, REFERENCE, body["schemas"])
