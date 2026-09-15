<script setup lang="ts">
// 实时缺口阅读器只补读服务端已定位的文件字节范围，不能把不确定的缺口伪装为连续日志。
import { computed, onBeforeUnmount, ref, watch } from "vue";
import { ArrowRight, FileSearch, RotateCcw } from "lucide-vue-next";
import { api, type LogGapFragment } from "../../shared/api";
import type { LiveLogRange } from "../../shared/composables/liveLogBuffer";
import { TerminalDisplayCleaner } from "../../shared/terminalDisplay";

const props = defineProps<{
  taskId: string;
  ranges: LiveLogRange[];
  droppedCount: number;
}>();
const emit = defineEmits<{ history: [] }>();
const open = defineModel<boolean>({ default: false });

const pageBytes = 65536;
const maxPreviewCharacters = 262144;
const selected = ref<LiveLogRange>();
const preview = ref("");
const cursor = ref(0);
const busy = ref(false);
const completed = ref(false);
const pending = ref(false);
const previewTruncated = ref(false);
const readError = ref("");
const catalogFragments = ref<LogGapFragment[]>([]);
const catalogIndex = ref(0);
const catalogIssues = ref<string[]>([]);
const maxCatalogFragments = 200;
let generation = 0;
let decoder = new TextDecoder();
let terminalCleaner = new TerminalDisplayCleaner();
let requestController: AbortController | undefined;
let requestTimeout: number | undefined;

const newestRanges = computed(() =>
  props.ranges.slice().reverse(),
);
const selectedHasLocation = computed(() => {
  const range = activeRange.value;
  return Boolean(
    range?.fileId &&
      Number.isSafeInteger(range.start) &&
      Number.isSafeInteger(range.end) &&
      (range.start ?? -1) >= 0 &&
      (range.end ?? -1) >= (range.start ?? 0),
  );
});
const activeRange = computed<LogGapFragment | LiveLogRange | undefined>(() =>
  catalogFragments.value[catalogIndex.value] ?? selected.value,
);
const rangeEnd = computed(() => activeRange.value?.end ?? 0);
const selectedCanCatalog = computed(() => {
  const range = selected.value;
  return Boolean(range?.reason === "server" && range.beforeFileId && Number.isSafeInteger(range.beforeOffset) && range.beforeSessionId && range.afterFileId && Number.isSafeInteger(range.afterOffset) && range.afterSessionId);
});
const reasonLabel: Record<LiveLogRange["reason"], string> = {
  rate: "本地限速省略",
  transport: "传输中断",
  retention: "本地缓冲过期",
  server: "服务端缓冲缺口",
};

// 取消底层请求并推进代次；即使响应恰好到达，也不能覆盖当前任务或选择。
function invalidate() {
  generation += 1;
  requestController?.abort();
  requestController = undefined;
  window.clearTimeout(requestTimeout);
  requestTimeout = undefined;
  busy.value = false;
}

function resetReader() {
  invalidate();
  selected.value = undefined;
  preview.value = "";
  cursor.value = 0;
  completed.value = false;
  pending.value = false;
  previewTruncated.value = false;
  readError.value = "";
  catalogFragments.value = [];
  catalogIndex.value = 0;
  catalogIssues.value = [];
  decoder = new TextDecoder();
  terminalCleaner = new TerminalDisplayCleaner();
}

function formatOffset(offset: number | undefined) {
  return Number.isSafeInteger(offset) ? offset!.toLocaleString("zh-CN") : "未知";
}

function appendPreview(value: string) {
  const combined = preview.value + value;
  if (combined.length > maxPreviewCharacters) {
    preview.value = combined.slice(-maxPreviewCharacters);
    previewTruncated.value = true;
    return;
  }
  preview.value = combined;
}

function selectRange(range: LiveLogRange) {
  resetReader();
  // 范围来自持续刷新的缓冲区；复制元数据后本次阅读的文件、会话和终点不可被后续帧改写。
  selected.value = { ...range };
  if (range.reason === "server" && selectedCanCatalog.value) {
    void loadCatalog();
    return;
  }
  const start = range.start;
  const end = range.end;
  if (
    !range.fileId ||
    start === undefined ||
    end === undefined ||
    !Number.isSafeInteger(start) ||
    !Number.isSafeInteger(end) ||
    start < 0 ||
    end < start
  ) {
    pending.value = true;
    return;
  }
  cursor.value = start;
  if (end === start) completed.value = true;
  else void readNext();
}

// 目录本身受限分页；前端最多冻结 200 个片段，避免异常目录占满本地内存。
async function loadCatalog() {
  const range = selected.value;
  if (!range || !selectedCanCatalog.value) return;
  const current = ++generation;
  const controller = new AbortController();
  requestController = controller;
  requestTimeout = window.setTimeout(() => controller.abort(), 30000);
  busy.value = true;
  pending.value = false;
  readError.value = "";
  try {
    const anchors = { beforeFileId: range.beforeFileId!, beforeOffset: range.beforeOffset!, beforeSessionId: range.beforeSessionId!, afterFileId: range.afterFileId!, afterOffset: range.afterOffset!, afterSessionId: range.afterSessionId! };
    let nextCursor: string | null | undefined;
    const fragments: LogGapFragment[] = [];
    do {
      const result = await api.taskGapCatalog(props.taskId, anchors, nextCursor ?? undefined, controller.signal);
      if (current !== generation) return;
      for (const item of result.items) {
        if (!item.fileId || !Number.isSafeInteger(item.start) || !Number.isSafeInteger(item.end) || item.start < 0 || item.end <= item.start)
          throw new Error("缺口目录返回了无效片段");
        fragments.push(item);
        if (fragments.length > maxCatalogFragments) throw new Error("缺口目录片段过多，请通过小时归档查看");
      }
      const knownIssues = new Set(catalogIssues.value);
      for (const issue of result.unrecoverable) {
        if (!knownIssues.has(issue.message)) {
          knownIssues.add(issue.message);
          catalogIssues.value.push(issue.message);
        }
      }
      nextCursor = result.nextCursor;
    } while (nextCursor);
    if (!fragments.length) {
      pending.value = true;
      return;
    }
    catalogFragments.value = fragments;
    catalogIndex.value = 0;
    cursor.value = fragments[0].start;
    completed.value = false;
    // 目录请求已经结束，释放 busy 后再启动第一个文件页，避免被本函数的并发保护拦截。
    busy.value = false;
    void readNext();
  } catch (error) {
    if (current === generation) readError.value = controller.signal.aborted ? "目录读取超时或已取消，请重试" : error instanceof Error ? error.message : "定位缺口目录失败";
  } finally {
    if (requestController === controller) {
      window.clearTimeout(requestTimeout);
      requestTimeout = undefined;
      requestController = undefined;
    }
    if (current === generation) busy.value = false;
  }
}

// 每次只请求一个有上限的页，服务端响应必须正好落在当前缺口内，不能被下一文件或新会话混入。
async function readNext() {
  const range = activeRange.value;
  if (!range || !selectedHasLocation.value || busy.value || completed.value) return;
  const from = cursor.value;
  const end = range.end!;
  const limit = Math.min(pageBytes, end - from);
  if (limit <= 0) {
    completed.value = true;
    return;
  }
  const current = ++generation;
  const controller = new AbortController();
  requestController = controller;
  requestTimeout = window.setTimeout(() => controller.abort(), 30000);
  busy.value = true;
  pending.value = false;
  readError.value = "";
  try {
    const result = await api.fileContent(range.fileId!, from, limit, controller.signal);
    if (current !== generation) return;
    if (result.fileId !== range.fileId)
      throw new Error("返回文件与缺口记录不一致");
    if (range.sessionId && result.sessionId !== range.sessionId)
      throw new Error("返回会话与缺口记录不一致");
    const bytes = Uint8Array.from(atob(result.data), value => value.charCodeAt(0));
    if (bytes.byteLength > limit || result.nextOffset !== from + bytes.byteLength || result.nextOffset > end)
      throw new Error("返回字节边界无效");
    if (!bytes.byteLength) {
      // 文件仍未写到目标边界时不能把空响应理解为完整内容。
      pending.value = true;
      return;
    }
    const done = result.nextOffset === end;
    appendPreview(terminalCleaner.append(decoder.decode(bytes, { stream: !done }), done));
    cursor.value = result.nextOffset;
    if (done && catalogIndex.value + 1 < catalogFragments.value.length) {
      catalogIndex.value += 1;
      cursor.value = catalogFragments.value[catalogIndex.value].start;
      completed.value = false;
    } else completed.value = done;
  } catch (error) {
    if (current === generation)
      readError.value = controller.signal.aborted
        ? "读取超时或已取消，请重试"
        : error instanceof Error ? error.message : "读取缺口范围失败";
  } finally {
    if (requestController === controller) {
      window.clearTimeout(requestTimeout);
      requestTimeout = undefined;
      requestController = undefined;
    }
    if (current === generation) busy.value = false;
  }
}

function openHistory() {
  resetReader();
  open.value = false;
  emit("history");
}

watch(open, value => {
  if (!value) resetReader();
});
watch(() => props.taskId, resetReader);
onBeforeUnmount(invalidate);
</script>

<template>
  <el-dialog
    v-model="open"
    title="实时日志缺口"
    width="min(960px, calc(100% - 24px))"
    append-to-body
    destroy-on-close
  >
    <p class="range-summary">
      <span v-if="props.droppedCount">另有 {{ props.droppedCount.toLocaleString() }} 个较早缺口范围记录已从本地列表淘汰。</span>
      <span v-if="props.ranges.length">已记录 {{ props.ranges.length.toLocaleString() }} 个缺口范围，以下按最新优先显示。</span>
      <span v-else>当前没有可定位的精确缺口范围。</span>
    </p>

    <div v-if="selected" class="range-reader" aria-live="polite">
      <div class="range-reader-heading">
        <div>
          <h3>缺口原文</h3>
          <p>
            {{ reasonLabel[selected.reason] }}
            <span v-if="selected.fileId"> · 文件 {{ selected.fileId }}</span>
            <span v-if="selected.sessionId"> · 会话 {{ selected.sessionId }}</span>
            <span> · 字节范围 [{{ formatOffset(selected.start) }}, {{ formatOffset(selected.end) }})</span>
          </p>
          <p v-if="selectedCanCatalog">目录补读只包含服务端已保存的片段；设备断网期间未送达服务端的内容无法恢复。</p>
          <p v-else-if="selectedHasLocation">仅显示该文件内的精确字节范围；跨文件的行不会被拼接。</p>
        </div>
        <el-button text @click="resetReader">返回缺口列表</el-button>
      </div>
      <el-alert
        v-if="!selectedHasLocation && !readError"
        title="服务端没有提供可精确补读的文件范围"
        :description="selected.message || '请通过小时归档核对这一段日志。'"
        type="warning"
        :closable="false"
      />
      <template v-else-if="selectedHasLocation">
        <div class="range-reader-status">
          <span>已读取至 {{ formatOffset(cursor) }} / {{ formatOffset(rangeEnd) }}</span>
          <span v-if="catalogFragments.length">目录片段 {{ catalogIndex + 1 }} / {{ catalogFragments.length }}</span>
          <span v-if="completed">范围读取完成</span>
          <span v-else-if="pending">文件内容尚不可用，请稍后重试。</span>
          <span v-else-if="busy">正在读取…</span>
        </div>
        <el-alert
          v-if="readError"
          :title="readError"
          type="error"
          :closable="false"
          show-icon
        >
          <template #default>
            <el-button :icon="RotateCcw" size="small" @click="selectedCanCatalog && !catalogFragments.length ? loadCatalog() : readNext()">重试读取</el-button>
          </template>
        </el-alert>
        <el-alert
          v-if="previewTruncated"
          title="为限制本地内存，已仅保留最近的预览内容。"
          type="info"
          :closable="false"
        />
        <pre class="range-reader-content" v-loading="busy">{{ preview || (busy ? '' : '此页暂时没有可显示的内容') }}</pre>
        <div class="range-reader-actions">
          <el-button
            :icon="ArrowRight"
            :loading="busy"
            :disabled="busy || completed"
            @click="readNext"
          >继续读取范围</el-button>
        </div>
      </template>
      <el-alert v-else-if="readError" :title="readError" type="error" :closable="false" show-icon />
      <el-alert v-for="issue in catalogIssues" :key="issue" :title="issue" type="warning" :closable="false" />
    </div>

    <div v-else class="range-list" role="list" aria-label="缺口范围列表">
      <article v-for="range in newestRanges" :key="range.id" class="range-item" role="listitem">
        <div class="range-item-body">
          <strong>{{ reasonLabel[range.reason] }}</strong>
          <span v-if="range.fileId">文件 {{ range.fileId }}</span>
          <span v-if="range.sessionId">会话 {{ range.sessionId }}</span>
          <span v-if="range.fileId && range.start !== undefined && range.end !== undefined">字节 [{{ formatOffset(range.start) }}, {{ formatOffset(range.end) }})</span>
          <span v-else>{{ range.message || '没有可精确补读的文件边界。' }}</span>
          <span v-if="range.lines !== undefined">涉及 {{ range.lines.toLocaleString() }} 行</span>
        </div>
        <el-button
          v-if="(range.fileId && range.start !== undefined && range.end !== undefined) || (range.reason === 'server' && range.beforeFileId && range.afterFileId)"
          :aria-label="'读取缺口范围 ' + range.id"
          :icon="FileSearch"
          @click="selectRange(range)"
        >读取范围</el-button>
        <el-button v-else :icon="FileSearch" @click="openHistory">查看小时归档</el-button>
      </article>
      <el-empty v-if="!newestRanges.length" description="没有保存可阅读的精确缺口范围" :image-size="72" />
    </div>

    <template #footer>
      <el-button :icon="FileSearch" @click="openHistory">查看小时归档</el-button>
      <el-button @click="open = false">关闭</el-button>
    </template>
  </el-dialog>
</template>

<style scoped>
.range-summary { display: flex; flex-wrap: wrap; gap: 8px; margin: 0 0 16px; color: #6d8080; font-size: 13px; line-height: 1.6; }
.range-list { display: grid; gap: 8px; max-height: min(55vh, 520px); overflow: auto; padding-right: 4px; }
.range-item { display: flex; align-items: center; justify-content: space-between; gap: 12px; padding: 12px; border: 1px solid #dfe8e6; border-radius: 6px; background: #fbfcfc; }
.range-item-body { display: grid; min-width: 0; gap: 4px; color: #6d8080; font-size: 12px; overflow-wrap: anywhere; }
.range-item-body strong { color: #3c5050; font-size: 13px; font-weight: 600; }
.range-item .el-button { flex: 0 0 auto; }
.range-reader { display: grid; gap: 12px; }
.range-reader-heading { display: flex; align-items: start; justify-content: space-between; gap: 12px; }
.range-reader-heading h3 { margin: 0; font-size: 15px; color: #334949; }
.range-reader-heading p { margin: 6px 0 0; color: #6d8080; font-size: 12px; line-height: 1.6; overflow-wrap: anywhere; }
.range-reader-status { display: flex; flex-wrap: wrap; gap: 12px; color: #6d8080; font-size: 12px; }
.range-reader-content { min-height: 240px; height: 46vh; max-height: 560px; overflow: auto; margin: 0; padding: 16px; border-radius: 6px; background: #162521; color: #cdded8; font: 12px/1.7 ui-monospace, monospace; white-space: pre-wrap; overflow-wrap: anywhere; }
.range-reader-actions { display: flex; justify-content: flex-end; }
@media (max-width: 700px) {
  .range-item { align-items: stretch; flex-direction: column; }
  .range-item .el-button { align-self: flex-end; }
  .range-reader-heading { align-items: stretch; flex-direction: column; }
  .range-reader-heading .el-button { align-self: flex-start; }
  .range-reader-content { min-height: 200px; height: 42vh; padding: 12px; }
}
</style>
