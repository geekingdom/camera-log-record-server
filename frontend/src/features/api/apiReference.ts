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
  /** 服务端生成的 Python 调用文本；页面只允许复制和展示，不能解释或执行。 */
  pythonExample: string;
  responseStatus: number;
  responseExample: unknown;
  responseContract: ResponseContract;
}

export interface ReferenceSchema {
  $ref?: string;
  type?: string;
  format?: string;
  nullable?: boolean;
  additionalProperties?: ReferenceSchema | boolean;
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

export interface ResponseError {
  status: number;
  when: string;
  schema: ReferenceSchema;
}

export interface ResponseContract {
  kind: "json" | "empty" | "binary" | "text" | "websocket";
  description: string;
  schema: ReferenceSchema | null;
  conditional?: string[];
  errors: ResponseError[];
  errorSchema: ReferenceSchema | null;
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
function resolveReference(schema: ReferenceSchema, schemas: Record<string, unknown>, seen = new Set<string>()): ReferenceSchema {
  if (!schema.$ref || seen.has(schema.$ref)) return schema;
  seen.add(schema.$ref);
  const name = schema.$ref.split("/").at(-1);
  const referenced = schemas[name ?? ""] as ReferenceSchema | undefined;
  if (!referenced) return schema;
  const { $ref: _reference, ...own } = schema;
  return { ...resolveReference(referenced, schemas, seen), ...own };
}

function normalizeSchema(source: ReferenceSchema, schemas: Record<string, unknown>) {
  let schema = resolveReference(source, schemas);
  const alternatives = schema.anyOf ?? schema.oneOf ?? [];
  const resolved = alternatives.map(item => resolveReference(item, schemas));
  const nullable = Boolean(schema.nullable) || resolved.some(item => item.type === "null");
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

/** 展开对象、数组、联合类型及动态字典；递归引用仅展开一次以避免循环。 */
export function describeObjectFields(
  source: ReferenceSchema | undefined,
  schemas: Record<string, unknown>,
  prefix = "",
  ancestors = new Set<ReferenceSchema>(),
): SchemaFieldDescription[] {
  if (!source) return [];
  const referenced = source.$ref
    ? schemas[source.$ref.split("/").at(-1) ?? ""] as ReferenceSchema | undefined
    : source;
  if (referenced && ancestors.has(referenced)) return [];
  const visited = new Set(ancestors);
  if (referenced) visited.add(referenced);
  const raw = resolveReference(source, schemas);
  const variants = (raw.oneOf ?? raw.anyOf ?? []).filter(item => item.type !== "null");
  if (variants.length > 1) {
    return variants.flatMap((variant, index) => describeObjectFields(
      variant, schemas, `${prefix}${prefix ? "." : ""}{分支${index + 1}}`, visited,
    ));
  }
  const schema = objectSchema(raw, schemas);
  const rows: SchemaFieldDescription[] = [];
  const add = (name: string, field: ReferenceSchema, path: string, required: boolean) => {
    const detail = describeSchema(field, schemas, required);
    const resolved = objectSchema(field, schemas);
    rows.push({ name, path, ...detail, required, description: field.description ?? resolved.description ?? "" });
    if (resolved.type === "array") rows.push(...describeObjectFields(resolved.items, schemas, `${path}[]`, visited));
    else rows.push(...describeObjectFields(field, schemas, path, visited));
  };
  for (const [name, field] of Object.entries(schema.properties ?? {})) {
    add(name, field, prefix ? `${prefix}.${name}` : name, Boolean(schema.required?.includes(name)));
  }
  if (schema.additionalProperties && typeof schema.additionalProperties === "object") {
    add("{键}", schema.additionalProperties, prefix ? `${prefix}.{键}` : "{键}", false);
  }
  return rows;
}

export const loadApiReference = () => request<ApiReferenceCatalog>("/api-reference");
