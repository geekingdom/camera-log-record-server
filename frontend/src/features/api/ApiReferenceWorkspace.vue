<script setup lang="ts">
// 内置接口文档只展示后端目录；调用示例是可复制文本，页面不会发起任何业务接口请求。
import { computed, onMounted, ref } from "vue";
import {
  BookOpen,
  Check,
  ChevronRight,
  Copy,
  FileCode2,
  KeyRound,
  RefreshCw,
  Search,
  ShieldCheck,
} from "lucide-vue-next";
import { ElMessage } from "element-plus";
import { websocketUrl } from "../../shared/websocketUrl";
import {
  loadApiReference,
  describeSchema,
  objectSchema,
  type ApiReferenceCatalog,
  type ReferenceOperation,
} from "./apiReference";

const catalog = ref<ApiReferenceCatalog>();
const loading = ref(false);
const loadError = ref("");
const search = ref("");
const selectedId = ref("");
const activeGroup = ref("全部接口");
const copied = ref("");

const groups = computed(() => {
  const count = new Map<string, number>();
  catalog.value?.operations.forEach((operation) =>
    count.set(operation.group, (count.get(operation.group) ?? 0) + 1),
  );
  return ["全部接口", ...count.keys()].map((name) => ({
    name,
    count: name === "全部接口" ? catalog.value?.operations.length ?? 0 : count.get(name) ?? 0,
  }));
});
const filteredOperations = computed(() => {
  const keyword = search.value.trim().toLocaleLowerCase();
  return (catalog.value?.operations ?? []).filter((operation) =>
    (activeGroup.value === "全部接口" || operation.group === activeGroup.value) &&
    (!keyword || [operation.title, operation.path, operation.method, operation.permission, operation.group]
      .join(" ").toLocaleLowerCase().includes(keyword)),
  );
});
const selectedOperation = computed(() =>
  filteredOperations.value.find((operation) => operation.id === selectedId.value)
  ?? filteredOperations.value[0],
);
const requestFields = computed(() => {
  const operation = selectedOperation.value;
  const schema = objectSchema(operation?.requestSchema, catalog.value?.schemas ?? {});
  return Object.entries(schema?.properties ?? {}).map(([name, field]) => {
    const detail = describeSchema(field, catalog.value?.schemas ?? {}, Boolean(schema.required?.includes(name)));
    return { name, ...detail, description: objectSchema(field, catalog.value?.schemas ?? {}).description };
  });
});

function stringify(value: unknown) {
  return value === null || value === undefined ? "" : JSON.stringify(value, null, 2);
}
function parameterSchema(parameter: { schema?: Parameters<typeof describeSchema>[0]; required?: boolean }) {
  return describeSchema(parameter.schema, catalog.value?.schemas ?? {}, Boolean(parameter.required));
}
function requestExample(operation: ReferenceOperation) {
  const headers = Object.entries(operation.headers)
    .map(([name, value]) => `${name}: ${value}`)
    .join("\n");
  const body = stringify(operation.requestExample);
  if (operation.method === "WS")
    return `当前部署连接地址\n${websocketUrl(operation.path, location)}\n\nHTTP 部署：ws://<服务地址>${operation.path}\nHTTPS 部署：wss://<服务地址>${operation.path}\n\n连接后首帧（JSON）${body ? `\n${body}` : ""}`;
  return `${operation.method} ${operation.path} HTTP/1.1${headers ? `\n${headers}` : ""}${body ? `\n\n${body}` : ""}`;
}
function selectOperation(operation: ReferenceOperation) {
  selectedId.value = operation.id;
}
function selectGroup(group: string) {
  activeGroup.value = group;
  selectedId.value = "";
}
async function copy(value: string, key: string) {
  try {
    if (navigator.clipboard?.writeText) await navigator.clipboard.writeText(value);
    else {
      const fallback = document.createElement("textarea");
      fallback.value = value;
      fallback.setAttribute("readonly", "");
      fallback.style.position = "fixed";
      fallback.style.opacity = "0";
      document.body.append(fallback);
      fallback.select();
      const copiedText = document.execCommand("copy");
      fallback.remove();
      if (!copiedText) throw new Error("浏览器拒绝写入剪贴板");
    }
    copied.value = key;
    window.setTimeout(() => {
      if (copied.value === key) copied.value = "";
    }, 1600);
  } catch {
    ElMessage.warning("当前浏览器不允许写入剪贴板");
  }
}
async function reload() {
  if (loading.value) return;
  loading.value = true;
  loadError.value = "";
  try {
    catalog.value = await loadApiReference();
    if (!filteredOperations.value.some((operation) => operation.id === selectedId.value))
      selectedId.value = filteredOperations.value[0]?.id ?? "";
  } catch (error) {
    loadError.value = error instanceof Error ? error.message : "接口目录暂时无法读取";
  } finally {
    loading.value = false;
  }
}
onMounted(() => void reload());
</script>

<template>
  <section class="api-reference" aria-label="内置 API 文档">
    <div class="api-reference-intro">
      <div class="api-reference-title"><BookOpen :size="22" /><div><strong>目录版本 {{ catalog?.version ?? "-" }}</strong><p>{{ catalog?.operations.length ?? 0 }} 个公共接口 · 示例不会自动执行</p></div></div>
      <el-button :icon="RefreshCw" :loading="loading" aria-label="重新加载接口目录" @click="reload">重新加载</el-button>
    </div>

    <div v-if="loadError" class="api-reference-error" role="alert">
      <strong>接口目录加载失败</strong><span>{{ loadError }}</span><el-button type="primary" :icon="RefreshCw" @click="reload">重试</el-button>
    </div>
    <div v-else-if="loading && !catalog" class="api-reference-loading" aria-busy="true"><el-skeleton :rows="8" animated /></div>
    <div v-else-if="catalog" class="api-reference-grid">
      <aside class="api-reference-directory" aria-label="接口目录">
        <label class="api-search"><Search :size="16" /><input v-model="search" type="search" placeholder="搜索路径、权限或名称" aria-label="搜索接口目录" /></label>
        <div class="api-groups" role="listbox" aria-label="接口分类">
          <button v-for="group in groups" :key="group.name" :class="{ active: activeGroup === group.name }" :aria-selected="activeGroup === group.name" @click="selectGroup(group.name)"><span>{{ group.name }}</span><small>{{ group.count }}</small></button>
        </div>
        <div class="api-operation-list" aria-label="接口列表">
          <button v-for="operation in filteredOperations" :key="operation.id" :class="{ active: selectedOperation?.id === operation.id }" @click="selectOperation(operation)"><span class="http-method" :class="operation.method.toLowerCase()">{{ operation.method }}</span><span><strong>{{ operation.title }}</strong><code>{{ operation.path }}</code></span><ChevronRight :size="14" /></button>
          <p v-if="!filteredOperations.length" class="api-empty">没有匹配的接口</p>
        </div>
      </aside>

      <article v-if="selectedOperation" class="api-operation-detail">
        <header class="operation-heading"><div class="operation-label"><span class="http-method" :class="selectedOperation.method.toLowerCase()">{{ selectedOperation.method }}</span><code>{{ selectedOperation.path }}</code><el-tag effect="plain" type="success"><ShieldCheck :size="14" /> {{ selectedOperation.permission }}</el-tag></div><h2>{{ selectedOperation.title }}</h2><p>{{ selectedOperation.description }}</p></header>
        <section class="operation-section"><h3>请求参数</h3><div v-if="selectedOperation.parameters.length" class="parameter-list"><div v-for="parameter in selectedOperation.parameters" :key="parameter.in + parameter.name" class="parameter-row"><div><strong>{{ parameter.name }}</strong><span>{{ parameter.in }} · {{ parameterSchema(parameter).constraints }}</span></div><p>{{ parameter.description || parameter.schema?.description || "目录未提供字段说明" }}</p><code>{{ parameterSchema(parameter).type }}</code></div></div><p v-else class="section-empty">此接口没有路径或查询参数。</p><div v-if="requestFields.length" class="request-fields"><h4>JSON 请求字段</h4><div class="parameter-list"><div v-for="field in requestFields" :key="field.name" class="parameter-row"><div><strong>{{ field.name }}</strong><span>{{ field.type }} · {{ field.constraints }}</span></div><p>{{ field.description || "目录未提供字段说明" }}</p><code>{{ field.type }}</code></div></div></div></section>
        <section class="operation-section"><h3><KeyRound :size="16" /> 权限与请求头</h3><p class="permission-copy">需要 <strong>{{ selectedOperation.permission }}</strong>。资源范围和来源 IP 策略会在实际请求时再次校验。</p><div class="header-list"><code v-for="(value, name) in selectedOperation.headers" :key="name">{{ name }}: {{ value }}</code><span v-if="!Object.keys(selectedOperation.headers).length">此连接不使用 HTTP 请求头。</span></div></section>
        <section class="operation-section api-code-section"><div class="code-heading"><h3><FileCode2 :size="16" /> 请求示例</h3><el-tooltip :content="copied === 'request' ? '已复制' : '复制请求示例'"><el-button text :icon="copied === 'request' ? Check : Copy" aria-label="复制请求示例" @click="copy(requestExample(selectedOperation), 'request')" /></el-tooltip></div><pre>{{ requestExample(selectedOperation) }}</pre></section>
        <section class="operation-section api-code-section"><div class="code-heading"><h3>响应示例 <span>{{ selectedOperation.responseStatus }}</span></h3><el-tooltip :content="copied === 'response' ? '已复制' : '复制响应示例'"><el-button text :icon="copied === 'response' ? Check : Copy" aria-label="复制响应示例" @click="copy(stringify(selectedOperation.responseExample), 'response')" /></el-tooltip></div><pre>{{ stringify(selectedOperation.responseExample) }}</pre></section>
      </article>
      <div v-else class="api-detail-empty"><BookOpen :size="30" /><p>选择左侧接口查看参数和调用示例。</p></div>

      <aside class="api-guides" aria-label="接入指南"><div class="guide-heading"><BookOpen :size="18" /><h2>接入指南</h2></div><section v-for="guide in catalog.guides" :key="guide.title"><h3>{{ guide.title }}</h3><p>{{ guide.text }}</p></section><section class="guide-error"><h3>统一错误响应</h3><pre>{{ stringify(catalog.errorExample) }}</pre></section></aside>
    </div>
  </section>
</template>

<style scoped>
.api-reference { min-width: 0; }
.api-guides p { overflow-wrap: anywhere; }
.api-guides pre { overflow-x: auto; }
.api-reference-intro, .api-reference-title, .operation-label, .operation-section h3, .code-heading, .guide-heading { display: flex; align-items: center; gap: 10px; }
.api-reference-intro { justify-content: space-between; gap: 16px; padding: 18px 0; border-top: 1px solid #e1e8e8; border-bottom: 1px solid #e1e8e8; }
.api-reference-title strong, .operation-heading h2, .api-guides h2 { margin: 0; color: #263536; font-size: 16px; }
.api-reference-title p { margin-top: 4px; font-size: 12px; }
.api-reference-grid { display: grid; grid-template-columns: minmax(230px, .8fr) minmax(420px, 1.6fr) minmax(230px, .8fr); min-height: 0; height: max(580px, calc(100dvh - 250px)); border: 1px solid #dfe7e7; background: #fff; }
.api-reference-directory, .api-guides { min-width: 0; min-height: 0; background: #fbfcfc; }
.api-reference-directory { display: flex; flex-direction: column; overflow: hidden; border-right: 1px solid #e2e9e8; }
.api-search { display: flex; align-items: center; gap: 8px; margin: 14px; padding: 0 10px; height: 36px; color: #7a898b; background: #fff; border: 1px solid #dce5e4; border-radius: 5px; }
.api-search input { width: 100%; min-width: 0; color: #334446; border: 0; outline: 0; background: transparent; }
.api-groups { display: flex; gap: 5px; overflow-x: auto; padding: 0 14px 12px; border-bottom: 1px solid #e8eeee; }
.api-groups button { display: flex; gap: 7px; align-items: center; flex: 0 0 auto; padding: 5px 7px; color: #657477; border: 0; border-radius: 4px; background: transparent; font-size: 11px; cursor: pointer; }
.api-groups button:hover, .api-groups button.active { color: #08756a; background: #eaf5f1; }
.api-groups small { color: inherit; font-variant-numeric: tabular-nums; }
.api-operation-list { flex: 1 1 auto; min-height: 0; overflow: auto; }
.api-operation-list > button { display: grid; grid-template-columns: auto minmax(0, 1fr) auto; gap: 8px; width: 100%; padding: 11px 14px; border: 0; border-bottom: 1px solid #edf1f1; color: #6c7b7d; background: transparent; text-align: left; cursor: pointer; }
.api-operation-list > button:hover, .api-operation-list > button.active { background: #f0f8f5; color: #08756a; }
.api-operation-list strong, .api-operation-list code { display: block; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.api-operation-list strong { color: #334345; font-size: 13px; font-weight: 600; }
.api-operation-list code { margin-top: 4px; color: #879497; font-size: 11px; }
.http-method { display: inline-block; min-width: 38px; color: #087568; font: 600 11px ui-monospace, monospace; line-height: 19px; }
.http-method.post { color: #286c9e; }.http-method.patch { color: #986622; }.http-method.delete { color: #b5484b; }.http-method.ws { color: #7c579a; }
.api-operation-detail { min-width: 0; min-height: 0; padding: 26px; overflow-y: auto; }
.operation-heading { padding-bottom: 22px; border-bottom: 1px solid #e5eceb; }.operation-heading h2 { margin-top: 13px; font-size: 19px; }.operation-heading p { margin-top: 8px; line-height: 1.65; }.operation-label { min-width: 0; flex-wrap: wrap; align-items: center; }.operation-label code { min-width: 0; overflow-wrap: anywhere; }.operation-label .el-tag { display: inline-flex; align-items: center; gap: 5px; height: auto; max-width: 100%; white-space: normal; line-height: 1.5; }
.operation-section { padding: 20px 0; border-bottom: 1px solid #e9eeee; }.operation-section h3 { margin: 0 0 12px; color: #405154; font-size: 13px; }.operation-section h3 span { color: #25806d; font: 600 12px ui-monospace, monospace; }
.parameter-list { border: 1px solid #e1e9e8; }.parameter-row { display: grid; grid-template-columns: minmax(120px, .75fr) minmax(0, 1fr) minmax(90px, .7fr); gap: 12px; padding: 10px 12px; border-bottom: 1px solid #edf1f1; font-size: 12px; }.parameter-row:last-child { border-bottom: 0; }.parameter-row strong, .parameter-row span { display: block; }.parameter-row strong { color: #334446; }.parameter-row span, .parameter-row p { margin-top: 4px; color: #819093; font-size: 12px; line-height: 1.45; }.parameter-row code { align-self: center; color: #64777a; overflow-wrap: anywhere; }.request-fields { margin-top: 18px; }.request-fields h4 { margin: 0 0 9px; color: #536669; font-size: 12px; }
.permission-copy { line-height: 1.65; }.header-list { display: grid; gap: 6px; margin-top: 12px; }.header-list code { max-width: 100%; padding: 7px 9px; color: #506568; background: #f2f6f5; border-left: 2px solid #bcded6; overflow-wrap: anywhere; }.header-list span, .section-empty { color: #879598; font-size: 13px; }
.api-code-section pre, .guide-error pre { max-height: 320px; margin: 0; padding: 14px; overflow: auto; color: #d6e3df; background: #182723; border-radius: 5px; font: 12px/1.6 ui-monospace, SFMono-Regular, Menlo, monospace; white-space: pre; }.code-heading { justify-content: space-between; }.code-heading h3 { margin-bottom: 0; }
.api-guides { min-height: 0; padding: 22px 18px; border-left: 1px solid #e2e9e8; overflow-y: auto; }.guide-heading { padding-bottom: 16px; border-bottom: 1px solid #e3e9e8; }.api-guides section { padding: 14px 0; border-bottom: 1px solid #e7edec; }.api-guides h3 { margin: 0 0 7px; color: #45575a; font-size: 13px; }.api-guides p { color: #738286; font-size: 13px; line-height: 1.65; }.guide-error { border-bottom: 0 !important; }.guide-error pre { max-height: 180px; padding: 10px; font-size: 11px; }
.api-reference-error, .api-reference-loading, .api-detail-empty { min-height: 300px; padding: 32px; border: 1px solid #dee7e5; background: #fff; }.api-reference-error { display: grid; justify-items: start; gap: 10px; color: #6f7e81; }.api-reference-error strong { color: #a03f43; }.api-reference-loading { padding-top: 48px; }.api-detail-empty { display: grid; place-items: center; align-content: center; gap: 12px; color: #879599; }
@media (max-width: 1200px) { .api-reference-grid { height: auto; grid-template-columns: minmax(220px, .8fr) minmax(0, 1.8fr); }.api-reference-directory { overflow: visible; }.api-operation-list { max-height: 620px; flex: initial; }.api-operation-detail { overflow: visible; }.api-guides { display: grid; grid-column: 1 / -1; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 0 16px; overflow: visible; border-top: 1px solid #e2e9e8; border-left: 0; }.guide-heading { grid-column: 1 / -1; }.api-guides section { min-width: 0; }.guide-error { grid-column: span 2; } }
@media (max-width: 760px) { .api-reference-intro { align-items: flex-start; }.api-reference-intro .el-button { flex: 0 0 auto; }.api-reference-grid { display: block; }.api-reference-directory { border-right: 0; border-bottom: 1px solid #e2e9e8; }.api-operation-list { max-height: 300px; }.api-operation-detail { padding: 18px; }.parameter-row { grid-template-columns: 1fr; gap: 5px; }.api-guides { display: block; padding: 18px; }.guide-error { grid-column: auto; }.api-code-section pre { max-height: 280px; } }
</style>
