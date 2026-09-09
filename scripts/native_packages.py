"""Ubuntu/Debian 原生依赖安装；不调用 Docker 或修改现有软件服务配置。"""

import hashlib
import os
import shutil
import subprocess
from pathlib import Path
from urllib.request import urlopen


def run(command, **kwargs):
    """检查命令退出码；命令参数中禁止加入部署密码。"""
    return subprocess.run([str(item) for item in command], check=True, **kwargs)


def install_packages(component):
    """使用发行版依赖源；MongoDB使用官方8.0签名仓库，可由管理员提前安装跳过。"""
    info = {}
    for line in Path("/etc/os-release").read_text().splitlines():
        key, _, value = line.partition("=")
        info[key] = value.strip('"')
    distro, version = info.get("ID"), info.get("VERSION_ID")
    if distro not in {"ubuntu", "debian"}:
        raise ValueError("自动安装仅支持Ubuntu/Debian")
    packages = ["ca-certificates", "curl", "gnupg", "python3", "python3-venv", "xz-utils"]
    if component in {"all", "frontend"}:
        packages += ["nginx", "nodejs", "npm"]
    env = os.environ | {"DEBIAN_FRONTEND": "noninteractive"}
    run(["apt-get", "update"], env=env)
    run(["apt-get", "install", "-y", *packages], env=env)
    if component in {"all", "database"} and not shutil.which("mongod"):
        supported = {("ubuntu", "22.04"): "jammy", ("ubuntu", "24.04"): "noble",
                     ("debian", "12"): "bookworm"}
        codename = supported.get((distro, version))
        if not codename:
            raise ValueError("该系统版本请先安装MongoDB 7/8；自动源支持Ubuntu22.04/24.04及Debian12")
        architecture = subprocess.check_output(["dpkg", "--print-architecture"], text=True).strip()
        if architecture not in {"amd64", "arm64"} or (distro == "debian" and architecture != "amd64"):
            raise ValueError("当前架构请手动预装官方支持的MongoDB")
        key = Path("/usr/share/keyrings/camera-logs-mongodb-8.gpg")
        # 官方签名密钥通过HTTPS取得并独立保存，不扩大其它APT源的信任范围。
        downloaded = subprocess.check_output(["curl", "--fail", "--silent", "--show-error", "--location",
                                               "https://pgp.mongodb.com/server-8.0.asc"])
        run(["gpg", "--batch", "--yes", "--dearmor", "--output", key], input=downloaded)
        section = "multiverse" if distro == "ubuntu" else "main"
        source = f"deb [arch={architecture} signed-by={key}] https://repo.mongodb.org/apt/{distro} {codename}/mongodb-org/8.0 {section}\n"
        Path("/etc/apt/sources.list.d/camera-logs-mongodb.list").write_text(source)
        run(["apt-get", "update"], env=env)
        # 只安装server，避免引入不需要的shell/tools与守护进程；使用专用systemd unit。
        run(["apt-get", "install", "-y", "mongodb-org-server"], env=env)


def python_runtime(values):
    """优先用已安装3.12+；旧发行版通过uv在程序目录隔离安装，系统Python不变。"""
    candidate = shutil.which(values.get("PYTHON_BIN", "python3.12"))
    if candidate and subprocess.run([candidate, "-c", "import sys; sys.exit(sys.version_info < (3,12))"],
                                    capture_output=True, check=False).returncode == 0:
        return candidate
    if values["INSTALL_PACKAGES"] != "true":
        raise ValueError("未找到Python3.12+，请设置PYTHON_BIN或允许自动安装依赖")
    root = Path(values["INSTALL_ROOT"])
    tools = root / "tools"
    if not (tools / "bin/python").exists():
        run(["python3", "-m", "venv", tools])
    run([tools / "bin/python", "-m", "pip", "install", "uv>=0.6,<1"])
    env = os.environ | {"UV_PYTHON_INSTALL_DIR": str(root / "python")}
    run([tools / "bin/uv", "python", "install", "3.12"], env=env)
    result = subprocess.check_output([str(tools / "bin/uv"), "python", "find", "--managed-python", "3.12"], env=env, text=True)
    return result.strip()


def prepare_venv(values, source, component, python):
    """每组件单独虚拟环境，更新API依赖不会改写正在运行的Worker环境。"""
    venv = Path(values["INSTALL_ROOT"]) / "venvs" / component
    if not (venv / "bin/python").exists():
        run([python, "-m", "venv", venv])
    run([venv / "bin/python", "-m", "pip", "install", "--upgrade", "pip", "setuptools>=75"])
    requirement = "pymongo>=4.11,<5" if component == "database" else str(source)
    run([venv / "bin/python", "-m", "pip", "install", "--upgrade", requirement])
    return venv / "bin/python"


def node_runtime(values):
    """旧发行版的Node不足18时，下载官方22二进制并核验SHA256，不覆盖系统Node。"""
    if shutil.which("node") and shutil.which("npm"):
        major = subprocess.check_output(["node", "-p", "process.versions.node.split('.')[0]"], text=True).strip()
        if int(major) >= 18:
            return os.environ.copy()
    if values["INSTALL_PACKAGES"] != "true":
        raise ValueError("前端需要Node.js18+及npm，请预装或允许自动安装")
    architecture = subprocess.check_output(["dpkg", "--print-architecture"], text=True).strip()
    arch = {"amd64": "x64", "arm64": "arm64"}.get(architecture)
    if not arch:
        raise ValueError("该架构请预装Node.js18+及npm")
    runtime = Path(values["INSTALL_ROOT"]) / "node-runtime"
    if not (runtime / "bin/node").exists():
        version = "v22.18.0"
        name = f"node-{version}-linux-{arch}.tar.xz"
        base = f"https://nodejs.org/dist/{version}/"
        with urlopen(base + "SHASUMS256.txt", timeout=60) as response:
            checksums = {parts[1]: parts[0] for line in response.read().decode().splitlines()
                         if len(parts := line.split()) == 2}
        archive = Path(values["INSTALL_ROOT"]) / name
        try:
            digest = hashlib.sha256()
            with urlopen(base + name, timeout=120) as response, archive.open("wb") as output:
                while chunk := response.read(1024 * 1024):
                    output.write(chunk)
                    digest.update(chunk)
            if digest.hexdigest() != checksums.get(name):
                raise ValueError("Node官方包SHA256不匹配")
            runtime.mkdir(exist_ok=True)
            run(["tar", "-xJf", archive, "--strip-components=1", "-C", runtime])
        finally:
            archive.unlink(missing_ok=True)
    return os.environ | {"PATH": str(runtime / "bin") + os.pathsep + os.environ.get("PATH", "")}


def build_frontend(values, source):
    """在受管构建目录编译前端；不复制工作区依赖和开发配置。"""
    if not Path("/usr/sbin/nginx").exists():
        raise ValueError("请安装nginx")
    env = node_runtime(values)
    root = Path(values["INSTALL_ROOT"])
    build = root / "frontend-build"
    shutil.copytree(Path(source) / "frontend", build, dirs_exist_ok=True,
                    ignore=shutil.ignore_patterns("node_modules", "dist", ".env*"))
    run(["npm", "ci", "--no-audit", "--no-fund"], cwd=build, env=env)
    run(["npm", "run", "build"], cwd=build, env=env)
    destination = root / "frontend"
    shutil.copytree(build / "dist", destination, dirs_exist_ok=True)
    # 构建产物保留，立即回收仅用于开发构建的依赖，减少服务器占用。
    shutil.rmtree(build / "node_modules")
