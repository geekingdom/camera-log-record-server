"""按逻辑小时汇总片段，区分已校验归档与源端日志完整性。"""

from camera_logs.common.database import public


def summarize_hours(files):
    """聚合状态按固定优先级归并，不受 MongoDB 同小时文档返回顺序影响。"""
    groups = {}
    for file in files:
        hour = file["hour"]
        entry = groups.setdefault(hour, {"hourId": hour, "hour": hour, "bytes": 0, "archiveBytes": 0, "files": []})
        entry["bytes"] += file.get("bytes", 0)
        entry["archiveBytes"] += file.get("archiveBytes", 0)
        entry["files"].append(public(file))
    for entry in groups.values():
        fragments = entry["files"]
        fragments.sort(key=lambda f: (str(f.get("runStartedAt", "")), str(f.get("sessionStartedAt", "")), f.get("firstSequence") or 0, f["id"]))
        states = {f["status"] for f in fragments}
        entry["fragmentCount"] = len(fragments)
        entry["readyCount"] = sum(f["status"] == "READY" for f in fragments)
        entry["openCount"] = sum(f["status"] == "OPEN" for f in fragments)
        entry["unavailableCount"] = len(fragments) - entry["readyCount"] - entry["openCount"]
        entry["status"] = "UNAVAILABLE" if entry["unavailableCount"] else "OPEN" if "OPEN" in states else "READY"
        entry["integrity"] = (
            "UNAVAILABLE" if entry["unavailableCount"] else "OPEN" if entry["openCount"] else
            "VERIFIED" if all(f.get("sha256") for f in fragments) else "UNVERIFIED"
        )
    return sorted(groups.values(), key=lambda entry: entry["hour"], reverse=True)
