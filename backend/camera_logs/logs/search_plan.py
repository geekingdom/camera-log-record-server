"""全文索引的保守搜索规划，任何无法证明安全排除的文件都必须保留。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any


def _keyword_prefix(keyword: str | bytes) -> bytes | None:
    """取得前三个 Unicode 字符的 UTF-8 前缀；短词和换行词不能安全规划。"""
    if isinstance(keyword, str):
        if "\n" in keyword or "\r" in keyword or "\0" in keyword:
            return None
        characters = keyword[:3]
        if len(characters) < 3:
            return None
        return characters.encode("utf-8")
    if not isinstance(keyword, bytes) or b"\n" in keyword or b"\r" in keyword or b"\0" in keyword:
        return None
    try:
        characters = keyword.decode("utf-8")[:3]
    except UnicodeDecodeError:
        return None
    return characters.encode("utf-8") if len(characters) == 3 else None


def _evidence(file: Mapping[str, Any], value: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """只接受根目录已按 READY、水位和指纹确认过的最小索引证据。"""
    if not isinstance(value, Mapping) or file.get("status") != "READY":
        return None
    try:
        if int(file.get("bytes")) != int(value.get("bytes")):
            return None
    except (TypeError, ValueError):
        return None
    if not isinstance(value.get("head"), bytes) or not isinstance(value.get("tail"), bytes):
        return None
    if not isinstance(value.get("hasNewline"), bool) or not isinstance(value.get("mayMatch"), bool):
        return None
    return dict(value)


def plan_candidate_files(files: Sequence[Mapping[str, Any]], evidence: Mapping[str, Mapping[str, Any] | None],
                         keyword: str | bytes) -> set[str]:
    """返回保持全文结果完整所需的冻结文件 ID 子集。

    索引仅表示前三字符可能出现，正文扫描仍负责关键词、时间范围和连续会话判定。
    规划器特别保留跨文件关键词边界和命中完整行的相邻分卷；不可信证据永远不排除文件。
    """
    prefix = _keyword_prefix(keyword)
    identifiers = [str(file["id"]) for file in files if file.get("id") is not None]
    if prefix is None:
        return set(identifiers)
    # 无 ID 的冻结项不能在返回集合中表达，不能借由错位 zip 把后续证据用于前项；
    # 此时对其余可引用文件也不作索引排除，交由调用方走保守正文扫描。
    if len(identifiers) != len(files):
        return set(identifiers)
    entries = [(file, str(file["id"])) for file in files]
    checked = [_evidence(file, evidence.get(identifier)) for file, identifier in entries]
    candidates = {identifier for identifier, item in zip(identifiers, checked) if item is None or item["mayMatch"]}
    # 每个字节带来源文件，最多保留 prefix-1 字节，支持多个极小文件组成边界关键词。
    suffix: list[tuple[int, str]] = []
    previous_id: str | None = None
    for identifier, item in zip(identifiers, checked):
        if item is None:
            if previous_id is not None:
                candidates.update({previous_id, identifier})
            suffix = []
            previous_id = identifier
            continue
        head = list(item["head"][:len(prefix)])
        joined = suffix + [(byte, identifier) for byte in head]
        raw = bytes(byte for byte, _source in joined)
        start = 0
        while (start := raw.find(prefix, start)) >= 0:
            end = start + len(prefix)
            sources = {source for _byte, source in joined[start:end]}
            if identifier in sources and len(sources) > 1:
                candidates.update(sources)
            start += 1
        # 小文件需要用完整 head 接续；大文件只需可信末尾窗口。
        content = head if item["bytes"] <= len(prefix) - 1 else list(item["tail"][-(len(prefix) - 1):])
        suffix = (suffix + [(byte, identifier) for byte in content])[-(len(prefix) - 1):]
        previous_id = identifier

    # 匹配行可跨连续分卷。未知换行不能作为终点，宁可一直扩到边界。
    seeds = set(candidates)
    for position, identifier in enumerate(identifiers):
        if identifier not in seeds:
            continue
        before = position - 1
        while before >= 0:
            candidates.add(identifiers[before])
            item = checked[before]
            if item is not None and item["hasNewline"]:
                break
            before -= 1
        after = position + 1
        while after < len(identifiers):
            candidates.add(identifiers[after])
            item = checked[after]
            if item is not None and item["hasNewline"]:
                break
            after += 1
    return candidates
