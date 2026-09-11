/** 归档片段读取的边界计算，保留小重叠以修复 UTF-8 与终端控制序列跨页。 */
export const FILE_SEGMENT_BYTES = 262_144;
export const FILE_SEGMENT_OVERLAP_BYTES = 128;
export const FILE_SEGMENT_STEP_BYTES = FILE_SEGMENT_BYTES - FILE_SEGMENT_OVERLAP_BYTES;

/** 从任意字节定位读取时向前带入有限上下文，显示段不会跨请求累计。 */
export function segmentFetchOffset(offset: number): number {
  const target = Math.max(0, Math.floor(offset));
  return Math.max(0, target - FILE_SEGMENT_OVERLAP_BYTES);
}

/** 返回末尾完整 UTF-8 的字节长度，下一段的重叠读取会重新呈现未完成字符。 */
export function completeUtf8Length(bytes: Uint8Array): number {
  if (!bytes.length) return 0;
  let index = bytes.length - 1;
  while (index >= 0 && (bytes[index] & 0xc0) === 0x80) index -= 1;
  if (index < 0) return 0;
  const lead = bytes[index];
  const expected = lead < 0x80 ? 1 : lead >= 0xc2 && lead <= 0xdf ? 2
    : lead >= 0xe0 && lead <= 0xef ? 3 : lead >= 0xf0 && lead <= 0xf4 ? 4 : 1;
  return bytes.length - index < expected ? index : bytes.length;
}

/** 段首若正落在 UTF-8 延续字节，跳过残片，由前一段或重叠读取完整呈现该字符。 */
export function firstCompleteUtf8Offset(bytes: Uint8Array): number {
  let index = 0;
  while (index < bytes.length && (bytes[index] & 0xc0) === 0x80) index += 1;
  return index;
}

/** 服务端游标必须属于当前文件且单调推进，避免错误响应导致前后段循环。 */
export function validateSegmentResponse(fileId: string, expectedFileId: string, from: number,
                                        nextOffset: number, dataLength: number, limit: number,
                                        sessionId?: string, expectedSessionId?: string): void {
  if (fileId !== expectedFileId) throw new Error("日志片段响应与当前文件不一致");
  if (expectedSessionId && sessionId && sessionId !== expectedSessionId)
    throw new Error("日志片段响应与当前会话不一致");
  if (!Number.isSafeInteger(nextOffset) || nextOffset !== from + dataLength || dataLength > limit)
    throw new Error("日志片段响应偏移无效");
}
