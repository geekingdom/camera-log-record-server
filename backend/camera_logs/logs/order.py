"""目录、搜索与导出共用的分卷顺序，不能用会话内块序号替代小时分卷号。"""

import heapq
from datetime import UTC, datetime


def _time_key(file):
    """优先使用首块接收时间；旧记录回退到会话、运行及小时边界。"""
    for field in ("firstReceivedAt", "sessionStartedAt", "runStartedAt", "hour"):
        value = file.get(field)
        if value is None:
            continue
        try:
            instant = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
            return instant.replace(tzinfo=UTC).timestamp() if instant.tzinfo is None else instant.timestamp()
        except (TypeError, ValueError):
            continue
    return 0.


def ordered_files(files):
    """各节点小时分卷先按持久编号排序，再按时间合并各有序输入流。

    同节点即使会话预算重置或时间回拨，也不得越过前一分卷；不同节点仅比较
    当前队首的时间，支持迁出后又迁回原节点。旧无分卷号记录保持独立流。
    """
    groups = {}
    for file in files:
        part = file.get("segmentNumber") or 0
        if part > 0:
            key = ("hour", file.get("taskId"), file.get("nodeId"), file.get("hour"))
        elif file.get("sessionId"):
            key = ("session", file.get("taskId"), file.get("nodeId"), file.get("runId"), file["sessionId"])
        else:
            key = (file["id"],)
        groups.setdefault(key, []).append(file)
    streams = list(groups.values())
    for stream in streams:
        stream.sort(key=lambda file: (file.get("segmentNumber") or 0, file.get("firstSequence") or 0, file["id"]))
    heap = []

    def push(group, position):
        file = streams[group][position]
        key = (_time_key(file), file.get("firstSequence") or 0, file["id"])
        heapq.heappush(heap, (key, group, position))

    for group in range(len(streams)):
        push(group, 0)
    result = []
    while heap:
        _, group, position = heapq.heappop(heap)
        result.append(streams[group][position])
        if position + 1 < len(streams[group]):
            push(group, position + 1)
    return result
