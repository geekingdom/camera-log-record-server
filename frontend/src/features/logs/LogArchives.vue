<script setup lang="ts">
// 小时归档与检索工作区：作业轮询支持取消，generation 防止切换任务后显示旧结果。
import { onBeforeUnmount, ref, watch } from "vue";
import { Download, FileSearch, RefreshCw, X } from "lucide-vue-next";
import { ElMessage } from "element-plus";
import { api } from "../../shared/api";
import type { LogHour } from "../../shared/types";
const props = defineProps<{ taskId: string }>();
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
let runningJob: { id: string; kind: "downloads" | "log-searches" } | undefined;
function fail(error: unknown) {
  ElMessage.error(error instanceof Error ? error.message : "操作失败");
}
async function load() {
  const current = generation;
  try {
    const response = await api.logHours(props.taskId);
    if (current === generation) hours.value = response.items;
  } catch (error) {
    if (current === generation) fail(error);
  }
}
// 服务端以异步 job 处理下载与检索，统一轮询到终态后再取结果或授权 URL。
async function poll(
  id: string,
  kind: "downloads" | "log-searches",
  current: number,
) {
  for (;;) {
    if (current !== generation) throw new Error("页面已切换");
    const job =
      kind === "downloads"
        ? await api.downloadStatus(id)
        : await api.searchStatus(id);
    status.value = job.status;
    if (job.status === "SUCCEEDED") return job;
    if (["FAILED", "CANCELLED", "EXPIRED"].includes(job.status))
      throw new Error(job.error || "作业状态：" + job.status);
    await new Promise((resolve) => setTimeout(resolve, 1000));
  }
}
async function download() {
  if (!selected.value.length) return ElMessage.warning("请选择至少一个小时");
  const current = generation;
  busy.value = true;
  try {
    const job = await api.download(
      props.taskId,
      selected.value,
      allowPartial.value,
    );
    runningJob = { id: job.id, kind: "downloads" };
    const finished = await poll(job.id, "downloads", current);
    const authorization = await api.browserDownload(job.id);
    if (current !== generation) return;
    const link = document.createElement("a");
    link.href = authorization.url;
    link.download =
      "filename" in finished && finished.filename
        ? finished.filename
        : job.id + ".tar.gz";
    link.click();
  } catch (error) {
    if (current === generation) fail(error);
  } finally {
    if (current === generation) {
      busy.value = false;
      runningJob = undefined;
    }
  }
}
async function search() {
  if (!keyword.value.trim()) return ElMessage.warning("请输入关键词");
  if (
    range.value &&
    (range.value[1].getTime() - range.value[0].getTime() > 86400000 ||
      range.value[1] <= range.value[0])
  )
    return ElMessage.warning("查询范围必须大于零且不超过24小时");
  const current = generation;
  busy.value = true;
  results.value = [];
  truncated.value = false;
  try {
    const job = await api.search(
      props.taskId,
      keyword.value,
      range.value?.[0].toISOString(),
      range.value?.[1].toISOString(),
    );
    runningJob = { id: job.id, kind: "log-searches" };
    const finished = await poll(job.id, "log-searches", current);
    const found = await api.searchResults(job.id);
    if (current === generation) {
      results.value = found.items;
      truncated.value = "truncated" in finished && Boolean(finished.truncated);
    }
  } catch (error) {
    if (current === generation) fail(error);
  } finally {
    if (current === generation) {
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
watch(
  () => props.taskId,
  () => {
    generation++;
    selected.value = [];
    results.value = [];
    busy.value = false;
    void load();
  },
  { immediate: true },
);
onBeforeUnmount(() => {
  generation++;
});
</script>
<template>
  <section class="form-section">
    <div class="section-heading">
      <h2>小时归档</h2>
      <el-button :icon="RefreshCw" aria-label="刷新小时归档" @click="load" />
    </div>
    <div class="archive-table">
      <el-table
        :data="hours"
        size="small"
        @selection-change="
          (items: LogHour[]) => (selected = items.map((item) => item.hourId))
        "
      >
        <el-table-column type="selection" width="42" /><el-table-column
          label="小时"
          min-width="185"
          ><template #default="{ row }">{{
            new Date(row.hour).toLocaleString("zh-CN", {
              timeZone: "Asia/Shanghai",
            })
          }}</template></el-table-column
        >
        <el-table-column
          prop="status"
          label="状态"
          width="100"
        /><el-table-column label="原始大小" min-width="100"
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
    <div class="archive-controls">
      <el-checkbox v-model="allowPartial">允许部分片段不可用</el-checkbox
      ><el-button
        :icon="Download"
        :disabled="busy || !selected.length"
        @click="download"
        >下载选中小时</el-button
      ><el-button v-if="busy" :icon="X" @click="cancel">取消作业</el-button
      ><span>{{ status }}</span>
    </div>
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
      :data="results"
      size="small"
      max-height="320"
      empty-text="暂无匹配结果"
      ><el-table-column
        prop="offset"
        label="字节位置"
        width="100" /><el-table-column prop="text" label="日志片段"
    /></el-table>
  </section>
</template>
<style scoped>
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
  .archive-table :deep(.el-table) {
    min-width: 620px;
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
