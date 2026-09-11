<script setup lang="ts">
// 归档阅读器只保存当前有界段；切换、关闭和跳段均取消旧请求并以代次隔离迟到响应。
import { computed, nextTick, onBeforeUnmount, ref, watch } from "vue";
import { ArrowLeft, ArrowRight, RotateCcw, Search } from "lucide-vue-next";
import { ElMessage } from "element-plus";
import { api } from "../../shared/api";
import type { LogFile } from "../../shared/types";
import { TerminalDisplayCleaner } from "../../shared/terminalDisplay";
import { findTextMatches } from "./logText";
import { completeUtf8Length, FILE_SEGMENT_BYTES, FILE_SEGMENT_STEP_BYTES, firstCompleteUtf8Offset, segmentFetchOffset, validateSegmentResponse } from "./fileViewerSegment";
import LogFindBar from "./LogFindBar.vue";
import LogText from "./LogText.vue";

const CONTEXT_BYTES = 8 * 1024;
const props = defineProps<{ file?: LogFile; initialOffset?: number; keyword?: string }>();
const open = defineModel<boolean>({ default: false });
const start = ref(0), end = ref(0), requestedOffset = ref(0), text = ref(""), busy = ref(false), eof = ref(false);
const findOpen = ref(false), findQuery = ref(""), activeMatch = ref(0);
const previousAnchors = ref<number[]>([]), nextAnchors = ref<number[]>([]);
const content = ref<HTMLElement>();
const matches = computed(() => findTextMatches(text.value, findQuery.value, Infinity));
const activeStart = computed(() => matches.value[activeMatch.value]?.start);
let generation = 0;
let controller: AbortController | undefined;

function cancelRead() {
  generation += 1;
  controller?.abort();
  controller = undefined;
  busy.value = false;
}

function clampOffset(value: number) {
  return Math.max(0, Math.floor(value));
}

function resetSegment() {
  start.value = 0;
  end.value = 0;
  requestedOffset.value = 0;
  text.value = "";
  eof.value = false;
  activeMatch.value = 0;
  previousAnchors.value = [];
  nextAnchors.value = [];
}

function findIndexNearCharacter(value: number) {
  if (!findQuery.value.trim()) return 0;
  if (!matches.value.length) return 0;
  return matches.value.reduce((nearest, match, index) =>
    Math.abs(match.start - value) < Math.abs(matches.value[nearest].start - value) ? index : nearest, 0);
}

async function read(offset: number, focusOffset?: number) {
  if (!props.file) return;
  cancelRead();
  const current = generation;
  const requested = clampOffset(offset);
  const from = segmentFetchOffset(requested);
  requestedOffset.value = requested;
  const activeController = new AbortController();
  controller = activeController;
  busy.value = true;
  try {
    const result = await api.fileContent(props.file.id, from, FILE_SEGMENT_BYTES, activeController.signal);
    if (current !== generation || activeController.signal.aborted) return;
    const bytes = Uint8Array.from(atob(result.data), value => value.charCodeAt(0));
    validateSegmentResponse(result.fileId, props.file.id, from, result.nextOffset, bytes.length,
      FILE_SEGMENT_BYTES, result.sessionId, props.file.sessionId);
    // 单次段独立解码，避免跳转时带入上一段尾部 UTF-8 或终端控制码状态。
    const firstComplete = firstCompleteUtf8Offset(bytes);
    const completeLength = completeUtf8Length(bytes);
    const readable = bytes.slice(Math.min(firstComplete, completeLength), completeLength);
    const decoded = new TextDecoder().decode(readable);
    text.value = new TerminalDisplayCleaner().append(decoded, true);
    start.value = from;
    end.value = result.nextOffset;
    eof.value = bytes.length === 0;
    // 命中 offset 是原始字节位置。先在同一段原始字节上解码前缀为字符位置，再在
    // 已去除终端控制码的展示文本选择最近命中，避免 UTF-8 多字节字符造成行定位漂移。
    const rawFocusCharacter = focusOffset === undefined ? undefined
      : new TextDecoder().decode(bytes.slice(Math.min(firstComplete, completeLength),
        Math.min(completeLength, Math.max(firstComplete, focusOffset - from)))).length;
    const focusCharacter = rawFocusCharacter === undefined ? undefined
      : new TerminalDisplayCleaner().append(decoded.slice(0, rawFocusCharacter), true).length;
    activeMatch.value = focusCharacter === undefined ? 0 : findIndexNearCharacter(focusCharacter);
    await nextTick();
    content.value?.querySelector(".log-search-active")?.scrollIntoView({ block: "center", behavior: "smooth" });
  } catch (error) {
    if (current === generation && !activeController.signal.aborted)
      ElMessage.error(error instanceof Error ? error.message : "读取片段失败");
  } finally {
    if (current === generation) {
      busy.value = false;
      controller = undefined;
    }
  }
}

function readInitial() {
  const target = clampOffset(props.initialOffset ?? 0);
  findQuery.value = props.keyword ?? "";
  findOpen.value = Boolean(findQuery.value);
  void read(Math.max(0, target - CONTEXT_BYTES), target);
}
// 请求锚点和网络重叠分离。history 使非整步命中先退到 0 后还能准确回到原段。
function previousSegment() {
  if (!requestedOffset.value) return;
  const target = previousAnchors.value.pop() ?? Math.max(0, requestedOffset.value - FILE_SEGMENT_STEP_BYTES);
  nextAnchors.value.push(requestedOffset.value);
  void read(target);
}
function nextSegment() {
  if (eof.value) return;
  previousAnchors.value.push(requestedOffset.value);
  const target = nextAnchors.value.pop() ?? requestedOffset.value + FILE_SEGMENT_STEP_BYTES;
  void read(target);
}
function changeMatch(direction: -1 | 1) {
  if (!matches.value.length) return;
  activeMatch.value = (activeMatch.value + direction + matches.value.length) % matches.value.length;
  void nextTick(() => content.value?.querySelector(".log-search-active")?.scrollIntoView({ block: "center" }));
}

watch(findQuery, () => {
  activeMatch.value = 0;
  void nextTick(() => content.value?.querySelector(".log-search-active")?.scrollIntoView({ block: "center" }));
});
watch(() => [open.value, props.file?.id, props.initialOffset, props.keyword], () => {
  cancelRead();
  resetSegment();
  if (open.value && props.file) readInitial();
}, { immediate: true });
onBeforeUnmount(cancelRead);
</script>
<template>
  <el-dialog v-model="open" title="日志片段内容" width="min(1180px, calc(100% - 24px))" class="file-viewer-dialog" append-to-body destroy-on-close @closed="cancelRead">
    <div class="file-viewer-meta"><strong>{{ file?.archiveName || file?.rawFileName || file?.id }}</strong><span>当前字节范围 {{ start.toLocaleString() }} 至 {{ end.toLocaleString() }}</span></div>
    <div class="file-viewer-tools">
      <el-button :icon="ArrowLeft" :disabled="busy || start === 0" @click="previousSegment">上一段</el-button>
      <el-button :icon="ArrowRight" :disabled="busy || eof" @click="nextSegment">下一段</el-button>
      <el-button :icon="RotateCcw" :loading="busy" @click="read(requestedOffset)">重新读取</el-button>
      <el-button :icon="Search" :type="findOpen ? 'primary' : 'default'" @click="findOpen = true">段内查找</el-button>
      <span class="segment-size">每段最多 256 KiB</span>
    </div>
    <LogFindBar v-if="findOpen" v-model="findQuery" :current="matches.length ? activeMatch + 1 : 0" :total="matches.length" @previous="changeMatch(-1)" @next="changeMatch(1)" @close="findOpen = false" />
    <pre ref="content" class="file-viewer-content" v-loading="busy"><LogText v-if="text" :text="text" :query="findQuery" :active-start="activeStart" /><span v-else-if="!busy">此范围没有内容</span></pre>
    <template #footer><el-button @click="open = false">关闭</el-button></template>
  </el-dialog>
</template>
<style scoped>
:global(.file-viewer-dialog) { max-height: calc(100dvh - 112px); margin: 32px auto; }
.file-viewer-meta { display: grid; gap: 7px; overflow-wrap: anywhere; color: #8a9a9f; font-size: 12px; }
.file-viewer-meta strong { color: #254344; font-weight: 600; }
.file-viewer-tools { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; margin: 16px 0 10px; }
.segment-size { color: #7d9294; font-size: 12px; }
.file-viewer-content { min-height: 300px; height: min(58vh, 720px); overflow: auto; margin: 10px 0 0; padding: 16px; border: 1px solid #203c40; border-radius: 6px; background: #09171b; color: #d3e7e5; font: 12px/1.7 ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace; white-space: pre-wrap; overflow-wrap: anywhere; }
@media (max-width: 640px) { .file-viewer-tools .el-button { flex: 1 1 calc(50% - 8px); } .file-viewer-content { height: 56vh; padding: 12px; } }
</style>
