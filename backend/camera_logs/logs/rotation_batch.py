"""跨小时、分片与时钟回拨时按连续目标分组，保持小块身份而合并物理追加。"""

from dataclasses import replace


async def write_rotating(writer, chunks, write_thread):
    """调用者持有文件锁；每个分组只写一次，不重放结果未知的部分写入。"""
    positions, group, identities = [], [], []
    group_bytes = 0

    async def flush():
        """确认整组写入后补齐源偏移/回拨标记，索引序号仍按接收顺序推进。"""
        nonlocal group_bytes
        if not group:
            return
        written = await write_thread(writer._append_batch_sync, group)
        positions.extend(replace(position, source_offset=offset, rollback_from=rollback)
                         for position, (offset, rollback) in zip(written, identities, strict=True))
        group.clear()
        identities.clear()
        group_bytes = 0

    for source_index, data, instant in chunks:
        previous = group[-1][2] if group else writer._last_received_at
        rollback = previous if previous is not None and instant < previous else None
        hour = writer._hour_of(instant, writer.zone)
        if writer._hour is None:
            await writer._open(hour)
        elif hour != writer._hour or rollback:
            await flush()
            await writer._seal_locked()
            await writer._publish_pending_locked(background=True)
            await writer._open(hour)
        consumed = 0
        while consumed < len(data):
            remaining = writer._segment_max_bytes - writer._size - group_bytes
            if remaining == 0:
                await flush()
                await writer._seal_locked()
                await writer._open(hour)
                remaining = writer._segment_max_bytes - writer._size
            count = min(remaining, len(data) - consumed)
            group.append((source_index, data[consumed:consumed + count], instant))
            identities.append((consumed, rollback if consumed == 0 else None))
            group_bytes += count
            consumed += count
    await flush()
    return positions
