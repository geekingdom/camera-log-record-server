<script setup lang="ts">
// 历史片段按字节游标读取；仅顺序翻页复用 decoder，跳转和换文件必须重置解码状态。
import { onBeforeUnmount, ref, watch } from "vue";
import { ArrowRight, RotateCcw } from "lucide-vue-next";
import { ElMessage } from "element-plus";
import { api } from "../../shared/api";
import type { LogFile } from "../../shared/types";
import { TerminalDisplayCleaner } from "../../shared/terminalDisplay";
const props = defineProps<{ file?: LogFile }>();
const open = defineModel<boolean>({ default: false });
const offset = ref(0), end = ref(0), start = ref(0), text = ref(""), displayText = ref(""), busy = ref(false), eof = ref(false);
let decoder = new TextDecoder(), terminalCleaner = new TerminalDisplayCleaner(), generation = 0;
async function read(continuing = false) {
  if (!props.file || busy.value) return;
  const current = ++generation;
  busy.value = true;
  try {
    if (!continuing) {
      decoder = new TextDecoder();
      terminalCleaner = new TerminalDisplayCleaner();
    }
    const from = continuing ? end.value : offset.value;
    const result = await api.fileContent(props.file.id, from);
    if (current !== generation) return;
    const bytes = Uint8Array.from(atob(result.data), value => value.charCodeAt(0));
    eof.value = result.nextOffset >= props.file.bytes || bytes.length === 0;
    text.value = decoder.decode(bytes, { stream: !eof.value });
    displayText.value = terminalCleaner.append(text.value, eof.value);
    start.value = from;
    end.value = result.nextOffset;
    offset.value = from;
  } catch (error) {
    if (current === generation) ElMessage.error(error instanceof Error ? error.message : "读取片段失败");
  } finally {
    if (current === generation) busy.value = false;
  }
}
watch(() => [open.value, props.file?.id], () => {
  generation++;
  busy.value = false;
  text.value = displayText.value = "";
  offset.value = end.value = start.value = 0;
  eof.value = false;
  if (open.value) void read();
});
onBeforeUnmount(() => generation++);
</script>
<template>
  <el-dialog v-model="open" title="日志片段内容" width="min(1100px, calc(100% - 24px))" append-to-body destroy-on-close>
    <div class="file-viewer-meta"><strong>{{ file?.archiveName || file?.rawFileName || file?.id }}</strong><span>字节范围 {{ start.toLocaleString() }} 至 {{ end.toLocaleString() }}</span></div>
    <div class="file-viewer-tools"><label>起始字节</label><el-input-number v-model="offset" :min="0" :max="file?.bytes" :precision="0" controls-position="right" aria-label="起始字节" /><el-button :icon="RotateCcw" :loading="busy" @click="read()">读取</el-button><el-button :icon="ArrowRight" :disabled="busy || eof" @click="read(true)">下一段</el-button></div>
    <pre class="file-viewer-content" v-loading="busy">{{ displayText || (busy ? '' : '此范围没有内容') }}</pre>
    <template #footer><el-button @click="open = false">关闭</el-button></template>
  </el-dialog>
</template>
<style scoped>
.file-viewer-meta { display: grid; gap: 8px; overflow-wrap: anywhere; color: #7c8b8e; font-size: 12px; }
.file-viewer-meta strong { color: #3c5050; font-weight: 500; }
.file-viewer-tools { display: flex; flex-wrap: wrap; align-items: center; gap: 10px; margin: 20px 0; }
.file-viewer-content { min-height: 240px; height: 50vh; overflow: auto; margin: 0; padding: 16px; background: #162521; color: #cdded8; border-radius: 6px; font: 12px/1.7 ui-monospace, monospace; white-space: pre; }
</style>
