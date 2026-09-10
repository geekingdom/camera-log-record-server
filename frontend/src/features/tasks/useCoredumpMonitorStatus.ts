// 同资源 Coredump 负责人状态只服务于任务编辑展示；状态从不写入任务草稿。
import { computed, getCurrentInstance, onBeforeUnmount, ref, watch, type Ref } from "vue";

import type { CoredumpMonitorStatus, Protocol, ResourceKind } from "../../shared/types";

const POLL_INTERVAL_MS = 5_000;

export interface CoredumpMonitorStatusSource {
  open: Ref<boolean>;
  protocol: Ref<Protocol>;
  resourceId: Ref<string>;
  resourceKind: Ref<ResourceKind | undefined>;
  getStatus: (resourceId: string) => Promise<CoredumpMonitorStatus>;
}

/**
 * 弹窗打开期间轮询资源级负责人。每次条件改变均使旧请求失效，避免迟到响应覆盖新资源。
 */
export function useCoredumpMonitorStatus(source: CoredumpMonitorStatusSource) {
  const status = ref<CoredumpMonitorStatus | null>(null);
  const loading = ref(false);
  const error = ref<string | null>(null);
  const eligible = computed(() => source.open.value && ["SSH", "TELNET_DEVICE"].includes(source.protocol.value) &&
    source.resourceKind.value === "HIKVISION_NETWORK" && Boolean(source.resourceId.value));
  let generation = 0;
  let timer: ReturnType<typeof setInterval> | undefined;
  const pendingRefreshes = new Map<number, Promise<void>>();

  function clearPolling() {
    if (timer) clearInterval(timer);
    timer = undefined;
  }
  async function refresh(current = generation) {
    // 单飞避免慢请求与下一轮轮询交叠；切换条件仍由 generation 使在途响应失效。
    if (!eligible.value || current !== generation) return;
    const existing = pendingRefreshes.get(current);
    if (existing) return existing;
    const resourceId = source.resourceId.value;
    let pending!: Promise<void>;
    pending = (async () => {
      loading.value = true;
      error.value = null;
      try {
        const response = await source.getStatus(resourceId);
        if (current !== generation || !eligible.value) return;
        status.value = response;
      } catch (cause) {
        if (current !== generation || !eligible.value) return;
        // 查询失败时不保留可能已经过期的“关闭”结论，交由界面明确提示无法确认。
        status.value = null;
        error.value = cause instanceof Error ? cause.message : "读取 Coredump 监控状态失败";
      } finally {
        if (current === generation) loading.value = false;
        if (pendingRefreshes.get(current) === pending) pendingRefreshes.delete(current);
      }
    })();
    pendingRefreshes.set(current, pending);
    return pending;
  }
  function restart() {
    const current = ++generation;
    clearPolling();
    status.value = null;
    error.value = null;
    loading.value = false;
    if (!eligible.value) return;
    void refresh(current);
    timer = setInterval(() => void refresh(current), POLL_INTERVAL_MS);
  }
  function stop() {
    ++generation;
    clearPolling();
  }

  watch([source.open, source.protocol, source.resourceId, source.resourceKind], restart, { immediate: true });
  // 组合模块也供无组件挂载的状态测试复用；调用方可在该场景显式 stop。
  if (getCurrentInstance()) onBeforeUnmount(stop);
  return { status, loading, error, refresh: () => refresh(generation), stop };
}
