<script setup lang="ts">
// 节点监控只展示真实心跳与资源数据，登记和准入修改由后台配置模块负责。
import type { Node } from "../../shared/types";
defineProps<{ items: Node[]; loading: boolean }>();
</script>
<template>
  <el-table
    scrollbar-always-on
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
    <el-table-column label="新任务准入" min-width="130"><template #default="{ row }"><el-tag :type="row.accepting ? 'success' : 'warning'">{{ row.configurationMismatch ? '地址配置不一致' : row.accepting ? '允许接入' : '暂停接入' }}</el-tag></template></el-table-column>
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
