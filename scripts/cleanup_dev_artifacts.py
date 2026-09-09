"""清理已完成服务压测的本地下载副本，保留报告且不访问正式日志目录。"""

import argparse
import json
import re
from pathlib import Path


def candidates(root: Path) -> list[Path]:
    """只接受报告、收尾记录和逐路摘要一致的已完成压测目录。"""
    root = root.resolve(strict=True)
    found = []
    for directory in root.iterdir():
        if directory.is_symlink() or not directory.is_dir():
            continue
        records = [directory / name for name in ("report.json", "cleanup.json")]
        if any(path.is_symlink() or not path.is_file() for path in records):
            continue
        try:
            report, cleanup = [json.loads(path.read_text()) for path in records]
            if (report.get("scope") not in {
                "real-ssh-api-worker-mongo-download", "real-telnet-api-worker-mongo-download"
            } or report.get("integrityVerified") is not True
                or report.get("cleanupVerified") is not True or cleanup != {"errors": []}):
                continue
            results = report["results"]
            if not results or len(results) != report["routes"]:
                continue
            routes = set()
            for result in results:
                verification = result["verification"]
                if (verification["sha256"] != result["sourceSha256"]
                    or verification["lines"] != result["sourceLines"]):
                    raise ValueError("逐路验证证据不一致")
                routes.add(int(result["route"]))
            for path in directory.iterdir():
                match = re.fullmatch(r"route-(\d{4,})-hour-\d{3,}\.archive", path.name)
                if match and int(match[1]) in routes and path.is_file() and not path.is_symlink():
                    found.append(path)
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            # 失败或不完整实验留给人工核查，不能因清理压力扩大删除范围。
            continue
    return sorted(found)


def cleanup(root: Path, *, apply: bool = False) -> dict:
    """默认仅预览；执行前再次生成候选集合，逐文件删除并返回实际释放量。"""
    paths = candidates(root)
    total = 0
    removed = 0
    for path in paths:
        if path.is_symlink() or not path.is_file():
            continue
        size = path.stat().st_size
        if apply:
            path.unlink()
            removed += 1
        total += size
    return {"mode": "apply" if apply else "dry-run", "files": len(paths),
            "removedFiles": removed, "bytes": total}


def main():
    """开发人员显式指定实验根目录；不修改产品保留策略和资源软删除行为。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(".local"))
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    print(json.dumps(cleanup(args.root, apply=args.apply)))


if __name__ == "__main__":
    main()
