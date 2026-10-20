"""响应 Schema 到 Pydantic 模型的编译回归测试。"""

from copy import deepcopy
from datetime import UTC, date, datetime

import pytest
from camera_logs.common.api_contracts.response_reference import RESPONSE_SCHEMA
from camera_logs.common.presentation_time import present_json_time
from camera_logs.common.response_models import response_model_from_schema
from pydantic import ValidationError


def test_nested_objects_preserve_chinese_descriptions_and_reject_unknown_fields():
    """嵌套对象应保留字段说明，且严格拒绝文档外的响应字段。"""
    model = response_model_from_schema("DeviceResponse", {
        "type": "object", "description": "设备响应。", "required": ["device"],
        "properties": {"device": {"type": "object", "description": "设备详情。", "required": ["id"], "properties": {
            "id": {"type": "string", "description": "设备唯一编号。"},
        }}},
    })

    assert model.model_fields["device"].description == "设备详情。"
    assert any(definition.get("description") == "设备详情。" for definition in model.model_json_schema()["$defs"].values())
    assert model.model_validate({"device": {"id": "camera-1"}}).device.id == "camera-1"
    with pytest.raises(ValidationError, match="extra_forbidden"):
        model.model_validate({"device": {"id": "camera-1", "unexpected": True}})


def test_optional_and_nullable_fields_keep_distinct_required_semantics():
    """可省略字段不能因默认 None 而接受显式 null，必填可空字段必须出现。"""
    model = response_model_from_schema("NullableResponse", {
        "type": "object", "required": ["requiredNullable"], "properties": {
            "optionalText": {"type": "string", "description": "可省略但不可为空。"},
            "requiredNullable": {"type": "integer", "nullable": True, "description": "必填但可为空。"},
        },
    })

    assert model.model_validate({"requiredNullable": None}).optionalText is None
    with pytest.raises(ValidationError):
        model.model_validate({"optionalText": None, "requiredNullable": 1})
    with pytest.raises(ValidationError):
        model.model_validate({})


def test_dynamic_dictionary_and_unions_validate_nested_values():
    """additionalProperties、oneOf 与 anyOf 编译成可校验的字典和联合类型。"""
    model = response_model_from_schema("DictionaryResponse", {
        "type": "object", "required": ["labels", "state"], "properties": {
            "labels": {"type": "object", "additionalProperties": {"type": "integer"}},
            "state": {"oneOf": [{"type": "string"}, {"type": "integer"}]},
            "flag": {"anyOf": [{"type": "boolean"}, {"type": "null"}]},
        },
    })

    assert model.model_validate({"labels": {"retries": 2}, "state": "ready", "flag": None}).labels == {"retries": 2}
    with pytest.raises(ValidationError):
        model.model_validate({"labels": {"retries": "two"}, "state": "ready"})


def test_named_reference_supports_recursive_models_without_recreating_them():
    """递归 $ref 必须复用命名模型，并允许任意深度的有效嵌套。"""
    node = {"type": "object", "required": ["name"], "properties": {
        "name": {"type": "string"},
        "children": {"type": "array", "items": {"$ref": "#/components/schemas/ResponseSchema"}},
    }}
    model = response_model_from_schema("Envelope", {"type": "object", "required": ["root"], "properties": {
        "root": {"$ref": "#/components/schemas/ResponseSchema"},
    }}, {"ResponseSchema": node})

    value = model.model_validate({"root": {"name": "root", "children": [{"name": "child", "children": []}]}})
    assert value.root.children[0].name == "child"
    with pytest.raises(ValueError, match="未知的响应 Schema 引用"):
        response_model_from_schema("Broken", {"type": "object", "properties": {
            "value": {"$ref": "#/components/schemas/Missing"},
        }})


def test_repository_response_schema_definition_compiles_recursively():
    """仓库 API 目录使用的递归 ResponseSchema 可直接成为模型定义。"""
    model = response_model_from_schema("SchemaEnvelope", {"type": "object", "required": ["root"], "properties": {
        "root": {"$ref": "#/components/schemas/ResponseSchema"},
    }}, {"ResponseSchema": RESPONSE_SCHEMA})

    result = model.model_validate({"root": {
        "type": "object", "properties": {"child": {"type": "string"}},
    }})
    assert result.root.properties["child"].type == "string"


def test_datetime_format_serializes_for_existing_shanghai_presentation_boundary():
    """date-time 字段保留 datetime 类型，JSON 文本可由既有中间件转换为上海时间。"""
    model = response_model_from_schema("TimeResponse", {"type": "object", "required": ["createdAt"], "properties": {
        "createdAt": {"type": "string", "format": "date-time", "description": "创建时刻。"},
    }})

    payload = model.model_validate({"createdAt": datetime(2026, 10, 19, 4, 0, tzinfo=UTC)}).model_dump(mode="json")
    assert payload["createdAt"] == "2026-10-19T04:00:00Z"
    assert present_json_time(payload)["createdAt"] == "2026-10-19T12:00:00+08:00"


def test_plain_string_schema_serializes_route_datetime_without_touching_opaque_strings():
    """无 format 的既有时间字段接纳 datetime，普通字符串不按字段名或内容改写。"""
    model = response_model_from_schema("LegacyTimeResponse", {"type": "object", "required": ["createdAt", "opaque"], "properties": {
        "createdAt": {"type": "string"}, "opaque": {"type": "string"},
    }})

    payload = model.model_validate({
        "createdAt": datetime(2026, 10, 19, 4, 0, tzinfo=UTC),
        "opaque": "2026-10-19T04:00:00Z",
    }).model_dump(mode="json")
    assert payload == {"createdAt": "2026-10-19T04:00:00+00:00", "opaque": "2026-10-19T04:00:00Z"}
    assert present_json_time(payload) == {"createdAt": "2026-10-19T12:00:00+08:00", "opaque": "2026-10-19T04:00:00Z"}
    assert model.model_validate({"createdAt": date(2026, 10, 19), "opaque": "unchanged"}).createdAt == "2026-10-19"


def test_equivalent_schema_snapshots_reuse_one_model_without_retaining_mutable_input():
    """等价不可变快照应复用模型，调用方随后修改原字典不能污染已编译合同。"""
    schema = {"type": "object", "required": ["id"], "properties": {"id": {"type": "string"}}}
    equivalent = deepcopy(schema)
    first = response_model_from_schema("FirstEndpoint", schema)
    schema["properties"]["id"]["description"] = "调用方后续修改。"
    second = response_model_from_schema("SecondEndpoint", equivalent)

    assert first is second
    assert first.model_fields["id"].description is None


def test_number_keeps_integers_and_nested_schema_metadata_survives_openapi():
    """number 不得把整数变为浮点，数组元素应携带独立说明和约束。"""
    model = response_model_from_schema("ScheduledTemplate", {"type": "object", "required": ["intervalSeconds", "scopes", "state", "fixed"], "properties": {
        "intervalSeconds": {"type": "number", "minimum": 1, "multipleOf": 1},
        "scopes": {"type": "array", "minItems": 1, "items": {
            "type": "string", "description": "用户权限范围。", "enum": ["tasks:read", "tasks:write"],
        }},
        "state": {"type": "string", "pattern": "^(READY|STOPPED)$", "enum": ["READY", "STOPPED"]},
        "fixed": {"const": "scheduled"},
    }})

    value = model.model_validate({"intervalSeconds": 30, "scopes": ["tasks:read"], "state": "READY", "fixed": "scheduled"})
    assert isinstance(value.intervalSeconds, int)
    assert value.intervalSeconds == 30
    interval_schema = model.model_json_schema()["properties"]["intervalSeconds"]
    assert interval_schema == {"minimum": 1, "multipleOf": 1, "title": "Intervalseconds", "type": "number"}
    item_schema = model.model_json_schema()["properties"]["scopes"]["items"]
    assert item_schema["description"] == "用户权限范围。"
    assert item_schema["enum"] == ["tasks:read", "tasks:write"]
    with pytest.raises(ValidationError):
        model.model_validate({"intervalSeconds": 0, "scopes": [], "state": "RUNNING", "fixed": "manual"})


def test_nullable_number_documents_null_without_degrading_to_integer_or_float_union():
    """number 运行时可保留 int，同时 OpenAPI 用标准 number 加 null 表达可空。"""
    model = response_model_from_schema("NullableNumber", {"type": "object", "required": ["value"], "properties": {
        "value": {"type": "number", "nullable": True, "description": "可空数值。"},
    }})

    assert model.model_validate({"value": 1}).value == 1
    assert model.model_validate({"value": None}).value is None
    assert model.model_json_schema()["properties"]["value"] == {
        "anyOf": [{"description": "可空数值。", "type": "number"}, {"type": "null"}],
        "description": "可空数值。", "title": "Value",
    }


def test_optional_defaults_remain_unset_in_response_serialization():
    """可选字段的 Schema default 可供模型读取，但 exclude_unset 不得凭空输出它。"""
    model = response_model_from_schema("OptionalDefault", {"type": "object", "required": ["id"], "properties": {
        "id": {"type": "string"}, "pageSize": {"type": "integer", "default": 20},
    }})

    value = model.model_validate({"id": "task-1"})
    assert value.pageSize == 20
    assert value.model_dump(mode="json", exclude_unset=True) == {"id": "task-1"}
