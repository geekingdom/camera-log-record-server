<script setup lang="ts">
// 小时归档与检索工作区：作业轮询支持取消，generation 防止切换任务后显示旧结果。
import { onBeforeUnmount, ref, watch } from "vue";
import { Download, FileSearch, RefreshCw, X } from "lucide-vue-next";
import { ElMessage } from "element-plus";
import { api } from "../../shared/api";
import type { LogFile, LogHour } from "../../shared/types";
import HourFragments from "./HourFragments.vue";
import LogFileViewer from "./LogFileViewer.vue";
import { stripTerminalControls } from "../../shared/terminalDisplay";
const props = defineProps<{ taskId: string }>();
const date = ref<string>(), hourPage = ref(1), hourTotal = ref(0), progress = ref(0);
const file = ref<LogFile>(), viewerOpen = ref(false), resultPage = ref(1), resultTotal = ref(0), resultJob = ref("");
const integrityLabels: Record<string, string> = { VERIFIED: "归档已校验", OPEN: "正在写入", UNAVAILABLE: "含不可用片段", UNVERIFIED: "待确认摘要" };
const jobStatusLabels: Record<string, string> = { QUEUED: "等待执行", RUNNING: "正在执行", SUCCEEDED: "已完成", FAILED: "执行失败", CANCELLED: "已取消", EXPIRED: "已过期" };
function viewFile(selectedFile: LogFile) { file.value = selectedFile; viewerOpen.value = true; }
function displayResultText(row: Record<string, unknown>) {
  return stripTerminalControls(typeof row.text === "string" ? row.text : "");
}
const hours = ref<LogHour[]>([]),
  selected = ref<string[]>([]),
  keyword = ref("");
const results = ref<Record<string, unknown>[]>([]),
  busy = ref(false),
  allowPartial = ref(false);
const status = ref(""),
  truncated = ref(false),
  range = ref<[Date, Date]>();
let generation = 0;
let hoursGeneration = 0;
let resultsGeneration = 0;
let jobGeneration = 0;
let runningJob: { id: string; kind: "downloads" | "log-searches" } | undefined;
function fail(error: unknown) {
  ElMessage.error(error instanceof Error ? error.message : "操作失败");
}
async function load() {
  const workspace = generation;
  const current = ++hoursGeneration;
  // 日期或分页变更后立即撤销旧小时选择，不能在新目录到达前下载上一次的内容。
  selected.value = [];
  hours.value = [];
  hourTotal.value = 0;
  try {
    const response = await api.logHours(props.taskId, date.value, hourPage.value);
    if (workspace === generation && current === hoursGeneration) {
      hours.value = response.items;
      hourTotal.value = response.total;
    }
  } catch (error) {
    if (workspace === generation && current === hoursGeneration) fail(error);
  }
}
// 服务端以异步 job 处理下载与检索，统一轮询到终态后再取结果或授权 URL。
async function poll(
  id: string,
  kind: "downloads" | "log-searches",
  current: number,
) {
  for (;;) {
    if (current !== jobGeneration) throw new Error("页面已切换");
    const job =
      kind === "downloads"
        ? await api.downloadStatus(id)
        : await api.searchStatus(id);
    if (current !== jobGeneration) throw new Error("页面已切换");
    status.value = job.status;
    progress.value = job.progress ?? 0;
    if (job.status === "SUCCEEDED") return job;
    if (["FAILED", "CANCELLED", "EXPIRED"].includes(job.status))
      throw new Error(job.error || "作业状态：" + job.status);
    await new Promise((resolve) => setTimeout(resolve, 1000));
  }
}
async function download() {
  if (busy.value) return;
  if (!selected.value.length) return ElMessage.warning("请选择至少一个小时");
  const current = ++jobGeneration;
  busy.value = true;
  progress.value = 0;
  try {
    const job = await api.download(
      props.taskId,
      selected.value,
      allowPartial.value,
    );
    if (current !== jobGeneration) return;
    runningJob = { id: job.id, kind: "downloads" };
    const finished = await poll(job.id, "downloads", current);
    const authorization = await api.browserDownload(job.id);
    if (current !== jobGeneration) return;
    const link = document.createElement("a");
    link.href = authorization.url;
    link.download =
      "filename" in finished && finished.filename
        ? finished.filename
        : job.id + ".tar.gz";
    link.click();
  } catch (error) {
    if (current === jobGeneration) fail(error);
  } finally {
    if (current === jobGeneration) {
      busy.value = false;
      runningJob = undefined;
    }
  }
}
async function search() {
  if (busy.value) return;
  if (!keyword.value.trim()) return ElMessage.warning("请输入关键词");
  if (
    range.value &&
    (range.value[1].getTime() - range.value[0].getTime() > 86400000 ||
      range.value[1] <= range.value[0])
  )
    return ElMessage.warning("查询范围必须大于零且不超过24小时");
  const current = ++jobGeneration;
  // 新检索必须作废旧结果页请求，避免旧作业的分页响应覆盖新作业首屏。
  resultsGeneration++;
  busy.value = true;
  results.value = [];
  resultPage.value = 1;
  resultTotal.value = 0;
  resultJob.value = "";
  progress.value = 0;
  truncated.value = false;
  try {
    const job = await api.search(
      props.taskId,
      keyword.value,
      range.value?.[0].toISOString(),
      range.value?.[1].toISOString(),
    );
    if (current !== jobGeneration) return;
    runningJob = { id: job.id, kind: "log-searches" };
    resultJob.value = job.id;
    const finished = await poll(job.id, "log-searches", current);
    const found = await api.searchResults(job.id);
    if (current === jobGeneration) {
      results.value = found.items;
      resultTotal.value = found.total;
      truncated.value = "truncated" in finished && Boolean(finished.truncated);
    }
  } catch (error) {
    if (current === jobGeneration) fail(error);
  } finally {
    if (current === jobGeneration) {
      busy.value = false;
      runningJob = undefined;
    }
  }
}
async function cancel() {
  if (!runningJob) return;
  try {
    await api.cancelJob(runningJob.id, runningJob.kind);
  } catch (error) {
    fail(error);
  }
}
async function loadResultPage() {
  const workspace = generation;
  const current = ++resultsGeneration;
  try {
    const found = await api.searchResults(resultJob.value, resultPage.value);
    if (workspace === generation && current === resultsGeneration) results.value = found.items;
  } catch (error) { if (workspace === generation && current === resultsGeneration) fail(error); }
}
watch(date, () => {
  if (hourPage.value !== 1) hourPage.value = 1;
  else void load();
});
watch(hourPage, () => void load());
watch(
  () => props.taskId,
  () => {
    generation++;
    hoursGeneration++;
    resultsGeneration++;
    jobGeneration++;
    runningJob = undefined;
    selected.value = [];
    if (hourPage.value !== 1) hourPage.value = 1;
    else void load();
    status.value = "";
    progress.value = 0;
    viewerOpen.value = false;
    results.value = [];
    busy.value = false;
  },
  { immediate: true },
);
onBeforeUnmount(() => {
  generation++;
  hoursGeneration++;
  resultsGeneration++;
  jobGeneration++;
  runningJob = undefined;
});
</script>
<template>
  <section class="form-section">
    <div class="section-heading">
      <h2>小时归档</h2>
      <el-button :icon="RefreshCw" aria-label="刷新小时归档" @click="load" />
    </div>
    <div class="archive-date"><el-date-picker v-model="date" type="date" value-format="YYYY-MM-DD" placeholder="全部日期（上海时区）" aria-label="归档日期" clearable /></div>
    <div class="archive-table">
      <el-table
        scrollbar-always-on
        :data="hours"
        size="small"
        @selection-change="
          (items: LogHour[]) => (selected = items.map((item) => item.hourId))
        "
      >
        <el-table-column type="expand" width="36"><template #default="{ row }"><HourFragments :files="row.files || []" @view="viewFile" /></template></el-table-column>
        <el-table-column type="selection" width="42" /><el-table-column
          label="小时"
          min-width="185"
          ><template #default="{ row }">{{
            new Date(row.hour).toLocaleString("zh-CN", {
              timeZone: "Asia/Shanghai",
            })
          }}</template></el-table-column
        >
        <el-table-column label="完整性" min-width="135"><template #default="{ row }"><el-tag size="small" :type="row.integrity === 'VERIFIED' ? 'success' : row.integrity === 'UNAVAILABLE' ? 'danger' : 'info'">{{ integrityLabels[row.integrity] || row.status }}</el-tag></template></el-table-column>
        <el-table-column label="片段" width="70"><template #default="{ row }">{{ row.fragmentCount ?? row.files?.length ?? 0 }}</template></el-table-column>
        <el-table-column label="原始大小" min-width="100"
          ><template #default="{ row }"
            >{{ (row.bytes / 1048576).toFixed(2) }} MiB</template
          ></el-table-column
        >
        <el-table-column label="压缩大小" min-width="100"
          ><template #default="{ row }"
            >{{ (row.archiveBytes / 1048576).toFixed(2) }} MiB</template
          ></el-table-column
        >
      </el-table>
    </div>
    <el-pagination v-if="hourTotal > 24" v-model:current-page="hourPage" :page-size="24" :total="hourTotal" layout="total, prev, pager, next" />
    <div class="archive-controls">
      <el-checkbox v-model="allowPartial">允许部分片段不可用</el-checkbox
      ><el-button
        :icon="Download"
        :disabled="busy || !selected.length"
        @click="download"
        >下载选中小时</el-button
      ><el-button v-if="busy" :icon="X" @click="cancel">取消作业</el-button
      ><span>{{ jobStatusLabels[status] || status }}</span>
    </div>
    <el-progress v-if="busy || status === 'SUCCEEDED'" :percentage="Math.min(100, Math.max(0, progress))" :status="status === 'SUCCEEDED' ? 'success' : undefined" />
    <h3>历史检索</h3>
    <el-date-picker
      v-model="range"
      type="datetimerange"
      start-placeholder="开始时间"
      end-placeholder="结束时间"
    />
    <div class="search-row">
      <el-input
        v-model="keyword"
        placeholder="关键词"
        @keyup.enter="search"
      /><el-button :icon="FileSearch" :loading="busy" @click="search"
        >检索</el-button
      >
    </div>
    <el-alert
      v-if="truncated"
      title="匹配结果较多，请缩小时间范围"
      type="warning"
      :closable="false"
    />
    <el-table
      scrollbar-always-on
      :data="results"
      size="small"
      max-height="320"
      empty-text="暂无匹配结果"
      ><el-table-column
        prop="offset"
        label="字节位置"
        width="100" /><el-table-column label="日志片段"><template #default="{ row }">{{ displayResultText(row) }}</template></el-table-column>
    </el-table>
    <el-pagination v-if="resultTotal > 100" v-model:current-page="resultPage" :page-size="100" :total="resultTotal" layout="total, prev, pager, next" @current-change="loadResultPage" />
    <LogFileViewer v-model="viewerOpen" :file="file" />
  </section>
</template>
<style scoped>
.archive-date { margin: 0 0 16px; }
.archive-controls {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 12px;
  margin: 16px 0;
}
.search-row {
  display: flex;
  gap: 8px;
  margin: 12px 0;
}
:deep(.el-date-editor) {
  max-width: 100%;
}
@media (max-width: 640px) {
  .archive-table {
    width: 100%;
    max-width: 100%;
    min-width: 0;
    overflow-x: auto;
    overscroll-behavior-x: contain;
  }
  .archive-controls {
    align-items: stretch;
  }
  .archive-controls .el-button {
    flex: 1 1 auto;
  }
  .search-row {
    align-items: stretch;
  }
}
</style>
