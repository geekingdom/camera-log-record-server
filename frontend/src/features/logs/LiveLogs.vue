<script setup lang="ts">
// 实时日志只暂停本地视图；连接、去重和有界接收始终继续运行。
import { computed, nextTick, onBeforeUnmount, ref, watch } from "vue";
import { AArrowDown, AArrowUp, LocateFixed, Pause, Play, Send, Trash2, FileSearch, Search } from "lucide-vue-next";
import { ElMessage } from "element-plus";
import { api, getToken } from "../../shared/api";
import type { Task } from "../../shared/types";
import {
  LiveLogBuffer,
  type LiveLogFrame,
  type LiveLogRange,
} from "../../shared/composables/liveLogBuffer";
import { stripTerminalControls } from "../../shared/terminalDisplay";
import { websocketUrl } from "../../shared/websocketUrl";
import LiveLogRanges from "./LiveLogRanges.vue";
import LogFindBar from "./LogFindBar.vue";
import LogText from "./LogText.vue";
import { findTextMatches } from "./logText";
import { appendCommandHistory, commandSuggestions, readCommandHistory, writeCommandHistory } from "./liveCommandHistory";

const props = defineProps<{ taskId: string; userId?: string; canSend?: boolean }>();
const emit = defineEmits<{ history: [] }>();
const buffer = new LiveLogBuffer();
const lines = ref<string[]>([]);
const connected = ref(false);
const taskState = ref<Task>();
let stateTimer: ReturnType<typeof setTimeout> | undefined;
let stateGeneration = 0;
// 控制通道与实时接收独立显示，命令阻断不能使日志订阅断开。
async function readTaskState(current: number) {
  try {
    const state = await api.task(props.taskId);
    if (current === stateGeneration) taskState.value = state;
  } catch { /* 短时状态请求失败不影响已建立的日志订阅。 */ }
  finally {
    if (current === stateGeneration) stateTimer = setTimeout(() => void readTaskState(current), 3000);
  }
}
const command = ref("");
const commandHistory = ref<string[]>([]);
const commandHistoryIndex = ref(-1);
const commandDraft = ref("");
const sending = ref(false);
const composing = ref(false);
const gaps = ref(0);
const omitted = ref(0);
const missingRanges = ref<LiveLogRange[]>([]);
const droppedRangeCount = ref(0);
const rangesOpen = ref(false);
const scrollTop = ref(0);
const pausedView = ref(false);
const follow = ref(true);
const findOpen = ref(false);
const findQuery = ref("");
const activeMatchIndex = ref(0);
const fontSize = ref(12);
const consoleRef = ref<HTMLElement>();
const rowHeight = 24;
const viewportHeight = ref(264);
const overscan = 8;
const first = computed(() =>
  Math.max(0, Math.floor(scrollTop.value / rowHeight) - overscan),
);
const last = computed(() =>
  Math.min(
    lines.value.length,
    first.value + Math.ceil(viewportHeight.value / rowHeight) + overscan * 2,
  ),
);
// buffer 保留原始行与 offset；只为当前虚拟视口生成去终端控制码的显示文本。
const visible = computed(() => lines.value.slice(first.value, last.value));
const matches = computed(() => {
  const result: Array<{ line: number; start: number }> = [];
  for (let line = 0; line < lines.value.length && result.length < 1000; line += 1) {
    for (const match of findTextMatches(stripTerminalControls(lines.value[line]), findQuery.value)) {
      result.push({ line, start: match.start });
      if (result.length >= 1000) break;
    }
  }
  return result;
});
const matchesTruncated = computed(() => matches.value.length >= 1000);
const activeMatch = computed(() => matches.value[activeMatchIndex.value]);
const commandCandidates = computed(() => commandSuggestions(commandHistory.value, command.value));
const topSpacer = computed(() => ({
  height: String(first.value * rowHeight) + "px",
}));
const bottomSpacer = computed(() => ({
  height: String((lines.value.length - last.value) * rowHeight) + "px",
}));
let socket: WebSocket | undefined;
let generation = 0;
let retry: number | undefined;
let syncTimer: number | undefined;

// 每 100ms 批量复制一次数组，避免高频帧逐条触发虚拟列表重算。
function scheduleSync() {
  if (syncTimer) return;
  syncTimer = window.setTimeout(async () => {
    syncTimer = undefined;
    gaps.value = buffer.gaps;
    omitted.value = buffer.omitted;
    // 范围列表独立于暂停视图刷新；读取窗口自行冻结选中的范围终点。
    missingRanges.value = buffer.missingRanges.map(range => ({ ...range }));
    droppedRangeCount.value = buffer.droppedRangeCount;
    // 查找时冻结显示快照，接收器和 buffer 仍继续写入，防止定位被新日志挤走。
    if (pausedView.value || Boolean(findQuery.value && matches.value.length)) return;
    lines.value = [...buffer.lines];
    await nextTick();
    if (follow.value)
      consoleRef.value?.scrollTo({ top: consoleRef.value.scrollHeight });
  }, 100);
}
function close() {
  generation += 1;
  window.clearTimeout(retry);
  window.clearTimeout(syncTimer);
  retry = undefined;
  syncTimer = undefined;
  socket?.close();
  socket = undefined;
  connected.value = false;
}
function resetForTask() {
  buffer.reset();
  lines.value = [];
  gaps.value = 0;
  omitted.value = 0;
  missingRanges.value = [];
  droppedRangeCount.value = 0;
  rangesOpen.value = false;
  scrollTop.value = 0;
  pausedView.value = false;
  follow.value = true;
  findOpen.value = false;
  findQuery.value = "";
  activeMatchIndex.value = 0;
}
function socketUrl() {
  return websocketUrl(`/api/v1/tasks/${encodeURIComponent(props.taskId)}/logs`, location);
}
// generation 防止旧任务的延迟 socket 回调污染新任务视图；cursor 用于断线恢复。
function openSocket(current: number) {
  socket = new WebSocket(socketUrl());
  socket.onopen = () => {
    if (current !== generation) return;
    // Cookie 会话同源建立后无需在帧中重复发送凭据；保留 Bearer 工具兼容值。
    socket?.send(JSON.stringify({ token: getToken(), cursor: buffer.cursor }));
    connected.value = true;
  };
  socket.onmessage = ({ data }) => {
    if (current !== generation) return;
    try {
      buffer.ingest(JSON.parse(data) as LiveLogFrame);
      scheduleSync();
    } catch {
      // 忽略格式不完整的传输帧。
    }
  };
  socket.onclose = () => {
    if (current !== generation) return;
    connected.value = false;
    retry = window.setTimeout(() => openSocket(current), 1000);
  };
  socket.onerror = () => {
    if (current === generation) socket?.close();
  };
}
function connect() {
  close();
  resetForTask();
  openSocket(generation);
}
function toggleView() {
  pausedView.value = !pausedView.value;
  if (!pausedView.value) scheduleSync();
}
function clearView() {
  buffer.clearView();
  lines.value = [];
  gaps.value = 0;
  omitted.value = 0;
  missingRanges.value = [];
  droppedRangeCount.value = 0;
  rangesOpen.value = false;
  scrollTop.value = 0;
}
function followLatest() {
  follow.value = true;
  findQuery.value = "";
  scheduleSync();
}
function onScroll(event: Event) {
  const target = event.target as HTMLElement;
  scrollTop.value = target.scrollTop;
  follow.value =
    target.scrollHeight - target.clientHeight - target.scrollTop <
    rowHeight * 2;
}
async function send() {
  const value = command.value.trim();
  if (!props.canSend || sending.value || !value) return;
  if (taskState.value?.commandBlocked && value !== "debug") return ElMessage.warning("命令通道尚未恢复，日志采集仍在继续");
  const current = ++commandGeneration;
  sending.value = true;
  try {
    await api.command(props.taskId, {
      command: value,
      newline: "\n",
      delaySeconds: 0,
      prompt: null,
      timeoutSeconds: 30,
    });
    if (current !== commandGeneration) return;
    commandHistory.value = appendCommandHistory(commandHistory.value, value);
    writeCommandHistory(props.userId, props.taskId, commandHistory.value);
    command.value = "";
    commandHistoryIndex.value = -1;
    ElMessage.success("命令已提交");
  } catch (error) { if (current === commandGeneration) ElMessage.error(error instanceof Error ? error.message : "命令发送失败"); }
  finally { if (current === commandGeneration) sending.value = false; }
}
let commandGeneration = 0;
function locate(delta: number) {
  if (!matches.value.length) return;
  activeMatchIndex.value = (activeMatchIndex.value + delta + matches.value.length) % matches.value.length;
  const match = matches.value[activeMatchIndex.value];
  consoleRef.value?.scrollTo({ top: match.line * rowHeight });
}
function closeFind() { findQuery.value = ""; findOpen.value = false; activeMatchIndex.value = 0; scheduleSync(); }
function onCommandKeydown(event: KeyboardEvent) {
  if (composing.value || event.isComposing || event.keyCode === 229) return;
  if (event.key === "Enter") { event.preventDefault(); void send(); return; }
  if (!commandHistory.value.length || !["ArrowUp", "ArrowDown"].includes(event.key)) return;
  event.preventDefault();
  if (commandHistoryIndex.value < 0) commandDraft.value = command.value;
  commandHistoryIndex.value = event.key === "ArrowUp"
    ? Math.min(commandHistoryIndex.value + 1, commandHistory.value.length - 1)
    : Math.max(commandHistoryIndex.value - 1, -1);
  command.value = commandHistoryIndex.value < 0 ? commandDraft.value : commandHistory.value[commandHistoryIndex.value];
}
watch(() => props.taskId, connect, { immediate: true });
watch(() => props.taskId, () => {
  clearTimeout(stateTimer);
  taskState.value = undefined;
  void readTaskState(++stateGeneration);
}, { immediate: true });
watch(() => [props.userId, props.taskId], () => {
  commandGeneration += 1;
  sending.value = false;
  command.value = "";
  commandDraft.value = "";
  commandHistoryIndex.value = -1;
  commandHistory.value = readCommandHistory(props.userId, props.taskId);
}, { immediate: true });
watch(matches, value => { if (activeMatchIndex.value >= value.length) activeMatchIndex.value = 0; });
watch(findQuery, async () => {
  activeMatchIndex.value = 0;
  await nextTick();
  if (findQuery.value) locate(0);
  else scheduleSync();
});
// 日志窗高度随抽屉/工作台变化，虚拟列表按实际高度计算，避免放大后底部空白。
const resize = new ResizeObserver(entries => {
  viewportHeight.value = entries[0]?.contentRect.height ?? 264;
});
watch(consoleRef, (current, previous) => {
  if (previous) resize.unobserve(previous);
  if (current) resize.observe(current);
});
onBeforeUnmount(() => { stateGeneration++; commandGeneration++; clearTimeout(stateTimer); resize.disconnect(); close(); });
</script>
<template>
  <section class="form-section runtime runtime-terminal">
    <div class="section-heading runtime-heading">
      <h2>运行日志</h2>
      <div class="log-tools">
        <el-tag :type="connected ? 'success' : 'info'">{{
          connected ? "实时连接中" : "正在重连"
        }}</el-tag>
        <el-tooltip :content="pausedView ? '继续视图' : '暂停视图'"
          ><el-button
            text
            :icon="pausedView ? Play : Pause"
            :aria-label="pausedView ? '继续视图' : '暂停视图'"
            @click="toggleView"
        /></el-tooltip>
        <el-tooltip :content="follow ? '正在跟随最新日志' : '跟随最新日志'"
          ><el-button
            text
            :icon="LocateFixed"
            aria-label="跟随最新日志"
            :type="follow ? 'primary' : 'default'"
            @click="followLatest"
        /></el-tooltip>
        <el-tooltip content="清空本地视图"
          ><el-button
            text
            :icon="Trash2"
            aria-label="清空本地视图"
            @click="clearView"
        /></el-tooltip>
        <el-tooltip content="减小字号"><el-button text :icon="AArrowDown" aria-label="减小字号" :disabled="fontSize <= 10" @click="fontSize -= 1" /></el-tooltip>
        <el-tooltip content="增大字号"><el-button text :icon="AArrowUp" aria-label="增大字号" :disabled="fontSize >= 18" @click="fontSize += 1" /></el-tooltip>
        <el-tooltip content="查找"><el-button text :icon="Search" aria-label="查找日志" :type="findOpen ? 'primary' : 'default'" @click="findOpen = true" /></el-tooltip>
      </div>
    </div>
    <LogFindBar v-if="findOpen" v-model="findQuery" :current="matches.length ? activeMatchIndex + 1 : 0" :total="matches.length" :truncated="matchesTruncated" @previous="locate(-1)" @next="locate(1)" @close="closeFind" />
    <p v-if="gaps || omitted || missingRanges.length" class="log-gap">
      <span v-if="gaps">检测到 {{ gaps }} 个传输缺口。</span>
      <span v-if="omitted">本地已省略 {{ omitted }} 行高频日志。</span>
      <span v-if="!gaps && !omitted">有日志已移出本地视图。</span>
      <el-button :icon="FileSearch" @click="rangesOpen = true">查看省略范围</el-button>
      <el-button :icon="FileSearch" @click="emit('history')">查看原始日志</el-button>
    </p>
    <div ref="consoleRef" class="log-console virtual-log terminal-console" :style="{ fontSize: `${fontSize}px` }" @scroll="onScroll">
      <div :style="topSpacer" />
      <div
        v-for="(line, index) in visible"
        :key="first + index"
        class="log-line"
      >
        <LogText :text="line" :query="findQuery" :active-start="activeMatch?.line === first + index ? activeMatch.start : undefined" />
      </div>
      <div :style="bottomSpacer" />
    </div>
    <div class="manual-command">
      <div class="command-entry">
        <el-input v-model="command" :disabled="!props.canSend || sending" placeholder="输入手工命令" aria-label="输入手工命令" @input="commandHistoryIndex = -1" @compositionstart="composing = true" @compositionend="composing = false" @keydown="onCommandKeydown" />
        <div v-if="command.trim() && commandCandidates.length" class="command-suggestions" role="listbox">
          <button v-for="item in commandCandidates" :key="item" type="button" role="option" @mousedown.prevent="command = item; commandHistoryIndex = -1">{{ item }}</button>
        </div>
      </div>
      <el-button v-if="props.canSend" type="primary" :icon="Send" :loading="sending" :disabled="sending || !command.trim() || (taskState?.commandBlocked && command.trim() !== 'debug')" @click="send">发送</el-button>
    </div>
    <el-alert v-if="taskState?.debugError || taskState?.commandBlocked" :title="taskState.commandBlocked ? '命令通道尚未恢复，日志采集继续' : '调试切换失败，普通命令与日志采集继续'" :description="taskState.debugError || undefined" type="warning" :closable="false" />
    <LiveLogRanges v-model="rangesOpen" :task-id="props.taskId" :ranges="missingRanges" :dropped-count="droppedRangeCount" @history="emit('history')" />
  </section>
</template>
<style scoped>
.runtime-terminal { display: flex; flex: 1 1 auto; flex-direction: column; height: min(680px, 70dvh); min-height: 360px; padding: 8px 0 0; border-top: 0; }
.runtime-heading { flex: 0 0 auto; margin-bottom: 0; }
.runtime-heading h2 { font-size: 13px; }
.log-tools { display: flex; align-items: center; gap: 2px; }
.log-gap { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; }
.log-gap .el-button + .el-button { margin-left: 0; }
.terminal-console { flex: 1 1 auto; min-height: 0; height: auto; margin: 8px 0; padding: 4px 0; border-radius: 3px; background: #151719; border-color: #30353b; color: #dce1e6; }
.manual-command { align-items: flex-start; }
.command-entry { position: relative; flex: 1; min-width: 0; }
.command-suggestions { position: relative; margin-top: 2px; max-height: 120px; overflow: auto; border: 1px solid #3c5148; background: #17221e; }
.command-suggestions button { display: block; width: 100%; padding: 5px 8px; border: 0; background: transparent; color: #d6e6de; font: 12px ui-monospace, SFMono-Regular, Menlo, monospace; text-align: left; }
.command-suggestions button:hover, .command-suggestions button:focus-visible { background: #294237; outline: none; }
.log-tools { flex-wrap: wrap; }
.log-tools .el-button + .el-button { margin-left: 0; }
@media (max-width: 520px) {
  .runtime-heading { gap: 4px; }
  .log-tools { width: 100%; }
  .log-tools .el-button { padding: 7px; }
}
</style>
