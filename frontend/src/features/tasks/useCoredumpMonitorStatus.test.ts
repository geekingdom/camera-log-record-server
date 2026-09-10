/** 同资源 Coredump 负责人查询的轮询与异步失效保护。 */
import { nextTick, ref } from "vue";
import { describe, expect, it, vi } from "vitest";

import type { CoredumpMonitorStatus } from "../../shared/types";
import { useCoredumpMonitorStatus } from "./useCoredumpMonitorStatus";

const activeStatus = (taskId = "owner-1"): CoredumpMonitorStatus => ({
  active: true,
  ownerTask: { id: taskId, name: "负责采集" },
  mountStatus: "MOUNTED",
});

describe("useCoredumpMonitorStatus", () => {
  it("仅对打开的 SSH 或 Telnet 设备海康资源读取负责人状态", async () => {
    const open = ref(true), protocol = ref<"SSH" | "TELNET_DEVICE" | "TELNET_SERIAL">("SSH");
    const resourceId = ref("resource-1"), kind = ref<"HIKVISION_NETWORK" | "SERIAL_SERVER">("HIKVISION_NETWORK");
    const getStatus = vi.fn().mockResolvedValue(activeStatus());
    const subject = useCoredumpMonitorStatus({
      open, protocol, resourceId, resourceKind: kind, getStatus,
    });

    await nextTick();
    await vi.waitFor(() => expect(subject.status.value).toEqual(activeStatus()));
    expect(getStatus).toHaveBeenCalledWith("resource-1");

    protocol.value = "TELNET_SERIAL";
    await nextTick();
    expect(subject.status.value).toBeNull();
    expect(getStatus).toHaveBeenCalledTimes(1);
    subject.stop();
  });

  it("忽略资源切换后旧请求的迟到响应", async () => {
    const open = ref(true), protocol = ref<"SSH">("SSH");
    const resourceId = ref("old"), kind = ref<"HIKVISION_NETWORK">("HIKVISION_NETWORK");
    let resolveOld!: (value: CoredumpMonitorStatus) => void;
    const getStatus = vi.fn((id: string) => id === "old"
      ? new Promise<CoredumpMonitorStatus>(resolve => { resolveOld = resolve; })
      : Promise.resolve(activeStatus("new-owner")));
    const subject = useCoredumpMonitorStatus({ open, protocol, resourceId, resourceKind: kind, getStatus });

    await nextTick();
    resourceId.value = "new";
    await nextTick();
    await vi.waitFor(() => expect(subject.status.value).toEqual(activeStatus("new-owner")));
    resolveOld(activeStatus("old-owner"));
    await nextTick();
    expect(subject.status.value).toEqual(activeStatus("new-owner"));
    subject.stop();
  });

  it("请求失败保留无法确认状态，不将其显示为关闭", async () => {
    const open = ref(true), protocol = ref<"SSH">("SSH");
    const resourceId = ref("resource-1"), kind = ref<"HIKVISION_NETWORK">("HIKVISION_NETWORK");
    const subject = useCoredumpMonitorStatus({
      open, protocol, resourceId, resourceKind: kind,
      getStatus: vi.fn().mockRejectedValue(new Error("网络不可用")),
    });

    await nextTick();
    await vi.waitFor(() => expect(subject.error.value).toBe("网络不可用"));
    expect(subject.status.value).toBeNull();
    expect(subject.loading.value).toBe(false);
    subject.stop();
  });

  it("慢请求尚未完成时不启动重叠刷新", async () => {
    const open = ref(true), protocol = ref<"SSH">("SSH");
    const resourceId = ref("resource-1"), kind = ref<"HIKVISION_NETWORK">("HIKVISION_NETWORK");
    let resolve!: (value: CoredumpMonitorStatus) => void;
    const getStatus = vi.fn(() => new Promise<CoredumpMonitorStatus>(done => { resolve = done; }));
    const subject = useCoredumpMonitorStatus({ open, protocol, resourceId, resourceKind: kind, getStatus });

    await nextTick();
    void subject.refresh();
    expect(getStatus).toHaveBeenCalledTimes(1);
    resolve(activeStatus());
    await vi.waitFor(() => expect(subject.status.value).toEqual(activeStatus()));
    subject.stop();
  });

  it("旧资源请求完成不会解除新资源的单飞保护", async () => {
    const open = ref(true), protocol = ref<"SSH">("SSH");
    const resourceId = ref("resource-a"), kind = ref<"HIKVISION_NETWORK">("HIKVISION_NETWORK");
    let resolveA!: (value: CoredumpMonitorStatus) => void;
    let resolveB!: (value: CoredumpMonitorStatus) => void;
    const getStatus = vi.fn((id: string) => new Promise<CoredumpMonitorStatus>(resolve => {
      if (id === "resource-a") resolveA = resolve;
      else resolveB = resolve;
    }));
    const subject = useCoredumpMonitorStatus({ open, protocol, resourceId, resourceKind: kind, getStatus });

    await nextTick();
    resourceId.value = "resource-b";
    await nextTick();
    expect(getStatus).toHaveBeenCalledTimes(2);
    resolveA(activeStatus("owner-a"));
    await nextTick();
    void subject.refresh();
    expect(getStatus).toHaveBeenCalledTimes(2);
    resolveB(activeStatus("owner-b"));
    await vi.waitFor(() => expect(subject.status.value).toEqual(activeStatus("owner-b")));
    subject.stop();
  });
});
