// 批量资源草稿集中管理地址展开与认证状态，避免界面事件让迟到响应恢复过期凭据的成功结果。
import { reactive, ref } from "vue";
import type { ResourceAuthType } from "../../shared/types";

export const MAX_BATCH_RESOURCES = 256;
export type BatchAuthentication = "IDLE" | "AUTHENTICATING" | "SUCCESS" | "FAILED";
export interface BatchResourceRow {
  ip: string;
  name: string;
  username: string;
  password: string;
  authType: ResourceAuthType;
  authentication: BatchAuthentication;
  error?: string;
  version: number;
}
export interface SharedCredentials {
  username: string;
  password: string;
  authType: ResourceAuthType;
}
export type AuthenticateRow = (row: Pick<BatchResourceRow, "ip" | "name" | "username" | "password" | "authType">) => Promise<unknown>;

/** 展开包含边界的同族地址范围；大整数避免 IPv6 在 JavaScript Number 中丢失精度。 */
export function expandIpRange(startInput: string, endInput: string): string[] {
  const start = parseIp(startInput);
  const end = parseIp(endInput);
  if (start.family !== end.family) throw new Error("起止 IP 必须属于同一地址族");
  if (start.value > end.value) throw new Error("结束 IP 必须不小于起始 IP");
  const count = end.value - start.value + 1n;
  if (count > BigInt(MAX_BATCH_RESOURCES)) throw new Error(`批量添加最多 ${MAX_BATCH_RESOURCES} 台设备`);
  return Array.from({ length: Number(count) }, (_, index) => formatIp(start.family, start.value + BigInt(index)));
}

/** 创建一批可编辑设备行；每次认证与字段改动都以版本号隔离。 */
export function createBulkResourceDraft() {
  const rows = reactive<BatchResourceRow[]>([]);
  const shared = reactive<SharedCredentials>({ username: "", password: "", authType: "DIGEST" });
  let authenticationRun = 0;
  const authenticating = ref(false);

  function invalidate(row: BatchResourceRow) {
    row.version += 1;
    row.authentication = "IDLE";
    row.error = undefined;
  }
  function applyRange(start: string, end: string) {
    const addresses = expandIpRange(start, end);
    cancelAuthentication();
    rows.splice(0, rows.length, ...addresses.map(ip => ({
      ip, name: ip, username: shared.username, password: shared.password, authType: shared.authType,
      authentication: "IDLE" as const, version: 0,
    })));
  }
  function setShared(value: Partial<SharedCredentials>) {
    const credentialsChanged = (value.username !== undefined && value.username !== shared.username)
      || (value.password !== undefined && value.password !== shared.password)
      || (value.authType !== undefined && value.authType !== shared.authType);
    Object.assign(shared, value);
    if (!credentialsChanged) return;
    cancelAuthentication();
    for (const row of rows) {
      if (value.username !== undefined) row.username = value.username;
      if (value.password !== undefined) row.password = value.password;
      if (value.authType !== undefined) row.authType = value.authType;
      invalidate(row);
    }
  }
  function updateRow(index: number, value: Partial<Pick<BatchResourceRow, "name" | "username" | "password" | "authType">>) {
    const row = rows[index];
    if (!row) return;
    const credentialsChanged = (value.username !== undefined && value.username !== row.username)
      || (value.password !== undefined && value.password !== row.password)
      || (value.authType !== undefined && value.authType !== row.authType);
    Object.assign(row, value);
    if (credentialsChanged) {
      cancelAuthentication();
      invalidate(row);
    }
  }
  function remove(index: number) {
    cancelAuthentication();
    rows.splice(index, 1);
  }
  async function authenticateAll(authenticate: AuthenticateRow) {
    if (!rows.length) throw new Error("请先生成 IP 地址范围");
    if (rows.some(row => !row.username.trim() || !row.password)) throw new Error("请填写每台设备的 HTTP 用户名和密码");
    return authenticateIndices(rows.map((_, index) => index), authenticate);
  }
  async function authenticateRow(index: number, authenticate: AuthenticateRow) {
    const row = rows[index];
    if (!row) return;
    if (!row.username.trim() || !row.password) throw new Error("请填写 HTTP 用户名和密码");
    return authenticateIndices([index], authenticate);
  }
  async function authenticateIndices(indices: number[], authenticate: AuthenticateRow) {
    if (authenticating.value) throw new Error("认证仍在进行，请等待当前请求结束");
    const run = ++authenticationRun;
    authenticating.value = true;
    const entries = indices.map(index => ({ index, row: rows[index], version: rows[index]?.version }))
      .filter((entry): entry is { index: number; row: BatchResourceRow; version: number } => Boolean(entry.row));
    let cursor = 0;
    const worker = async () => {
      while (true) {
        if (run !== authenticationRun) return;
        const entry = entries[cursor++];
        if (!entry) return;
        const { index, row, version } = entry;
        if (rows[index] !== row || row.version !== version) continue;
        row.authentication = "AUTHENTICATING";
        row.error = undefined;
        try {
          await authenticate(row);
          if (run === authenticationRun && rows[index] === row && row.version === version) row.authentication = "SUCCESS";
        } catch (error) {
          if (run === authenticationRun && rows[index] === row && row.version === version) {
            row.authentication = "FAILED";
            row.error = error instanceof Error ? error.message : "认证失败";
          }
        }
      }
    };
    try {
      await Promise.all(Array.from({ length: Math.min(4, entries.length) }, worker));
    } finally {
      if (run === authenticationRun) authenticating.value = false;
    }
  }
  function cancelAuthentication() {
    authenticationRun += 1;
    authenticating.value = false;
    for (const row of rows) {
      if (row.authentication === "AUTHENTICATING") invalidate(row);
    }
  }
  return { rows, shared, authenticating, applyRange, setShared, updateRow, remove, authenticateAll, authenticateRow, cancelAuthentication };
}

type IpFamily = 4 | 6;
function parseIp(input: string): { family: IpFamily; value: bigint } {
  const value = input.trim();
  if (value.includes(":")) return { family: 6, value: parseIpv6(value) };
  const octets = value.split(".");
  if (octets.length !== 4 || octets.some(part => !/^(0|[1-9]\d{0,2})$/.test(part) || Number(part) > 255))
    throw new Error("IP 地址格式不合法");
  return { family: 4, value: octets.reduce((total, part) => total * 256n + BigInt(part), 0n) };
}
function parseIpv6(input: string): bigint {
  if (!input || input.split("::").length > 2) throw new Error("IPv6 地址格式不合法");
  const halves = input.toLowerCase().split("::");
  const convert = (part: string) => part ? part.split(":") : [];
  const left = convert(halves[0]);
  const right = convert(halves[1] ?? "");
  const expandIpv4 = (parts: string[]) => {
    const last = parts.at(-1);
    if (!last?.includes(".")) return parts;
    const ipv4 = parseIp(last);
    if (ipv4.family !== 4) throw new Error("IPv6 地址格式不合法");
    return [...parts.slice(0, -1), Number(ipv4.value >> 16n).toString(16), Number(ipv4.value & 0xffffn).toString(16)];
  };
  const rawGroups = [...left, ...right];
  if (rawGroups.some((group, index) => group.includes(".") && (index !== rawGroups.length - 1 || input.endsWith("::"))))
    throw new Error("IPv6 地址格式不合法");
  const expandedLeft = expandIpv4(left);
  const expandedRight = expandIpv4(right);
  const groups = [...expandedLeft, ...expandedRight];
  if (groups.some(group => !/^[0-9a-f]{1,4}$/.test(group)) || groups.length > 8 || (!input.includes("::") && groups.length !== 8))
    throw new Error("IPv6 地址格式不合法");
  const missing = 8 - groups.length;
  if (input.includes("::") && missing < 1) throw new Error("IPv6 地址格式不合法");
  const normalized = input.includes("::") ? [...expandedLeft, ...Array(missing).fill("0"), ...expandedRight] : groups;
  return normalized.reduce((total, group) => total * 65536n + BigInt(`0x${group}`), 0n);
}
function formatIp(family: IpFamily, value: bigint): string {
  if (family === 4) return [24n, 16n, 8n, 0n].map(shift => Number((value >> shift) & 255n)).join(".");
  const groups = Array.from({ length: 8 }, (_, index) => Number((value >> BigInt((7 - index) * 16)) & 0xffffn));
  let bestStart = -1;
  let bestLength = 0;
  for (let index = 0; index < groups.length;) {
    if (groups[index] !== 0) { index += 1; continue; }
    const start = index;
    while (groups[index] === 0) index += 1;
    if (index - start > bestLength) [bestStart, bestLength] = [start, index - start];
  }
  if (bestLength < 2) return groups.map(group => group.toString(16)).join(":");
  const left = groups.slice(0, bestStart).map(group => group.toString(16)).join(":");
  const right = groups.slice(bestStart + bestLength).map(group => group.toString(16)).join(":");
  return left && right ? `${left}::${right}` : left ? `${left}::` : right ? `::${right}` : "::";
}
