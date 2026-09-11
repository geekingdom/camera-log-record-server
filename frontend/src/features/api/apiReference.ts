// 内置接口目录的数据边界：只读取服务端生成的静态 catalog，不执行其中的业务示例。
import { request } from "../../shared/api";

export interface ReferenceParameter {
  name: string;
  in: string;
  required?: boolean;
  example?: unknown;
  schema?: ReferenceSchema;
  description?: string;
}

export interface ReferenceOperation {
  id: string;
  method: string;
  path: string;
  title: string;
  group: string;
  description: string;
  permission: string;
  headers: Record<string, string>;
  parameters: ReferenceParameter[];
  requestSchema?: ReferenceSchema;
  requestExample: unknown;
  responseStatus: number;
  responseExample: unknown;
}

export interface ReferenceSchema {
  $ref?: string;
  type?: string;
  format?: string;
  description?: string;
  enum?: unknown[];
  default?: unknown;
  minimum?: number;
  maximum?: number;
  minLength?: number;
  maxLength?: number;
  minItems?: number;
  maxItems?: number;
  properties?: Record<string, ReferenceSchema>;
  required?: string[];
  items?: ReferenceSchema;
  anyOf?: ReferenceSchema[];
  oneOf?: ReferenceSchema[];
}

export interface ApiReferenceCatalog {
  version: string;
  guides: { title: string; text: string }[];
  operations: ReferenceOperation[];
  schemas: Record<string, unknown>;
  errorExample: unknown;
}

export interface SchemaDescription {
  type: string;
  constraints: string;
}

export interface SchemaFieldDescription extends SchemaDescription {
  name: string;
  path: string;
  description: string;
  required: boolean;
}

// OpenAPI 既会直接给出类型，也会通过 $ref、anyOf 表达 Optional；统一展开后再生成可读约束。
function resolveReference(schema: ReferenceSchema, schemas: Record<string, unknown>): ReferenceSchema {
  if (!schema.$ref) return schema;
  const name = schema.$ref.split("/").at(-1);
  const referenced = schemas[name ?? ""] as ReferenceSchema | undefined;
  if (!referenced) return schema;
  const { $ref: _reference, ...own } = schema;
  return { ...resolveReference(referenced, schemas), ...own };
}

function normalizeSchema(source: ReferenceSchema, schemas: Record<string, unknown>) {
  let schema = resolveReference(source, schemas);
  const alternatives = schema.anyOf ?? schema.oneOf ?? [];
  const resolved = alternatives.map(item => resolveReference(item, schemas));
  const nullable = resolved.some(item => item.type === "null");
  const preferred = resolved.find(item => item.type !== "null");
  if (preferred) {
    const { anyOf: _anyOf, oneOf: _oneOf, ...base } = schema;
    schema = { ...base, ...preferred };
  }
  return { schema, nullable };
}

export function objectSchema(source: ReferenceSchema | undefined, schemas: Record<string, unknown>): ReferenceSchema {
  return source ? normalizeSchema(source, schemas).schema : {};
}

export function describeSchema(
  source: ReferenceSchema | undefined,
  schemas: Record<string, unknown>,
  required: boolean,
): SchemaDescription {
  if (!source) return { type: "-", constraints: required ? "必填" : "可选" };
  const { schema, nullable } = normalizeSchema(source, schemas);
  let type = schema.type ?? (schema.properties ? "object" : "-");
  if (type === "array") {
    const item = describeSchema(schema.items, schemas, false).type;
    type = `array<${item}>`;
  }
  if (nullable) type += " | null";
  const constraints = [
    required ? "必填" : "可选",
    schema.format && `格式 ${schema.format}`,
    schema.enum?.length && `可选 ${schema.enum.map(String).join(" / ")}`,
    schema.default !== undefined && `默认 ${JSON.stringify(schema.default)}`,
    schema.minimum !== undefined && `最小 ${schema.minimum}`,
    schema.maximum !== undefined && `最大 ${schema.maximum}`,
    schema.minLength !== undefined && `最短 ${schema.minLength}`,
    schema.maxLength !== undefined && `最长 ${schema.maxLength}`,
    schema.minItems !== undefined && `最少 ${schema.minItems} 项`,
    schema.maxItems !== undefined && `最多 ${schema.maxItems} 项`,
  ].filter(Boolean).join(" · ");
  return { type, constraints };
}

/** 递归展开请求对象，令嵌套命令、白名单规则和分页结构也能逐字段阅读。 */
export function describeObjectFields(
  source: ReferenceSchema | undefined,
  schemas: Record<string, unknown>,
  prefix = "",
): SchemaFieldDescription[] {
  const schema = objectSchema(source, schemas);
  const rows: SchemaFieldDescription[] = [];
  for (const [name, field] of Object.entries(schema.properties ?? {})) {
    const path = prefix ? `${prefix}.${name}` : name;
    const required = Boolean(schema.required?.includes(name));
    const detail = describeSchema(field, schemas, required);
    const resolved = objectSchema(field, schemas);
    rows.push({ name, path, ...detail, required, description: resolved.description ?? field.description ?? "" });
    if (resolved.properties) rows.push(...describeObjectFields(field, schemas, path));
    if (resolved.type === "array" && objectSchema(resolved.items, schemas).properties)
      rows.push(...describeObjectFields(resolved.items, schemas, `${path}[]`));
  }
  return rows;
}

export const loadApiReference = () => request<ApiReferenceCatalog>("/api-reference");
