<script setup lang="ts">
// 实时日志只暂停本地视图；连接、去重和有界接收始终继续运行。
import { computed, nextTick, onBeforeUnmount, ref, watch } from "vue";
import { LocateFixed, Pause, Play, Send, Trash2, FileSearch } from "lucide-vue-next";
import { ElMessage } from "element-plus";
import { api, getToken } from "../../shared/api";
import {
  LiveLogBuffer,
  type LiveLogFrame,
} from "../../shared/composables/liveLogBuffer";

const props = defineProps<{ taskId: string }>();
const emit = defineEmits<{ history: [] }>();
const buffer = new LiveLogBuffer();
const lines = ref<string[]>([]);
const connected = ref(false);
const command = ref("");
const gaps = ref(0);
const omitted = ref(0);
const scrollTop = ref(0);
const pausedView = ref(false);
const follow = ref(true);
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
const visible = computed(() => lines.value.slice(first.value, last.value));
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
    if (pausedView.value) return;
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
  scrollTop.value = 0;
  pausedView.value = false;
  follow.value = true;
}
function socketUrl() {
  const protocol = location.protocol === "https:" ? "wss:" : "ws:";
  return (
    protocol +
    "//" +
    location.host +
    "/api/v1/tasks/" +
    encodeURIComponent(props.taskId) +
    "/logs"
  );
}
// generation 防止旧任务的延迟 socket 回调污染新任务视图；cursor 用于断线恢复。
function openSocket(current: number) {
  socket = new WebSocket(socketUrl());
  socket.onopen = () => {
    if (current !== generation) return;
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
  scrollTop.value = 0;
}
function followLatest() {
  follow.value = true;
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
  if (!command.value.trim()) return;
  try {
    await api.command(props.taskId, {
      command: command.value,
      newline: "\n",
      delaySeconds: 0,
      prompt: null,
      timeoutSeconds: 30,
    });
    command.value = "";
    ElMessage.success("命令已提交");
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : "命令发送失败");
  }
}
watch(() => props.taskId, connect, { immediate: true });
// 日志窗高度随抽屉/工作台变化，虚拟列表按实际高度计算，避免放大后底部空白。
const resize = new ResizeObserver(entries => {
  viewportHeight.value = entries[0]?.contentRect.height ?? 264;
});
watch(consoleRef, (current, previous) => {
  if (previous) resize.unobserve(previous);
  if (current) resize.observe(current);
});
onBeforeUnmount(() => { resize.disconnect(); close(); });
</script>
<template>
  <section class="form-section runtime">
    <div class="section-heading">
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
      </div>
    </div>
    <p v-if="gaps || omitted" class="log-gap">
      <span v-if="gaps">服务端报告 {{ gaps }} 个日志缺口。</span>
      <span v-if="omitted">本地已省略 {{ omitted }} 行高频日志。</span>
      <el-button :icon="FileSearch" @click="emit('history')">查看原始日志</el-button>
    </p>
    <div ref="consoleRef" class="log-console virtual-log" @scroll="onScroll">
      <div :style="topSpacer" />
      <div
        v-for="(line, index) in visible"
        :key="first + index"
        class="log-line"
      >
        {{ line }}
      </div>
      <div :style="bottomSpacer" />
    </div>
    <div class="manual-command">
      <el-input
        v-model="command"
        placeholder="输入手工命令"
        @keyup.enter="send"
      /><el-button type="primary" :icon="Send" @click="send">发送</el-button>
    </div>
  </section>
</template>
