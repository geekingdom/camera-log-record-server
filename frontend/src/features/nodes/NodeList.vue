<script setup lang="ts">
// 服务节点只读列表，节点详情字段由服务端扩展时通过 JSON 兜底展示。
import type { Node } from "../../shared/types";
defineProps<{ items: Node[]; loading: boolean }>();
</script>
<template>
  <el-table
    v-loading="loading"
    :data="items"
    class="data-table"
    empty-text="暂无节点"
  >
    <el-table-column prop="id" label="节点" min-width="160" /><el-table-column
      prop="url"
      label="地址"
      min-width="190"
    />
    <el-table-column label="状态" width="100"
      ><template #default="{ row }">{{
        Date.now() - new Date(row.heartbeat).getTime() < 30000 ? "在线" : "失联"
      }}</template></el-table-column
    >
    <el-table-column label="任务" width="100"
      ><template #default="{ row }"
        >{{ row.activeTasks }} / {{ row.capacity }}</template
      ></el-table-column
    >
    <el-table-column label="磁盘使用" min-width="130"
      ><template #default="{ row }"
        ><el-progress
          :percentage="Number(Number(row.diskPercent).toFixed(1))"
          :status="row.diskPercent >= 90 ? 'exception' : undefined" /></template
    ></el-table-column>
    <el-table-column label="接收速率" width="150"
      ><template #default="{ row }"
        >{{
          (Number(row.inputBytesPerSecond) / 1024).toFixed(1)
        }}
        KiB/s</template
      ></el-table-column
    >
  </el-table>
</template>
