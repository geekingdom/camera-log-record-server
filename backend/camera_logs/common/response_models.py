"""把共享 API 响应合同编译为运行时校验及 OpenAPI 共用的 Pydantic 模型。"""

from __future__ import annotations

import json
import keyword
from collections.abc import Mapping
from datetime import date, datetime
from functools import lru_cache
from hashlib import blake2b
from typing import Annotated, Any, ForwardRef, Literal, Union

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, WithJsonSchema, create_model


class StrictResponseModel(BaseModel):
    """响应对象的共同基类，阻止实现遗漏字段被静默丢弃。"""

    model_config = ConfigDict(extra="forbid", populate_by_name=True, serialize_by_alias=True)


def _serialize_response_string(value: Any) -> Any:
    """仅把路由返回的日期时间对象转成 FastAPI 同口径的 ISO 文本。"""
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return value


ResponseString = Annotated[str, BeforeValidator(_serialize_response_string)]


def _safe_field_name(name: str) -> str:
    """把 JSON 属性名映射为不会遮蔽 Pydantic 基类属性的 Python 字段名。"""
    if name.isidentifier() and not keyword.iskeyword(name) and name not in dir(StrictResponseModel):
        return name
    digest = blake2b(name.encode(), digest_size=6).hexdigest()
    return f"field_{digest}"


class ResponseModelCompiler:
    """编译一组响应合同，并缓存命名模型以支持递归引用。"""

    def __init__(self, definitions: Mapping[str, Mapping[str, Any]] | None = None):
        """保存外部命名定义；调用方可在同一编译器中复用模型缓存。"""
        self.definitions = dict(definitions or {})
        self.models: dict[str, type[BaseModel]] = {}
        self._building: set[str] = set()
        self._anonymous_index = 0

    def model(self, name: str, schema: Mapping[str, Any]) -> type[BaseModel]:
        """返回 Schema 对应的对象模型，并在全部字段创建后重建前向引用。"""
        result = self._object_model(name, schema)
        self.rebuild()
        return result

    def rebuild(self) -> None:
        """解析递归字段引用；缓存确保同一命名定义只创建一次。"""
        namespace = dict(self.models)
        for model in self.models.values():
            model.model_rebuild(_types_namespace=namespace, force=True)

    def _object_model(self, name: str, schema: Mapping[str, Any]) -> type[BaseModel]:
        reference = schema.get("$ref")
        if isinstance(reference, str):
            resolved = self._reference_name(reference)
            if resolved in self._building:
                raise ValueError(f"顶层 Schema 不能直接递归引用自身：{resolved}")
            return self._named_model(resolved)
        if name in self.models:
            return self.models[name]
        if schema.get("type") != "object" and "properties" not in schema:
            raise ValueError(f"响应模型 {name} 必须是 object Schema")

        self._building.add(name)
        try:
            fields: dict[str, tuple[Any, Any]] = {}
            required = set(schema.get("required", ()))
            properties = schema.get("properties", {})
            if not isinstance(properties, Mapping):
                raise TypeError(f"响应模型 {name} 的 properties 必须是对象")
            for field_name, field_schema in properties.items():
                if not isinstance(field_schema, Mapping):
                    raise TypeError(f"响应模型 {name}.{field_name} 的 Schema 必须是对象")
                annotation = self._type(field_schema, f"{name}{field_name[:1].upper()}{field_name[1:]}")
                default = ... if field_name in required else field_schema.get("default", None)
                description = field_schema.get("description")
                external_name = str(field_name)
                fields[_safe_field_name(external_name)] = (
                    annotation,
                    Field(
                        default,
                        description=description,
                        validation_alias=external_name,
                        serialization_alias=external_name,
                        # Pydantic 会把嵌套模型字段转为 $ref；额外保留说明，
                        # 使 API 文档无需再追踪 $defs 才能显示中文字段释义。
                        json_schema_extra={"description": description} if description else None,
                    ),
                )
            model = create_model(name, __base__=StrictResponseModel, __module__=__name__, **fields)
            model.__doc__ = str(schema.get("description", "")) or None
            self.models[name] = model
            return model
        finally:
            self._building.remove(name)

    def _named_model(self, name: str) -> type[BaseModel]:
        if name in self.models:
            return self.models[name]
        try:
            schema = self.definitions[name]
        except KeyError as exc:
            raise ValueError(f"未知的响应 Schema 引用：{name}") from exc
        return self._object_model(name, schema)

    def _type(self, schema: Mapping[str, Any], hint: str) -> Any:
        reference = schema.get("$ref")
        if isinstance(reference, str):
            name = self._reference_name(reference)
            base: Any = ForwardRef(name) if name in self._building else self._named_model(name)
            return self._finish_type(base, schema)

        choices = schema.get("oneOf", schema.get("anyOf"))
        if isinstance(choices, list):
            if not choices:
                raise ValueError("oneOf/anyOf 不能是空数组")
            base = Union[tuple(self._type(item, f"{hint}Option{index}") for index, item in enumerate(choices))]  # noqa: UP007
            return self._finish_type(base, schema)

        if "const" in schema:
            return self._finish_type(Literal[schema["const"]], schema)
        if "enum" in schema:
            values = schema["enum"]
            if not isinstance(values, list) or not values:
                raise ValueError("enum 必须是非空数组")
            return self._finish_type(Literal[tuple(values)], schema)

        kind = schema.get("type", "any")
        primitive: dict[str, Any] = {
            # JSON number 同时容纳整数和小数；先匹配 int 能避免 30 被转换为 30.0，
            # 从而可安全回填到后续严格的整数请求模型。
            "any": Any, "string": ResponseString, "integer": int, "number": int | float,
            "boolean": bool, "null": type(None),
        }
        if kind in primitive:
            base = primitive[kind]
            if kind == "string" and schema.get("format") == "date-time":
                base = datetime
            elif kind == "string" and schema.get("format") == "date":
                base = date
        elif kind == "array":
            items = schema.get("items", {})
            if not isinstance(items, Mapping):
                raise ValueError("array 的 items 必须是对象")
            base = list[self._type(items, f"{hint}Item")]
        elif kind == "object" or "properties" in schema:
            properties = schema.get("properties")
            additional = schema.get("additionalProperties", None)
            if not properties and additional is not None:
                value_type = Any if additional is True else self._type(additional, f"{hint}Value") if isinstance(additional, Mapping) else Any
                base = dict[str, value_type]
            elif not properties and additional is None:
                base = dict[str, Any]
            else:
                base = self._object_model(self._anonymous_name(hint), schema)
        else:
            raise ValueError(f"不支持的响应 Schema 类型：{kind}")
        return self._finish_type(base, schema)

    def _finish_type(self, base: Any, schema: Mapping[str, Any]) -> Any:
        """附加可空、中文说明及 JSON Schema 约束，供字段和数组元素共同复用。"""
        base = self._nullable(base, schema)
        constraints = {
            "minimum": "ge", "maximum": "le", "exclusiveMinimum": "gt",
            "exclusiveMaximum": "lt", "multipleOf": "multiple_of",
            "minLength": "min_length", "maxLength": "max_length",
            "minItems": "min_length", "maxItems": "max_length",
            "pattern": "pattern",
        }
        metadata = {target: schema[source] for source, target in constraints.items() if source in schema}
        if "description" in schema:
            metadata["description"] = schema["description"]
        if schema.get("uniqueItems") is True:
            metadata["json_schema_extra"] = {"uniqueItems": True}
        if schema.get("type") == "number":
            number_schema = {"type": "number", **_json_schema_constraints(schema)}
            if schema.get("nullable") is True:
                number_schema = {"anyOf": [number_schema, {"type": "null"}]}
            return Annotated[base, Field(**metadata), WithJsonSchema(number_schema)]
        return Annotated[base, Field(**metadata)] if metadata else base

    @staticmethod
    def _nullable(base: Any, schema: Mapping[str, Any]) -> Any:
        """仅在 nullable 明示时接纳 null，保留 required 与可空的独立语义。"""
        if schema.get("nullable") is True and base is not type(None):
            return base | None
        return base

    @staticmethod
    def _reference_name(reference: str) -> str:
        """提取本目录 components/schemas 引用的模型名，拒绝无法解析的外部引用。"""
        prefix = "#/components/schemas/"
        if not reference.startswith(prefix) or not reference.removeprefix(prefix):
            raise ValueError(f"不支持的响应 Schema 引用：{reference}")
        return reference.removeprefix(prefix)

    def _anonymous_name(self, hint: str) -> str:
        self._anonymous_index += 1
        return f"{hint}Model{self._anonymous_index}"


def response_model_from_schema(
    name: str,
    schema: Mapping[str, Any],
    definitions: Mapping[str, Mapping[str, Any]] | None = None,
) -> type[BaseModel]:
    """从共享响应合同创建严格的 Pydantic 模型并按合同内容复用结果。"""
    # `name` 只保留调用方可读性和向后兼容；同一合同不能因路由名称不同重复建模。
    del name
    schema_json = _canonical_json(schema)
    definitions_json = _canonical_json(definitions or {})
    return _cached_response_model(schema_json, definitions_json)


def _canonical_json(value: Mapping[str, Any]) -> str:
    """生成不依赖字典插入顺序的合同指纹输入，且不保留调用方可变对象。"""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _json_schema_constraints(schema: Mapping[str, Any]) -> dict[str, Any]:
    """保留 number 的合同元数据，避免联合运行时类型把文档降级成 anyOf。"""
    keys = ("description", "minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "multipleOf")
    return {key: schema[key] for key in keys if key in schema}


@lru_cache(maxsize=512)
def _cached_response_model(schema_json: str, definitions_json: str) -> type[BaseModel]:
    """按不可变合同快照缓存动态模型，避免每次应用装配重复创建同构类。"""
    fingerprint = blake2b(f"{schema_json}|{definitions_json}".encode(), digest_size=8).hexdigest()
    return ResponseModelCompiler(json.loads(definitions_json)).model(
        f"ResponseModel{fingerprint}", json.loads(schema_json)
    )
