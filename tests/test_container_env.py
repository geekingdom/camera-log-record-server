"""验证验收密钥隔离、文件权限和已有配置保护。"""

import os
import runpy
import stat
from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from dotenv import dotenv_values

prepare = runpy.run_path(str(Path(__file__).resolve().parents[1] / "scripts/prepare_container_env.py"))["prepare"]


def test_prepare_uses_distinct_secrets_and_restricts_file_permissions(tmp_path):
    path = tmp_path / ".env"
    prepare(path)
    values = dotenv_values(path)
    Fernet(values["ENCRYPTION_KEY"])
    assert values["BOOTSTRAP_TOKEN"] != values["INTERNAL_TOKEN"]
    assert len(values["BOOTSTRAP_TOKEN"]) >= 48
    if os.name == "posix":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_prepare_refuses_to_overwrite_existing_configuration(tmp_path):
    path = tmp_path / ".env"
    path.write_text("existing-development-config")
    with pytest.raises(FileExistsError):
        prepare(path)
    assert path.read_text() == "existing-development-config"
