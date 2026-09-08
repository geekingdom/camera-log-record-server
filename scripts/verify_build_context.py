"""用 Docker 实际构建确认本地敏感目录被排除，不读取或复制真实凭据。"""

import shutil
import subprocess
import tempfile
from pathlib import Path


def main():
    """在临时目录放置合成敏感文件，使用 scratch 导出并检查实际上下文。"""
    root = Path(__file__).resolve().parents[1]
    allowed = ["backend/camera_logs/main.py", "frontend/src/main.ts", "frontend/package-lock.json"]
    forbidden = [
        ".env", ".env.production", ".local/secrets/devices.json", ".git/config",
        ".venv/bin/python", "frontend/.env", "frontend/.env.production",
        "frontend/.playwright-cli/snapshot.yml", ".playwright-cli/snapshot.yml",
        "frontend/node_modules/example/index.js", "frontend/dist/index.html",
        "output/device.log", "data/hour.tar.gz", "download.zip",
        "backend/.pytest_cache/state", "backend/.ruff_cache/state", "backend/.coverage",
        "backend/htmlcov/index.html", "backend/data/device-output.txt",
    ]
    marker = b"synthetic-private-build-fixture"
    with tempfile.TemporaryDirectory(prefix="camera-log-build-check-") as temporary:
        context = Path(temporary) / "input"
        exported = Path(temporary) / "output"
        context.mkdir()
        shutil.copyfile(root / ".dockerignore", context / ".dockerignore")
        for name in allowed + forbidden:
            path = context / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(marker if name in forbidden else b"public-build-fixture")
        dockerfile = context / "Dockerfile"
        dockerfile.write_text("FROM scratch\nCOPY . /context\n")
        subprocess.run([
            "docker", "buildx", "build", "--file", str(dockerfile),
            "--output", f"type=local,dest={exported}", str(context),
        ], check=True, timeout=120)
        result = exported / "context"
        for name in allowed:
            if not (result / name).is_file():
                raise RuntimeError(f"必要源码被构建规则排除：{name}")
        for name in forbidden:
            if (result / name).exists():
                raise RuntimeError(f"本地敏感文件进入构建上下文：{name}")
        if any(marker in path.read_bytes() for path in result.rglob("*") if path.is_file()):
            raise RuntimeError("构建上下文残留合成敏感内容")
    print(f"构建上下文检查通过：{len(allowed)} 个必要文件、{len(forbidden)} 个排除场景")


if __name__ == "__main__":
    main()
