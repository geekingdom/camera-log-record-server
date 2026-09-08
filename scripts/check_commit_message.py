"""校验中文提交标题及包含背景、变更、测试、影响的详细正文。"""
import re
import sys
from pathlib import Path


def validate(message):
    """返回不符合团队提交规范的中文错误说明列表。"""
    lines = message.strip().splitlines()
    errors = []
    if not lines or not re.search(r"[\u4e00-\u9fff]", lines[0]):
        errors.append("提交标题必须包含中文")
    for label in ("背景", "变更", "测试", "影响"):
        match = re.search(rf"^{label}：(.+)$", message, re.MULTILINE)
        if not match or len(match.group(1).strip()) < 8 or not re.search(r"[\u4e00-\u9fff]", match.group(1)):
            errors.append(f"正文必须包含详细的中文{label}说明，至少8个字符")
    return errors


if __name__ == "__main__":
    errors = validate(Path(sys.argv[1]).read_text())
    if errors:
        print("\n".join(errors), file=sys.stderr)
        raise SystemExit(1)
